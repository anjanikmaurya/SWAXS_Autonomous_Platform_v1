"""
src/analysis/core.py — SWAXS Analysis Functions
================================================
Pure-Python science functions for 1-D SAXS/WAXS analysis.
All functions accept numpy arrays and return plain dicts (JSON-serialisable).
They contain no Flask, no file I/O, and no side effects — keeping the
"all logic lives in src/" architecture rule.

Functions
---------
guinier_fit     — Rg, I₀ from ln(I) vs q² linear regression
porod_fit       — power-law exponent n, Porod constant K
kratky_plot     — I·q² vs q data (no fit — display only)
peak_fit        — N Gaussian peaks + linear background
sasmodels_fit   — arbitrary sasmodels model fitting via scipy

Each function returns a dict.  On error the dict contains an "error" key
with a human-readable message; the caller should check for this before
using numeric results.

Usage (from any app.py)
-----------------------
    from src.analysis.core import guinier_fit, porod_fit

    result = guinier_fit(q, I, sigma, auto_range=True)
    if "error" in result:
        print(result["error"])
    else:
        print("Rg =", result["Rg"])
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import curve_fit
from scipy.stats import linregress

# NumPy 2.0 renamed ``np.trapz`` to ``np.trapezoid`` and deprecated the old
# name (removal expected in a future release). Use the new name where available
# and fall back on older NumPy.
_trapezoid = getattr(np, "trapezoid", getattr(np, "trapz", None))

__all__ = [
    "guinier_fit",
    "porod_fit",
    "kratky_plot",
    "peak_fit",
    "sasmodels_fit",
]


# ── Guinier ───────────────────────────────────────────────────────────────────

def guinier_fit(
    q:          np.ndarray,
    I:          np.ndarray,
    sigma:      np.ndarray,
    q_min:      float | None = None,
    q_max:      float | None = None,
    auto_range: bool = True,
) -> dict:
    """
    Fit ln(I) = ln(I0) − Rg²/3 · q²  in the Guinier region.

    Parameters
    ----------
    q, I, sigma : np.ndarray
        1-D scattering data (positive values only expected).
    q_min, q_max : float or None
        Manual q-range limits.  None = use all data in that direction.
    auto_range : bool
        If True, iteratively refine q_max so that q_max·Rg ≤ 1.3 after
        an initial fit.

    Returns
    -------
    dict with keys:
        Rg, I0, slope, intercept, R2, q_range, qRg_max, plot
    On failure:
        {"error": "<message>"}
    """
    mask = np.ones(len(q), dtype=bool)
    if q_min is not None:
        mask &= q >= q_min
    if q_max is not None:
        mask &= q <= q_max

    q_r, I_r = q[mask], I[mask]

    if len(q_r) < 5:
        return {"error": "Insufficient data points in Guinier range"}

    q2  = q_r**2
    lnI = np.log(I_r)

    slope, intercept, r, _, _ = linregress(q2, lnI)

    if slope >= 0:
        return {"error": "Positive slope in Guinier plot — not a valid Guinier region"}

    Rg = float(np.sqrt(max(-3 * slope, 0)))
    I0 = float(np.exp(intercept))

    # Auto-refine range: enforce q_max·Rg ≤ 1.3
    if auto_range and Rg > 0:
        q_max_ok = 1.3 / Rg
        mask2 = mask & (q <= q_max_ok)
        if mask2.sum() >= 5:
            q_r2, I_r2 = q[mask2], I[mask2]
            sl2, ic2, r2, _, _ = linregress(q_r2**2, np.log(I_r2))
            if sl2 < 0:
                Rg              = float(np.sqrt(-3 * sl2))
                I0              = float(np.exp(ic2))
                slope, intercept, r = sl2, ic2, r2
                q2, lnI         = q_r2**2, np.log(I_r2)
                q_r             = q_r2

    q2_fit  = np.linspace(q2.min(), q2.max(), 100)
    lnI_fit = intercept + slope * q2_fit

    return {
        "Rg":        round(Rg, 4),
        "I0":        float(f"{I0:.4g}"),
        "slope":     float(f"{slope:.4g}"),
        "intercept": float(f"{intercept:.4g}"),
        "R2":        round(r**2, 5),
        "q_range":   [float(q_r.min()), float(q_r.max())],
        "qRg_max":   round(float(q_r.max()) * Rg, 3),
        "plot": {
            "q2_data":  q2.tolist(),
            "lnI_data": lnI.tolist(),
            "q2_fit":   q2_fit.tolist(),
            "lnI_fit":  lnI_fit.tolist(),
        },
    }


# ── Porod ─────────────────────────────────────────────────────────────────────

def porod_fit(
    q:     np.ndarray,
    I:     np.ndarray,
    sigma: np.ndarray,
    q_min: float | None = None,
    q_max: float | None = None,
) -> dict:
    """
    Power-law fit in log-log space: ln(I) = a + n·ln(q).

    Typical exponents:
      n ≈ −4  → smooth surface (Porod)
      n ≈ −2  → Gaussian chain / polymer in solution
      n ≈ −1  → rigid rod

    Returns
    -------
    dict with keys: n, K, R2, interpretation, q_range, plot
    On failure: {"error": "<message>"}
    """
    mask = np.ones(len(q), dtype=bool)
    if q_min is not None:
        mask &= q >= q_min
    if q_max is not None:
        mask &= q <= q_max

    q_r, I_r = q[mask], I[mask]

    if len(q_r) < 4:
        return {"error": "Insufficient data points for power-law fit"}

    lnq = np.log(q_r)
    lnI = np.log(I_r)
    slope, intercept, r, _, _ = linregress(lnq, lnI)
    K = float(np.exp(intercept))

    lnq_fit = np.linspace(lnq.min(), lnq.max(), 100)
    lnI_fit = intercept + slope * lnq_fit

    interp = (
        "smooth interface"           if abs(slope + 4) < 0.3 else
        "polymer / Gaussian chain"   if abs(slope + 2) < 0.5 else
        "rigid rod"                  if abs(slope + 1) < 0.4 else
        "other"
    )

    return {
        "n":              round(float(slope), 4),
        "K":              float(f"{K:.4g}"),
        "R2":             round(r**2, 5),
        "interpretation": interp,
        "q_range":        [float(q_r.min()), float(q_r.max())],
        "plot": {
            "lnq_data": lnq.tolist(),
            "lnI_data": lnI.tolist(),
            "lnq_fit":  lnq_fit.tolist(),
            "lnI_fit":  lnI_fit.tolist(),
        },
    }


# ── Kratky ────────────────────────────────────────────────────────────────────

def kratky_plot(
    q:     np.ndarray,
    I:     np.ndarray,
    q_min: float | None = None,
    q_max: float | None = None,
) -> dict:
    """
    Return Kratky-plot data: I·q² vs q.

    No fitting is performed — this is display data only.
    The ideal Guinier–Kratky peak for a compact globule appears at
    q·Rg = √3 with a value of (3/e) × I(0)/Rg².

    Returns
    -------
    dict with keys: q, Iq2
    """
    mask = np.ones(len(q), dtype=bool)
    if q_min is not None:
        mask &= q >= q_min
    if q_max is not None:
        mask &= q <= q_max
    q_r, I_r = q[mask], I[mask]
    return {"q": q_r.tolist(), "Iq2": (I_r * q_r**2).tolist()}


# ── Pair-distance distribution p(r) — indirect Fourier transform ──────────────

def pair_distance_ift(
    q:      np.ndarray,
    I:      np.ndarray,
    sigma:  np.ndarray | None = None,
    dmax:   float | None = None,
    n_r:    int = 120,
    alpha:  float | None = None,
) -> dict:
    """
    Estimate the pair-distance distribution p(r) by a Tikhonov-regularized
    indirect Fourier transform (numpy-only; no scipy/sasmodels needed).

    Model:  I(q) = 4π ∫₀^Dmax p(r) · sin(qr)/(qr) dr
    Solved as a smoothness-regularized least-squares for p on r∈[0,Dmax] with
    p(0)=p(Dmax)=0 and p≥0. Derived quantities:
        I0 = 4π ∫ p dr ,   Rg² = ∫ p r² dr / (2 ∫ p dr)

    Parameters
    ----------
    dmax  : maximum dimension (nm). If None, estimated as π/q_min.
    n_r   : number of r grid points.
    alpha : smoothness weight. If None, scaled automatically from the data.

    Returns
    -------
    dict: r, pr, Rg, I0, Dmax, chi2, q_fit, I_fit   (or {"error": ...})
    """
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0)
    q, I = q[m], I[m]
    if q.size < 10:
        return {"error": "Not enough positive q points for a p(r) estimate."}

    if sigma is not None:
        sigma = np.asarray(sigma, float)[m]
        sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, np.nan)
        if np.all(np.isnan(sigma)):
            sigma = np.ones_like(I)
        else:
            sigma = np.where(np.isnan(sigma), np.nanmax(sigma), sigma)
    else:
        sigma = np.ones_like(I)

    if dmax is None or dmax <= 0:
        dmax = float(np.pi / q.min())

    r  = np.linspace(0.0, dmax, n_r)
    dr = r[1] - r[0]

    # Design matrix A[i,j] = 4π · sinc(q_i r_j) · dr  (sinc(0)=1)
    qr = np.outer(q, r)
    with np.errstate(invalid="ignore", divide="ignore"):
        K = np.where(qr > 0, np.sin(qr) / qr, 1.0)
    A = 4.0 * np.pi * K * dr

    # Weighted rows (inverse-variance)
    w  = 1.0 / sigma
    Aw = A * w[:, None]
    Iw = I * w

    # Second-difference smoothness operator
    L = np.zeros((n_r, n_r))
    for i in range(1, n_r - 1):
        L[i, i - 1], L[i, i], L[i, i + 1] = 1.0, -2.0, 1.0

    AtA = Aw.T @ Aw
    LtL = L.T @ L
    if alpha is None:
        # scale smoothness to the data/operator magnitudes
        alpha = 1e-2 * np.trace(AtA) / max(np.trace(LtL), 1e-30)

    M = AtA + alpha * LtL
    # Boundary conditions p(0)=p(Dmax)=0 via strong penalty
    bc = np.zeros((2, n_r)); bc[0, 0] = 1.0; bc[1, -1] = 1.0
    M = M + (1e6 * np.trace(M) / n_r) * (bc.T @ bc)
    b = Aw.T @ Iw

    try:
        p = np.linalg.solve(M, b)
    except np.linalg.LinAlgError:
        p = np.linalg.lstsq(M, b, rcond=None)[0]
    p = np.clip(p, 0.0, None)          # p(r) ≥ 0

    integ = _trapezoid(p, r)
    if integ <= 0:
        return {"error": "p(r) solution non-positive — try a different Dmax."}
    I0 = float(4.0 * np.pi * integ)
    Rg = float(np.sqrt(_trapezoid(p * r**2, r) / (2.0 * integ)))

    I_fit = A @ p
    chi2  = float(np.sum(((I - I_fit) / sigma) ** 2) / max(len(I) - 1, 1))

    return {
        "r":     r.tolist(),
        "pr":    p.tolist(),
        "Rg":    round(Rg, 4),
        "I0":    float(f"{I0:.4g}"),
        "Dmax":  round(float(dmax), 4),
        "chi2":  round(chi2, 4),
        "q_fit": q.tolist(),
        "I_fit": I_fit.tolist(),
    }


# ── Dimensionless Kratky ──────────────────────────────────────────────────────

def dimensionless_kratky(q, I, Rg: float, I0: float) -> dict:
    """
    Dimensionless Kratky plot: (q·Rg)² · I(q)/I(0) vs q·Rg.
    A compact, folded particle peaks at q·Rg = √3 with height 1.104 (= 3/e).
    Requires Rg and I0 (from a Guinier fit). Returns {qRg, y, peak_qRg, peak_y}.
    """
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0)
    q, I = q[m], I[m]
    if Rg <= 0 or I0 <= 0 or q.size < 3:
        return {"error": "Need a valid Rg and I0 (run Guinier first)."}
    x = q * Rg
    y = (x ** 2) * (I / I0)
    k = int(np.argmax(y))
    return {"qRg": x.tolist(), "y": y.tolist(),
            "peak_qRg": round(float(x[k]), 3), "peak_y": round(float(y[k]), 3),
            "ideal_peak_qRg": round(float(np.sqrt(3)), 3), "ideal_peak_y": 1.104}


# ── Porod invariant, volume, MW, surface area ─────────────────────────────────

def classical_invariants(q, I, Rg: float, I0: float) -> dict:
    """
    Compute the Porod invariant and derived size/mass estimates from a curve,
    extrapolating below q_min (Guinier) and above q_max (Porod q^-4 tail).

    Returns (all scale-independent ratios; safe without absolute calibration):
        Q              — Porod invariant ∫ q² I dq
        porod_volume   — Vp = 2π² I0 / Q          [nm³]
        mw_porod_kda   — Vp / 1.66  (protein rule, approximate)
        Vc             — volume of correlation I0 / ∫ q I dq   [nm²]
        Qr             — Vc² / Rg   (Rambo–Tainer)
        mw_vc_kda      — protein MW from Qr (Rambo–Tainer, approximate)
        surface_area   — specific surface S/V = π·Kp / Q       [nm⁻¹]
        porod_constant — Kp = ⟨q⁴ I⟩ at high q
    Estimates assume a globular/two-phase particle; flag in QC if Porod region
    (q⁴I plateau) is not reached.
    """
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
    q, I = q[m], I[m]
    if q.size < 10 or Rg <= 0 or I0 <= 0:
        return {"error": "Need ≥10 positive points and a valid Rg/I0."}
    order = np.argsort(q); q, I = q[order], I[order]
    qmin, qmax = float(q[0]), float(q[-1])

    # measured-range integrals (trapezoid)
    Q_meas  = float(_trapezoid(q ** 2 * I, q))
    qI_meas = float(_trapezoid(q * I, q))

    # low-q Guinier extrapolation (0 → qmin)
    qg = np.linspace(0.0, qmin, 64)
    Ig = I0 * np.exp(-(Rg ** 2) * qg ** 2 / 3.0)
    Q_low  = float(_trapezoid(qg ** 2 * Ig, qg))
    qI_low = float(_trapezoid(qg * Ig, qg))

    # high-q Porod tail: I ≈ Kp / q⁴ for q > qmax
    hi = q >= np.quantile(q, 0.85)
    Kp = float(np.median(q[hi] ** 4 * I[hi])) if hi.sum() else 0.0
    Q_hi  = Kp / qmax            # ∫_qmax^∞ q²·(Kp/q⁴) dq = Kp/qmax
    qI_hi = Kp / (2 * qmax ** 2) # ∫_qmax^∞ q·(Kp/q⁴) dq = Kp/(2 qmax²)

    Q = Q_low + Q_meas + Q_hi
    qI_tot = qI_low + qI_meas + qI_hi
    if Q <= 0 or qI_tot <= 0:
        return {"error": "Invariant non-positive — check the curve/range."}

    Vp = 2 * np.pi ** 2 * I0 / Q
    Vc = I0 / qI_tot
    Qr = Vc ** 2 / Rg
    # Rambo–Tainer protein MW (constants calibrated in Å; convert nm→Å):
    Vc_A, Rg_A = Vc * 100.0, Rg * 10.0
    Qr_A = Vc_A ** 2 / Rg_A
    mw_vc_da = Qr_A / 0.1231
    sa = np.pi * Kp / Q

    return {
        "Q":              float(f"{Q:.4g}"),
        "porod_volume":   round(float(Vp), 3),
        "mw_porod_kda":   round(float(Vp / 1.66), 2),
        "Vc":             round(float(Vc), 4),
        "Qr":             round(float(Qr), 4),
        "mw_vc_kda":      round(float(mw_vc_da / 1000.0), 2),
        "porod_constant": float(f"{Kp:.4g}"),
        "surface_area":   float(f"{sa:.4g}"),
        "porod_tail_reached": bool(hi.sum() and Kp > 0),
    }


# ── Fit-quality QC ────────────────────────────────────────────────────────────

def guinier_quality(result: dict, shape: str = "globular") -> dict:
    """
    QC verdict for a Guinier fit: checks the qRg validity window and R².
    Upper qRg bound is shape-dependent (~1.3 globular, ~1.0 rod, ~1.7 disc).
    Returns {verdict, qRg_min, qRg_max, warnings:[...]}.
    """
    if "error" in result:
        return {"verdict": "FAIL", "warnings": [result["error"]]}
    Rg = result.get("Rg", 0.0)
    qr = result.get("q_range") or [0.0, 0.0]
    upper = {"globular": 1.3, "rod": 1.0, "disc": 1.7}.get(shape, 1.3)
    qRg_min = round(qr[0] * Rg, 3)
    qRg_max = round(qr[1] * Rg, 3)
    warns = []
    if qRg_max > upper + 1e-6:
        warns.append(f"q_max·Rg = {qRg_max} exceeds ~{upper} for a {shape} particle.")
    if qRg_min > 0.65:
        warns.append(f"q_min·Rg = {qRg_min} is high — extend to lower q if possible.")
    r2 = result.get("r2")
    if r2 is not None and r2 < 0.99:
        warns.append(f"Guinier R² = {r2} (<0.99) — fit may be poor.")
    verdict = "PASS" if not warns else "WARN"
    return {"verdict": verdict, "qRg_min": qRg_min, "qRg_max": qRg_max,
            "warnings": warns}


# ── Peak fit ──────────────────────────────────────────────────────────────────

_SQRT_4LN2 = float(2 * np.sqrt(np.log(2)))   # FWHM = _SQRT_4LN2 * sigma (Gaussian)


def _detect_peaks(q, y, max_peaks=6, prom_frac=0.04):
    """Numpy local-maxima detection above a linear baseline. Returns sorted q's."""
    n = len(y)
    if n < 5:
        return []
    base = np.linspace(y[0], y[-1], n)          # crude linear baseline
    yc = y - base
    span = float(yc.max() - yc.min()) or 1.0
    thr = prom_frac * float(yc.max())
    cand = [i for i in range(1, n - 1)
            if yc[i] > yc[i - 1] and yc[i] >= yc[i + 1] and yc[i] > thr]
    cand.sort(key=lambda i: yc[i], reverse=True)
    chosen = cand[:max_peaks]
    return sorted(float(q[i]) for i in chosen)


def _peak_shapes(shape: str):
    """Return (n_params, unit_fn, area_fn). Components parameterised by FWHM f
    with height A at centre q0."""
    if shape == "lorentzian":
        def unit(q, q0, f):
            return 1.0 / (1.0 + 4.0 * ((q - q0) / f) ** 2)
        def area(A, f, eta=None):
            return A * np.pi * f / 2.0
        return 3, unit, area
    if shape == "voigt":          # pseudo-Voigt (eta·Lorentz + (1-eta)·Gauss)
        def unit(q, q0, f, eta):
            g = np.exp(-4.0 * np.log(2) * ((q - q0) / f) ** 2)
            lo = 1.0 / (1.0 + 4.0 * ((q - q0) / f) ** 2)
            return eta * lo + (1.0 - eta) * g
        def area(A, f, eta):
            return A * (eta * np.pi * f / 2.0 +
                        (1 - eta) * f * np.sqrt(np.pi / (4 * np.log(2))))
        return 4, unit, area
    # gaussian (default)
    def unit(q, q0, f):
        return np.exp(-4.0 * np.log(2) * ((q - q0) / f) ** 2)
    def area(A, f, eta=None):
        return A * f * np.sqrt(np.pi / (4 * np.log(2)))
    return 3, unit, area


def peak_fit(
    q:      np.ndarray,
    I:      np.ndarray,
    sigma:  np.ndarray,
    q_min:  float | None = None,
    q_max:  float | None = None,
    n_peaks: int | None = None,
    shape:   str = "gaussian",
) -> dict:
    """
    Fit peaks on a linear background — WAXS peak analysis.

    shape   : "gaussian" | "lorentzian" | "voigt" (pseudo-Voigt).
    n_peaks : if None, peaks are auto-detected; otherwise the strongest
              ``n_peaks`` local maxima are used as seeds.

    Per peak returns: q0 (position), fwhm, area, height, d-spacing (nm and Å),
    and (voigt) the mixing η. Also bg_a/bg_b, chi2, and plot data + per-peak
    components.
    """
    shape = (shape or "gaussian").lower()
    if shape not in ("gaussian", "lorentzian", "voigt"):
        return {"error": f"Unknown peak shape '{shape}'."}
    mask = np.ones(len(q), dtype=bool)
    if q_min is not None:
        mask &= q >= q_min
    if q_max is not None:
        mask &= q <= q_max
    q_r, I_r, sig_r = q[mask], I[mask], sigma[mask]
    if len(q_r) < 6:
        return {"error": "Insufficient data points for peak fitting."}

    centers = _detect_peaks(q_r, I_r, max_peaks=(n_peaks or 6))
    if n_peaks:
        centers = centers[:n_peaks] or [float(q_r[int(np.argmax(I_r))])]
    if not centers:
        return {"error": "No peaks detected — adjust the q-range."}
    npk = len(centers)
    npar, unit, area_fn = _peak_shapes(shape)

    a0 = float(I_r.min())
    b0 = float((I_r[-1] - I_r[0]) / (q_r[-1] - q_r[0] + 1e-12))
    base0 = a0 + b0 * q_r
    f0 = float((q_r.max() - q_r.min()) / (8 * npk)) or 1e-3

    def model(qq, *p):
        val = p[0] + p[1] * qq
        for i in range(npk):
            seg = p[2 + npar * i: 2 + npar * (i + 1)]
            val = val + seg[0] * unit(qq, *seg[1:])
        return val

    p0 = [a0, b0]; lo = [-np.inf, -np.inf]; hi = [np.inf, np.inf]
    for c in centers:
        A0 = max(float(np.interp(c, q_r, I_r) - np.interp(c, q_r, base0)), 1e-6)
        if shape == "voigt":
            p0 += [A0, c, f0, 0.5]
            lo += [0.0, q_r.min(), 1e-5, 0.0]
            hi += [np.inf, q_r.max(), (q_r.max() - q_r.min()), 1.0]
        else:
            p0 += [A0, c, f0]
            lo += [0.0, q_r.min(), 1e-5]
            hi += [np.inf, q_r.max(), (q_r.max() - q_r.min())]

    try:
        popt, pcov = curve_fit(model, q_r, I_r, p0=p0, sigma=sig_r,
                               absolute_sigma=True, bounds=(lo, hi), maxfev=8000)
    except Exception as exc:
        return {"error": f"Fitting failed: {exc}"}
    perr = np.sqrt(np.abs(np.diag(pcov)))

    peaks, components = [], []
    for i in range(npk):
        seg = popt[2 + npar * i: 2 + npar * (i + 1)]
        A, q0, f = float(seg[0]), float(seg[1]), float(seg[2])
        eta = float(seg[3]) if shape == "voigt" else None
        d_nm = (2 * np.pi / q0) if q0 > 0 else 0.0
        peaks.append({
            "q0":     round(q0, 6),
            "q0_err": round(float(perr[3 + npar * i]) if len(perr) > 3 + npar * i else 0.0, 6),
            "height": round(A, 5),
            "fwhm":   round(f, 6),
            "area":   round(float(area_fn(A, f, eta)), 5),
            "d_nm":   round(d_nm, 4),
            "d_A":    round(d_nm * 10.0, 3),
            **({"eta": round(eta, 3)} if eta is not None else {}),
        })
        comp = (popt[0] + popt[1] * q_r) + A * unit(q_r, *seg[1:])
        components.append(comp.tolist())

    q_fit = np.linspace(q_r.min(), q_r.max(), 500)
    I_fit = model(q_fit, *popt)
    resid = (I_r - model(q_r, *popt)) / np.where(sig_r > 0, sig_r, 1.0)
    chi2 = float(np.sum(resid ** 2) / max(len(q_r) - len(popt), 1))

    return {
        "shape":   shape,
        "n_peaks": npk,
        "peaks":   peaks,
        "bg_a":    round(float(popt[0]), 5),
        "bg_b":    round(float(popt[1]), 6),
        "chi2":    round(chi2, 4),
        "q_range": [float(q_r.min()), float(q_r.max())],
        "plot": {
            "q_data": q_r.tolist(), "I_data": I_r.tolist(),
            "q_fit":  q_fit.tolist(), "I_fit": I_fit.tolist(),
            "components": components,
        },
    }


# ── sasmodels fit ─────────────────────────────────────────────────────────────

def sasmodels_fit(
    q:          np.ndarray,
    I:          np.ndarray,
    sigma:      np.ndarray,
    model_name: str,
    params:     dict,
    free:       list | None = None,
    bounds:     dict | None = None,
    q_unit:     str = "nm^-1",
) -> dict:
    """
    Fit a `sasmodels` model to I(q).

    Parameters
    ----------
    model_name : str
        A valid sasmodels model name. Product models with a structure factor are
        supported via the ``form@structure`` syntax, e.g. ``"sphere@hardsphere"``.
        Polydispersity is set by passing the usual ``*_pd``, ``*_pd_n``,
        ``*_pd_type`` parameters in ``params``.
    params : dict
        ``{param_name: number}`` — initial values. A value may also be the
        string ``"fit"`` (legacy) meaning "free, start from the model default".
    free : list[str] | None
        Names of parameters to OPTIMISE (others held fixed).
    bounds : dict | None
        Optional ``{param: [low, high]}`` for free parameters. When given, a
        bounded optimiser (L-BFGS-B) is used; the result flags any parameter
        pinned at a bound.
    q_unit : str
        Unit of the incoming ``q`` array — ``"nm^-1"`` (default, platform native)
        or ``"A^-1"`` / ``"1/A"``. sasmodels works internally in Å⁻¹ (lengths in
        Å, SLD in 1e-6 Å⁻²), and every model's default values and limits are
        Å-based, so q is converted to Å⁻¹ before fitting and the fitted lengths
        come out in Å. The returned fit-curve q is converted back to ``q_unit``
        so it overlays the input data correctly.

    Returns
    -------
    dict with keys: model, params, chi2, plot, converged, at_bounds, length_unit
    On failure: {"error": "<message>"}

    Notes
    -----
    Requires the ``sasmodels`` package:
        pip install sasmodels --break-system-packages
    """
    try:
        import sasmodels.core        as sm_core
        import sasmodels.data        as sm_data
        import sasmodels.direct_model as sm_dm
    except ImportError:
        return {
            "error": (
                "sasmodels not installed. "
                "Run: pip install sasmodels --break-system-packages"
            )
        }

    try:
        kernel = sm_core.load_model(model_name)
    except Exception as exc:
        return {"error": f"Could not load model '{model_name}': {exc}"}

    try:
        from scipy.optimize import minimize

        # sasmodels is native in Å⁻¹ (lengths in Å, SLD in 1e-6 Å⁻²). Convert the
        # incoming q so the model's Å-based defaults/limits are meaningful. q in
        # Å⁻¹ = q in nm⁻¹ / 10.
        _u = (q_unit or "nm^-1").lower().replace(" ", "")
        to_A = 0.1 if _u in ("nm^-1", "nm-1", "1/nm", "nm⁻¹") else 1.0
        q_A = np.asarray(q, dtype=float) * to_A

        data = sm_data.Data1D(x=q_A, y=I, dy=sigma)
        calc = sm_dm.DirectModel(data, kernel)

        # model defaults — used as starting values when none is given
        try:
            defaults = dict(kernel.info.parameters.defaults)
        except Exception:
            defaults = {}

        def _num(v, d):
            try:
                return float(v)
            except (TypeError, ValueError):
                return float(d)

        # Determine free vs fixed and the starting vector x0.
        if free is None:
            free_names = [k for k, v in params.items()
                          if (isinstance(v, str) and v.lower() == "fit")
                          or isinstance(v, (int, float))]
        else:
            free_names = [k for k in free]
        free_names = list(dict.fromkeys(free_names))   # dedupe, keep order

        starts, fixed = {}, {}
        for k, v in params.items():
            if k in free_names:
                starts[k] = (float(defaults.get(k, 1.0))
                             if isinstance(v, str) else _num(v, defaults.get(k, 1.0)))
            elif not (isinstance(v, str) and v.lower() == "fit"):
                fixed[k] = _num(v, defaults.get(k, 0.0))
        for k in free_names:                            # ensure every free has a start
            starts.setdefault(k, float(defaults.get(k, 1.0)))

        def residuals(vals):
            p = dict(zip(free_names, vals)); p.update(fixed)
            try:
                I_calc = calc(**p)
                return float(np.sum(((I - I_calc) / sigma) ** 2))
            except Exception:
                return 1e30

        converged = True
        at_bounds: list[str] = []
        if free_names:
            x0 = [starts[k] for k in free_names]
            bnd = None
            if bounds:
                bnd = [tuple(bounds.get(k, (None, None))) for k in free_names]
            if bnd and any(b != (None, None) for b in bnd):
                res = minimize(residuals, x0, method="L-BFGS-B", bounds=bnd,
                               options={"maxiter": 4000})
                # flag params sitting on a bound
                for k, v in zip(free_names, res.x):
                    lo, hi = bounds.get(k, (None, None))
                    if (lo is not None and abs(v - lo) <= 1e-6 * (abs(lo) + 1)) or \
                       (hi is not None and abs(v - hi) <= 1e-6 * (abs(hi) + 1)):
                        at_bounds.append(k)
            else:
                res = minimize(residuals, x0, method="Nelder-Mead",
                               options={"maxiter": 4000, "xatol": 1e-4, "fatol": 1e-4})
            converged = bool(getattr(res, "success", True))
            best = dict(zip(free_names, res.x)); best.update(fixed)
        else:
            best = dict(fixed)

        I_calc = calc(**best)
        # A DirectModel is bound to the q-grid of the Data1D it was built on, so we
        # cannot pass q=… into it. To sample the fit on a smooth grid we build a
        # second DirectModel on a fine q-grid (Å⁻¹); fall back to the data grid if
        # that fails for any reason.
        q_fine_A = np.geomspace(q_A.min(), q_A.max(), 400)
        try:
            calc_fine = sm_dm.DirectModel(sm_data.empty_data1D(q_fine_A), kernel)
            I_fine = calc_fine(**best)
        except Exception:                                  # noqa: BLE001
            q_fine_A, I_fine = q_A, I_calc
        # convert the fit-curve q back to the input unit so it overlays the data
        q_fine = q_fine_A / to_A
        chi2   = float(
            np.sum(((I - I_calc) / sigma) ** 2) / max(len(I) - len(free_names), 1)
        )

        return {
            "model":       model_name,
            "params":      {k: float(v) for k, v in best.items()},
            "chi2":        round(chi2, 4),
            "converged":   converged,
            "at_bounds":   at_bounds,
            "q_unit":      q_unit,
            "length_unit": "A",          # fitted lengths (radius, length, …) are in Å
            "sld_unit":    "1e-6 A^-2",  # fitted SLDs are in 1e-6 Å⁻²
            "plot": {
                "q_data": np.asarray(q, dtype=float).tolist(),
                "I_data": I.tolist(),
                "q_fit":  q_fine.tolist(),
                "I_fit":  I_fine.tolist(),
            },
        }

    except Exception as exc:
        return {"error": str(exc)}


def sasmodels_params(model_name: str) -> dict:
    """
    Return the fittable parameters of a sasmodels model for UI population:
    {"model": name, "parameters": [{name, default, units, lower, upper}, ...]}.
    On failure (or sasmodels absent): {"error": ...}.
    """
    try:
        import sasmodels.core as sm_core
    except ImportError:
        return {"error": "sasmodels not installed."}
    try:
        info = sm_core.load_model_info(model_name)
    except Exception as exc:
        return {"error": f"Could not load model '{model_name}': {exc}"}
    out = []
    try:
        for p in info.parameters.kernel_parameters:
            lo, hi = (p.limits if isinstance(p.limits, (list, tuple)) else (None, None))
            out.append({
                "name":    p.name,
                "default": float(p.default) if p.default is not None else None,
                "units":   getattr(p, "units", "") or "",
                "lower":   (None if lo in (-np.inf, None) else float(lo)),
                "upper":   (None if hi in (np.inf, None) else float(hi)),
                "description": getattr(p, "description", "") or "",
            })
    except Exception as exc:
        return {"error": str(exc)}
    return {"model": model_name, "parameters": out}
