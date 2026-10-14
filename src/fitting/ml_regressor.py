"""ML regressor fitter — numpy-only k-nearest-neighbour over a synthetic library.

An instance-based machine-learning method: a library of polydisperse-sphere
curves is generated once from the physics model across a grid of (radius, PDI);
a measured profile is resampled onto the same q-grid, scale/background-normalised,
and its radius/PDI predicted from the weighted nearest library curves. The
confidence is ABSOLUTE: how well the predicted model (scale and background
fitted) reproduces the measured curve, whether the curve carries any form-factor
signal at all, and a hard penalty when the answer sits on the library edge
(1 to 18 nm), where larger or smaller particles clamp.

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


def _absolute_fit(q: np.ndarray, I: np.ndarray, R: float, pdi: float):
    """How well the predicted (R, PDI) explains the measured curve, absolutely.

    Scale and a flat background are fitted by relative-weighted linear least
    squares over the library q-range. Returns (log10 RMS residual, decades the
    fitted model spans across that range). Never raises: a degenerate case
    returns (inf, 0.0), which scores zero confidence."""
    try:
        m = (q >= _QGRID[0]) & (q <= _QGRID[-1]) & (I > 0)
        if m.sum() < 8:
            return float("inf"), 0.0
        qm, Im = q[m], I[m]
        P = np.asarray(model_intensity(qm, R, float(pdi), scale=1.0, bkg=0.0,
                                       dist="schulz"), float)
        A = np.vstack([P, np.ones_like(P)]).T
        wt = 1.0 / Im
        coef, *_ = np.linalg.lstsq(A * wt[:, None], Im * wt, rcond=None)
        coef = np.maximum(coef, 0.0)
        model = A @ coef
        if not np.all(np.isfinite(model)) or np.max(model) <= 0:
            return float("inf"), 0.0
        model = np.maximum(model, 1e-30)
        rms = float(np.sqrt(np.mean((np.log10(Im) - np.log10(model)) ** 2)))
        span = float(np.log10(np.max(model) / max(np.min(model), 1e-30)))
        return rms, span
    except Exception:
        return float("inf"), 0.0


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
        # Confidence. It GATES campaign convergence (campaign._check_stop), so it
        # must say how well the answer explains THIS curve, not only how the
        # neighbours agree with each other. The old relative score (neighbour
        # agreement + distance relative to the median library distance) gave flat
        # noise R = 4.1 nm at 0.51 and a 30 nm sphere 17.5 nm at 0.73, both past
        # confidence_min 0.5 (audit Q10). Now three absolute terms multiply:
        #   * fit:    log10 RMS of the predicted model (scale + background fitted
        #             by least squares) against the measured curve, same scale as
        #             ls_sphere (about 0.05 = borderline);
        #   * signal: the fitted model must actually vary across q (a decade or
        #             more); a flat curve fitted by "background only" gets ~0;
        #   * railed: a size or PDI at the library edge is a wall, not an answer
        #             (sizes outside 1 to 18 nm clamp there), so it is cut hard.
        rstd = float(np.sqrt(np.sum(w * (lib.T[idx, 0] - R) ** 2)))
        agree = 1.0 / (1.0 + rstd / max(0.1 * R, 1e-3))
        rms_log, span = _absolute_fit(qg, Ig, R, pdi)
        f_fit = 1.0 / (1.0 + (rms_log / 0.05) ** 2)
        f_signal = float(np.clip(span / 1.0, 0.0, 1.0))
        r_step = float(_R_LIB[1] - _R_LIB[0])
        p_step = float(_PDI_LIB[1] - _PDI_LIB[0])
        railed = bool(R <= _R_LIB[0] + r_step or R >= _R_LIB[-1] - r_step
                      or pdi >= _PDI_LIB[-1] - p_step)
        conf = f_fit * f_signal * (0.5 + 0.5 * agree)
        if railed:
            conf *= 0.25
        conf = float(np.clip(conf, 0.0, 0.95))
        res.update({
            "size": {"radius": R, "diameter": 2.0 * R, "source": "ml"},
            "pdi": pdi, "confidence": conf,
            "diagnostics": {"method": "ml_knn", "k": _K,
                            "neighbour_R_std": round(rstd, 3),
                            "min_feat_dist": round(float(np.min(d)), 4),
                            "rms_log": round(rms_log, 4),
                            "model_span_decades": round(span, 3),
                            "railed": railed,
                            "library_R_nm": [float(_R_LIB[0]), float(_R_LIB[-1])]},
        })
        return res


register(MLRegressor())
