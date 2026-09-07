"""
src/analysis/guidelines.py — tiered analysis-guideline engine
==============================================================
Backs Change 3: the SAXS/WAXS analysis ladder. Three jobs:

  1. load + validate the guideline YAMLs (analysis/{saxs,waxs}_knowledge.yaml)
     and expose their raw text for injection into the assistant's STATIC cached
     prefix (mtime-cached — a mid-beamtime edit is picked up next turn, no
     re-ingest, no ChromaDB write).
  2. ROUTE modality from the DATA, not the user's phrasing: sharp Bragg peaks
     (widths near the instrumental resolution) => WAXS; a smooth monotonic decay
     => SAXS; both present => BOTH; genuinely borderline => AMBIGUOUS (ask).
  3. run the TIER-1 model-free pass as ONE local numpy computation (reusing
     src.analysis.core — no re-implemented fitting math), and GATE higher-tier
     entries by their requires/inapplicable_when/consumes so the ladder ordering
     enforces itself.

Everything here is deterministic and local: it must not cost the model any
tokens beyond interpreting the compact numeric summary.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

# ── Where the YAMLs live ───────────────────────────────────────────────────────
# repo_root/analysis/<modality>_knowledge.yaml. This module is
# repo_root/src/analysis/guidelines.py, so parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

VALID_MODALITIES = ("saxs", "waxs")
_SCHEMA_KEYS = {
    "id", "tier", "modality", "package", "observes", "requires",
    "inapplicable_when", "provides", "consumes", "initial_guesses",
    "my_notes", "confidence", "cost", "invocable",
}
_VALID_CONFIDENCE = {"tested", "documented", "untested"}
_VALID_COST = {"free_local", "cheap", "expensive"}

# State quantities produced OUTSIDE the entry graph (router / raw data), so the
# graph-closure check and the gate know they are legitimately external.
EXTERNAL_STATE_KEYS = {
    "bragg_present", "bragg_dominates", "n_frames_ge_2", "has_conc_series",
    "is_subtracted", "has_peaks",
    # Tier -1 PRE-FLIGHT inputs (Change 4). Computed locally in the tool ctx
    # (units/geometry/subtraction/uncertainties/high-q tail) or affirmed by the
    # operator (damage/low-q/detector-gaps/concentration); the caveat commitments
    # (monodispersity/plotting) default satisfied. Each gates a tier -1 entry.
    "q_convention_settled", "geometry_ok", "subtraction_sane", "damage_checked",
    "low_q_triaged", "high_q_tail_excluded", "detector_gaps_checked",
    "conc_effects_considered", "monodispersity_acknowledged", "plot_conventions_ok",
    "uncertainties_present",
}


# ── YAML load + validate (+ raw text for injection), mtime-cached ──────────────
_yaml_cache: dict = {}
_yaml_lock = threading.Lock()


def guideline_path(modality: str, base_dir: str | Path | None = None) -> Path:
    root = Path(base_dir) if base_dir else (_REPO_ROOT / "analysis")
    return root / f"{modality.lower()}_knowledge.yaml"


def load_guideline(modality: str, base_dir: str | Path | None = None) -> dict:
    """Return {'doc': parsed, 'text': raw_yaml, 'path': str}, validated and
    mtime-cached. Raises ValueError on a schema violation, FileNotFoundError if
    the file is absent (callers that inject degrade gracefully on the latter)."""
    modality = modality.lower()
    if modality not in VALID_MODALITIES:
        raise ValueError(f"unknown modality {modality!r}")
    p = guideline_path(modality, base_dir)
    st = p.stat()                                   # FileNotFoundError propagates
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _yaml_lock:
        hit = _yaml_cache.get(key)
    if hit is not None:
        return hit
    import yaml
    text = p.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    validate_guideline(doc, modality)
    entry = {"doc": doc, "text": text, "path": str(p)}
    with _yaml_lock:
        _yaml_cache[key] = entry
        if len(_yaml_cache) > 8:
            for k in list(_yaml_cache)[:-8]:
                _yaml_cache.pop(k, None)
    return entry


def validate_guideline(doc: dict, modality: str) -> None:
    """Structural validation: schema keys present, enums valid,
    requires/inapplicable_when are lists of {key, hint}, and the
    provides/consumes graph is CLOSED (every consumed / required key has a
    provider, or is a known external key)."""
    if not isinstance(doc, dict) or "entries" not in doc:
        raise ValueError("guideline YAML must be a mapping with an 'entries' list")
    entries = doc["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("'entries' must be a non-empty list")

    ids = set()
    provided: set[str] = set()
    for e in entries:
        missing = _SCHEMA_KEYS - set(e)
        if missing:
            raise ValueError(f"entry {e.get('id','?')} missing keys: {sorted(missing)}")
        if e["id"] in ids:
            raise ValueError(f"duplicate entry id {e['id']!r}")
        ids.add(e["id"])
        if not isinstance(e["tier"], int) or not (-1 <= e["tier"] <= 5):
            raise ValueError(f"{e['id']}: tier must be -1..5")
        if e["confidence"] not in _VALID_CONFIDENCE:
            raise ValueError(f"{e['id']}: bad confidence {e['confidence']!r}")
        if e["cost"] not in _VALID_COST:
            raise ValueError(f"{e['id']}: bad cost {e['cost']!r}")
        if not isinstance(e["invocable"], bool):
            raise ValueError(f"{e['id']}: invocable must be bool")
        for fld in ("requires", "inapplicable_when"):
            for it in e[fld]:
                if not (isinstance(it, dict) and "key" in it and "hint" in it):
                    raise ValueError(f"{e['id']}.{fld} items must be {{key, hint}}")
        provided.update(e["provides"])

    # graph closure
    for e in entries:
        for k in e["consumes"]:
            if k not in provided:
                raise ValueError(f"{e['id']} consumes {k!r} with no provider")
        for fld in ("requires", "inapplicable_when"):
            for it in e[fld]:
                if it["key"] not in provided and it["key"] not in EXTERNAL_STATE_KEYS:
                    raise ValueError(
                        f"{e['id']}.{fld} references {it['key']!r} with no provider"
                        " and not a known external key")


def guideline_text(modality: str, base_dir: str | Path | None = None) -> str | None:
    """Raw YAML text for a modality, or None if unavailable (inject-safe)."""
    try:
        return load_guideline(modality, base_dir)["text"]
    except Exception:
        return None


# ── Modality router — decide from the DATA, not the phrasing ───────────────────
# A WAXS Bragg reflection is SHARP: its relative width (FWHM/q) is near the
# instrumental resolution. A SAXS structure-factor / correlation bump is much
# broader, and pure form-factor scattering has no peaks at all. We detrend the
# smooth decay (as detect_bragg_peaks does) and measure each residual peak's
# relative width; sharp ones vote WAXS.
_SHARP_REL_WIDTH = 0.06     # FWHM/q below this => crystalline-sharp (Bragg)
_MIN_PROMINENCE = 0.08      # in log10 units, matches detect_bragg_peaks


def route_modality(q, I, sigma=None) -> dict:
    """Classify the current curve. Returns:
        {modality: 'saxs'|'waxs'|'both'|'ambiguous',
         bragg_present: bool, n_sharp_peaks: int, n_peaks: int, reason: str}
    """
    q = np.asarray(q, float)
    I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
    q, I = q[m], I[m]
    out = {"modality": "saxs", "bragg_present": False,
           "n_sharp_peaks": 0, "n_peaks": 0, "sharp_peak_q": [], "reason": ""}
    if q.size < 20:
        out["reason"] = "too few points to detect peaks; defaulting to SAXS"
        return out
    order = np.argsort(q)
    q, I = q[order], I[order]

    from scipy.signal import find_peaks
    from scipy.ndimage import uniform_filter1d
    logI = np.log10(I)
    win = max(5, logI.size // 12)
    resid = logI - uniform_filter1d(logI, size=win, mode="nearest")
    idx, props = find_peaks(resid, prominence=_MIN_PROMINENCE,
                            width=1, distance=max(3, q.size // 60))
    out["n_peaks"] = int(idx.size)
    if idx.size == 0:
        out["reason"] = "no peaks above the smooth decay => SAXS"
        return out

    # relative width of each peak: convert scipy's index-space width to q-space.
    dq = np.gradient(q)
    rel_w = []
    for pk, w in zip(idx, props["widths"]):
        fwhm_q = float(w * dq[pk])
        rel_w.append(fwhm_q / max(q[pk], 1e-12))
    sharp = np.asarray(rel_w) < _SHARP_REL_WIDTH
    n_sharp = int(sharp.sum())
    out["n_sharp_peaks"] = n_sharp
    out["bragg_present"] = n_sharp >= 1
    out["sharp_peak_q"] = sorted(float(q[pk]) for pk, sh in zip(idx, sharp) if sh)

    # Is there a substantial smooth-decay region (SAXS content) as well? Look at
    # the low-q half's baseline slope in log-log.
    half = q.size // 2
    ll = np.polyfit(np.log(q[:half]), uniform_filter1d(logI, size=win)[:half], 1)[0]
    smooth_decay = ll < -0.5      # clearly decreasing at low q

    if n_sharp == 0:
        out["modality"] = "saxs"
        out["reason"] = (f"{idx.size} broad bump(s), none sharper than "
                         f"rel-width {_SHARP_REL_WIDTH} => SAXS (structure factor)")
    elif smooth_decay:
        out["modality"] = "both"
        out["reason"] = (f"{n_sharp} sharp Bragg peak(s) AND a smooth low-q decay "
                         "=> SWAXS; treat q-ranges separately")
    elif n_sharp == 1 and idx.size == 1:
        out["modality"] = "ambiguous"
        out["reason"] = ("a single sharp peak and no clear decay — cannot decide "
                         "SAXS vs WAXS from the data alone")
    else:
        out["modality"] = "waxs"
        out["reason"] = f"{n_sharp} sharp Bragg peak(s), no smooth decay => WAXS"
    return out


# ── q-range + tier-1 single local pass ─────────────────────────────────────────
def compute_q_usable(q, I, sigma=None) -> tuple[float, float]:
    """Heuristic usable q-range: drop the low-q beamstop rise and the high-q
    noise floor. Deterministic; documented (not `tested`)."""
    q = np.asarray(q, float)
    I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
    q, I = q[m], I[m]
    if q.size < 5:
        raise ValueError("fewer than 5 valid (q>0, I>0) points")
    order = np.argsort(q)
    q, I = q[order], I[order]

    # low-q: skip a leading beamstop RISE (I increasing with q at the very start).
    lo = 0
    while lo + 1 < q.size // 4 and I[lo + 1] > I[lo]:
        lo += 1
    q_min = float(q[lo])

    # high-q: if sigma present, cut where SNR = I/sigma drops below 2; else keep all.
    q_max = float(q[-1])
    if sigma is not None:
        s = np.asarray(sigma, float)[m][order]
        good = np.isfinite(s) & (s > 0) & (I / s >= 2.0)
        if good.any():
            q_max = float(q[np.max(np.where(good))])
    if q_max <= q_min:
        q_max = float(q[-1])
    return q_min, q_max


def run_tier1(q, I, sigma=None, context: dict | None = None) -> dict:
    """The unconditional tier-1 model-free pass — ONE local computation.

    Reuses src.analysis.core (no re-implemented math). Fitted windows are
    CLAMPED to [q_usable_min, q_usable_max]; if a fit's returned window escapes
    that band (e.g. a Guinier fit reaching into the beamstop shadow) it FAILS
    LOUDLY into `warnings` rather than silently truncating — that is the
    "beautiful, wrong Rg" case the ladder exists to prevent.

    Returns {'state': {quantity: value}, 'summary': {...compact...},
             'warnings': [...]}. `context` seeds flags that cannot be derived
    from a single 1D curve (concentration, azimuthal_isotropy, interparticle_free,
    frames_stable, bragg_present, ...)."""
    from src.analysis.core import (
        guinier_fit, guinier_quality, dimensionless_kratky,
        porod_fit, classical_invariants,
    )
    q = np.asarray(q, float)
    I = np.asarray(I, float)
    sig = np.asarray(sigma, float) if sigma is not None else None
    ctx = dict(context or {})
    warnings: list[str] = []
    state: dict[str, Any] = {}
    summary: dict[str, Any] = {}

    # tier-0 flags carried in from context (manifest / user confirmation / router)
    for k in ("concentration", "azimuthal_isotropy", "interparticle_free",
              "frames_stable", "background_ok", "is_subtracted",
              "has_conc_series", "n_frames_ge_2", "bragg_present"):
        if k in ctx:
            state[k] = ctx[k]

    qu_min, qu_max = compute_q_usable(q, I, sig)
    state["q_usable_min"], state["q_usable_max"] = qu_min, qu_max
    summary["q_usable"] = [round(qu_min, 4), round(qu_max, 4)]

    # SWAXS scoping (follow-up #1): on a curve with Bragg peaks AND a smooth low-q
    # region, the SAXS model-free analyses apply ONLY below the first sharp peak.
    # Restrict the working arrays to [qu_min, qu_max_saxs] so the power law is fit
    # over the SAXS decay — NOT through the peaks — and Kratky/invariant don't pick
    # up a Bragg peak. `bragg_dominates` is True only when no usable SAXS window
    # remains; the gate refuses SAXS entries on THAT, not on the mere presence of
    # peaks (so the exponent is produced on a normal SWAXS curve, not refused).
    bragg = state.get("bragg_present") is True
    first_bragg_q = ctx.get("first_bragg_q")
    qu_max_saxs = qu_max
    bragg_dominates = False
    if bragg and first_bragg_q:
        qu_max_saxs = min(qu_max, float(first_bragg_q) * 0.9)   # stay below the peak
        if qu_max_saxs <= qu_min * 1.2:            # no smooth region below the peak
            bragg_dominates = True
            qu_max_saxs = qu_max                    # nothing to scope to
    state["bragg_dominates"] = bragg_dominates
    if bragg and not bragg_dominates and qu_max_saxs < qu_max:
        summary["saxs_window"] = [round(qu_min, 4), round(qu_max_saxs, 4)]
        summary["note_swaxs"] = (
            f"Bragg peak at q~{round(float(first_bragg_q), 3)}; SAXS model-free "
            "analyses scoped below it — the peak region is WAXS, treated separately.")

    # working arrays for the SAXS pass: usable range, capped below the first peak
    sl = (q >= qu_min) & (q <= qu_max_saxs)
    qa, Ia = q[sl], I[sl]
    siga = sig[sl] if sig is not None else None

    # tier-0 local check: background/subtraction sanity. Over-subtraction drives
    # I(q) negative; a sane subtracted curve is almost entirely positive. Compute
    # it here (the tool doesn't run a separate tier-0 pass) unless the caller
    # already asserted it. This is what unblocks the Guinier `requires`.
    if "background_ok" not in state:
        usable = I[(q >= qu_min) & (q <= qu_max)]
        neg_frac = float(np.mean(usable < 0)) if usable.size else 1.0
        state["background_ok"] = neg_frac < 0.05
        summary["background_ok"] = state["background_ok"]

    def _within(rng, tol=1e-9):
        return rng and (rng[0] >= qu_min - tol) and (rng[1] <= qu_max_saxs + tol)

    # --- Guinier (clamped to the SAXS window) ---
    g = guinier_fit(qa, Ia, siga, q_min=qu_min, q_max=qu_max_saxs, auto_range=True)
    if "error" in g:
        warnings.append(f"Guinier: {g['error']}")
        summary["guinier"] = {"error": g["error"]}
    else:
        if not _within(g.get("q_range")):
            warnings.append(
                f"Guinier window {g.get('q_range')} escaped usable range "
                f"[{round(qu_min,4)}, {round(qu_max_saxs,4)}] — refusing this Rg "
                "(would fit into the beamstop/noise region).")
            summary["guinier"] = {"error": "fit window outside usable q-range"}
        else:
            state["Rg"] = g["Rg"]; state["I0"] = g["I0"]
            state["guinier_qmin"] = g["q_range"][0]
            state["guinier_qmax"] = g["q_range"][1]
            state["guinier_R2"] = g["R2"]
            gq = guinier_quality(g)
            state["guinier_valid"] = (gq["verdict"] == "PASS")
            state["qRg_max"] = g.get("qRg_max")
            # linearity flag as a diagnostic (aggregation / repulsion)
            state["guinier_linearity"] = "linear" if g["R2"] >= 0.99 else "nonlinear"
            summary["guinier"] = {
                "Rg": g["Rg"], "I0": g["I0"], "R2": g["R2"],
                "window": [round(x, 4) for x in g["q_range"]],
                "qRg_max": g.get("qRg_max"), "valid": state["guinier_valid"],
                "linearity": state["guinier_linearity"],
                "qc": gq.get("warnings", []),
            }

    # --- dimensionless Kratky (needs Rg, I0) ---
    if "Rg" in state and "I0" in state:
        k = dimensionless_kratky(qa, Ia, state["Rg"], state["I0"])
        if "error" not in k:
            comp = ("compact-globular"
                    if abs(k["peak_qRg"] - k["ideal_peak_qRg"]) < 0.4
                    and abs(k["peak_y"] - k["ideal_peak_y"]) < 0.4
                    else "extended/flexible/disordered")
            state["kratky_peak_qRg"] = k["peak_qRg"]
            state["kratky_peak_height"] = k["peak_y"]
            state["compactness_class"] = comp
            summary["kratky"] = {"peak_qRg": k["peak_qRg"],
                                 "peak_height": k["peak_y"], "class": comp}

    # --- high-q power law (clamped to below the first Bragg peak) ---
    if bragg_dominates:
        warnings.append("Bragg peaks dominate the high-q region — no SAXS "
                        "power-law window; route the peak region to WAXS.")
    else:
        hi_min = 10 ** (0.5 * (np.log10(qu_min) + np.log10(qu_max_saxs)))
        p = porod_fit(qa, Ia, siga, q_min=hi_min, q_max=qu_max_saxs)
        if "error" in p:
            warnings.append(f"power-law: {p['error']}")
        elif not _within(p.get("q_range")):
            warnings.append("power-law window escaped usable range — skipped.")
        else:
            state["powerlaw_exponent"] = p["n"]
            state["powerlaw_qmin"], state["powerlaw_qmax"] = p["q_range"]
            state["interface_class"] = p["interpretation"]
            summary["power_law"] = {"exponent": p["n"], "class": p["interpretation"],
                                    "window": [round(x, 4) for x in p["q_range"]],
                                    "R2": p["R2"]}

    # --- structure-factor screen (indirect low-q trend hint) ---
    if "structure_factor_present" in ctx:
        state["structure_factor_present"] = ctx["structure_factor_present"]
    else:
        state["structure_factor_present"] = _structure_factor_hint(qa, Ia)
    summary["structure_factor_present"] = state["structure_factor_present"]

    # --- Porod volume (needs Rg, I0) — invariant over the SAXS window only ---
    if "Rg" in state and "I0" in state:
        inv = classical_invariants(qa, Ia, state["Rg"], state["I0"])
        if "error" not in inv:
            state["porod_volume"] = inv["porod_volume"]
            summary["porod_volume"] = inv["porod_volume"]
            # --- MW only when concentration is known (never with a caveat) ---
            if state.get("concentration"):
                state["mw_estimate"] = inv.get("mw_vc_kda")
                summary["mw_estimate_kda"] = inv.get("mw_vc_kda")
            else:
                summary["mw_estimate_kda"] = None  # not offered without concentration

    summary["warnings"] = warnings
    return {"state": state, "summary": summary, "warnings": warnings}


def _structure_factor_hint(q, I) -> bool | None:
    """Very-low-q trend: a downturn (I rising toward q->0 slower / dipping) hints
    at S(q)<1 (repulsion). Indirect — returns True/False when clear, else None
    (unknown). A concentration series is the real confirmation."""
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
    q, I = q[m], I[m]
    if q.size < 12:
        return None
    order = np.argsort(q); q, I = q[order], I[order]
    n = max(5, q.size // 6)
    lo_q, lo_I = np.log(q[:n]), np.log(I[:n])
    slope = float(np.polyfit(lo_q, lo_I, 1)[0])
    # A Guinier plateau flattens (slope ~0); a clear positive low-q slope (I
    # increasing with q at the lowest q) is a downturn => S(q) repulsion.
    if slope > 0.15:
        return True
    if slope < -0.05:
        return False
    return None


# ── Tier -1 PRE-FLIGHT local checks (Change 4) ─────────────────────────────────
def subtraction_sanity(q, I, neg_tol: float = 0.05) -> dict:
    """Judge a background-subtracted curve from its LINEAR values, never the log
    plot — over-subtraction drives I(q) negative, but a log axis silently drops
    those points and only shows a spurious sharp upturn, so the eye (and most
    plotting code) misses it. Checks: (1) fraction of NEGATIVE linear points is
    small; (2) the high-q tail tends toward zero (no residual offset / upturn).

    Returns {sane, neg_fraction_linear, high_q_tends_to_zero, reason}. Deterministic.
    """
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0)
    q, I = q[m], I[m]
    if q.size < 8:
        return {"sane": None, "neg_fraction_linear": None,
                "high_q_tends_to_zero": None, "reason": "too few points to judge"}
    order = np.argsort(q); q, I = q[order], I[order]
    neg_frac = float(np.mean(I < 0))
    # high-q tail: last ~15% of points should sit near/below the curve's own scale,
    # i.e. small relative to the low-q signal — not offset high (under-sub) or
    # swinging negative (over-sub).
    n_tail = max(3, q.size // 7)
    tail = I[-n_tail:]
    lowq_scale = float(np.nanmedian(np.abs(I[: max(3, q.size // 5)]))) or 1.0
    tail_med = float(np.nanmedian(tail))
    tends_zero = bool(abs(tail_med) <= 0.15 * lowq_scale)
    sane = bool(neg_frac <= neg_tol and tends_zero)
    reason = []
    if neg_frac > neg_tol:
        reason.append(f"{neg_frac*100:.0f}% of LINEAR points are negative "
                      "(over-subtraction — hidden on a log axis)")
    if not tends_zero:
        reason.append("high-q tail does not tend to zero "
                      "(mis-scaled subtraction / residual background)")
    return {"sane": sane, "neg_fraction_linear": neg_frac,
            "high_q_tends_to_zero": tends_zero,
            "reason": "; ".join(reason) or "linear values sane, tail → 0"}


def classify_tail_peak(q, I, sigma=None, tail_frac: float = 0.35,
                       resolution_rel: float = _SHARP_REL_WIDTH,
                       min_points_above_noise: int = 3) -> dict:
    """Is an apparent 'peak' in the HIGH-q tail of a SAXS curve real, or noise?
    Default to NOISE. A feature is only a candidate if MULTIPLE points sit above
    the local noise level AND its relative width is consistent with the
    instrumental resolution. One or two high points are noise until proven
    otherwise — never report a Bragg peak from the SAXS tail on that basis.

    Returns {classification: 'noise'|'candidate_peak', n_points_above_noise,
             rel_width, reason}.
    """
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0)
    q, I = q[m], I[m]
    if q.size < 8:
        return {"classification": "noise", "n_points_above_noise": 0,
                "rel_width": None, "reason": "too few points"}
    order = np.argsort(q); q, I = q[order], I[order]
    sig = None
    if sigma is not None:
        sig = np.asarray(sigma, float)[m][order]
    n_tail = max(5, int(q.size * tail_frac))
    qt, It = q[-n_tail:], I[-n_tail:]
    # local noise level in the tail: sigma if available, else robust MAD of It.
    if sig is not None and np.all(np.isfinite(sig[-n_tail:])) and np.any(sig[-n_tail:] > 0):
        noise = sig[-n_tail:]
    else:
        med = float(np.nanmedian(It))
        mad = float(np.nanmedian(np.abs(It - med))) or (np.nanstd(It) or 1.0)
        noise = np.full_like(It, 1.4826 * mad)
    baseline = float(np.nanmedian(It))
    above = It - baseline > 2.0 * noise
    n_above = int(np.sum(above))
    if n_above < min_points_above_noise:
        return {"classification": "noise", "n_points_above_noise": n_above,
                "rel_width": None,
                "reason": (f"only {n_above} point(s) above 2σ local noise "
                           f"(need ≥{min_points_above_noise}) — noise until proven")}
    # contiguous run around the max defines a width; check it against resolution.
    pk = int(np.argmax(It))
    lo = pk
    while lo - 1 >= 0 and (It[lo - 1] - baseline) > noise[lo - 1]:
        lo -= 1
    hi = pk
    while hi + 1 < It.size and (It[hi + 1] - baseline) > noise[hi + 1]:
        hi += 1
    rel_w = float((qt[hi] - qt[lo]) / max(qt[pk], 1e-12))
    if rel_w < resolution_rel * 0.3:
        return {"classification": "noise", "n_points_above_noise": n_above,
                "rel_width": rel_w,
                "reason": (f"feature width rel {rel_w:.3f} is far below the "
                           f"instrumental resolution (~{resolution_rel}) — a spike, "
                           "not a resolved peak")}
    return {"classification": "candidate_peak", "n_points_above_noise": n_above,
            "rel_width": rel_w,
            "reason": (f"{n_above} points above noise, width rel {rel_w:.3f} — a "
                       "candidate; still verify a physically sensible d-spacing")}


# ── The applicability + dependency gate ────────────────────────────────────────
def gate_entries(entries: list[dict], state: dict) -> dict:
    """Split entries into proposable vs refused given the produced `state`.

    - consumes:          every key must be present AND non-null, else "needs X first"
    - requires:          each {key} must be state[key] is True (unknown/None/False
                         => REFUSE; "measured, not assumed"), quoting `hint`
    - inapplicable_when: refuse iff state[key] is True (unknown does NOT trigger)

    Refusals name the failed precondition AND what would satisfy it (the hint).
    """
    proposable: list[str] = []
    refused: list[dict] = []
    for e in entries:
        reasons: list[str] = []
        for kk in e.get("consumes", []):
            if state.get(kk) is None:
                reasons.append(f"needs `{kk}` first (not yet produced this session)")
        for it in e.get("requires", []):
            if state.get(it["key"]) is not True:
                reasons.append(f"requires `{it['key']}` — {it['hint']}")
        for it in e.get("inapplicable_when", []):
            if state.get(it["key"]) is True:
                reasons.append(f"inapplicable (`{it['key']}` holds) — {it['hint']}")
        if reasons:
            refused.append({"id": e["id"], "tier": e["tier"], "reasons": reasons})
        else:
            proposable.append(e["id"])
    return {"proposable": proposable, "refused": refused}
