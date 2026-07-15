"""
src/optimizer/gp.py — a tiny Gaussian-process surrogate + acquisition, numpy-only.

Just enough for Bayesian optimization of an expensive, low-dimensional, noisy
objective: an RBF-kernel GP with per-point noise (so low-confidence measurements
are trusted less, not dropped) and Expected Improvement for MINIMIZATION.

Deliberately dependency-light (no sklearn/GPy/BoTorch). Swappable for Ax/BoTorch
behind the CampaignController if a heavier engine is ever wanted.
"""

from __future__ import annotations

import numpy as np


def _rbf(A: np.ndarray, B: np.ndarray, ls: float, var: float) -> np.ndarray:
    d2 = (A[:, None, :] - B[None, :, :]) ** 2
    return var * np.exp(-0.5 * d2.sum(-1) / (ls ** 2))


class GP:
    """Zero-mean GP on inputs already scaled to the unit cube."""

    def __init__(self, length_scale: float = 0.3, jitter: float = 1e-8):
        self.ls = length_scale
        self.jitter = jitter

    def fit(self, X: np.ndarray, y: np.ndarray, noise: np.ndarray):
        self.X = np.asarray(X, float)
        self.ymean = float(np.mean(y))
        self.y = np.asarray(y, float) - self.ymean
        self.var = max(float(np.var(self.y)), 1e-6)
        K = _rbf(self.X, self.X, self.ls, self.var)
        K[np.diag_indices_from(K)] += np.asarray(noise, float) + self.jitter
        self.L = np.linalg.cholesky(K)
        self.alpha = np.linalg.solve(self.L.T, np.linalg.solve(self.L, self.y))
        return self

    def predict(self, Xs: np.ndarray):
        Xs = np.asarray(Xs, float)
        Ks = _rbf(self.X, Xs, self.ls, self.var)
        mu = Ks.T @ self.alpha + self.ymean
        v = np.linalg.solve(self.L, Ks)
        var = np.clip(self.var - np.sum(v ** 2, axis=0), 1e-12, None)
        return mu, var


def expected_improvement(mu: np.ndarray, var: np.ndarray, y_best: float) -> np.ndarray:
    """EI for MINIMIZATION (larger = more promising)."""
    from scipy.stats import norm
    sd = np.sqrt(var)
    imp = y_best - mu
    z = np.divide(imp, sd, out=np.zeros_like(imp), where=sd > 0)
    ei = imp * norm.cdf(z) + sd * norm.pdf(z)
    return np.where(sd > 0, ei, 0.0)
