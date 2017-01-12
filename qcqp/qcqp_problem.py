"""
Copyright 2016 Jaehyun Park

This file is part of CVXPY.

CVXPY is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CVXPY is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CVXPY.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import division
import warnings
import cvxpy as cvx
import numpy as np
from numpy import linalg as LA
import cvxpy.lin_ops.lin_utils as lu
import scipy.sparse as sp
import scipy.sparse.linalg as SLA
from cvxpy.utilities import QuadCoeffExtractor
from joblib import Parallel, delayed
from utilities import *
#from utilities import onevar_qcqp, QuadraticFunction, QCQP

def get_id_map(vars):
    id_map = {}
    N = 0
    for x in vars:
        id_map[x.id] = N
        N += x.size[0]*x.size[1]
    return id_map, N

def assign_vars(xs, vals):
    ind = 0
    for x in xs:
        size = x.size[0]*x.size[1]
        x.value = np.reshape(vals[ind:ind+size], x.size, order='F')
        ind += size

def get_qcqp_form(prob):
    """Returns the problem metadata in QCQP class
    """
    if not prob.objective.args[0].is_quadratic():
        raise Exception("Objective is not quadratic.")
    if not all([constr._expr.is_quadratic() for constr in prob.constraints]):
        raise Exception("Not all constraints are quadratic.")
    if prob.is_dcp():
        warnings.warn("Problem is already convex; specifying solve method is unnecessary.")

    id_map, N = get_id_map(prob.variables())
    extractor = QuadCoeffExtractor(id_map, N)

    P0, q0, r0 = extractor.get_coeffs(prob.objective.args[0])
    P0 = P0[0]
    q0 = q0.T.tocsc()
    r0 = r0[0]

    if prob.objective.NAME == "maximize":
        P0 = -P0
        q0 = -q0
        r0 = -r0

    f0 = QuadraticFunction(P0, q0, r0)

    fs = []
    relops = []
    for constr in prob.constraints:
        sz = constr._expr.size[0]*constr._expr.size[1]
        Pc, qc, rc = extractor.get_coeffs(constr._expr)
        for i in range(sz):
            fs.append(QuadraticFunction(Pc[i], qc[i, :].T.tocsc(), rc[i]))
            relops.append(constr.OP_NAME)

    return QCQP(f0, fs, relops)

def solve_relaxation(prob, *args, **kwargs):
    """Solve the SDP relaxation.
    """

    N, M = prob.n(), prob.m()
    # lifted variables and semidefinite constraint
    X = cvx.Semidef(N + 1)

    W = prob.f0.bmat_form()
    rel_obj = cvx.Minimize(cvx.sum_entries(cvx.mul_elemwise(W, X)))
    rel_constr = [X[N, N] == 1]

    for i in range(M):
        W = prob.fi(i).bmat_form()
        lhs = cvx.sum_entries(cvx.mul_elemwise(W, X))
        if prob.relop(i) == '==':
            rel_constr.append(lhs == 0)
        else:
            rel_constr.append(lhs <= 0)

    rel_prob = cvx.Problem(rel_obj, rel_constr)
    rel_prob.solve(*args, **kwargs)

    if rel_prob.status not in [cvx.OPTIMAL, cvx.OPTIMAL_INACCURATE]:
        print ("Relaxation problem status: " + rel_prob.status)
        return None, rel_prob.value

    return X.value, rel_prob.value

def generate_samples(use_sdp, num_samples, prob, eps=1e-8, *args, **kwargs):
    N = prob.n()
    if use_sdp:
        X, _ = solve_relaxation(prob, *args, **kwargs)
        mu = np.asarray(X[:-1, -1]).flatten()
        Sigma = X[:-1, :-1] - mu*mu.T + eps*sp.identity(N)
        samples = np.random.multivariate_normal(mu, Sigma, num_samples)
    else:
        samples = np.random.randn(num_samples, N)
    ret = [np.asmatrix(samples[i, :].reshape((N, 1))) for i in range(num_samples)]
    return ret

def sdp_relax(self, *args, **kwargs):
    prob = get_qcqp_form(self)
    X, sdp_bound = solve_relaxation(prob, *args, **kwargs)
    if self.objective.NAME == "maximize":
        sdp_bound = -sdp_bound
    assign_vars(self.variables(), X[:, -1])
    return sdp_bound

def qcqp_admm(self, use_sdp=True,
    num_samples=100, num_iters=1000, viollim=1e10,
    tol=1e-4, *args, **kwargs):
    prob = get_qcqp_form(self)
    N, M = prob.n(), prob.m()

    lmb0, P0Q = map(np.asmatrix, LA.eigh(prob.f0.P.todense()))
    lmb_min = np.min(lmb0)
    if lmb_min < 0: rho = 2*(1-lmb_min)/M
    else: rho = 1./M
    rho *= 5

    bestx = None
    bestf = np.inf

    samples = generate_samples(use_sdp, num_samples, prob, *args, **kwargs)

    def x_update(x, y, z, rho, f, relop):
        return one_qcqp(z + (1/rho)*y, f, relop)
    def y_update(x, y, z, rho):
        return y + rho*(z - x)

    for x0 in samples:
        z = x0
        xs = [x0 for i in range(M)]
        ys = [np.zeros((N, 1)) for i in range(M)]
        #print("trial %d: %f" % (sample, bestf))

        zlhs = 2*P0 + rho*M*sp.identity(N)
        lstza = None
        for t in range(num_iters):
            rhs = sum([rho*xs[i]-ys[i] for i in range(M)]) - q0
            z = np.asmatrix(SLA.spsolve(zlhs.tocsr(), rhs)).T
            xs = Parallel(n_jobs=4)(
                delayed(x_update)(xs[i], ys[i], z, rho, prob.fi(i), prob.relop(i))
                for i in range(M)
            )
            ys = Parallel(n_jobs=4)(
                delayed(y_update)(xs[i], ys[i], z, rho)
                for i in range(M)
            )

            za = (sum(xs)+z)/(M+1)
            #if lstza is not None and LA.norm(lstza-za) < tol:
            #    break
            lstza = za
            maxviol = max(prob.violations(za))

            #print(t, maxviol)

            objt = prob.f0.eval(za)
            if maxviol > viollim:
                rho *= 2
                break

            if maxviol < tol and bestf > objt:
                bestf = objt
                bestx = za
                print("best found point has objective: %.5f" % (bestf))
                print("best found point: ", bestx)


    #print("Iteration %d:" % (t))
    print("best found point has objective: %.5f" % (bestf))
    print("best found point: ", bestx)

    assign_vars(self.variables(), bestx)
    if self.objective.NAME == "maximize": bestf *= -1
    return bestf

def qcqp_dccp(self, use_sdp=True, use_eigen_split=False,
    num_samples=100, *args, **kwargs):
    try:
        import dccp
    except ImportError:
        print("DCCP package is not installed; qcqp-dccp method is unavailable.")
        raise

    prob = get_qcqp_form(self)
    N, M = prob.n(), prob.m()

    x = cvx.Variable(N)
    # dummy objective
    T = cvx.Variable()

    obj = cvx.Minimize(T)
    P0p, P0m = split_quadratic(prob.f0.P, use_eigen_split)
    cons = [cvx.quad_form(x, P0p)+prob.f0.q*x+prob.f0.r <= cvx.quad_form(x, P0m)+T]

    for i in range(M):
        Pp, Pm = split_quadratic(prob.fi(i).P, use_eigen_split)
        lhs = cvx.quad_form(x, Pp)+prob.fi(i).q.T*x+prob.fi(i).r
        rhs = cvx.quad_form(x, Pm)
        if relops[i] == '==':
            cons.append(lhs == rhs)
        else:
            cons.append(lhs <= rhs)

    samples = generate_samples(use_sdp, num_samples, prob, *args, **kwargs)

    prob = cvx.Problem(obj, cons)
    bestx = None
    bestf = np.inf
    for x0 in samples:
        x.value = x0
        val = prob.solve(method='dccp')[0]
        if val is not None and bestf > val:
            bestf = val
            bestx = x.value
            print("found new point with f: %.5f" % (bestf))

    assign_vars(self.variables(), x.value)
    if self.objective.NAME == "maximize": bestf *= -1
    return bestf

# TODO: rewrite the dirty stuff below
def coord_descent(self, use_sdp=True,
    num_samples=100, num_iters=1000,
    bsearch_tol=1e-4, tol=1e-4, *args, **kwargs):
    prob = get_qcqp_form(self)
    N, M = prob.n(), prob.m()

    bestx = None
    bestf = np.inf

    samples = generate_samples(use_sdp, num_samples, prob, *args, **kwargs)

    # debugging purpose
    counter = 0
    for x in samples:
        # TODO: give some epsilon slack to equality constraint
        # number of iterations since last infeasibility improvement
        update_counter = 0
        # phase 1: optimize infeasibility
        failed = False
        while True:
            # optimize over x[i]
            for i in range(N):
                coefs = [(0, 0, 0)]
                for j in range(M):
                    # quadratic, linear, constant terms
                    c = get_onevar_coeffs(x, i, prob.fi(j))
                    # constraint not relevant to xi is ignored
                    if abs(c[0]) > tol or abs(c[1]) > tol:
                        if prob.relop(j) == '<=':
                            coefs.append(c)
                        else:
                            coefs.append(c)
                            coefs.append((-c[0], -c[1], -c[2]-tol))

                viol = max(get_violation_onevar(x[i], coefs))
                #print('current violation in %d: %f' % (i, viol))
                #print('x: ', x)
                new_xi = x[i]
                new_viol = viol
                ss, es = 0, viol
                while es - ss > bsearch_tol:
                    s = (ss + es) / 2
                    xi = onevar_qcqp(coefs, s, tol)
                    if xi is None:
                        ss = s
                    else:
                        new_xi = xi
                        new_viol = s
                        es = s
                if new_viol < viol:
                    x[i] = new_xi
                    update_counter = 0
                else:
                    update_counter += 1
                    if update_counter == N:
                        failed = True
                        break
            if failed: break
            viol = max(prob.violations(x))
            if viol < tol: break

        # phase 2: optimize objective over feasible points
        if failed: continue
        update_counter = 0
        converged = False
        for t in range(num_iters):
            # optimize over x[i]
            for i in range(N):
                coefs = [get_onevar_coeffs(x, i, prob.f0)]
                for j in range(M):
                    # quadratic, linear, constant terms
                    c = get_onevar_coeffs(x, i, prob.fi(j))
                    # constraint not relevant to xi is ignored
                    if abs(c[0]) > tol or abs(c[1]) > tol:
                        if prob.relop(j) == '<=':
                            coefs.append(c)
                        else:
                            coefs.append(c)
                            coefs.append((-c[0], -c[1], -c[2]-tol))
                new_xi = onevar_qcqp(coefs, 0, tol)
                if np.abs(new_xi - x[i]) > tol:
                    x[i] = new_xi
                    update_counter = 0
                else:
                    update_counter += 1
                    if update_counter == N:
                        converged = True
                        break
                #print('x: ', x)

            if converged: break
        fval = prob.f0.eval(x)
        if bestf > fval:
            bestf = fval
            bestx = x
            print("best found point has objective: %.5f" % (bestf))
            #print("best found point: ", bestx)

        counter += 1
        #print("trial %d: %f" % (counter, bestf))


    print("best found point has objective: %.5f" % (bestf))
    #print("best found point: ", bestx)

    assign_vars(self.variables(), bestx)
    if self.objective.NAME == "maximize": bestf *= -1
    return bestf


# Add solution methods to Problem class.
cvx.Problem.register_solve("sdp-relax", sdp_relax)
cvx.Problem.register_solve("qcqp-admm", qcqp_admm)
cvx.Problem.register_solve("qcqp-dccp", qcqp_dccp)
cvx.Problem.register_solve("coord-descent", coord_descent)
