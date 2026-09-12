"""
src/background/core.py — Background-subtraction science (pure numpy).

Moved out of background/app.py (defect D3) so the maths can be tested and
reused without Flask. Nothing here touches app state, files or the network.

  _interpolate_onto        log-log interpolation of a positive profile onto a q-grid
  _interpolate_onto_signed same, for signed (already-subtracted) data
  _subtract                I_sam - s*I_bkg with sigma^2 = sigma_sam^2 + s^2 sigma_bkg^2
  _auto_scale              high-q weighted least-squares scale, MAD-clipped
  truncate_rebin           truncate to [q_min, q_max] and resample onto n points
  _qc_metrics              subtraction QC numbers (negative fraction, high-q ratio…)

Error-propagation caveats are documented in docs/ERROR_PROPAGATION.md §4.
"""
from __future__ import annotations

import numpy as np

def _interpolate_onto(q_target: np.ndarray,
                      q_src: np.ndarray,
                      I_src: np.ndarray,
                      sig_src: np.ndarray
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Log-space interpolation of (I_src, sig_src) onto q_target grid.
    Only finite, positive source points are used (so non-positive background
    points can't corrupt the log interpolation). (Audit C2)

    ERROR PROPAGATION caveat: interpolating sigma this way is an approximation
    to the exact rule sigma_new^2 = (1-t)^2*sigma_1^2 + t^2*sigma_2^2, and it
    correlates neighbouring q-points in a way nothing downstream accounts for
    (Gardner 2003, 10.6028/jres.108.008). Avoided entirely when sample and
    background were reduced onto the same q-grid — the usual case here, since
    one config.yml governs both. See docs/ERROR_PROPAGATION.md §4.
    """
    q_src = np.asarray(q_src, float); I_src = np.asarray(I_src, float); sig_src = np.asarray(sig_src, float)
    m = np.isfinite(q_src) & np.isfinite(I_src) & (q_src > 0) & (I_src > 0)
    if m.sum() < 2:
        z = np.zeros_like(q_target, dtype=float)
        return q_target, z, z
    log_q_t = np.log(q_target)
    log_q_s = np.log(q_src[m])
    I_interp   = np.exp(np.interp(log_q_t, log_q_s, np.log(I_src[m])))
    sig_interp = np.exp(np.interp(log_q_t, log_q_s, np.log(np.maximum(sig_src[m], 1e-30))))
    return q_target, I_interp, sig_interp


def _interpolate_onto_signed(q_target: np.ndarray,
                             q_src: np.ndarray,
                             I_src: np.ndarray,
                             sig_src: np.ndarray
                             ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resample (I_src, sig_src) onto q_target while PRESERVING SIGN.

    Same log-q axis as _interpolate_onto, but intensity is interpolated in LINEAR
    space and the (I_src > 0) mask is dropped — so negative intensities survive.
    This is for the SUBTRACTED curve: negative high-q points are a real, load-
    bearing signature of over-subtraction, and the positive-only log scheme in
    _interpolate_onto silently dropped them and log-interpolated across the gap,
    fabricating a positive plateau. That defeated the very over-subtraction check
    the Quality Gate grades the WRITTEN file on. (The positive-only scheme remains
    correct for a background, which is physically positive.)
    """
    q_src = np.asarray(q_src, float); I_src = np.asarray(I_src, float); sig_src = np.asarray(sig_src, float)
    m = np.isfinite(q_src) & np.isfinite(I_src) & (q_src > 0)   # NOTE: no positivity on I
    if m.sum() < 2:
        z = np.zeros_like(q_target, dtype=float)
        return q_target, z, z
    log_q_s = np.log(q_src[m])
    order   = np.argsort(log_q_s)                # np.interp needs ascending xp
    log_q_s = log_q_s[order]
    I_m     = I_src[m][order]
    sig_m   = np.where(np.isfinite(sig_src[m]), sig_src[m], 0.0)[order]
    log_q_t = np.log(q_target)
    I_interp   = np.interp(log_q_t, log_q_s, I_m)        # linear in I → sign kept
    sig_interp = np.interp(log_q_t, log_q_s, sig_m)
    return q_target, I_interp, sig_interp


def _subtract(q_sam: np.ndarray, I_sam: np.ndarray, sig_sam: np.ndarray,
              q_bkg: np.ndarray, I_bkg: np.ndarray, sig_bkg: np.ndarray,
              scale: float
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Subtract background with scale factor, propagating errors.
    Interpolates bkg onto sample q-grid; returns (q, I_sub, sigma_sub).

    ERROR PROPAGATION: sigma_sub = sqrt(sigma_sam^2 + (scale*sigma_bkg)^2), the
    textbook form for sigma^2(A - c*B) = sigma^2(A) + c^2*sigma^2(B) and exactly
    Sedlak, Bruetzel & Lipfert 2017 eq. (6) (10.1107/S1600576717003077).
    Known omission: the fitted `scale` carries its own uncertainty from
    `_auto_scale`, and the rigorous form would add I_bkg^2 * sigma^2(scale).
    See docs/ERROR_PROPAGATION.md §4.
    """
    _, I_b, sig_b = _interpolate_onto(q_sam, q_bkg, I_bkg, sig_bkg)
    I_sub   = I_sam - scale * I_b
    sig_sub = np.sqrt(sig_sam**2 + (scale * sig_b)**2)
    return q_sam, I_sub, sig_sub


def truncate_rebin(q_nm: np.ndarray, I: np.ndarray, sigma: np.ndarray,
                   q_min: float, q_max: float, n_points: int,
                   spacing: str = "linear", q_unit: str = "A"):
    """Truncate to [q_min, q_max] and resample onto n_points. Source q is nm⁻¹;
    output q is in q_unit ('A' → Å⁻¹ = nm⁻¹/10). Linear or log grid. Intensity is
    interpolated in log-space (same scheme as the background interpolation)."""
    q_nm = np.asarray(q_nm, float)
    scale = 0.1 if str(q_unit).lower().startswith("a") else 1.0   # nm⁻¹ → Å⁻¹
    q_src = q_nm * scale
    n = int(n_points)

    # NEVER extrapolate. np.interp holds the edge value flat outside the source
    # range, so a window wider than the detector's actual q coverage produced a
    # long fabricated plateau (measured: 74% of the default 0.03–0.6 Å⁻¹ grid on a
    # 3 m camera). That plateau is invented data: it biases the fitted PDI ~2× and
    # corrupts the confidence the optimizer gates on. Clip the request to what was
    # really measured and report the clip.
    lo_src, hi_src = float(np.nanmin(q_src)), float(np.nanmax(q_src))
    lo = max(float(q_min), lo_src)
    hi = min(float(q_max), hi_src)
    if not (hi > lo):
        raise ValueError(
            f"requested q window [{q_min:g}, {q_max:g}] does not overlap the "
            f"measured range [{lo_src:g}, {hi_src:g}] — nothing to rebin")
    clipped = (lo > float(q_min) + 1e-12) or (hi < float(q_max) - 1e-12)

    if str(spacing).lower().startswith("log"):
        grid = np.logspace(np.log10(lo), np.log10(hi), n)
    else:
        grid = np.linspace(lo, hi, n)
    # Sign-preserving: the subtracted curve can legitimately go negative, and
    # dropping those points here would hide over-subtraction from the Quality Gate.
    _, I_g, sig_g = _interpolate_onto_signed(grid, q_src, I, sigma)
    return grid, I_g, sig_g, clipped, (lo, hi)


def _auto_scale(q_sam, I_sam, sig_sam, q_bkg, I_bkg, sig_bkg,
                frac: float = 0.25, qmin=None, qmax=None) -> dict:
    """
    Determine a background scale by matching sample and background in a HIGH-q
    window — where the macromolecular signal is negligible and only the
    solvent/cell remains (standard SAXS validity check; see SSRL/EMBL/BioXTAS).

    Weighted least squares over the window:
        s = Σ w·I_s·I_b / Σ w·I_b²,    w = 1/σ_sample²
    Default window = top `frac` of the overlapping q-range; override with qmin/qmax.
    Returns {scale, q_min, q_max, n_points}.
    """
    _, I_b, _sig_b = _interpolate_onto(q_sam, q_bkg, I_bkg, sig_bkg)
    if qmin is None or qmax is None:
        qlo = q_sam.min() + (1.0 - frac) * (q_sam.max() - q_sam.min())
        qhi = q_sam.max()
    else:
        qlo, qhi = float(qmin), float(qmax)
    # A single NaN in I_sam/sig_sam inside the window would give w = NaN, s = NaN,
    # and min(max(nan, .1), 5) is still NaN — the whole subtracted curve goes NaN.
    win = ((q_sam >= qlo) & (q_sam <= qhi) & (I_b > 0)
           & np.isfinite(I_sam) & np.isfinite(sig_sam) & (sig_sam > 0))
    if win.sum() < 3:
        return {"scale": 1.0, "q_min": float(qlo), "q_max": float(qhi),
                "n_points": int(win.sum()), "n_clipped": 0}

    Is, Ib = I_sam[win], I_b[win]
    w = 1.0 / np.maximum(sig_sam[win] ** 2, 1e-30)

    def _ls(Is, Ib, w):
        den = float(np.sum(w * Ib ** 2))
        return (float(np.sum(w * Is * Ib)) / den) if den > 0 else 1.0

    s = _ls(Is, Ib, w)
    # One robust sigma-clip pass on the residuals so sharp WAXS Bragg peaks /
    # outliers in the window don't bias the scale. (Audit C3)
    n_clipped = 0
    r   = Is - s * Ib
    med = float(np.median(r))
    mad = float(np.median(np.abs(r - med))) * 1.4826
    if mad > 0:
        keep = np.abs(r - med) <= 3.0 * mad
        n_clipped = int((~keep).sum())
        if keep.sum() >= 3:
            s = _ls(Is[keep], Ib[keep], w[keep])

    if not np.isfinite(s):
        s = 1.0
    s = float(min(max(s, 0.1), 5.0))   # clamp to a sane range
    return {"scale": s, "q_min": float(qlo), "q_max": float(qhi),
            "n_points": int(win.sum()), "n_clipped": n_clipped}


def _qc_metrics(q, I_sub, I_sam, frac: float = 0.25) -> dict:
    """
    Quality-control metrics + warnings for a subtracted curve.
      • pct_negative  — over-subtraction indicator (negatives → upturns in log)
      • highq_ratio   — mean|I_sub|/mean(I_sample) in the high-q window
                        (≈0 good; ≈1 suggests under-subtraction / buffer left in)
      • lowq_slope    — ln I vs ln q slope in the low-q decade (steep ⇒ aggregation)
    """
    q = np.asarray(q, float); I_sub = np.asarray(I_sub, float); I_sam = np.asarray(I_sam, float)
    n = len(q)
    warnings = []
    n_neg = int(np.sum(I_sub < 0))
    pct_neg = 100.0 * n_neg / max(n, 1)

    qlo = q.min() + (1.0 - frac) * (q.max() - q.min())
    hi  = q >= qlo
    highq_ratio = (float(np.mean(np.abs(I_sub[hi]))) /
                   float(np.mean(np.abs(I_sam[hi])) + 1e-30)) if hi.sum() else None

    # low-q upturn (aggregation): slope of ln I vs ln q over lowest 10%
    lowq_slope = None
    pos = I_sub > 0
    if pos.sum() > 10:
        ql, Il = q[pos], I_sub[pos]
        k = max(5, int(len(ql) * 0.10))
        try:
            lowq_slope = float(np.polyfit(np.log(ql[:k]), np.log(Il[:k]), 1)[0])
        except Exception:
            lowq_slope = None

    if pct_neg > 5:
        warnings.append({"severity": "error" if pct_neg > 15 else "warning",
                         "msg": f"Over-subtraction: {pct_neg:.0f}% of points are negative "
                                f"(sharp upturns in log). Lower the scale."})
    if highq_ratio is not None and highq_ratio > 0.5:
        warnings.append({"severity": "warning",
                         "msg": f"Possible under-subtraction: high-q residual is "
                                f"{highq_ratio*100:.0f}% of the sample — raise the scale or "
                                f"check buffer match."})
    if lowq_slope is not None and lowq_slope < -3.0:
        warnings.append({"severity": "warning",
                         "msg": f"Low-q upturn (slope {lowq_slope:.1f}) — possible "
                                f"aggregation; consider SEC-SAXS or re-centrifugation."})
    if not warnings:
        warnings.append({"severity": "ok", "msg": "No subtraction issues detected."})

    return {"n_negative": n_neg, "pct_negative": round(pct_neg, 1),
            "highq_ratio": round(highq_ratio, 3) if highq_ratio is not None else None,
            "lowq_slope": round(lowq_slope, 2) if lowq_slope is not None else None,
            "warnings": warnings}
