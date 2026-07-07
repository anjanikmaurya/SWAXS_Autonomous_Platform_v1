"""
src/quality/core.py — Quality grading for subtracted scattering profiles
=========================================================================
Shared by the Quality Gate app (port 5006).  Pure, deterministic scoring of a
single background-subtracted 1-D profile, plus series-level consensus.

A profile is scored 0–100 (100 = pristine).  The score starts at 100 and each
quality signal subtracts a penalty:

  • over-subtraction  — fraction of negative intensities (two near-equal curves
                        subtracted leave noise crossing zero)
  • low SNR           — median I/σ over the usable range
  • coverage          — usable q-decades and point count
  • smoothness        — spike/outlier fraction (robust z on Δ²logI)
  • featureless       — flat / structureless shape ≈ background (shape test only)

verdict = "good" if score ≥ threshold (default 60) else "bad".

The grading is rule-based and fully reproducible.  The Quality Gate app may
additionally call an LLM to adjudicate borderline scores (see app.py); that is
an optional layer on top of these numbers, never a replacement for them.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from ..utils.read_dat_metadata import read_dat_data_metadata

__all__ = ["grade_profile", "score_metrics", "DEFAULT_THRESHOLDS",
           "thresholds_for", "sample_key"]

logger = logging.getLogger("swaxs_platform")

# ── Tunable thresholds ────────────────────────────────────────────────────────
# Penalties are scaled so a single severe failure can flip a profile to "bad".
DEFAULT_THRESHOLDS: dict = {
    "score_pass":      60.0,   # verdict cutoff (good ≥ this)
    "borderline":      10.0,   # ± window around the cutoff that the LLM may adjudicate
    # over-subtraction
    "neg_warn_pct":    5.0,    # negatives above this start costing points
    "neg_fail_pct":    25.0,   # negatives at/above this ≈ full over-subtraction penalty
    # SNR
    "snr_good":        10.0,   # median I/σ at/above this = no SNR penalty
    "snr_floor":       2.0,    # at/below this = full SNR penalty
    # coverage
    "min_decades":     1.0,    # usable log10(q) span below this loses coverage points
    "min_points":      50,     # fewer usable points than this loses coverage points
    # smoothness
    "spike_fail_frac": 0.10,   # this fraction of spiky points ≈ full smoothness penalty
    # featureless (shape only)
    "dyn_range_min":   0.5,    # low dynamic range (with low SNR) looks structureless
    "dyn_range_hard":  0.2,    # this flat ⇒ featureless regardless of SNR
    # low-q aggregation
    "aggr_slope":     -3.2,    # ln I vs ln q slope (low decade) steeper ⇒ aggregation
    # penalty weights (max points removable by each signal)
    "w_neg":           40.0,
    "w_snr":           40.0,
    "w_cov":           15.0,
    "w_spike":         15.0,
    "w_featureless":   55.0,
    "w_aggr":          20.0,
}

# Per-detector overrides merged on top of DEFAULT_THRESHOLDS.
# WAXS profiles are peak-dominated: lower dynamic range and SNR are normal, and
# a steep low-q rise is not "aggregation", so those checks are relaxed.
DETECTOR_THRESHOLDS: dict = {
    "saxs": {},
    "waxs": {
        "snr_good":       6.0,
        "dyn_range_min":  0.3,
        "dyn_range_hard": 0.12,
        "aggr_slope":    -6.0,   # effectively disables the low-q aggregation flag
        "w_aggr":         0.0,
    },
}


def thresholds_for(detector: str | None) -> dict:
    """Merge the per-detector overrides onto the defaults."""
    t = dict(DEFAULT_THRESHOLDS)
    t.update(DETECTOR_THRESHOLDS.get((detector or "").lower(), {}))
    return t

# Strip averaging / subtraction boilerplate to recover the sample identity.
_SUFFIX_RE = re.compile(
    r"(_sub)?(_batch\d+)?(_\d+files)?(_(?:Average|Avg))?$", re.IGNORECASE)


def sample_key(name: str) -> str:
    """Sample identity (drops _sub/_batchNNN/_NNfiles/_Average) — used for labels."""
    stem = Path(name).stem
    prev = None
    while prev != stem:                       # strip repeatedly (order-independent)
        prev = stem
        stem = _SUFFIX_RE.sub("", stem)
    return stem or Path(name).stem


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(min(max(x, lo), hi))


def _lerp_penalty(value: float, good: float, fail: float, weight: float) -> float:
    """Penalty in [0, weight]: 0 when value≤good, weight when value≥fail (linear)."""
    if fail <= good:
        return 0.0
    frac = (value - good) / (fail - good)
    return weight * _clamp(frac, 0.0, 1.0)


def _inv_penalty(value: float, good: float, floor: float, weight: float) -> float:
    """Penalty in [0, weight]: 0 when value≥good, weight when value≤floor (linear)."""
    if good <= floor:
        return 0.0
    frac = (good - value) / (good - floor)
    return weight * _clamp(frac, 0.0, 1.0)


def compute_metrics(q: np.ndarray, I: np.ndarray, sigma: np.ndarray) -> dict:
    """Raw quality metrics for a subtracted profile (no scoring)."""
    q = np.asarray(q, float); I = np.asarray(I, float); sigma = np.asarray(sigma, float)
    finite = np.isfinite(q) & np.isfinite(I) & (q > 0)
    q, I, sigma = q[finite], I[finite], sigma[finite]
    n = int(q.size)
    if n < 3:
        return {"n_points": n, "usable": False}

    n_neg   = int(np.sum(I < 0))
    pct_neg = 100.0 * n_neg / n

    pos = I > 0
    sig_ok = pos & np.isfinite(sigma) & (sigma > 0)
    snr = float(np.median(I[sig_ok] / sigma[sig_ok])) if sig_ok.sum() >= 3 else 0.0

    q_decades = float(np.log10(q.max() / q.min())) if q.min() > 0 else 0.0

    # dynamic range over positive points (structure indicator)
    if pos.sum() >= 5:
        Ip = I[pos]
        hi = float(np.percentile(Ip, 95)); lo = float(np.percentile(Ip, 5))
        dyn_range = float(np.log10(hi / lo)) if lo > 0 else 0.0
    else:
        dyn_range = 0.0

    # spike fraction: robust z-score on the 2nd difference of log I (positive pts)
    spike_frac = 0.0
    if pos.sum() >= 8:
        li = np.log(I[pos])
        d2 = np.diff(li, n=2)
        med = np.median(d2); mad = np.median(np.abs(d2 - med)) or 1e-12
        z = 0.6745 * (d2 - med) / mad
        spike_frac = float(np.mean(np.abs(z) > 5.0))

    # low-q slope: ln I vs ln q over the lowest ~15% of positive points
    lowq_slope = None
    if pos.sum() >= 10:
        qp, Ip2 = q[pos], I[pos]
        order = np.argsort(qp)
        qp, Ip2 = qp[order], Ip2[order]
        k = max(5, int(qp.size * 0.15))
        try:
            lowq_slope = float(np.polyfit(np.log(qp[:k]), np.log(Ip2[:k]), 1)[0])
        except Exception:
            lowq_slope = None

    return {
        "n_points":   n,
        "usable":     True,
        "q_min":      float(q.min()),
        "q_max":      float(q.max()),
        "q_decades":  round(q_decades, 3),
        "pct_negative": round(pct_neg, 2),
        "snr":        round(snr, 2),
        "dyn_range":  round(dyn_range, 3),
        "spike_frac": round(spike_frac, 4),
        "lowq_slope": round(lowq_slope, 2) if lowq_slope is not None else None,
    }


def grade_profile(path: str | Path, thresholds: dict | None = None,
                  detector: str | None = None) -> dict:
    """
    Grade one subtracted .dat file.

    ``detector`` selects per-detector default thresholds (SAXS vs WAXS); an
    explicit ``thresholds`` dict is merged on top (e.g. the user's live
    ``score_pass`` cutoff).

    Returns::

        {
          "name": str, "path": str,
          "usable": bool,
          "score": float (0–100), "verdict": "good"|"bad",
          "flags": [str, ...], "reasons": [str, ...],
          "metrics": {...},
        }
    """
    t = {**thresholds_for(detector), **(thresholds or {})}
    path = Path(path)
    try:
        _, q, I, sigma, _meta = read_dat_data_metadata(path)
    except Exception as exc:
        return {"name": path.name, "path": str(path), "usable": False,
                "score": 0.0, "verdict": "bad", "flags": ["unreadable"],
                "reasons": [f"could not read file: {exc}"], "metrics": {}}

    m = compute_metrics(q, I, sigma)
    if not m.get("usable"):
        return {"name": path.name, "path": str(path), "usable": False,
                "score": 0.0, "verdict": "bad", "flags": ["empty"],
                "reasons": ["fewer than 3 usable points"], "metrics": m}

    score, flags, reasons = score_metrics(m, t)
    verdict = "good" if score >= t["score_pass"] else "bad"
    if verdict == "good" and not reasons:
        reasons.append("clean profile — good SNR, structured, no over-subtraction")

    return {"name": path.name, "path": str(path), "usable": True,
            "score": round(score, 1), "verdict": verdict,
            "flags": flags, "reasons": reasons, "metrics": m}


def score_metrics(m: dict, thresholds: dict) -> tuple[float, list[str], list[str]]:
    """
    Compute the 0–100 score, flags, and reasons from a metrics dict and a
    fully-merged ``thresholds`` dict (all weight/threshold keys present).

    Factored out of :func:`grade_profile` so the score can be recomputed from
    cached metrics whenever the user edits weights/thresholds — no file re-read.
    """
    t = thresholds
    if not m or not m.get("usable"):
        return 0.0, ["empty"], ["fewer than 3 usable points"]

    flags: list[str] = []
    reasons: list[str] = []

    p_neg = _lerp_penalty(m["pct_negative"], t["neg_warn_pct"], t["neg_fail_pct"], t["w_neg"])
    if m["pct_negative"] > t["neg_warn_pct"]:
        flags.append("over_subtraction")
        reasons.append(f"{m['pct_negative']:.0f}% negative points (over-subtraction)")

    p_snr = _inv_penalty(m["snr"], t["snr_good"], t["snr_floor"], t["w_snr"])
    if m["snr"] < t["snr_good"]:
        flags.append("low_snr")
        reasons.append(f"low SNR (median I/σ = {m['snr']:.1f})")

    p_cov = 0.0
    if m["q_decades"] < t["min_decades"]:
        p_cov += t["w_cov"] * 0.6
        reasons.append(f"narrow q-range ({m['q_decades']:.2f} decades)")
        flags.append("narrow_q")
    if m["n_points"] < t["min_points"]:
        p_cov += t["w_cov"] * 0.4
        reasons.append(f"sparse ({m['n_points']} points)")
        if "narrow_q" not in flags:
            flags.append("sparse")
    p_cov = min(p_cov, t["w_cov"])

    p_spike = _lerp_penalty(m["spike_frac"], 0.0, t["spike_fail_frac"], t["w_spike"])
    if m["spike_frac"] > 0.02:
        flags.append("spikes")
        reasons.append(f"{m['spike_frac']*100:.0f}% spiky/outlier points")

    # featureless: low dynamic range with weak SNR ⇒ ≈ background noise; OR a
    # hard-flat curve (almost no dynamic range) regardless of SNR.
    featureless = ((m["dyn_range"] < t["dyn_range_min"]) and (m["snr"] < t["snr_good"])) \
        or (m["dyn_range"] < t["dyn_range_hard"])
    p_feat = t["w_featureless"] if featureless else 0.0
    if featureless:
        flags.append("featureless")
        reasons.append(
            f"featureless (dynamic range {m['dyn_range']:.2f} decades) — "
            f"profile resembles background with no structural features")

    # low-q aggregation: steep low-q upturn (disabled for WAXS via threshold)
    p_aggr = 0.0
    slope = m.get("lowq_slope")
    if slope is not None and slope < t["aggr_slope"]:
        p_aggr = t["w_aggr"]
        if p_aggr > 0:
            flags.append("aggregation")
            reasons.append(f"low-q upturn (slope {slope:.1f}) — possible aggregation")

    score = _clamp(
        100.0 - (p_neg + p_snr + p_cov + p_spike + p_feat + p_aggr), 0.0, 100.0)
    return round(score, 1), flags, reasons
