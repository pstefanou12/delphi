"""
Truncated Linear Regression.
"""

from re import A
import torch as ch
from torch import Tensor
import torch.linalg as LA
import cox
import warnings
from typing import Callable
import collections

from .linear_model import LinearModel
from ..grad import TruncatedMSE, TruncatedUnknownVarianceMSE, SwitchGrad
from ..utils.datasets import make_train_and_val
from ..utils.helpers import Parameters
from .linear_model import LinearModel
from ..trainer import train_model

REQ = 'required'

# DEFAULT PARAMETERS
TRUNC_REG_DEFAULTS = {
        'phi': (Callable, REQ),
        'noise_var': (Tensor, None), 
        'fit_intercept': (bool, True), 
        'val': (float, .2),
        'var_lr': (float, 1e-2), 
        'l1': (float, 0.0),
        'weight_decay': (float, 0.0), 
        'eps': (float, 1e-5),
        'r': (float, 1.0), 
        'rate': (float, 1.5), 
        'batch_size': (int, 50),
        'workers': (int, 0),
        'num_samples': (int, 50),
        'c_s': (float, 100.0),
        'shuffle': (bool, True)
}

class TruncatedLinearRegression(LinearModel):
    """
    Truncated linear regression class. Supports truncated linear regression
    with known noise, unknown noise, and confidence intervals. Module uses 
    delphi.trainer.Trainer to train truncated linear regression by performing 
    projected stochastic gradient descent on the truncated population log likelihood. 
    Module requires the user to specify an oracle from the delphi.oracle.oracle class, 
    and the survival probability. 
    """
    def __init__(self,
                args: Parameters,
                dependent: bool=False,
                emp_weight: ch.Tensor=None,
                rand_seed=0,
                store: cox.store.Store=None):
        """
        Args: 
            phi (delphi.oracle.oracle) : oracle object for truncated regression model 
            alpha (float) : survival probability for truncated regression model
            fit_intercept (bool) : boolean indicating whether to fit a intercept or not 
            val (int) : number of samples to use for validation set 
            tol (float) : gradient tolerance threshold 
            workers (int) : number of workers to spawn 
            r (float) : size for projection set radius 
            rate (float): rate at which to increase the size of the projection set, when procedure does not converge - input as a decimal percentage
            num_samples (int) : number of samples to sample in gradient 
            batch_size (int) : batch size
            lr (float) : initial learning rate for regression weight parameters 
            var_lr (float) : initial learning rate to use for variance parameter in the settign where the variance is unknown 
            step_lr (int) : number of gradient steps to take before decaying learning rate for step learning rate 
            custom_lr_multiplier (str) : "cosine" (cosine annealing), "adam" (adam optimizer) - different learning rate schedulers available
            lr_interpolation (str) : "linear" linear interpolation
            step_lr_gamma (float) : amount to decay learning rate when running step learning rate
            momentum (float) : momentum for SGD optimizer 
            eps (float) :  epsilon value for gradient to prevent zero in denominator
            dependent (bool) : boolean indicating whether dataset is dependent and you should run SwitchGrad instead
            store (cox.store.Store) : cox store object for logging 
        """
        super().__init__(args, dependent, emp_weight=emp_weight, defaults=TRUNC_REG_DEFAULTS, store=store)
        self.rand_seed = rand_seed
        if self.dependent: assert self.args.noise_var is not None, "if linear dynamical system, noise variance must be known"

        del self.criterion
        del self.criterion_params 
        if self.dependent: 
            self.criterion = SwitchGrad.apply
        elif args.noise_var is None: 
            self.criterion = TruncatedUnknownVarianceMSE.apply
        else: 
            self.criterion = TruncatedMSE.apply
            self.criterion_params = [ 
                self.args.phi, self.args.noise_var,
                self.args.num_samples, self.args.eps]

        # property instance variables 
        self.coef, self.intercept = None, None

    def fit(self, 
            X: Tensor, 
            y: Tensor):
        """
        Train truncated linear regression model by running PSGD on the truncated negative 
        population log likelihood.
        Args: 
            X (torch.Tensor): input feature covariates num_samples by dims
            y (torch.Tensor): dependent variable predictions num_samples by 1
        """
        assert isinstance(X, Tensor), "X is type: {}. expected type torch.Tensor.".format(type(X))
        assert isinstance(y, Tensor), "y is type: {}. expected type torch.Tensor.".format(type(y))
        assert X.size(0) >  X.size(1), "number of dimensions, larger than number of samples. procedure expects matrix with size num samples by num feature dimensions." 
        assert y.dim() == 2 and y.size(1) <= X.size(1), "y is size: {}. expecting y tensor to have y.size(1) < X.size(1).".format(y.size()) 
        assert self.args.noise_var.size(0) == y.size(1), "noise var size is: {}. y size is: {}. expecting noise_var.size(0) == y.size(1)".format(self.args.noise_var.size(0), y.size(1))

        # add number of samples to args 
        self.args.__setattr__('T', X.size(0))
        if self.dependent:
            self.criterion_params = [ 
                self.args.phi, self.args.c_gamma, self.args.alpha, self.args.T, 
                self.args.noise_var, self.args.num_samples, self.args.eps,
            ]
            self.args.__setattr__('lr', (1/self.args.alpha) ** self.args.c_gamma)

        # add one feature to x when fitting intercept
        if self.args.fit_intercept:
            X = ch.cat([X, ch.ones(X.size(0), 1)], axis=1)

        # normalize features so that the maximum l_2 norm is 1
        self.beta = ch.ones(1, 1)
        if X.norm(dim=1, p=2).max() > 1 and not self.dependent:  
            l_inf = LA.norm(X, dim=-1, ord=float('inf')).max()
            self.beta = l_inf * (X.size(1) ** .5)

        best_params, self.history, best_loss = train_model(self.args, self, 
                                                        *make_train_and_val(self.args, X / self.beta, y), 
                                                        rand_seed=self.rand_seed,
                                                        store=self.store)

        # reparameterize the regression's parameters
        if self.args.noise_var is None: 
            lambda_ = best_params[1]['params'][0]
            v = best_params[0]['params'][0]
            self.variance = lambda_.inverse()
            self.weight = v * self.variance
        else: 
            self.weight = best_params

        self.avg_weight = self.history.mean(0)
        # re-scale coefficients
        self.weight /= self.beta
        self.avg_weight /= self.beta

        # assign results from procedure to instance variables
        if self.args.fit_intercept: 
            self.coef = self.weight[:-1]
            self.intercept = self.weight[-1]
            self.avg_coef = self.avg_weight[:-1]
            self.avg_intercept = self.avg_weight[-1]
        else: 
            self.coef = self.weight[:]
            self.avg_coef = self.avg_weight[:]
        return self

    def predict(self, 
                X: Tensor): 
        """
        Make predictions with regression estimates.
        """
        assert self.coef is not None, "must fit model before using predict method"
        if self.args.fit_intercept: 
            return X@self.coef + self.intercept
        return X@self.coef

    def final_nll(self, 
            X: Tensor, 
            y: Tensor) -> Tensor:
        with ch.no_grad():
            return self.criterion(self.predict(X), y, *self.criterion_params)

    def avg_nll(self,
            X: Tensor, 
            y: Tensor) -> Tensor:
        with ch.no_grad(): 
            pred = X@self.avg_coef + self.avg_intercept
            return self.criterion(pred, y, *self.criterion_params)


    def best_nll(self,
            X: Tensor, 
            y: Tensor) -> Tensor:
        with ch.no_grad(): 
            return self.criterion(self.predict(X), y, *self.criterion_params)

    def emp_nll(self, 
            X: Tensor, 
            y: Tensor) -> Tensor:
        if self.args.fit_intercept: 
            X = ch.cat([X, ch.ones(X.size(0), 1)], axis=1)
        with ch.no_grad():
            return self.criterion(X@self.emp_weight, y, *self.criterion_params)
    
    @property
    def best_coef_(self): 
        """
        Regression coefficient weights.
        """
        return self.coef.clone()

    @property
    def best_intercept_(self): 
        """
        Regression intercept.
        """
        if self.intercept is not None:
            return self.intercept.clone()
        warnings.warn("intercept not fit, check args input.") 
    
    @property
    def avg_coef_(self): 
        """
        Regression coefficients, averaging over all gradient steps. 
        """
        return self.avg_coef.clone()

    @property
    def avg_intercept_(self): 
        """
        Regression intercept, averaging over all gradient steps. 
        """
        if self.avg_intercept is not None:
            return self.avg_intercept.clone()
        warnings.warn("intercept not fit, check args input.") 

    @property
    def variance_(self): 
        """
        Noise variance prediction for linear regression with
        unknown noise variance algorithm.
        """
        if self.args.noise_var is None: 
            return self.variance
        else: 
            warnings.warn("no variance prediction because regression with known variance was run")
    
    @property
    def ols_coef_(self): 
        """
        OLS empirical estimates for coefficients.
        """
        return self.trunc_reg.emp_weight.clone()

    @property
    def ols_intercept_(self):
        """
        OLS empirical estimates for intercept.
        """
        return self.trunc_reg.emp_bias.clone()

    @property
    def ols_variance_(self): 
        """
        OLS empirical estimates for noise variance.
        """
        return self.trunc_reg.emp_var.clone()

    def __call__(self, X: ch.Tensor, y: ch.Tensor):
        if self.args.noise_var is None:
            weight = self._parameters[0]['params'][0]
            lambda_ = self._parameters[1]['params'][0]
            return X@weight * lambda_.inverse()

        if self.dependent:
            self.Sigma += ch.bmm(X.view(X.size(0), X.size(1), 1),  X.view(X.size(0), 1, X.size(1))).mean(0)
            import pdb; pdb.set_trace()
            if self.args.b:
                # import pdb; pdb.set_trace()
                return X@self.weight    
            return (self.weight@X.T).T
        return X@self.weight

    def pre_step_hook(self, inp) -> None:
        # TODO: find a cleaner way to do this
        if self.args.noise_var is not None and not self.dependent:
            self.weight.grad += (self.args.l1 * ch.sign(inp)).mean(0)[...,None]

        if self.dependent: 
            self.weight.grad = self.weight.grad@self.Sigma.inverse()

    def iteration_hook(self, i, loop_type, loss, batch) -> None:
        if self.args.noise_var is None:
            # project model parameters back to domain 
            var = self._parameters[1]['params'][0].inverse()
            self._parameters[1]['params'][0].data = ch.clamp(var, self.var_bounds.lower, self.var_bounds.upper).inverse()

    def parameters(self): 
        if self._parameters is None: 
            raise "model parameters are not set"
        elif isinstance(self._parameters, collections.OrderedDict):
            return self._parameters.values()
        return self._parameters

