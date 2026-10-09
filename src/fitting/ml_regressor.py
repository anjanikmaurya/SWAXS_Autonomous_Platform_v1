"""ML regressor fitter — numpy-only k-nearest-neighbour over a synthetic library.

An instance-based machine-learning method: a library of polydisperse-sphere
curves is generated once from the physics model across a grid of (radius, PDI);
a measured profile is resampled onto the same q-grid, scale/background-normalised,
and its radius/PDI predicted from the weighted nearest library curves. Neighbour
agreement gives an uncertainty-aware confidence.

Kept numpy-only (no scikit-learn / torch) so it is always available and matches
the platform's numpy-only GP. It is deliberately a baseline ML method — fast,
dependency-free, uncertainty-aware — for the comparison study; a trained RF/NN
can later register under the same interface.
"""
from __future__ import annotations

import numpy as np

from src.analysis.nanoparticle import model_intensity, guinier_estimate
from . import register

_QGRID = np.logspace(np.log10(0.06), np.log10(4.0), 128)   # nm^-1, fixed feature grid
_R_LIB = np.linspace(1.0, 18.0, 36)                        # nm
_PDI_LIB = np.linspace(0.02, 0.40, 16)
_K = 8                                                     # neighbours


def _feat(logI: np.ndarray) -> np.ndarray:
    """Scale/background-tolerant feature: z-scored log intensity on the grid.
    A multiplicative scale is an additive shift in log (removed by the mean);
    the std-normalisation makes the shape, not the amplitude, the signal."""
    m, s = np.mean(logI), np.std(logI)
    return (logI - m) / (s if s > 1e-9 else 1.0)


class _Library:
    """Built once, cached. Rows = z-scored log-I features; targets = (R, PDI)."""
    def __init__(self):
        feats, tgts = [], []
        for R in _R_LIB:
            for pdi in _PDI_LIB:
                I = model_intensity(_QGRID, R, float(pdi), scale=1.0, bkg=0.0, dist="schulz")
                I = np.maximum(np.asarray(I, float), 1e-12)
                feats.append(_feat(np.log10(I)))
                tgts.append((R, float(pdi)))
        self.F = np.asarray(feats)          # (N, 128)
        self.T = np.asarray(tgts)           # (N, 2)


_LIB: _Library | None = None


def _library() -> _Library:
    global _LIB
    if _LIB is None:
        _LIB = _Library()
    return _LIB


class MLRegressor:
    id = "ml_regressor"
    label = "ML regressor (k-NN)"
    ready = True

    def fit(self, q, I, sigma=None, **opts) -> dict:
        q = np.asarray(q, float); I = np.asarray(I, float)
        g = guinier_estimate(q, I, sigma)          # for the displayed Rg only
        m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
        res = {"distribution": "ml_knn", "size": None, "pdi": None, "phase": {},
               "invariant": None, "fit": None, "guinier": g, "confidence": 0.0,
               "diagnostics": {"method": "ml_knn"}}
        if m.sum() < 8:
            return res
        qg, Ig = q[m], I[m]
        logI = np.interp(np.log10(_QGRID), np.log10(qg), np.log10(Ig))  # resample onto the grid
        x = _feat(logI)
        lib = _library()
        d = np.sqrt(np.sum((lib.F - x) ** 2, axis=1))      # Euclidean in feature space
        idx = np.argsort(d)[:_K]
        w = 1.0 / (d[idx] + 1e-6); w /= w.sum()
        R = float(np.sum(w * lib.T[idx, 0]))
        pdi = float(np.sum(w * lib.T[idx, 1]))
        # confidence: neighbour agreement on R (tight → high) and feature closeness.
        rstd = float(np.sqrt(np.sum(w * (lib.T[idx, 0] - R) ** 2)))
        agree = 1.0 / (1.0 + rstd / max(0.1 * R, 1e-3))
        close = float(np.exp(-np.min(d) / (np.median(d) + 1e-9)))
        conf = float(np.clip(0.5 * agree + 0.5 * close, 0.05, 0.95))
        res.update({
            "size": {"radius": R, "diameter": 2.0 * R, "source": "ml"},
            "pdi": pdi, "confidence": conf,
            "diagnostics": {"method": "ml_knn", "k": _K,
                            "neighbour_R_std": round(rstd, 3),
                            "min_feat_dist": round(float(np.min(d)), 4)},
        })
        return res


register(MLRegressor())
