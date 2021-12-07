import warnings
import torch as ch
from torch import Tensor
from torch.distributions.multivariate_normal import MultivariateNormal, _batch_mahalanobis
from abc import ABC
from decimal import Decimal
from orthnet import Hermite
import math
from scipy.linalg import sqrtm

from .utils.helpers import Bounds, cov


class oracle(ABC):
    """
    Oracle for data sets.
    """
    def __call__(self, x):
        """
        Membership oracle.
        Args: 
            x: samples to check membership
        """
        pass


class Interval(oracle):
    """
    Interval truncation
    """
    # tensor inputs for vector interval truncation
    def __init__(self, lower, upper):
        self.bounds = Bounds(lower, upper)

    def __call__(self, x):
        return ((self.bounds.lower < x).prod(-1) * (x < self.bounds.upper).prod(-1))[...,None]


class KIntervalUnion(oracle):
    """
    Receives an iterable of tuples that contain a lower, upper bound tensor.
    """

    def __init__(self, intervals):
        self.oracles = [Interval(int_[0], int_[1]) for int_ in intervals]

    def __call__(self, x):
        result = Tensor([])
        for oracle_ in self.oracles:
            result = ch.logical_or(result, oracle_(x)) if result.nelement() > 0 else oracle_(x)
        return result[..., None]

    def __str__(self): 
        return 'k-interval union'


class Left_Regression(oracle):
    """
    Left Regression Truncation.
    """
    def __init__(self, left):
        """
        Args: 
            left: left truncation
        """
        super(Left_Regression, self).__init__()
        self.left = left

    def __call__(self, x): 
        return x > self.left

    def __str__(self): 
        return 'left regression'

class Left_Distribution(oracle):
    """
    Left Distribution Truncation.
    """
    def __init__(self, left):
        """
        Args: 
            left: left truncation for distributions - multiplies whether each dimension of sample is within bounds
        """
        super(Left_Distribution, self).__init__()
        self.left = left

    def __call__(self, x): 
        return (x > self.left).prod(dim=-1)

    def __str__(self): 
        return 'left'

class Right_Regression(oracle):
    """
    Right Regression Truncation.
    """
    def __init__(self, right):
        """
        Args: 
            right: right truncation
        """
        super(Right_Regression, self).__init__()
        self.right = right

    def __call__(self, x): 
        return x < self.right

    def __str__(self): 
        return 'right'

class Right_Distribution(oracle):
    """
    Right Distribution Truncation.
    """
    def __init__(self, right):
        """
        Args: 
            right: right truncation
        """
        super(Right_Distribution, self).__init__()
        self.right = right

    def __call__(self, x): 
        return (x < self.right).prod(dim=-1)

    def __str__(self): 
        return 'right'


class Lambda(oracle):   
    """
    Lambda function oracle. Takes in a lambda function/callable that can be applied to one [n,] sized sample as pytorch tensor
    """
    def __init__(self, lambda_): 
        self.lambda_ = lambda_
    
    def __call__(self, x):
        return ch.cat([self.lambda_(x[i]).unsqueeze(0) for i in range(x.size(0))])

    def __str__(self): 
        return 'lambda'


class Sphere(oracle):
    """
    Spherical truncation
    """
    def __init__(self, covariance_matrix, centroid, radius):
        self._unbroadcasted_scale_tril = covariance_matrix.cholesky()
        self.centroid = centroid
        self.radius = radius

    def __call__(self, x):
        diff = x - self.centroid
        dist = ch.sqrt(_batch_mahalanobis(self._unbroadcasted_scale_tril, diff))
        return (dist < self.radius).float().flatten()

    def __str__(self): 
        return 'sphere'


# LAMBDA FUNCTIONS TRIED
#  2D DIMENSIONAL GAUSSIAN LAMBDA FUNCTIONS
set_two_d = lambda x: (x[1].pow(2) + x[0].pow(2) > .5)
horseshoe = lambda x: x[0] > 0 and x[0] ** 2 + x[1] ** 2 > 1 and x[0] ** 2 + x[1] ** 2 < 2
horseshoe_dot = lambda x: x[0] > 0 and 1 < x.pow(2).sum() < 2 or ((x[0] - .5).pow(2) + x[1].pow(2)) < (1 / 6)
triangle = lambda x: x[1] >= 0 and x[1] <= +x[0] + 1 and x[1] <= 1 - x[0] and not (
            (x[0] / 2) ** 2 + (x[1] - 0.52) ** 2 <= 0.02)
# 3D DIMENSIONAL GAUSSIAN LAMBDA FUNCTIONS
three_d_union_check = lambda x: x[0] > 0 and x[2] > 0 or x.pow(2).sum() < 1.0


class UnknownGaussian(oracle):
    """
    Oracle that learns truncation set
    """

    def __init__(self, emp_loc, emp_covariance_matrix, S,  d):
        '''
        '''
        # assumes that input features have been normalized, and now are dealing with a standard normal disribution
        self.emp_loc = emp_loc 
        self.emp_covariance_matrix = emp_covariance_matrix

        # empirical estimates used for membership oracle
        self._emp_dist = MultivariateNormal(emp_loc, emp_covariance_matrix)

        self._d = d
        # calculate the normalizing hermite polynomial constant for degree d
        self._norm_const = Tensor([])

        for i in range(self._d + 1):
            try:
                self._norm_const = ch.cat([self._norm_const, ch.DoubleTensor([math.factorial(i)]).pow(.5)])
            except OverflowError:
                self._norm_const = ch.cat([self._norm_const, ch.DoubleTensor([Decimal(math.factorial(i))]).pow(.5)])
            
        self._norm_const = self._norm_const.unsqueeze(1)

        # truncation coefficient
        self._C_v = self.H_v(S).mean(0)
        # must learn distribution for membership oracle
        self._dist = None

    def __call__(self, x):
        if self.dist is None:
            raise Exception("must learn underlying distribution for membership oracle")
        return ((ch.exp(self.emp_dist.log_prob(x)[...,None]) / ch.exp(self.dist.log_prob(x))[...,None]) * self.psi_k(
            x) > .5).float()

    # x - (n, d) matrix
    def H_v(self, x):
        return ch.div(Hermite(x.unsqueeze(1).double(), self._d).tensor, self._norm_const).prod(2)

    def psi_k(self, x):
        """
        Characteristic function, determines whether a sample falls within truncation set or not.
        """
        return ch.clamp((self._C_v * self.H_v(x)).sum(1), 0.0)[...,None]

    @property
    def emp_dist(self):
        return self._emp_dist

    @property
    def dist(self):
        return self._dist

    @dist.setter
    def dist(self, dist_):
        self._dist = dist_

    @property
    def C_v(self):
        return self._C_v

    @property
    def norm_const(self):
        return self._norm_const

    @property
    def d(self):
        return self._d

    def __str__(self): 
        return 'unknown gaussian'


class Identity(oracle): 
    """
    Identity membership oracle for DNNs. All logits are accepted within the truncation set.
    """
    def __call__(self, x): 
        return ch.ones(x.size()).prod(-1, keepdim=True)

    def __str__(self): 
        return 'identity'


class LogitBall(oracle): 
    """
    Truncation based off of norm of logits. Logt norm needs to be smaller than input bound.
    In other words, retain the input that the classifier is less certain on. Smaller 
    unnormalized log probabilities implies uncertainty in classification.
    """
    def __init__(self, bound): 
        self.bound = bound
        
    def __call__(self, x): 
        return (x.norm(dim=-1, keepdim=True) <= self.bound)

    def __str__(self): 
        return 'logit ball'
   

class LogitBallComplement(oracle): 
    
    """
    Truncation based off of complement norm of logits. Logit norm needs to be greater than input bound.
    In other words, retain the inputs that the classifier is more certain on. Larger 
    unnormalized log probabilities implies more certraining in classification.
    """
    def __init__(self, bound, temperature=ch.ones(1)): 
        self.bound = bound
        self.temperature = temperature
        
    def __call__(self, x): 
        x_ = x
        return (x_.norm(dim=-1, keepdim=True) >= self.bound)

    def __str__(self): 
        return 'logit ball complement'

