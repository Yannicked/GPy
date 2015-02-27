# Copyright (c) 2012, James Hensman and Ricardo Andrade
# Licensed under the BSD 3-clause license (see LICENSE.txt)

from .kern import Kern
import numpy as np
from scipy import weave
from ...core.parameterization import Param
from ...core.parameterization.transformations import Logexp
from ...util.config import config # for assesing whether to use weave

class Coregionalize(Kern):
    """
    Covariance function for intrinsic/linear coregionalization models

    This covariance has the form:
    .. math::
       \mathbf{B} = \mathbf{W}\mathbf{W}^\top + \text{diag}(kappa)

    An intrinsic/linear coregionalization covariance function of the form:
    .. math::

       k_2(x, y)=\mathbf{B} k(x, y)

    it is obtained as the tensor product between a covariance function
    k(x, y) and B.

    :param output_dim: number of outputs to coregionalize
    :type output_dim: int
    :param rank: number of columns of the W matrix (this parameter is ignored if parameter W is not None)
    :type rank: int
    :param W: a low rank matrix that determines the correlations between the different outputs, together with kappa it forms the coregionalization matrix B
    :type W: numpy array of dimensionality (num_outpus, W_columns)
    :param kappa: a vector which allows the outputs to behave independently
    :type kappa: numpy array of dimensionality  (output_dim, )

    .. note: see coregionalization examples in GPy.examples.regression for some usage.
    """
    def __init__(self, input_dim, output_dim, rank=1, W=None, kappa=None, active_dims=None, name='coregion'):
        super(Coregionalize, self).__init__(input_dim, active_dims, name=name)
        self.output_dim = output_dim
        self.rank = rank
        if self.rank>output_dim:
            print("Warning: Unusual choice of rank, it should normally be less than the output_dim.")
        if W is None:
            W = 0.5*np.random.randn(self.output_dim, self.rank)/np.sqrt(self.rank)
        else:
            assert W.shape==(self.output_dim, self.rank)
        self.W = Param('W', W)
        if kappa is None:
            kappa = 0.5*np.ones(self.output_dim)
        else:
            assert kappa.shape==(self.output_dim, )
        self.kappa = Param('kappa', kappa, Logexp())
        self.link_parameters(self.W, self.kappa)

    def parameters_changed(self):
        self.B = np.dot(self.W, self.W.T) + np.diag(self.kappa)

    def K(self, X, X2=None):
        if config.getboolean('weave', 'working'):
            try:
                return self._K_weave(X, X2)
            except:
                print("\n Weave compilation failed. Falling back to (slower) numpy implementation\n")
                config.set('weave', 'working', 'False')
                return self._K_numpy(X, X2)
        else:
            return self._K_numpy(X, X2)


    def _K_numpy(self, X, X2=None):
        index = np.asarray(X, dtype=np.int)
        if X2 is None:
            return self.B[index,index.T]
        else:
            index2 = np.asarray(X2, dtype=np.int)
            return self.B[index,index2.T]

    def _K_weave(self, X, X2=None):
        """compute the kernel function using scipy.weave"""
        index = np.asarray(X, dtype=np.int)

        if X2 is None:
            target = np.empty((X.shape[0], X.shape[0]), dtype=np.float64)
            code="""
            for(int i=0;i<N; i++){
              target[i+i*N] = B[index[i]+output_dim*index[i]];
              for(int j=0; j<i; j++){
                  target[j+i*N] = B[index[i]+output_dim*index[j]];
                  target[i+j*N] = target[j+i*N];
                }
              }
            """
            N, B, output_dim = index.size, self.B, self.output_dim
            weave.inline(code, ['target', 'index', 'N', 'B', 'output_dim'])
        else:
            index2 = np.asarray(X2, dtype=np.int)
            target = np.empty((X.shape[0], X2.shape[0]), dtype=np.float64)
            code="""
            for(int i=0;i<num_inducing; i++){
              for(int j=0; j<N; j++){
                  target[i+j*num_inducing] = B[output_dim*index[j]+index2[i]];
                }
              }
            """
            N, num_inducing, B, output_dim = index.size, index2.size, self.B, self.output_dim
            weave.inline(code, ['target', 'index', 'index2', 'N', 'num_inducing', 'B', 'output_dim'])
        return target


    def Kdiag(self, X):
        return np.diag(self.B)[np.asarray(X, dtype=np.int).flatten()]

    def update_gradients_full(self, dL_dK, X, X2=None):
        index = np.asarray(X, dtype=np.int)
        if X2 is None:
            index2 = index
        else:
            index2 = np.asarray(X2, dtype=np.int)

        #attempt to use weave for a nasty double indexing loop: fall back to numpy
        if config.getboolean('weave', 'working'):
            try:
                dL_dK_small = self._gradient_reduce_weave(dL_dK, index, index2)
            except:
                print("\n Weave compilation failed. Falling back to (slower) numpy implementation\n")
                config.set('weave', 'working', 'False')
                dL_dK_small = self._gradient_reduce_weave(dL_dK, index, index2)
        else:
            dL_dK_small = self._gradient_reduce_numpy(dL_dK, index, index2)



        dkappa = np.diag(dL_dK_small)
        dL_dK_small += dL_dK_small.T
        dW = (self.W[:, None, :]*dL_dK_small[:, :, None]).sum(0)

        self.W.gradient = dW
        self.kappa.gradient = dkappa

    def _gradient_reduce_weave(self, dL_dK, index, index2):
        dL_dK_small = np.zeros_like(self.B)
        code="""
        for(int i=0; i<num_inducing; i++){
          for(int j=0; j<N; j++){
            dL_dK_small[index[j] + output_dim*index2[i]] += dL_dK[i+j*num_inducing];
          }
        }
        """
        N, num_inducing, output_dim = index.size, index2.size, self.output_dim
        weave.inline(code, ['N', 'num_inducing', 'output_dim', 'dL_dK', 'dL_dK_small', 'index', 'index2'])
        return dL_dK_small

    def _gradient_reduce_numpy(self, dL_dK, index, index2):
        index, index2 = index[:,0], index2[:,0]
        dL_dK_small = np.zeros_like(self.B)
        for i in range(self.output_dim):
            tmp1 = dL_dK[index==i]
            for j in range(self.output_dim):
                dL_dK_small[j,i] = tmp1[:,index2==j].sum()
        return dL_dK_small

    def update_gradients_diag(self, dL_dKdiag, X):
        index = np.asarray(X, dtype=np.int).flatten()
        dL_dKdiag_small = np.array([dL_dKdiag[index==i].sum() for i in xrange(self.output_dim)])
        self.W.gradient = 2.*self.W*dL_dKdiag_small[:, None]
        self.kappa.gradient = dL_dKdiag_small

    def gradients_X(self, dL_dK, X, X2=None):
        return np.zeros(X.shape)

    def gradients_X_diag(self, dL_dKdiag, X):
        return np.zeros(X.shape)

