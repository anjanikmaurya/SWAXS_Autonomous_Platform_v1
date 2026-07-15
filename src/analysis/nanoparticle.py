"""
src/analysis/nanoparticle.py — automatic SAXS analysis of colloidal spherical
nanoparticles (size, polydispersity, invariant, confidence).

Deterministic and self-contained (numpy + scipy only — no sasmodels). Designed
to run UNATTENDED on each subtracted profile in the autopilot loop, so every
result carries a 0-1 confidence and the code degrades to a Guinier-only estimate
rather than throwing when a full fit fails.

Model:  I(q) = scale · ∫ n(R) V(R)² P(q,R) dR + background
  P(q,R) = [3(sin x − x cos x)/x³]²,  x = qR          (sphere form factor)
  n(R)   = Schulz or log-normal size distribution, mean R̄, PDI = σ/R̄

q and R share units (q in nm⁻¹ → R in nm). Intensity is arbitrary units
(no absolute scale), so the Porod invariant is reported as a RELATIVE number.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares
from scipy.signal import find_peaks
from scipy.ndimage import uniform_filter1d
from scipy.stats import gamma, lognorm

_trapezoid = getattr(np, "trapezoid", getattr(np, "trapz"))

# Expected structure-factor peak-position ratios (q/q1) for ordered mesophases.
_PHASE_RATIOS = {
    "lamellar":     [1.0, 2.0, 3.0, 4.0],
    "hexagonal":    [1.0, 1.732, 2.0, 2.646, 3.0],
    "BCC":          [1.0, 1.414, 1.732, 2.0, 2.236],
    "FCC":          [1.0, 1.155, 1.633, 1.915, 2.309],
    "simple_cubic": [1.0, 1.414, 1.732, 2.0],
}


# ── sphere form factor ─────────────────────────────────────────────────────────
def _sphere_amp(x: np.ndarray) -> np.ndarray:
    """3(sin x − x cos x)/x³, with the x→0 limit (→1) handled."""
    x = np.asarray(x, dtype=float)
    out = np.ones_like(x)
    big = x > 1e-3
    xb = x[big]
    out[big] = 3.0 * (np.sin(xb) - xb * np.cos(xb)) / xb ** 3
    return out


def _size_grid(Rbar: float, pdi: float, n: int = 161):
    sig = max(pdi * Rbar, 1e-9)
    lo = max(Rbar - 5 * sig, 1e-4 * Rbar)
    return np.linspace(lo, Rbar + 5 * sig, n)


def _weights(R: np.ndarray, Rbar: float, pdi: float, dist: str) -> np.ndarray:
    if dist == "lognormal":
        s = np.sqrt(np.log(1.0 + pdi ** 2))
        median = Rbar / np.exp(s ** 2 / 2.0)     # keep the mean at R̄
        w = lognorm.pdf(R, s, scale=median)
    else:  # schulz (gamma): mean = a·scale = R̄, var = a·scale² = (pdi·R̄)²
        a = 1.0 / max(pdi ** 2, 1e-6)
        w = gamma.pdf(R, a=a, scale=Rbar * pdi ** 2)
    tot = w.sum()
    return w / tot if tot > 0 else w


def model_intensity(q, Rbar, pdi, scale, bkg, dist="schulz"):
    """Polydisperse-sphere I(q) (arbitrary units)."""
    R = _size_grid(Rbar, pdi)
    w = _weights(R, Rbar, pdi, dist)
    V = R ** 3                                    # V ∝ R³; constants absorbed in scale
    amp = _sphere_amp(np.outer(np.asarray(q, float), R))
    integ = (w * V ** 2 * amp ** 2).sum(axis=1)
    return scale * integ + bkg


# ── Guinier (model-free fallback + validity) ───────────────────────────────────
def guinier_estimate(q, I, sigma=None):
    """Rg, I0 from ln I vs q² over the lowest-q window (qRg ≲ 1.3). Returns a dict
    with Rg, I0, R_from_Rg (= √(5/3)·Rg for a sphere) and a validity flag."""
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (I > 0) & (q > 0)
    q, I = q[m], I[m]
    if q.size < 8:
        return {"Rg": None, "I0": None, "R_from_Rg": None, "valid": False}
    order = np.argsort(q); q, I = q[order], I[order]
    # iterate the window so that q_max·Rg ≈ 1.3
    n = max(8, q.size // 10)
    Rg = None
    for _ in range(6):
        x = q[:n] ** 2
        y = np.log(I[:n])
        A = np.vstack([x, np.ones_like(x)]).T
        slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
        if slope >= 0:
            break
        Rg = np.sqrt(-3.0 * slope)
        n_new = int(np.searchsorted(q, 1.3 / Rg)) if Rg > 0 else n
        n_new = max(8, min(n_new, q.size))
        if n_new == n:
            break
        n = n_new
    if Rg is None or not np.isfinite(Rg):
        return {"Rg": None, "I0": None, "R_from_Rg": None, "valid": False}
    I0 = float(np.exp(intercept))
    valid = bool(q[0] * Rg < 1.0 and q[min(n, q.size) - 1] * Rg < 1.5)
    return {"Rg": float(Rg), "I0": I0, "R_from_Rg": float(np.sqrt(5.0 / 3.0) * Rg),
            "valid": valid, "n_points": int(n)}


# ── ordered-phase (superlattice) detection — report only ───────────────────────
def detect_bragg_peaks(q, I, min_prominence: float = 0.08):
    """Find structure-factor / Bragg peaks: bumps sitting ABOVE the smooth
    form-factor decay. Returns the peak q-positions (ascending)."""
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
    q, I = q[m], I[m]
    if q.size < 20:
        return np.array([])
    order = np.argsort(q); q, I = q[order], I[order]
    logI = np.log10(I)
    win = max(5, logI.size // 12)
    resid = logI - uniform_filter1d(logI, size=win, mode="nearest")   # detrend the decay
    idx, _ = find_peaks(resid, prominence=min_prominence, distance=max(3, q.size // 60))
    return q[idx]


def index_phase(peak_q) -> dict:
    """Classify an ordered mesophase from Bragg-peak position ratios (q/q1).
    Report-only: never affects size/PDI/confidence."""
    peak_q = sorted(float(x) for x in peak_q if x > 0)
    out = {"phase": "none", "n_peaks": len(peak_q), "ratios": [],
           "d_spacing": None, "match": None, "score": 0.0}
    if len(peak_q) < 2:
        return out
    q1 = peak_q[0]
    ratios = [q / q1 for q in peak_q]
    best_name, best_score = None, 0.0
    for name, seq in _PHASE_RATIOS.items():
        matched = sum(1 for r in ratios
                      if abs(min(seq, key=lambda s: abs(s - r)) - r)
                      / min(seq, key=lambda s: abs(s - r)) < 0.06)
        score = matched / len(ratios)
        if score > best_score:
            best_name, best_score = name, score
    phase = best_name if (best_score >= 0.75 and len(peak_q) >= 2) else "disordered"
    out.update({"phase": phase, "ratios": [round(r, 3) for r in ratios],
                "d_spacing": round(2 * np.pi / q1, 3), "match": best_name,
                "score": round(best_score, 2)})
    return out


# ── the fit ─────────────────────────────────────────────────────────────────────
def _fit_one(q, I, dist, Rbar0, pdi0=0.15):
    """Fit the polydisperse-sphere model in log space. Returns (params, perr, rms)."""
    logI = np.log10(I)
    bkg0 = max(np.percentile(I, 5), 1e-12)
    # scale so the model roughly matches I at the lowest q
    m0 = model_intensity(q, Rbar0, pdi0, 1.0, 0.0, dist)
    scale0 = max(I[0] / max(m0[0], 1e-30), 1e-30)
    p0 = [Rbar0, pdi0, np.log10(scale0), bkg0]
    lo = [1e-3, 0.01, np.log10(scale0) - 6, 0.0]
    hi = [Rbar0 * 20, 0.6, np.log10(scale0) + 6, max(I) ]

    def resid(p):
        Rbar, pdi, logs, bkg = p
        model = model_intensity(q, Rbar, pdi, 10 ** logs, bkg, dist)
        return np.log10(np.clip(model, 1e-30, None)) - logI

    sol = least_squares(resid, p0, bounds=(lo, hi), method="trf", max_nfev=4000)
    rms = float(np.sqrt(np.mean(sol.fun ** 2)))
    # parameter covariance from the Jacobian (Gauss-Newton approximation)
    perr = [np.nan, np.nan, np.nan, np.nan]
    try:
        dof = max(1, len(sol.fun) - len(p0))
        cov = np.linalg.inv(sol.jac.T @ sol.jac) * (np.sum(sol.fun ** 2) / dof)
        perr = list(np.sqrt(np.abs(np.diag(cov))))
    except Exception:
        pass
    return sol.x, perr, rms


def _confidence(rms_log, guinier_valid, rel_err_R):
    """Blend fit residual + Guinier validity + size uncertainty into 0-1."""
    f_fit = 1.0 / (1.0 + (rms_log / 0.05) ** 2)          # ~0.05 log10 RMS = good
    f_guin = 1.0 if guinier_valid else 0.6
    re = rel_err_R if (rel_err_R is not None and np.isfinite(rel_err_R)) else 1.0
    f_unc = 1.0 / (1.0 + (re / 0.10) ** 2)               # 10% size error = borderline
    return float(round(max(0.0, min(1.0, f_fit * f_guin * f_unc)), 3))


def analyze_profile(q, I, sigma=None, dist="auto") -> dict:
    """Analyze one SAXS profile. Never raises — on failure returns a low-confidence
    result. ``dist``: 'schulz' | 'lognormal' | 'auto' (fit both, keep the better)."""
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
    q, I = q[m], I[m]
    order = np.argsort(q); q, I = q[order], I[order]
    result = {"n_points": int(q.size), "distribution": None, "size": None, "pdi": None,
              "guinier": None, "invariant": None, "phase": None, "fit": None,
              "uncertainty": None, "confidence": 0.0, "diagnostics": {}}
    if q.size < 12:
        result["diagnostics"]["error"] = "too few points"
        return result

    g = guinier_estimate(q, I, sigma)
    result["guinier"] = g
    result["invariant"] = {"Q_rel": float(_trapezoid(q ** 2 * I, q)), "absolute": False}
    try:
        result["phase"] = index_phase(detect_bragg_peaks(q, I))
    except Exception:
        result["phase"] = {"phase": "none", "n_peaks": 0}
    Rbar0 = g["R_from_Rg"] if g.get("R_from_Rg") else 1.0 / np.median(q)

    dists = ["schulz", "lognormal"] if dist == "auto" else [dist]
    best = None
    for d in dists:
        try:
            p, perr, rms = _fit_one(q, I, d, Rbar0)
            if best is None or rms < best[3]:
                best = (d, p, perr, rms)
        except Exception:
            continue
    if best is None:                              # full fit failed → Guinier fallback
        result["distribution"] = "guinier_only"
        if g.get("R_from_Rg"):
            result["size"] = {"radius": g["R_from_Rg"], "diameter": 2 * g["R_from_Rg"],
                              "unit": "same as 1/q (nm if q in nm^-1)", "source": "guinier"}
        result["confidence"] = _confidence(1.0, g.get("valid", False), None)
        result["diagnostics"]["note"] = "form-factor fit failed; Guinier-only size"
        return result

    d, (Rbar, pdi, logs, bkg), perr, rms = best
    rel_err_R = (perr[0] / Rbar) if (perr and np.isfinite(perr[0]) and Rbar) else None
    result["distribution"] = d
    result["size"] = {"radius": float(Rbar), "diameter": float(2 * Rbar),
                      "unit": "same as 1/q (nm if q in nm^-1)", "source": "form_factor"}
    result["pdi"] = float(pdi)
    result["fit"] = {"rms_log10": rms, "scale": float(10 ** logs), "background": float(bkg)}
    result["uncertainty"] = {"radius": float(perr[0]) if np.isfinite(perr[0]) else None,
                             "pdi": float(perr[1]) if np.isfinite(perr[1]) else None}
    result["confidence"] = _confidence(rms, g.get("valid", False), rel_err_R)
    result["diagnostics"] = {"rms_log10": round(rms, 4),
                             "guinier_valid": g.get("valid", False),
                             "rel_err_radius": round(rel_err_R, 3) if rel_err_R else None,
                             "chosen_distribution": d}
    return result
