"""src/analysis/features.py: model-free features of one SAXS curve.

The exact numbers an expert reads off a curve before choosing a model, so the
structure fitter (and anyone else) can reason from facts rather than raw I(q):

    guinier        Rg, I0, q·Rg_max, R², valid
    porod          high-q power-law exponent n (−4 smooth surface, −2 chain …)
    kratky         dimensionless Kratky peak position and height (globular: √3, 1.104)
    pr             p(r) Dmax and Rg (indirect Fourier transform)
    invariants     Porod volume, Vc, surface-to-volume
    minima         positions of the form-factor oscillation minima, their depth,
                   the sphere radius the first one implies (q·R = 4.493) and the
                   spacing ratio (≈ evenly spaced for spheres)
    lowq           low-q log-log slope (strongly negative: aggregation / upturn)
    data           q range (decades), points, median I/σ

Every block is computed independently and fails softly into ``warnings``;
units follow the input (nm⁻¹ → nm). All values are plain floats, JSON-safe.
"""
from __future__ import annotations

import math

import numpy as np

from src.analysis import core as _core

_SPHERE_FIRST_MIN_QR = 4.4934         # first zero of the sphere form factor


def _f(x, nd=5):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _clean(q, I, sigma):
    q = np.asarray(q, float)
    I = np.asarray(I, float)
    s = None if sigma is None else np.asarray(sigma, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
    if s is not None and s.shape == q.shape:
        m &= np.isfinite(s) & (s > 0)
        s = s[m]
    else:
        s = None
    o = np.argsort(q[m])
    return q[m][o], I[m][o], (None if s is None else s[o])


def _smooth(y, w):
    w = max(3, int(w) | 1)
    if len(y) < w:
        return y.copy()
    k = np.ones(w) / w
    pad = w // 2
    yp = np.concatenate([np.full(pad, y[0]), y, np.full(pad, y[-1])])
    return np.convolve(yp, k, mode="valid")


def oscillation_minima(q, I, max_minima=4, min_depth=0.05) -> dict:
    """Form-factor minima: local minima of the Porod-flattened log(I·q⁴).

    Flattening by q⁴ turns the decaying curve into a series of humps, so the
    dips between humps are the minima. Depth is the dip below the mean of its
    two neighbouring maxima, in natural-log units (0.05 ≈ 5 %); shallow dips
    are noise or smeared by polydispersity and are dropped.
    """
    if len(q) < 20:
        return {"q_min": [], "depth": [], "note": "too few points"}
    y = _smooth(np.log(I * q ** 4), len(q) * 0.03)
    idx = [i for i in range(2, len(y) - 2)
           if y[i] < y[i - 1] and y[i] <= y[i + 1] and y[i] < y[i - 2] and y[i] <= y[i + 2]]
    out_q, out_d = [], []
    for i in idx:
        left, right = y[:i].max(initial=-np.inf), y[i + 1:].max(initial=-np.inf)
        lm = y[max(0, i - len(y) // 6):i].max(initial=y[i])
        rm = y[i + 1:i + 1 + len(y) // 6].max(initial=y[i])
        depth = 0.5 * (lm + rm) - y[i]
        if np.isfinite(left) and np.isfinite(right) and depth >= min_depth:
            out_q.append(float(q[i]))
            out_d.append(float(depth))
        if len(out_q) >= max_minima:
            break
    res = {"q_min": [_f(v) for v in out_q], "depth": [_f(v, 3) for v in out_d]}
    if out_q:
        res["sphere_R_from_first_min"] = _f(_SPHERE_FIRST_MIN_QR / out_q[0], 4)
    if len(out_q) >= 3:
        d1, d2 = out_q[1] - out_q[0], out_q[2] - out_q[1]
        res["spacing_ratio"] = _f(d2 / d1, 3) if d1 > 0 else None
    return res


def lowq_slope(q, I, frac=0.12) -> float | None:
    n = max(5, int(len(q) * frac))
    if len(q) < n + 2:
        return None
    x, y = np.log(q[:n]), np.log(I[:n])
    return _f(np.polyfit(x, y, 1)[0], 3)


def compute_features(q, I, sigma=None, with_pr: bool = True) -> dict:
    """All features for one curve. Never raises."""
    q, I, s = _clean(q, I, sigma)
    out: dict = {"warnings": []}
    if len(q) < 10:
        out["warnings"].append("fewer than 10 usable points")
        return out
    out["data"] = {"q_min": _f(q[0]), "q_max": _f(q[-1]),
                   "q_decades": _f(math.log10(q[-1] / q[0]), 3), "n_points": int(len(q)),
                   "median_snr": _f(np.median(I / s), 2) if s is not None else None}
    sig = s if s is not None else I * 0.05

    g = None
    try:
        g = _core.guinier_fit(q, I, sig)
        out["guinier"] = {"Rg": _f(g.get("Rg")), "I0": _f(g.get("I0")),
                          "qRg_max": _f(g.get("qRg_max"), 3), "R2": _f(g.get("R2"), 4),
                          # a real Guinier region: converged, ends at q·Rg 0.5 to 1.3
                          # (a flat curve "fits" with Rg ≈ 0), and a straight line
                          "valid": (bool(g.get("converged"))
                                    and 0.5 <= (g.get("qRg_max") or 0) <= 1.3
                                    and (g.get("R2") or 0) >= 0.9),
                          "q_range": [_f(v) for v in (g.get("q_range") or [])]}
    except Exception as exc:                                        # noqa: BLE001
        out["warnings"].append(f"guinier: {exc}")

    try:
        qcut = float(np.exp(np.log(q[0]) + 0.7 * (np.log(q[-1]) - np.log(q[0]))))
        p = _core.porod_fit(q, I, sig, q_min=qcut)
        out["porod"] = {"n": _f(p.get("n"), 3), "R2": _f(p.get("R2"), 4),
                        "interpretation": p.get("interpretation"),
                        "q_range": [_f(v) for v in (p.get("q_range") or [])]}
    except Exception as exc:                                        # noqa: BLE001
        out["warnings"].append(f"porod: {exc}")

    if g and g.get("Rg") and g.get("I0"):
        try:
            d = _core.dimensionless_kratky(q, I, g["Rg"], g["I0"])
            out["kratky"] = {"peak_qRg": _f(d.get("peak_qRg"), 3),
                             "peak_height": _f(d.get("peak_y"), 3),
                             "globular_reference": [1.732, 1.104]}
        except Exception as exc:                                    # noqa: BLE001
            out["warnings"].append(f"kratky: {exc}")
        try:
            iv = _core.classical_invariants(q, I, g["Rg"], g["I0"])
            out["invariants"] = {k: _f(iv.get(k)) for k in
                                 ("porod_volume", "Vc", "surface_area", "porod_tail_reached")}
            out["invariants"]["porod_tail_reached"] = bool(iv.get("porod_tail_reached"))
        except Exception as exc:                                    # noqa: BLE001
            out["warnings"].append(f"invariants: {exc}")

    if with_pr:
        try:
            pr = _core.pair_distance_ift(q, I, sig)
            out["pr"] = {"Dmax": _f(pr.get("Dmax")), "Rg": _f(pr.get("Rg")),
                         "chi2": _f(pr.get("chi2"), 3)}
        except Exception as exc:                                    # noqa: BLE001
            out["warnings"].append(f"p(r): {exc}")

    try:
        out["minima"] = oscillation_minima(q, I)
    except Exception as exc:                                        # noqa: BLE001
        out["warnings"].append(f"minima: {exc}")
    out["lowq"] = {"slope": lowq_slope(q, I)}
    return out
