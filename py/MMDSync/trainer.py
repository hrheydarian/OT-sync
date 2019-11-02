import os
import time 
import numpy as np
import sys



import torch
import torch.optim as optim
import torch.optim as optim

from tensorboardX import SummaryWriter

import utils
import pprint
import mmd
import sinkhorn
import particles
import prior
import optimizers
from kernel.gaussian import Gaussian,ExpQuaternionGeodesicDist,ExpPowerQuaternionGeodesicDist
import pickle

torch.backends.cudnn.benchmark=True
torch.manual_seed(0)
torch.backends.cudnn.deterministic = True

class Trainer(object):
	def __init__(self,args):
		torch.manual_seed(args.seed)
		self.args = args
		if args.device >-1:
			self.device = 'cuda:'+str(args.device) if torch.cuda.is_available() and args.device>-1 else 'cpu'
		elif args.device==-1:
			self.device = 'cuda'
		elif args.device==-2:
			self.device = 'cpu'
		self.dtype = get_dtype(args)
		if args.product_particles:
			rm_map = '_RM_product'
		else:
			rmp_map = '_RM_joint'
		if args.with_weights:
			weights = '_with_weights'
		else:
			weights = '_no_weights'
		if args.unfaithfulness:
			unfaithfulness = 'true'
		else:
			unfaithfulness = 'false'
		if args.true_product_particles:
			true_prod = 'prod_true'
		else:
			true_prod = 'prod_false'

		model_name = args.model +'_' +  true_prod + '_N_' +str(args.num_true_particles) + '_noise_' +str(args.true_rm_noise_level)+'_B_noise_' + str(args.true_bernoulli_noise) + '_unfaithfulness_' + str(unfaithfulness) 
		method_name =  args.loss+'_' +args.kernel_cost + '_'+ weights + rmp_map
		self.log_dir = os.path.join(args.log_dir, args.log_name,  model_name , method_name, str(args.run_id))

		if not os.path.isdir(self.log_dir):
			from pathlib import Path
			path   = Path(self.log_dir)
			path.mkdir(parents=True, exist_ok=True)
			#os.mkdir(self.log_dir)
		
		if args.log_in_file:
			self.log_file = open(os.path.join(self.log_dir, 'log.txt'), 'w', buffering=1)
			sys.stdout = self.log_file

		pp = pprint.PrettyPrinter(indent=4)
		pp.pprint(vars(args))

		#print('Creating writer')
		#self.writer = SummaryWriter(self.log_dir)

		print('==> Building model..')
		self.build_model()


	def build_model(self):
		torch.manual_seed(self.args.seed)
		np.random.seed(self.args.seed)
		self.edges = get_edges(self.args)
		self.RM_map = get_rm_map(self.args,self.edges)
		self.prior = get_prior(self.args, self.dtype, self.device)
		self.true_RM, self.true_RM_weights = self.get_true_rm()
		self.particles = get_particles(self.args, self.prior)
		self.loss = self.get_loss()
		self.optimizer = self.get_optimizer(self.args.lr)
		self.scheduler = get_scheduler(self.args, self.optimizer)
		self.eval_loss = self.get_eval_loss()

	def get_loss(self):
		if self.args.loss=='mmd':
			kernel = get_kernel(self.args,self.dtype, self.device)
			#if self.args.with_weights==1:
			return mmd.MMD_weighted(kernel, self.particles,self.RM_map, with_noise = self.args.with_noise)
			#else:
			#	return mmd.MMD(kernel, self.particles,self.RM_map, with_noise = (self.args.with_noise==1))
		elif self.args.loss=='sinkhorn':
			#if self.args.with_weights==1:
			return sinkhorn.Sinkhorn_weighted(self.args.kernel_cost, self.particles,self.RM_map,self.args.SH_eps)
			#else:
			#	return sinkhorn.Sinkhorn(self.args.kernel_cost, self.particles,self.RM_map,self.args.SH_eps)
		else:
			raise NotImplementedError()
	def get_optimizer(self,lr):
		if self.args.particles_type=='euclidian':

			if self.args.optimizer=='SGD':
				return optim.SGD(self.particles.parameters(), lr=lr)
		elif self.args.particles_type=='quaternion':
			if self.args.optimizer=='SGD':
				return optimizers.quaternion_SGD(self.particles.parameters(), lr=lr)

	def get_true_rm(self):
		if self.args.model =='synthetic':
			true_args = make_true_dict(self.args)

			true_prior = get_prior(true_args, self.dtype, self.device)
			true_RM_map = get_true_rm_map(true_args, self.edges, self.dtype, self.device)
			#num_particles = self.args.num_true_particles 
			self.true_particles = true_prior.sample(true_args.N, true_args.num_particles)
			# Fixing the pose of the first camera!
			self.true_particles[0,:,0] = 1.
			self.true_particles[0,:,1:] = 0.
			# Weights are uniform
			self.true_weights = (1./true_args.num_particles)*torch.ones([true_args.N, true_args.num_particles], dtype=self.true_particles.dtype, device = self.true_particles.device )
			
			rm, rm_weights = true_RM_map(self.true_particles,  self.true_weights )
				
			return rm, rm_weights
		else:
			raise NotImplementedError()
	def get_eval_loss(self):
		if self.args.eval_loss=='sinkhorn':
			return sinkhorn.SinkhornEval(self.args.SH_eps, self.args.SH_max_iter,'quaternion')


	def train(self):
		print("Starting Training Loop...")
		start_time = time.time()
		best_valid_loss = np.inf
		with_config = True
		for iteration in range(self.args.total_iters):
			#scheduler.step()
			loss = self.train_iter(iteration)
			#if loss < 0.158:
			#	print(loss)
			if not np.isfinite(loss):
				break 
			#if self.args.use_scheduler:
			#	self.scheduler.step(loss)
			if np.mod(iteration,self.args.noise_decay_freq)==0 and iteration>0:
				self.particles.update_noise_level()
			if np.mod(iteration, self.args.freq_eval)==0:
				out = self.eval(iteration, loss,with_config=with_config)
				with_config=False
				if self.args.save==1:
					save_pickle(out, os.path.join(self.log_dir, 'data'), name =  'iter_'+ str(iteration))
			#save(self.writer,loss_val,self.particles,iteration,save_particles= save_particles)

		return loss

	def train_iter(self, iteration):
		self.particles.zero_grad()
		#min_norm = torch.min(torch.norm(self.particles.data,dim=-1))
		#max_norm = torch.max(torch.norm(self.particles.data,dim=-1))


		#print( ' Min norm  ' + str(min_norm.item()) +  ' Max_norm ' + str(max_norm.item()))
		#rm_particles = self.RM_map(self.particles)
		#if self.args.with_weights==1:
		loss = self.loss(self.true_RM, self.true_RM_weights)
		#else:
		#	loss = self.loss(self.true_RM)
		if iteration==86:
			print('bug here')
		#print('particles')
		#print(self.particles.data)
		#print('ground_truth')
		#print(self.true_particles)
		
		#loss = self.loss(self.true_particles)

		loss.backward()
		self.optimizer.param_groups[0]['lr'] = self.args.lr#/np.sqrt(iteration+1)
		self.optimizer.step(loss=loss)
		#print(self.particles.data)
		loss_val = loss.item()
		#self.scheduler.step(loss_val)

		#if np.mod(iteration,100)==0:
		print('Iteration: '+ str(iteration) + ' loss: ' + str(round(loss_val,3))  + ' lr ' + str(self.optimizer.param_groups[0]['lr']) )
		return loss_val
	def eval(self,iteration, loss_val, with_config=False):
		out ={}
		if with_config:
			out = {"type": self.args.particles_type, "N":self.args.N , "numParticles":self.args.num_particles, "numEdges":len(self.edges), "completeness":self.args.completeness,"I":self.edges  }
			out['true_RM'] = self.true_RM.cpu().detach().numpy()
			out['true_particles'] = self.true_particles.cpu().detach().numpy()
			out['true_weights'] = self.true_weights.cpu().detach().numpy()

		out['loss'] = loss_val
		out['particles'] = self.particles.data.cpu().detach().numpy()
		out['time'] = time.time()
		out['iteration'] = iteration
		out['weights'] = self.particles.weights().cpu().detach().numpy()

		if self.args.model =='synthetic':
			#U = utils.forward_quaternion_X_times_Y_inv_prod(self.true_particles[0,:,:].unsqueeze(0),self.particles.data[0,:,:].unsqueeze(0))
			out['eval_dist'] =   self.eval_loss(self.particles.data,self.true_particles, self.particles.weights(), self.true_weights).item()
			#if self.args.with_weights==1:
			rm, rm_weights =self.RM_map(self.particles.data,  self.particles.weights() )
			out['eval_RM_dist'] =   self.eval_loss(rm, self.true_RM,rm_weights,self.true_RM_weights).item()
			#else:
			#	rm =self.RM_map(self.particles.data)
			#	out['eval_RM_dist'] =   self.eval_loss(rm, self.true_RM,None,None).item()

			
			
			print('Sinkhorn distance 	absolute poses '+ str(out['eval_dist']))
			print('Sinkhorn distance 	relative poses '+ str(out['eval_RM_dist']))

		return out


def get_kernel(args,dtype, device):

	if args.kernel_cost == 'squared_euclidean':
		return Gaussian(1 , args.kernel_log_bw, particles_type=args.particles_type, dtype=dtype, device=device)
	elif args.kernel_cost == 'quaternion':
		return ExpQuaternionGeodesicDist(1 , args.kernel_log_bw, particles_type=args.particles_type, dtype=dtype, device=device)
	elif args.kernel_cost == 'power_quaternion':
		return ExpPowerQuaternionGeodesicDist(1 , args.kernel_log_bw, particles_type=args.particles_type, dtype=dtype, device=device)
	elif args.kernel_cost == 'sinkhorn_gaussian':
		return None
	else:
		raise NotImplementedError()

def get_particles(args, prior):
	if args.particles_type == 'euclidian':
		return particles.Particles(prior,args.N, args.num_particles ,args.with_weights, args.product_particles, args.noise_level, args.noise_decay)
	elif args.particles_type== 'quaternion':
		return particles.QuaternionParticles(prior, args.N, args.num_particles , args.with_weights, args.product_particles,args.noise_level, args.noise_decay)
	else:
		raise NotImplementedError()

def get_rm_map(args,edges):
	if args.kernel_cost=='quaternion' or args.kernel_cost=='power_quaternion':
		grad_type='quaternion'
	else:
		grad_type='euclidean'
	if args.particles_type=='euclidian':
		#if args.with_weights==1:
		return particles.RelativeMeasureMapWeights(edges,grad_type)
		#else:
		#	return particles.RelativeMeasureMap(edges,grad_type)
	elif args.particles_type=='quaternion':
		#if args.with_weights==1:
		if args.product_particles==1:
			return particles.QuaternionRelativeMeasureMapWeightsProduct(edges,grad_type)
		else:
			return particles.QuaternionRelativeMeasureMapWeights(edges,grad_type)
		#else:
		#	if args.product_particles==1:
		#		return particles.QuaternionRelativeMeasureMapProduct(edges,grad_type)
		#	else:
		#		return particles.QuaternionRelativeMeasureMap(edges,grad_type)
	else:
		raise NotImplementedError()


def get_true_rm_map(args,edges, dtype, device):
	if args.kernel_cost=='quaternion' or args.kernel_cost=='power_quaternion':
		grad_type='quaternion'
	else:
		grad_type='euclidean'
	if args.true_rm_noise_level>0.:
		noise_sampler = get_prior(args, dtype, device)
	else:
		noise_sampler= None
	if args.particles_type=='euclidian':
		return particles.RelativeMeasureMapWeights(edges,grad_type)
	elif args.particles_type=='quaternion':
		if args.product_particles==1:
			return particles.QuaternionRelativeMeasureMapWeightsProduct(edges,grad_type, noise_sampler,args.true_rm_noise_level,args.true_bernoulli_noise,args.unfaithfulness)
		else:
			return particles.QuaternionRelativeMeasureMapWeights(edges,grad_type, noise_sampler,args.true_rm_noise_level,args.true_bernoulli_noise,args.unfaithfulness)
	else:
		raise NotImplementedError()

def get_prior(args, dtype, device):
	if args.prior =='mixture_gaussians' and args.particles_type=='euclidian':
		return prior.MixtureGaussianPrior(args.maxNumModes, dtype, device)
	elif args.prior =='gaussian' and args.particles_type=='quaternion':
		return prior.GaussianQuaternionPrior(dtype, device)
	elif args.prior == 'gaussian' and args.particles_type=='euclidian':
		return prior.GaussianPrior(dtype,device)
	elif args.prior=='bingrham ':
		raise NotImplementedError()
	else:
		raise NotImplementedError()



def get_edges(args):
	if args.model=='synthetic':
		return utils.generate_graph(args.N,args.completeness)
	else:
		raise NotImplementedError()
def get_dtype(args):
	if args.dtype=='float32':
		return torch.float32
	else:
		return torch.float64

def get_scheduler(args, optimizer):
	if args.scheduler == 'ReduceLROnPlateau':
		return torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min',patience = 10,verbose=True, factor = 0.9)

def save(writer,loss,particles,iteration, save_particles=True):

	writer.add_scalars('data/',{"losses":loss},iteration)
	if np.mod(iteration,5)==0:
		print('Saving checkpoint at iteration'+ str(iteration))
		state = {
			'particles': particles,
			'loss': loss,
			'iteration':iteration,
		}
		if not os.path.isdir(writer.logdir +'/checkpoint'):
			os.mkdir(writer.logdir + '/checkpoint')
		torch.save(state,writer.logdir +'/checkpoint/ckpt.iter_'+str(iteration))

def save_pickle(out,exp_dir,name):
	os.makedirs(exp_dir, exist_ok=True)
	with  open(os.path.join(exp_dir,name+".pickle"),"wb") as pickle_out:
		pickle.dump(out, pickle_out)



class Struct:
	def __init__(self, **entries):
		self.__dict__.update(entries)

def make_true_dict(args):
	true_args= {}
	true_args['prior'] = args.true_prior
	true_args['particles_type']= args.particles_type
	true_args['N'] = args.N
	true_args['num_particles'] = args.num_true_particles
	true_args['product_particles'] = args.true_product_particles
	true_args['kernel_cost']	 = args.kernel_cost
	true_args['true_rm_noise_level'] = args.true_rm_noise_level
	true_args['true_bernoulli_noise'] = args.true_bernoulli_noise
	true_args['unfaithfulness'] = args.unfaithfulness
	true_args = Struct(**true_args)
	return true_args


