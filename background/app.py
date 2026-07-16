"""
background/app.py — SWAXS Background Subtraction App (port 5003)
=================================================================
Two modes:
  • Keyword mode  : subtract an averaged background from averaged sample curves
  • Scan-matched  : pair individual sample scans with individual background scans
                    by matching scan_idx

Run:  uv run background/app.py
Open: http://localhost:5003
"""

from __future__ import annotations

import collections
import datetime
import gc
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template, request, Response

# ── sys.path ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.read_dat_metadata import read_dat_data_metadata   # noqa: E402
from src.manifest import (                                        # noqa: E402
    update_manifest,
    add_file_entry, add_background_entry,
    manifest_path_for, make_provenance,
)

# ── Event bus (graceful degradation) ─────────────────────────────────────────
try:
    from src.events import EventBusClient as _EventBusClient
    _bus = _EventBusClient("background").connect(retry=True)
except Exception:
    _bus = None

app = Flask(__name__)

# Project root (set by hub or via /api/set_project)
_project_root: str = ""

# ── Automated-subtraction monitor state ───────────────────────────────────────
# A daemon thread polls the Averaged folder(s); each new (unsubtracted) sample
# average is paired with its nearest-index background and subtracted.
_sub_monitoring: bool = False
_sub_monitor_thread: threading.Thread | None = None
_sub_lock = threading.Lock()
_sub_log: collections.deque = collections.deque(maxlen=500)   # (seq, line)
_sub_seq: int = 0
_sub_done: set = set()        # resolved sample paths already subtracted
_sub_status: dict = {"monitoring": False, "subtracted": 0, "flagged": 0,
                     "last": None, "interval": None}


def _sub_emit(msg: str, tag: str = "info") -> None:
    """Append a line to the auto-subtraction log (consumed by the SSE stream)."""
    global _sub_seq
    line = {"ts": datetime.datetime.now().strftime("%H:%M:%S"), "msg": msg, "tag": tag}
    with _sub_lock:
        _sub_seq += 1
        _sub_log.append((_sub_seq, line))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_dat(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Return (q, I, sigma) from a .dat file. Keeps every finite point with q>0
    (does NOT drop non-positive I), so the sample's full q-coverage is retained;
    log-axis display and the background interpolation handle non-positive points
    where needed. (Audit C2)
    """
    _, q, I, sigma, _ = read_dat_data_metadata(path)
    q = np.asarray(q, float); I = np.asarray(I, float); sigma = np.asarray(sigma, float)
    mask = np.isfinite(q) & np.isfinite(I) & (q > 0)
    return q[mask], I[mask], sigma[mask]


def _interpolate_onto(q_target: np.ndarray,
                      q_src: np.ndarray,
                      I_src: np.ndarray,
                      sig_src: np.ndarray
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Log-space interpolation of (I_src, sig_src) onto q_target grid.
    Only finite, positive source points are used (so non-positive background
    points can't corrupt the log interpolation). (Audit C2)
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


def _subtract(q_sam: np.ndarray, I_sam: np.ndarray, sig_sam: np.ndarray,
              q_bkg: np.ndarray, I_bkg: np.ndarray, sig_bkg: np.ndarray,
              scale: float
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Subtract background with scale factor, propagating errors.
    Interpolates bkg onto sample q-grid; returns (q, I_sub, sigma_sub).
    """
    _, I_b, sig_b = _interpolate_onto(q_sam, q_bkg, I_bkg, sig_bkg)
    I_sub   = I_sam - scale * I_b
    sig_sub = np.sqrt(sig_sam**2 + (scale * sig_b)**2)
    return q_sam, I_sub, sig_sub


def _write_dat(out_path: Path, q: np.ndarray, I: np.ndarray, sigma: np.ndarray,
               header_extra: list[str] | None = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# SAXS/WAXS background-subtracted data",
        "# Columns: q_nm-1  I  sigma",
    ]
    if header_extra:
        lines.extend(header_extra)
    lines.append("# q_nm-1  I  sigma")
    for qi, Ii, si in zip(q, I, sigma):
        lines.append(f"{qi:.8e}  {Ii:.8e}  {si:.8e}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _glob_dats(folder: Path, keyword: str | None = None) -> list[Path]:
    dats = sorted(folder.glob("*.dat"))
    if keyword:
        dats = [f for f in dats if keyword in f.name]
    return dats


def _scan_idx(path: Path) -> int:
    """Extract trailing 4-digit scan index from filename, or 0."""
    stem = path.stem
    # e.g. sample_0012 → 12, sample_avg → 0
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return 0


def _load_meta(path: Path) -> dict:
    """Return the metadata footer dict for a .dat file ({} on failure)."""
    try:
        _, _q, _I, _s, meta = read_dat_data_metadata(path)
        return meta or {}
    except Exception:
        return {}


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
    win = (q_sam >= qlo) & (q_sam <= qhi) & (I_b > 0)
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


_BKG_TOKENS = ("buffer", "bkg", "background", "blank", "empty", "solvent", "water", "bg")

def _name_tokens(name: str) -> set[str]:
    """Lower-case alphanumeric tokens from a filename stem (for matching)."""
    import re
    return set(t for t in re.split(r"[^a-zA-Z0-9]+", Path(name).stem.lower()) if t)


def _suggest_background(sample_name: str, bkg_files: list[Path], hint: str = "") -> dict | None:
    """
    Suggest the best background for a sample by filename/keyword token overlap,
    preferring obvious background tokens (buffer/blank/empty/…). A user-supplied
    ``hint`` (a filename token they expect in the background, e.g. "buffer" or a
    shared run id) strongly boosts candidates that contain it. Returns
    {name, path, score, reason} or None.
    """
    s_tokens = _name_tokens(sample_name) - set(_BKG_TOKENS)
    hint_t   = _name_tokens(hint) if hint else set()
    best, best_score, best_reason = None, -1.0, ""
    for bf in bkg_files:
        b_tokens = _name_tokens(bf.name)
        shared   = s_tokens & b_tokens
        score    = float(len(shared))
        reason   = f"shares {sorted(shared)}" if shared else "no shared tokens"
        if hint_t and (hint_t & b_tokens):
            score += 3.0
            reason += f" + matches hint {sorted(hint_t & b_tokens)}"
        if b_tokens & set(_BKG_TOKENS):
            score += 1.5
            reason += " + background keyword"
        if score > best_score:
            best, best_score, best_reason = bf, score, reason
    if best is None:
        return None
    return {"name": best.name, "path": str(best), "score": round(best_score, 1), "reason": best_reason}


def _is_background(name: str) -> bool:
    """True if a filename's tokens include a background keyword
    (buffer/blank/empty/solvent/water/bkg/bg)."""
    return bool(_name_tokens(name) & set(_BKG_TOKENS))


_BATCH_RE = __import__("re").compile(r"_batch(\d+)", __import__("re").IGNORECASE)

def _seq_index(path: Path) -> int:
    """
    Sequence index used to pair samples with backgrounds by acquisition order.

    Prefers the rolling-batch number written by the viewer's auto-averager
    (``..._batch007_30files_Average.dat`` → 7); otherwise falls back to a
    trailing ``_NNNN`` scan index; otherwise 0.
    """
    m = _BATCH_RE.search(path.name)
    if m:
        return int(m.group(1))
    return _scan_idx(path)


def _sample_base_tokens(name: str) -> set[str]:
    """Sample identity tokens (drops background + averaging-boilerplate tokens)."""
    drop = set(_BKG_TOKENS) | {"average", "avg", "files", "batch", "sub", "subtracted"}
    return {t for t in _name_tokens(name) if t not in drop and not t.isdigit()}


_ROLE_TAGS = ("_sample", "_bkg", "_background", "_buffer", "_blank",
              "_bg", "_empty", "_solvent", "_water")


def _recipe_key(name: str) -> str:
    """The recipe/condition id a file belongs to — the filename up to its
    sample/background role tag (e.g. 'auto_42_sample_..' & 'auto_42_bkg_..' both
    → 'auto_42'). Empty if no role tag is present."""
    low = name.lower()
    hits = [low.find(t) for t in _ROLE_TAGS if low.find(t) > 0]
    return name[:min(hits)] if hits else ""


def _pick_background(sample: Path, bkgs: list[Path]) -> Path | None:
    """
    Choose the background for *sample*. Prefer a background from the SAME recipe
    (shared recipe_id in the filename) — deterministic for autonomous campaigns —
    and fall back to NEAREST sequence index (tie-broken by sample-token overlap)
    when there's no recipe_id match.
    """
    if not bkgs:
        return None
    s_idx = _seq_index(sample)

    skey = _recipe_key(sample.name)
    if skey:
        keyed = [b for b in bkgs if _recipe_key(b.name) == skey]
        if keyed:
            return sorted(keyed, key=lambda b: abs(_seq_index(b) - s_idx))[0]

    s_tok = _sample_base_tokens(sample.name)

    def _key(b: Path):
        overlap = len(s_tok & _sample_base_tokens(b.name))
        return (abs(_seq_index(b) - s_idx), -overlap)

    return sorted(bkgs, key=_key)[0]


def _auto_adjust_scale(q_s, I_s, sig_s, q_b, I_b, sig_b, frac: float = 0.25) -> dict:
    """
    Determine a background scale that drives the HIGH-q residual to zero,
    bounded to roughly ±50% of the weighted-least-squares auto-scale.

    1. ``s0`` = high-q weighted-LS auto-scale (the manual modes' estimator).
    2. ``s_zero`` = mean(I_sample) / mean(I_bkg) over the high-q window — the
       scale at which the *mean* high-q residual is exactly zero.
    3. Final scale = ``s_zero`` clamped to ``[0.5·s0, 1.5·s0]`` and the global
       sane range ``[0.1, 5.0]``.

    Returns {scale, ls_scale, zero_scale, clamped, q_min, q_max, n_points}.
    """
    base = _auto_scale(q_s, I_s, sig_s, q_b, I_b, sig_b, frac=frac)
    s0   = float(base["scale"])

    _, I_bi, _ = _interpolate_onto(q_s, q_b, I_b, sig_b)
    qlo = q_s.min() + (1.0 - frac) * (q_s.max() - q_s.min())
    qhi = q_s.max()
    win = (q_s >= qlo) & (q_s <= qhi) & (I_bi > 0)

    if win.sum() >= 3:
        mean_b = float(np.mean(I_bi[win]))
        s_zero = (float(np.mean(I_s[win])) / mean_b) if mean_b > 0 else s0
    else:
        s_zero = s0

    lo, hi  = 0.5 * s0, 1.5 * s0
    s_final = float(min(max(s_zero, lo), hi))
    s_final = float(min(max(s_final, 0.1), 5.0))

    return {"scale": s_final, "ls_scale": round(s0, 4),
            "zero_scale": round(float(s_zero), 4),
            "clamped": not (lo <= s_zero <= hi),
            "q_min": float(qlo), "q_max": float(qhi),
            "n_points": int(win.sum())}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "background"})


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    body = request.get_json(force=True)
    path = body.get("path", "").strip()
    if path and Path(path).is_dir():
        _project_root = path
    return jsonify({"ok": True})


@app.route("/api/project")
def api_project():
    """Current project root (set by the hub) — used by the UI to auto-fill paths."""
    return jsonify({"project_root": _project_root})


@app.route("/api/browse")
def api_browse():
    raw = request.args.get("path", "").strip()
    p   = Path(raw) if raw else Path.home()
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.is_dir():
        p = Path.home()
    try:
        dirs = sorted(d.name for d in p.iterdir()
                      if d.is_dir() and not d.name.startswith("."))
        files = sorted(d.name for d in p.iterdir()
                       if d.is_file() and not d.name.startswith(".")
                       and d.suffix.lower() == ".dat")
    except PermissionError:
        dirs, files = [], []
    return jsonify({"current": str(p),
                    "parent": str(p.parent) if p != p.parent else None,
                    "dirs": dirs, "files": files})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """List available .dat files in a folder, grouped by keyword."""
    body   = request.get_json(force=True)
    folder = Path(body.get("folder", "").strip())
    if not folder.exists():
        return jsonify({"error": f"Folder not found: {folder}"}), 400
    dats = _glob_dats(folder)
    # Collect keywords (first part of stem before trailing _NNNN)
    kw_set: set[str] = set()
    files_info = []
    for f in dats:
        stem   = f.stem
        idx    = _scan_idx(f)
        kw     = stem.rsplit("_", 1)[0] if stem.rsplit("_", 1)[-1].isdigit() else stem
        kw_set.add(kw)
        files_info.append({"name": f.name, "stem": stem, "scan_idx": idx, "keyword": kw})
    return jsonify({"files": files_info, "keywords": sorted(kw_set), "folder": str(folder)})


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Load sample + background curves for preview (no subtraction yet)."""
    body    = request.get_json(force=True)
    sam_f   = Path(body.get("sample_file",  "").strip())
    bkg_f   = Path(body.get("bkg_file",    "").strip())
    scale   = float(body.get("scale", 1.0))
    method  = body.get("method", "manual")
    qmin    = body.get("qmin"); qmax = body.get("qmax")

    if not sam_f.exists():
        return jsonify({"error": f"Sample not found: {sam_f}"}), 400
    if not bkg_f.exists():
        return jsonify({"error": f"Background not found: {bkg_f}"}), 400

    window = None
    try:
        q_s, I_s, sig_s = _load_dat(sam_f)
        q_b, I_b, sig_b = _load_dat(bkg_f)
        if method == "auto_highq":
            window = _auto_scale(q_s, I_s, sig_s, q_b, I_b, sig_b, qmin=qmin, qmax=qmax)
            scale  = window["scale"]
        q_r, I_r, sig_r = _subtract(q_s, I_s, sig_s, q_b, I_b, sig_b, scale)
        qc = _qc_metrics(q_s, I_r, I_s)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    def _ds(q, I, sig, n=500):
        """Downsample to n points."""
        if len(q) > n:
            idx = np.round(np.linspace(0, len(q)-1, n)).astype(int)
            q, I, sig = q[idx], I[idx], sig[idx]
        return q.tolist(), I.tolist(), sig.tolist()

    qs, Is, ss   = _ds(q_s, I_s, sig_s)
    qb, Ib, sb   = _ds(q_b, I_b, sig_b)
    qr, Ir, sr   = _ds(q_r, I_r, sig_r)

    # Ratio I_sample / (scale·I_bkg) on the sample grid — ≈1 at high q when the
    # background is correctly scaled (audit U4).
    _, I_b_on_s, _ = _interpolate_onto(q_s, q_b, I_b, sig_b)
    denom = scale * I_b_on_s
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(denom > 0, I_s / denom, np.nan)
    qra, rra, _ = _ds(q_s, ratio, ratio)
    rra = [None if (v is None or not np.isfinite(v)) else round(float(v), 4) for v in rra]

    return jsonify({
        "ratio":      {"q": qra, "ratio": rra},
        "sample":     {"q": qs, "I": Is, "sigma": ss},
        "background": {"q": qb, "I": Ib, "sigma": sb},
        "result":     {"q": qr, "I": Ir, "sigma": sr},
        "scale_used": round(float(scale), 4),
        "method":     method,
        "window":     window,
        "qc":         qc,
    })


@app.route("/api/auto_scale", methods=["POST"])
def api_auto_scale():
    """Return the high-q least-squares scale for one sample/background pair."""
    body  = request.get_json(force=True)
    sam_f = Path(body.get("sample_file", "").strip())
    bkg_f = Path(body.get("bkg_file",   "").strip())
    if not sam_f.exists() or not bkg_f.exists():
        return jsonify({"error": "Sample or background file not found"}), 400
    try:
        q_s, I_s, sig_s = _load_dat(sam_f)
        q_b, I_b, sig_b = _load_dat(bkg_f)
        return jsonify(_auto_scale(q_s, I_s, sig_s, q_b, I_b, sig_b,
                                   qmin=body.get("qmin"), qmax=body.get("qmax")))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pair_qc", methods=["POST"])
def api_pair_qc():
    """Lightweight scale + QC for one sample/background pair (no curves)."""
    body   = request.get_json(force=True)
    sam_f  = Path(body.get("sample_file", "").strip())
    bkg_f  = Path(body.get("bkg_file",   "").strip())
    method = body.get("method", "manual")
    scale  = float(body.get("scale", 1.0))
    if not sam_f.exists() or not bkg_f.exists():
        return jsonify({"error": "Sample or background file not found"}), 400
    try:
        q_s, I_s, sig_s = _load_dat(sam_f)
        q_b, I_b, sig_b = _load_dat(bkg_f)
        if method == "auto_highq":
            scale = _auto_scale(q_s, I_s, sig_s, q_b, I_b, sig_b)["scale"]
        _q, I_r, _s = _subtract(q_s, I_s, sig_s, q_b, I_b, sig_b, scale)
        return jsonify({"scale": round(float(scale), 4), "qc": _qc_metrics(q_s, I_r, I_s)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    """Return the metadata footer for a list of .dat files (Average Metadata tab)."""
    body  = request.get_json(force=True)
    files = [Path(p.strip()) for p in body.get("files", []) if str(p).strip()]
    rows, keys = [], []
    for f in files:
        meta = _load_meta(f) if f.exists() else {}
        rows.append({"name": f.name, "path": str(f), "metadata": meta})
        for k in meta:
            if k not in keys:
                keys.append(k)
    return jsonify({"rows": rows, "keys": keys})


def _token_number(name: str, token: str):
    """
    Return the integer immediately following `token` in a filename (e.g.
    token='ctr' on 'run_ctr0_scan1' → 0), or None. Case-insensitive; any
    non-digit separators between token and number are allowed.
    """
    import re
    if not token:
        return None
    m = re.search(re.escape(token) + r"[^0-9]*(\d+)", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_token(token: str):
    """
    Split a token like ``Nylon6*ctr`` into ``(keyword, counter_marker)``.

    The ``*`` separates the SERIES KEYWORD — the fixed part identifying a
    sample or background series (e.g. ``Nylon6`` / ``Empty``) — from the
    COUNTER MARKER (e.g. ``ctr``) whose trailing digits are the counter value.
    Files are first grouped by the keyword, then paired by that number, so a
    ``ctr`` count that restarts in each series can no longer cross-match between
    series. With no ``*`` the whole token is the counter marker and no keyword
    grouping is applied (legacy behaviour).
    """
    token = (token or "").strip()
    if "*" in token:
        kw, _, ctr = token.partition("*")
        return kw.strip(), ctr.strip()
    return "", token


@app.route("/api/match_token", methods=["POST"])
def api_match_token():
    """
    Group by keyword, then pair by the NUMBER after a common counter token.
    A sample (name contains ``sample_keyword``) pairs with a background (name
    contains ``bkg_keyword``) when the number after ``counter`` is equal in both.
    Pairs are returned sorted by that number for easy visual checking.
    Body: {sample_folder, bkg_folder, sample_keyword, bkg_keyword, counter}.
    (Legacy: sample_token/bkg_token in 'keyword*counter' form is still accepted.)
    """
    body       = request.get_json(force=True)
    sam_folder = Path(body.get("sample_folder", "").strip())
    bkg_folder = Path(body.get("bkg_folder",   "").strip())
    sam_kw     = body.get("sample_keyword", "").strip()
    bkg_kw     = body.get("bkg_keyword",   "").strip()
    counter    = body.get("counter",       "").strip()
    # Backward-compat: derive the three fields from old 'keyword*counter' tokens.
    if not counter:
        sk, sc = _parse_token(body.get("sample_token", ""))
        bk, bc = _parse_token(body.get("bkg_token", ""))
        sam_kw  = sam_kw or sk
        bkg_kw  = bkg_kw or bk
        counter = sc or bc
    if not sam_folder.exists():
        return jsonify({"error": f"Sample folder not found: {sam_folder}"}), 400
    if not bkg_folder.exists():
        return jsonify({"error": f"Background folder not found: {bkg_folder}"}), 400
    if not counter:
        return jsonify({"error": "Enter the match token whose following number is "
                                 "paired (e.g. 'ctr')."}), 400

    def _has_kw(name: str, kw: str) -> bool:
        return (kw.lower() in name.lower()) if kw else True

    # 1) Group by keyword: keep only files whose name contains the keyword.
    sam_files = [f for f in _glob_dats(sam_folder) if _has_kw(f.name, sam_kw)]
    bkg_files = [f for f in _glob_dats(bkg_folder) if _has_kw(f.name, bkg_kw)]
    if not bkg_files:
        return jsonify({"error": (f"No background .dat files contain keyword '{bkg_kw}'."
                                  if bkg_kw else "No background .dat files found")}), 400

    # 2) Within the keyword-scoped backgrounds, map counter number -> file.
    bkg_by_num: dict[int, Path] = {}
    for bf in bkg_files:
        n = _token_number(bf.name, counter)
        if n is not None and n not in bkg_by_num:
            bkg_by_num[n] = bf

    # 3) Pair each keyword-scoped sample to the background with the same number.
    pairs = []
    for sf in sam_files:
        n  = _token_number(sf.name, counter)
        bf = bkg_by_num.get(n) if n is not None else None
        if bf:
            reason = f"{counter}{n} ↔ {counter}{n}"
            sug = {"name": bf.name, "path": str(bf), "score": 1.0, "reason": reason}
        else:
            sug = None
        pairs.append({"sample": sf.name, "sample_path": str(sf),
                      "suggested": sug, "num": n})

    # 4) Sort the list by the counter number so sample/background line up in
    #    order (unmatched / number-less samples go to the bottom).
    pairs.sort(key=lambda p: (p["num"] is None, p["num"] if p["num"] is not None else 0,
                              p["sample"]))

    return jsonify({"pairs": pairs, "n_samples": len(sam_files),
                    "n_backgrounds": len(bkg_files),
                    "sample_keyword": sam_kw, "bkg_keyword": bkg_kw, "counter": counter,
                    "bkg_folder": str(bkg_folder),
                    "backgrounds": [f.name for f in bkg_files]})


@app.route("/api/suggest", methods=["POST"])
def api_suggest():
    """
    Auto-suggest a background for each sample file by filename/keyword tokens.
    Body: {sample_folder, bkg_folder, sample_keyword?, bkg_keyword?}
    """
    body       = request.get_json(force=True)
    sam_folder = Path(body.get("sample_folder", "").strip())
    bkg_folder = Path(body.get("bkg_folder",   "").strip())
    if not sam_folder.exists():
        return jsonify({"error": f"Sample folder not found: {sam_folder}"}), 400
    if not bkg_folder.exists():
        return jsonify({"error": f"Background folder not found: {bkg_folder}"}), 400
    hint      = body.get("match_token", "").strip()
    sam_files = _glob_dats(sam_folder, body.get("sample_keyword", "").strip() or None)
    bkg_files = _glob_dats(bkg_folder, body.get("bkg_keyword", "").strip() or None)
    if not bkg_files:
        return jsonify({"error": "No background .dat files found"}), 400
    pairs = []
    for sf in sam_files:
        sug = _suggest_background(sf.name, bkg_files, hint=hint)
        pairs.append({"sample": sf.name, "sample_path": str(sf),
                      "suggested": sug})
    return jsonify({"pairs": pairs, "n_samples": len(sam_files),
                    "n_backgrounds": len(bkg_files), "hint": hint,
                    "bkg_folder": str(bkg_folder),
                    "backgrounds": [f.name for f in bkg_files]})


@app.route("/api/subtract/keyword", methods=["POST"])
def api_subtract_keyword():
    """
    Keyword mode: subtract one background file from one or more sample files.
    Saves output to output_folder / Subtracted /.
    """
    body       = request.get_json(force=True)
    sam_folder = Path(body.get("sample_folder", "").strip())
    bkg_file   = Path(body.get("bkg_file",   "").strip())
    sam_kw     = body.get("sample_keyword", "").strip() or None
    scale      = float(body.get("scale", 1.0))
    out_folder = body.get("output_folder", "").strip()
    out_folder = Path(out_folder) if out_folder else sam_folder / "Subtracted"

    if not sam_folder.exists():
        return jsonify({"error": f"Sample folder not found: {sam_folder}"}), 400
    if not bkg_file.exists():
        return jsonify({"error": f"Background file not found: {bkg_file}"}), 400

    q_b, I_b, sig_b = _load_dat(bkg_file)
    sam_files = _glob_dats(sam_folder, sam_kw)
    if not sam_files:
        return jsonify({"error": "No sample .dat files found"}), 400

    saved   = []
    errors  = []
    results = []

    # Manifest
    pending = []   # manifest mutations, applied atomically under lock at the end

    for f in sam_files:
        try:
            q_s, I_s, sig_s = _load_dat(f)
            q_r, I_r, sig_r = _subtract(q_s, I_s, sig_s, q_b, I_b, sig_b, scale)
            out_name = f.stem + "_sub.dat"
            out_path = out_folder / out_name
            _write_dat(out_path, q_r, I_r, sig_r, [
                f"# Sample     : {f}",
                f"# Background : {bkg_file}",
                f"# Scale      : {scale}",
                f"# Mode       : keyword",
            ])
            saved.append(str(out_path))

            if _project_root:
                prov = make_provenance(
                    "background",
                    input_files = [f, bkg_file],
                    config      = {"scale": scale, "mode": "keyword"},
                )
                def _add(m, out_path=out_path, f=f, prov=prov):
                    add_file_entry(m, path=out_path, stage="subtracted",
                                   detector="saxs", keyword=f.stem,
                                   scan_idx=_scan_idx(f), provenance=prov)
                    add_background_entry(m, output_path=out_path,
                                         sample_path=f, bkg_path=bkg_file,
                                         scale=scale, mode="keyword",
                                         provenance=prov)
                pending.append(_add)

            # ── Event bus ──────────────────────────────────────────────────
            if _bus is not None:
                try:
                    _bus.emit_file_subtracted(
                        str(out_path),
                        keyword = f.stem,
                        scale   = scale,
                        mode    = "keyword",
                    )
                except Exception:
                    pass

            # Return downsampled curve for display
            n = 300
            if len(q_r) > n:
                idx = np.round(np.linspace(0, len(q_r)-1, n)).astype(int)
                q_r, I_r, sig_r = q_r[idx], I_r[idx], sig_r[idx]
            results.append({
                "name": out_name, "path": str(out_path),
                "q": q_r.tolist(), "I": I_r.tolist(), "sigma": sig_r.tolist(),
            })
        except Exception as exc:
            errors.append(f"{f.name}: {exc}")

    if _project_root and pending:
        update_manifest(_project_root, lambda m: [fn(m) for fn in pending])

    return jsonify({"saved": saved, "errors": errors, "results": results})


@app.route("/api/subtract/scan_matched", methods=["POST"])
def api_subtract_scan_matched():
    """
    Scan-matched mode: pair sample and background files by scan_idx.
    """
    body       = request.get_json(force=True)
    sam_folder = Path(body.get("sample_folder", "").strip())
    bkg_folder = Path(body.get("bkg_folder",   "").strip())
    sam_kw     = body.get("sample_keyword",  "").strip() or None
    bkg_kw     = body.get("bkg_keyword",     "").strip() or None
    scale      = float(body.get("scale", 1.0))
    method     = body.get("method", "manual")
    detector   = (body.get("detector", "saxs") or "saxs").lower()
    qmin       = body.get("qmin"); qmax = body.get("qmax")
    out_folder = body.get("output_folder", "").strip()
    out_folder = Path(out_folder) if out_folder else sam_folder / "Subtracted"

    if not sam_folder.exists():
        return jsonify({"error": f"Sample folder not found: {sam_folder}"}), 400
    if not bkg_folder.exists():
        return jsonify({"error": f"Background folder not found: {bkg_folder}"}), 400

    sam_list = _glob_dats(sam_folder, sam_kw)
    bkg_list = _glob_dats(bkg_folder, bkg_kw)
    if not sam_list or not bkg_list:
        return jsonify({"error": "No .dat files found in sample and/or background folder"}), 400

    sam_idx = {_scan_idx(f): f for f in sam_list}
    bkg_idx = {_scan_idx(f): f for f in bkg_list}

    # Robust pairing (audit C1): if filenames lack UNIQUE scan indices (they all
    # collapse to the same key, e.g. 0), fall back to pairing by sorted order so
    # we don't silently process just one pair.
    warning = None
    if len(sam_idx) < len(sam_list) or len(bkg_idx) < len(bkg_list):
        warning = ("Filenames lack unique scan indices — paired by sorted order "
                   "instead of scan index.")
        n = min(len(sam_list), len(bkg_list))
        pair_list = [(s, b, _scan_idx(s)) for s, b in
                     zip(sorted(sam_list), sorted(bkg_list))][:n]
    else:
        common = sorted(set(sam_idx) & set(bkg_idx))
        if not common:
            return jsonify({"error": "No matching scan indices found between sample and background folders"}), 400
        pair_list = [(sam_idx[i], bkg_idx[i], i) for i in common]

    saved   = []
    errors  = []
    results = []

    pending = []   # manifest mutations, applied atomically under lock at the end

    for f_s, f_b, idx in pair_list:
        try:
            q_s, I_s, sig_s = _load_dat(f_s)
            q_b, I_b, sig_b = _load_dat(f_b)
            s_use = scale
            if method == "auto_highq":
                s_use = _auto_scale(q_s, I_s, sig_s, q_b, I_b, sig_b, qmin=qmin, qmax=qmax)["scale"]
            q_r, I_r, sig_r = _subtract(q_s, I_s, sig_s, q_b, I_b, sig_b, s_use)
            out_name = f_s.stem + "_sub.dat"
            out_path = out_folder / out_name
            _write_dat(out_path, q_r, I_r, sig_r, [
                f"# Sample     : {f_s}",
                f"# Background : {f_b}",
                f"# Scale      : {s_use:.6g}",
                f"# Method     : {method}",
                f"# Detector   : {detector}",
                f"# Mode       : scan_matched",
                f"# scan_idx   : {idx}",
            ])
            saved.append(str(out_path))

            if _project_root:
                prov = make_provenance(
                    "background",
                    input_files = [f_s, f_b],
                    config      = {"scale": s_use, "mode": "scan_matched",
                                   "scan_idx": idx, "method": method},
                )
                def _add(m, out_path=out_path, f_s=f_s, f_b=f_b, idx=idx, prov=prov, s_use=s_use):
                    add_file_entry(m, path=out_path, stage="subtracted",
                                   detector=detector, keyword=f_s.stem,
                                   scan_idx=idx, provenance=prov)
                    add_background_entry(m, output_path=out_path,
                                         sample_path=f_s, bkg_path=f_b,
                                         scale=s_use, mode="scan_matched",
                                         scale_method=("auto" if method=="auto_highq" else "manual"),
                                         provenance=prov)
                pending.append(_add)

            # ── Event bus ──────────────────────────────────────────────────
            if _bus is not None:
                try:
                    _bus.emit_file_subtracted(
                        str(out_path),
                        keyword = f_s.stem,
                        scale   = scale,
                        mode    = "scan_matched",
                    )
                except Exception:
                    pass

            n = 300
            if len(q_r) > n:
                ix = np.round(np.linspace(0, len(q_r)-1, n)).astype(int)
                q_r, I_r, sig_r = q_r[ix], I_r[ix], sig_r[ix]
            results.append({
                "name": out_name, "scan_idx": idx,
                "q": q_r.tolist(), "I": I_r.tolist(), "sigma": sig_r.tolist(),
            })
        except Exception as exc:
            errors.append(f"scan {idx} ({f_s.name}): {exc}")

    if _project_root and pending:
        update_manifest(_project_root, lambda m: [fn(m) for fn in pending])

    return jsonify({"saved": saved, "errors": errors, "results": results,
                    "n_matched": len(pair_list), "warning": warning})


@app.route("/api/subtract/individual", methods=["POST"])
def api_subtract_individual():
    """
    Individual mode: subtract one background .dat from an explicit list of
    sample .dat files (one or more), each saved to the output folder.
    Body: {sample_files: [..], bkg_file, scale, output_folder?}
    """
    body         = request.get_json(force=True)
    sample_files = [Path(p.strip()) for p in body.get("sample_files", []) if str(p).strip()]
    bkg_file     = Path(body.get("bkg_file", "").strip())
    scale        = float(body.get("scale", 1.0))
    method       = body.get("method", "manual")
    detector     = (body.get("detector", "saxs") or "saxs").lower()
    qmin         = body.get("qmin"); qmax = body.get("qmax")
    out_raw      = body.get("output_folder", "").strip()

    if not sample_files:
        return jsonify({"error": "No sample files selected"}), 400
    if not bkg_file.exists():
        return jsonify({"error": f"Background file not found: {bkg_file}"}), 400

    out_folder = Path(out_raw) if out_raw else sample_files[0].parent / "Subtracted"

    q_b, I_b, sig_b = _load_dat(bkg_file)

    saved, errors, results, pending = [], [], [], []
    for f in sample_files:
        try:
            if not f.exists():
                errors.append(f"{f.name}: not found")
                continue
            q_s, I_s, sig_s = _load_dat(f)
            s_use = scale
            if method == "auto_highq":
                s_use = _auto_scale(q_s, I_s, sig_s, q_b, I_b, sig_b, qmin=qmin, qmax=qmax)["scale"]
            q_r, I_r, sig_r = _subtract(q_s, I_s, sig_s, q_b, I_b, sig_b, s_use)
            out_path = out_folder / (f.stem + "_sub.dat")
            _write_dat(out_path, q_r, I_r, sig_r, [
                f"# Sample     : {f}",
                f"# Background : {bkg_file}",
                f"# Scale      : {s_use:.6g}",
                f"# Method     : {method}",
                f"# Detector   : {detector}",
                f"# Mode       : individual",
            ])
            saved.append(str(out_path))

            if _project_root:
                prov = make_provenance(
                    "background",
                    input_files = [f, bkg_file],
                    config      = {"scale": s_use, "mode": "individual", "method": method},
                )
                def _add(m, out_path=out_path, f=f, prov=prov, s_use=s_use):
                    add_file_entry(m, path=out_path, stage="subtracted",
                                   detector=detector, keyword=f.stem,
                                   scan_idx=_scan_idx(f), provenance=prov)
                    add_background_entry(m, output_path=out_path,
                                         sample_path=f, bkg_path=bkg_file,
                                         scale=s_use, mode="individual",
                                         scale_method=("auto" if method=="auto_highq" else "manual"),
                                         provenance=prov)
                pending.append(_add)

            if _bus is not None:
                try:
                    _bus.emit_file_subtracted(str(out_path), keyword=f.stem,
                                              scale=scale, mode="individual")
                except Exception:
                    pass

            n = 300
            if len(q_r) > n:
                ix = np.round(np.linspace(0, len(q_r)-1, n)).astype(int)
                q_r, I_r, sig_r = q_r[ix], I_r[ix], sig_r[ix]
            results.append({"name": out_path.name,
                            "q": q_r.tolist(), "I": I_r.tolist(), "sigma": sig_r.tolist()})
        except Exception as exc:
            errors.append(f"{f.name}: {exc}")

    if _project_root and pending:
        update_manifest(_project_root, lambda m: [fn(m) for fn in pending])

    return jsonify({"saved": saved, "errors": errors, "results": results})


# ── Automated-subtraction monitor ─────────────────────────────────────────────

def _verdict_from_warnings(warnings: list[dict]) -> str:
    """Collapse QC warnings into PASS / WARN / FAIL."""
    sev = {w.get("severity") for w in warnings}
    if "error" in sev:
        return "FAIL"
    if "warning" in sev:
        return "WARN"
    return "PASS"


def _process_one(sample: Path, bkg: Path, out_folder: Path, det: str,
                 scale_mode: str = "auto", fixed_scale: float = 1.0) -> dict | None:
    """Subtract one sample/background pair with QC, then write the result.

    ``scale_mode`` = "auto"  → high-q auto-adjusted scale (driven to zero residual)
                   = "fixed" → use ``fixed_scale`` as-is.

    Always writes the file (best attempt) and returns a record, or None on error.
    """
    q_s, I_s, sig_s = _load_dat(sample)
    q_b, I_b, sig_b = _load_dat(bkg)

    if scale_mode == "fixed":
        scale   = float(fixed_scale)
        adj     = None
        scale_method = "manual"
        scale_note   = f"{scale:.4f}  (fixed)"
    else:
        adj     = _auto_adjust_scale(q_s, I_s, sig_s, q_b, I_b, sig_b)
        scale   = adj["scale"]
        scale_method = "auto"
        scale_note   = (f"{scale:.4f}  (auto, high-q→0; LS={adj['ls_scale']}, "
                        f"zero={adj['zero_scale']}{', clamped' if adj['clamped'] else ''})")

    q_r, I_r, sig_r = _subtract(q_s, I_s, sig_s, q_b, I_b, sig_b, scale)
    qc      = _qc_metrics(q_r, I_r, I_s)
    verdict = _verdict_from_warnings(qc["warnings"])

    out_path = out_folder / (sample.stem + "_sub.dat")
    _write_dat(out_path, q_r, I_r, sig_r, [
        f"# Sample       : {sample}",
        f"# Background   : {bkg}",
        f"# Scale        : {scale_note}",
        f"# Mode         : auto ({scale_method} scale)",
        f"# QC           : {verdict}  "
        f"(neg={qc['pct_negative']}%, highq_ratio={qc['highq_ratio']})",
    ])

    if _project_root:
        try:
            cfg = {"mode": "auto", "scale": scale, "scale_mode": scale_mode,
                   "scale_method": scale_method, "qc_verdict": verdict,
                   "qc": qc, "detector": det}
            if adj is not None:
                cfg.update({"ls_scale": adj["ls_scale"],
                            "zero_scale": adj["zero_scale"],
                            "clamped": adj["clamped"]})
            prov = make_provenance("background", input_files=[sample, bkg], config=cfg)

            def _add(m, out_path=out_path, sample=sample, bkg=bkg, scale=scale,
                     prov=prov, scale_method=scale_method):
                add_file_entry(m, path=out_path, stage="subtracted",
                               detector=det, keyword=sample.stem,
                               scan_idx=_seq_index(sample), provenance=prov)
                add_background_entry(m, output_path=out_path, sample_path=sample,
                                     bkg_path=bkg, scale=scale, mode="auto",
                                     scale_method=scale_method, provenance=prov)
            update_manifest(_project_root, _add)
        except Exception as exc:
            _sub_emit(f"⚠  manifest update failed for {sample.name}: {exc}", "warn")

    if _bus is not None:
        try:
            _bus.emit_file_subtracted(str(out_path), keyword=sample.stem,
                                      scale=scale, mode="auto")
        except Exception:
            pass

    return {"out": out_path, "scale": scale, "verdict": verdict,
            "clamped": bool(adj["clamped"]) if adj else False, "bkg": bkg}


def _sub_monitor_loop(dets, interval, sample_kw="", bkg_kw="",
                      scale_mode="auto", fixed_scale=1.0):
    """Continuous auto-subtraction loop (runs in a daemon thread).

    ``bkg_kw``    — if set, a file is a background when its name contains this
                    keyword (case-insensitive); otherwise background tokens
                    (buffer/blank/empty/…) are used.
    ``sample_kw`` — if set, only files whose name contains it are treated as
                    samples (others are ignored).
    """
    global _sub_monitoring
    s_mode = "fixed" if scale_mode == "fixed" else "auto"
    scale_desc = f"fixed scale {fixed_scale:g}" if s_mode == "fixed" else "auto high-q scale"
    kw_desc = (f"  ·  sample~'{sample_kw}'" if sample_kw else "") + \
              (f"  ·  bkg~'{bkg_kw}'" if bkg_kw else "")
    _sub_emit(f"▶  Auto-subtraction started — every {interval}s  ·  {scale_desc}{kw_desc}", "ok")

    sk = sample_kw.lower().strip()
    bk = bkg_kw.lower().strip()

    def _is_bg(name: str) -> bool:
        return (bk in name.lower()) if bk else _is_background(name)

    while _sub_monitoring:
        for det, avg_folder, out_folder in dets:
            fp = Path(avg_folder)
            if not fp.is_dir():
                continue
            dats = _glob_dats(fp)
            bkgs = [f for f in dats if _is_bg(f.name)]
            sams = [f for f in dats
                    if not _is_bg(f.name) and (not sk or sk in f.name.lower())]

            for sample in sams:
                if not _sub_monitoring:
                    break
                rp = str(sample.resolve())
                if rp in _sub_done:
                    continue
                bkg = _pick_background(sample, bkgs)
                if bkg is None:
                    continue            # no background yet — wait, retry next cycle
                try:
                    rec = _process_one(sample, bkg, Path(out_folder), det,
                                       scale_mode=s_mode, fixed_scale=fixed_scale)
                except Exception as exc:
                    _sub_emit(f"✗  {sample.name}: {exc}", "error")
                    _sub_done.add(rp)   # don't retry a hard-failing file forever
                    continue

                _sub_done.add(rp)
                _sub_status["subtracted"] += 1
                _sub_status["last"] = rec["out"].name
                tag = {"PASS": "ok", "WARN": "warn", "FAIL": "error"}[rec["verdict"]]
                if rec["verdict"] != "PASS":
                    _sub_status["flagged"] += 1
                _sub_emit(
                    f"{'✓' if rec['verdict']=='PASS' else '⚑'}  {rec['out'].name}"
                    f"  ←  {rec['bkg'].name}  ·  scale {rec['scale']:.3f}"
                    f"{' (clamped)' if rec['clamped'] else ''}  ·  QC {rec['verdict']}",
                    tag,
                )
        gc.collect()
        time.sleep(interval)

    _sub_monitoring = False
    _sub_status["monitoring"] = False
    _sub_emit("⏹  Auto-subtraction stopped", "warn")


@app.route("/api/monitor/start", methods=["POST"])
def monitor_start():
    """Start the automated-subtraction monitor.

    Body: { interval, saxs_avg_folder, waxs_avg_folder,
            output_dir_saxs?, output_dir_waxs?,
            sample_keyword?, bkg_keyword?,
            scale_mode? ("auto"|"fixed"), fixed_scale? }
    """
    global _sub_monitoring, _sub_monitor_thread, _sub_done
    if _sub_monitoring:
        return jsonify({"ok": False, "error": "Already monitoring"})

    body        = request.get_json(force=True)
    interval    = max(int(body.get("interval", 10) or 10), 1)
    saxs_folder = (body.get("saxs_avg_folder", "") or "").strip()
    waxs_folder = (body.get("waxs_avg_folder", "") or "").strip()
    out_saxs    = (body.get("output_dir_saxs", "") or "").strip() or None
    out_waxs    = (body.get("output_dir_waxs", "") or "").strip() or None
    sample_kw   = (body.get("sample_keyword", "") or "").strip()
    bkg_kw      = (body.get("bkg_keyword", "") or "").strip()
    scale_mode  = (body.get("scale_mode", "auto") or "auto").strip().lower()
    try:
        fixed_scale = float(body.get("fixed_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        fixed_scale = 1.0

    # Default output is a Subtracted/ folder that is a SIBLING of the watched
    # Averaged/ folder, i.e. 1D/<DET>/Subtracted (not nested under Averaged/).
    dets = []
    if saxs_folder:
        dets.append(("saxs", saxs_folder,
                     out_saxs or str(Path(saxs_folder).parent / "Subtracted")))
    if waxs_folder:
        dets.append(("waxs", waxs_folder,
                     out_waxs or str(Path(waxs_folder).parent / "Subtracted")))
    if not dets:
        return jsonify({"ok": False, "error": "No Averaged folder provided"}), 400

    _sub_done = set()
    _sub_status.update({"monitoring": True, "subtracted": 0, "flagged": 0,
                        "last": None, "interval": interval})
    _sub_monitoring = True
    _sub_monitor_thread = threading.Thread(
        target=_sub_monitor_loop,
        args=(dets, interval, sample_kw, bkg_kw, scale_mode, fixed_scale),
        daemon=True)
    _sub_monitor_thread.start()
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
def monitor_stop():
    global _sub_monitoring
    _sub_monitoring = False
    return jsonify({"ok": True})


@app.route("/api/monitor/status")
def monitor_status():
    return jsonify(_sub_status)


@app.route("/api/monitor/stream")
def monitor_stream():
    """Server-sent-events stream of auto-subtraction log lines."""
    def _generate():
        last_seq = 0
        while True:
            with _sub_lock:
                new = [(s, ln) for (s, ln) in _sub_log if s > last_seq]
            for s, ln in new:
                last_seq = s
                yield f"data: {json.dumps(ln)}\n\n"
            yield ": keepalive\n\n"
            time.sleep(0.8)
    return Response(_generate(), mimetype="text/event-stream")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    _project_root = os.environ.get("SWAXS_PROJECT", "")
    print("━" * 48)
    print("  SWAXS Background Subtraction App")
    print("  → http://localhost:5003")
    print("━" * 48)
    app.run(debug=False, port=5003, threaded=True)
