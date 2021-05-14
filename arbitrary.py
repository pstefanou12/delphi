"""
Script for running random.
"""

import subprocess 
import torch as ch
from torch import Tensor
from torch.distributions import Uniform
import pandas as pd
import numpy as np
import csv
import json
from cox.utils import Parameters
from cox.store import Store
import os
from sklearn.linear_model import LinearRegression
from argparse import ArgumentParser

from delphi.stats.linear_regression import TruncatedLinearRegression
from delphi import oracle
from delphi.utils import constants as consts

# CONSTANTS 
TABLE_NAME = 'results'

# experiment argument parser
parser = ArgumentParser()
parser.add_argument('--dims', type=int, required=True, help='dimensions to run regression alg')
parser.add_argument('--bias', action='store_true', help='bias term for regression')
parser.add_argument('--out-dir', required=True, help='experiment output')
parser.add_argument('--samples', type=int, required=False, default=10000,  help='number of samples to generate for ground-truth')
parser.add_argument('--c', type=float, required=False, default=0, help='truncation parameter for experiment')
parser.add_argument('--batch_size', type=int, required=False, default=100, help='batch size for procedure')
parser.add_argument('--lr', type=float, required=False, default=1e-1, help='learning rate for weight params')
parser.add_argument('--var-lr', type=float, required=False, default=1e-2, help='learning rate for variance parameter')
parser.add_argument('--var_', type=int, required=False, default=20, help='variance to use for experiments')
parser.add_argument('--trials', type=int, required=False, default=10, help='number of trials to run')


def main(args):
    # set experiment manual seed 
    ch.manual_seed(0)

    # MSE Loss
    mse_loss = ch.nn.MSELoss()

    # distribution for generating ground truth
    U = Uniform(args.lower, args.upper)
    U_ = Uniform(args.x_lower, args.x_upper)

    for i in range(args.trials):
        # create store and add table
        store = Store(args.out_dir)
        store.add_table(TABLE_NAME, { 
            'known_emp_param_mse': float,
            'known_param_mse': float,
            'unknown_param_mse': float,
            'unknown_var_mse': float,
            'ols_param_mse': float,
            'ols_var_mse': float,
            'alpha': float, 
            's': int,
        })

        # number of samples to 
        samples = [100, 250, 500, 750, 1000, 1500, 2500, 5000, 7500, 10000, 20000, 100000]
        # samples = [100, 250, 500, 750, 1000, 1500]

        for s in samples:
            # generate ground truth
            ground_truth = ch.nn.Linear(in_features=args.dims, out_features=1, bias=args.bias)
            ground_truth.weight = ch.nn.Parameter(U.sample(ch.Size([1, args.dims]))) 
            # bias term 
            if args.bias: 
                ground_truth.bias = ch.nn.Parameter(U.sample(ch.Size([1, 1])))

            # remove synthetic data from the computation graph
            with ch.no_grad():
                total = 0.0 # total number of samples sampled from linear regression
                x_trunc, y_trunc = Tensor([]), Tensor([])
                while x_trunc.size(0) < s: 
                    # generate data
                    X = U_.sample(ch.Size([args.samples, args.dims]))
                    y = ground_truth(X) + ch.sqrt(Tensor([args.var_])) * ch.randn(X.size(0), 1)
                    # truncate
                    indices = args.phi(y).nonzero(as_tuple=False).flatten()
                    x_trunc, y_trunc = ch.cat([x_trunc, X[indices]]), ch.cat([y[indices], y_trunc])
                    total += args.samples
                alpha = Tensor([s / total])
                # take only the first s samples that fall inside the truncation set
                x_trunc, y_trunc = x_trunc[:s], y_trunc[:s]

            # empirical linear regression
            ols = LinearRegression() 
            ols.fit(x_trunc, y_trunc)
            ols_var = ch.var(Tensor(ols.predict(x_trunc)) - y_trunc, dim=0)[..., None]

            # truncated linear regression with known noise variance using empirical noise variance
            trunc_reg = TruncatedLinearRegression(phi=args.phi, alpha=alpha, args=args, bias=args.bias, var=ols_var)
            results = trunc_reg.fit(x_trunc, y_trunc)
            w_, w0_ = results.weight.detach().cpu(), results.bias.detach().cpu()

            # truncated linear regression with known noise variance using actual noise variance
            trunc_reg = TruncatedLinearRegression(phi=args.phi, alpha=alpha, args=args, bias=args.bias, var=Tensor([args.var_])[...,None])
            results = trunc_reg.fit(x_trunc, y_trunc)
            w__, w0__ = results.weight.detach().cpu(), results.bias.detach().cpu()

            # truncated linear regression with unknown noise variance
            trunc_reg = TruncatedLinearRegression(phi=args.phi, alpha=alpha, args=args, bias=args.bias)
            results = trunc_reg.fit(x_trunc, y_trunc)
            var_ = results.lambda_.inverse().detach()
            w, w0 = (results.v.detach()*var_).cpu(), (results.bias.detach()*var_).cpu()

            # parameter estimates 
            known_emp_params = ch.cat([w_.T, w0_[..., None]])
            known_params = ch.cat([w__.T, w0__[..., None]])
            real_params = ch.cat([ground_truth.weight.T, ground_truth.bias])
            ols_params = ch.cat([Tensor(ols.coef_).T, Tensor(ols.intercept_)[..., None]])
            unknown_params = ch.cat([w, w0])

            # metrics
            known_emp_param_mse = mse_loss(known_emp_params, real_params)
            known_param_mse = mse_loss(known_params, real_params)
            unknown_param_mse = mse_loss(unknown_params, real_params)
            unknown_var_mse = mse_loss(var_, Tensor([args.var_])[None,...])
            ols_param_mse = mse_loss(Tensor(ols_params), Tensor(real_params))
            ols_var_mse = mse_loss(ols_var, Tensor([args.var_])[None,...])

            # add results to store
            store[TABLE_NAME].append_row({ 
                'known_emp_param_mse': known_emp_param_mse,
                'known_param_mse': known_param_mse,
                'unknown_param_mse': unknown_param_mse,
                'unknown_var_mse': unknown_var_mse,
                'ols_param_mse': ols_param_mse,
                'ols_var_mse': ols_var_mse,
                'alpha': float(alpha.flatten()),
                's': s, 
            })

        # close current store
        store.close()

if __name__ == '__main__': 
    # set environment variable so that stores can create output files
    os.environ['HDF5_USE_FILE_LOCKING'] = 'FALSE'

    args = Parameters(parser.parse_args().__dict__)
    args.__setattr__('workers', 8)
    # args.__setattr__('custom_lr_multiplier', consts.COSINE)
    args.__setattr__('step_lr', 100000)
    args.__setattr__('step_lr_gamma', 1.0)
    # independent variable bounds
    args.__setattr__('x_lower', -5)
    args.__setattr__('x_upper', 5)
    # parameter bounds
    args.__setattr__('lower', -1)
    args.__setattr__('upper', 1)
    args.__setattr__('device', 'cuda' if ch.cuda.is_available() else 'cpu')

    # set oracle with left truncation set C
    print('args: ', args)    
    args.__setattr__('phi', oracle.KIntervalUnion([(Tensor([-1.0]), Tensor([-.5])), (Tensor([0.0]), Tensor([0.5])), (Tensor([2.0]), Tensor([3.0]))]))


    # run experiment
    main(args)
