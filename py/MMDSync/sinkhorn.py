import torch
import torch.nn as nn
import torchvision.datasets as dsets
from torchvision import datasets, transforms
from torch.autograd import Variable
from torch.nn import functional as F
import numpy as np
import pandas as pd
from utils import stable_squared_angle,quaternion_geodesic_distance


# Adapted from ../OptimalTransportSync
# Adapted from https://github.com/gpeyre/SinkhornAutoDiff

from geomloss import SamplesLoss

def quaternion_geodesic_distance_geomloss(X,Y):

    return quaternion_geodesic_distance(X,Y)

def squared_quaternion_geodesic_distance_geomloss(X,Y):

    return quaternion_geodesic_distance_geomloss(X,Y)**2



def get_loss(kernel_type,eps):
    if kernel_type=='laplacequaternion':
        return SamplesLoss("sinkhorn", blur=eps, diameter=3.15, cost = quaternion_geodesic_distance_geomloss, backend= 'tensorized')
    elif kernel_type=='gaussian':
        return SamplesLoss("sinkhorn", p=2, blur=eps, diameter=4., backend= 'tensorized')
    elif kernel_type=='gaussianquaternion':
        return SamplesLoss("sinkhorn", blur=eps, diameter=10.,cost = squared_quaternion_geodesic_distance_geomloss, backend= 'tensorized')
    elif kernel_type=='sinkhorn_gaussian':
        return SamplesLoss("gaussian", blur=1., diameter=4., backend= 'tensorized')
#loss = SamplesLoss("sinkhorn", blur=.05, diameter=3.15, cost = quaternion_geodesic_distance_geomloss, backend= 'tensorized')


class SinkhornLoss(nn.Module):
    r"""
    Given two empirical measures each with :math:`P_1` locations
    :math:`x\in\mathbb{R}^{D_1}` and :math:`P_2` locations :math:`y\in\mathbb{R}^{D_2}`,
    outputs an approximation of the regularized OT cost for point clouds.
    Args:
        eps (float): regularization coefficient
        max_iter (int): maximum number of Sinkhorn iterations
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the sum of the output will be divided by the number of
            elements in the output, 'sum': the output will be summed. Default: 'none'
    Shape:
        - Input: :math:`(N, P_1, D_1)`, :math:`(N, P_2, D_2)`
        - Output: :math:`(N)` or :math:`()`, depending on `reduction`
    """
    def __init__(self, kernel_type, particles,rm_map, eps=0.05):
        super(SinkhornLoss, self).__init__()
        self.eps = eps
        self.kernel_type = kernel_type
        self.particles = particles
        self.rm_map = rm_map
        self.loss = get_loss(kernel_type,eps)
    def forward(self, true_data):
        # The Sinkhorn algorithm takes as input three variables :
        y = self.rm_map(self.particles.data)
        return  torch.sum(self.loss(true_data,y))




class Sinkhorn(nn.Module):
    r"""
    Given two empirical measures each with :math:`P_1` locations
    :math:`x\in\mathbb{R}^{D_1}` and :math:`P_2` locations :math:`y\in\mathbb{R}^{D_2}`,
    outputs an approximation of the regularized OT cost for point clouds.
    Args:
        eps (float): regularization coefficient
        max_iter (int): maximum number of Sinkhorn iterations
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the sum of the output will be divided by the number of
            elements in the output, 'sum': the output will be summed. Default: 'none'
    Shape:
        - Input: :math:`(N, P_1, D_1)`, :math:`(N, P_2, D_2)`
        - Output: :math:`(N)` or :math:`()`, depending on `reduction`
    """
    def __init__(self, eps, max_iter, particles_type, reduction='sum'):
        super(Sinkhorn, self).__init__()
        self.eps = eps
        self.max_iter = max_iter
        self.reduction = reduction
        self.particles_type = particles_type

    def forward(self, x, y,w_x,w_y):
        # The Sinkhorn algorithm takes as input three variables :
        C = self._cost_matrix(x, y,particles_type=self.particles_type)  # Wasserstein cost function
        x_points = x.shape[-2]
        y_points = y.shape[-2]
        if x.dim() == 2:
            batch_size = 1
        else:
            batch_size = x.shape[0]

        # both marginals are fixed with equal weights
        mu = w_x.clone().detach()
        #mu.requires_grad = False
        # both marginals are fixed with equal weights
        nu = w_y.clone().detach()
        #nu.requires_grad = False

        # mu = torch.empty(batch_size, x_points, dtype=x.dtype, device = x.device,
        #                  requires_grad=False).fill_(1.0 / x_points).squeeze()
        # nu = torch.empty(batch_size, y_points, dtype=x.dtype, device = x.device,
        #                  requires_grad=False).fill_(1.0 / y_points).squeeze()

        u = torch.zeros_like(mu)
        v = torch.zeros_like(nu)
        # To check if algorithm terminates because of threshold
        # or max iterations reached
        actual_nits = 0
        # Stopping criterion
        thresh = 1e-1

        # Sinkhorn iterations
        for i in range(self.max_iter):
            u1 = u  # useful to check the update
            u = self.eps * (torch.log(mu+1e-8) - torch.logsumexp(self.M(C, u, v), dim=-1)) + u
            v = self.eps * (torch.log(nu+1e-8) - torch.logsumexp(self.M(C, u, v).transpose(-2, -1), dim=-1)) + v
            err = (u - u1).abs().sum(-1).mean()

            actual_nits += 1
            if err.item() < thresh:
                break

        U, V = u, v
        # Transport plan pi = diag(a)*K*diag(b)
        pi = torch.exp(self.M(C, U, V))
        # Sinkhorn distance
        cost = torch.sum(pi * C, dim=(-2, -1))

        if self.reduction == 'mean':
            cost = cost.mean()
        elif self.reduction == 'sum':
            cost = cost.sum()

        return cost, pi, C

    def M(self, C, u, v):
        "Modified cost for logarithmic updates"
        "$M_{ij} = (-c_{ij} + u_i + v_j) / \epsilon$"
        return (-C + u.unsqueeze(-1) + v.unsqueeze(-2)) / self.eps

    @staticmethod
    def _cost_matrix(x, y, p=2,particles_type='euclidian'):
        "Returns the matrix of $|x_i-y_j|^p$."
        if particles_type=='euclidian':
            x_col = x.unsqueeze(-2)
            y_lin = y.unsqueeze(-3)
            C = torch.sum((torch.abs(x_col - y_lin)) ** p, -1)
        elif particles_type=='quaternion':
            with torch.no_grad():
                dist = quaternion_geodesic_distance(x,y)
                C = torch.einsum('nkl->kl',dist)
        return C

    @staticmethod
    def ave(u, u1, tau):
        "Barycenter subroutine, used by kinetic acceleration through extrapolation."
        return tau * u + (1 - tau) * u1