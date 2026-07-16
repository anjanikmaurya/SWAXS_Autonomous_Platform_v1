"""
viewer/app.py — Data Viewer Flask backend (port 5002)
======================================================
Run:  uv run viewer/app.py
Open: http://localhost:5002
"""

import base64
import collections
import datetime
import gc
import hashlib
import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from flask import Flask, render_template, request, jsonify

# ─── sys.path ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.plot_reduction import (                                       # noqa: E402
    read_folder, average_and_save, average_batch,
)
from src.utils.read_dat_metadata import read_dat_data_metadata         # noqa: E402
from src.loop_naming import condition_keyword                          # noqa: E402
from src.manifest import (                                             # noqa: E402
    update_manifest,
    add_file_entry, make_provenance,
)

# ── Event bus (graceful degradation) ─────────────────────────────────────────
try:
    from src.events import EventBusClient as _EventBusClient
    _bus = _EventBusClient("viewer").connect(retry=True)
except Exception:
    _bus = None

app = Flask(__name__)

# Project root (populated by hub via /api/set_project)
_project_root: str = ""

# ── Auto-averaging monitor state ──────────────────────────────────────────────
# A daemon thread polls the Reduction folder(s); whenever a sample accumulates
# another N frames it averages that batch and writes it to the Averaged folder.
_avg_monitoring: bool = False
_avg_monitor_thread: threading.Thread | None = None
_avg_lock = threading.Lock()
_avg_log: collections.deque = collections.deque(maxlen=500)   # (seq, line) items
_avg_seq: int = 0
# (detector, keyword) -> number of frames already consumed into batches
_avg_batch_state: dict = {}
_avg_status: dict = {"monitoring": False, "batches": 0, "last": None,
                     "frames_per_average": None, "interval": None}


def _avg_emit(msg: str, tag: str = "info") -> None:
    """Append a line to the auto-averaging log (consumed by the SSE stream)."""
    global _avg_seq
    line = {"ts": datetime.datetime.now().strftime("%H:%M:%S"), "msg": msg, "tag": tag}
    with _avg_lock:
        _avg_seq += 1
        _avg_log.append((_avg_seq, line))

# ── 2D image rendering — thread pool + LRU cache ─────────────────────────────
_render_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="img-render")
_img_cache: collections.OrderedDict = collections.OrderedDict()
_IMG_CACHE_MAX = 30


def _q_extent(poni_file: str, rows: int, cols: int):
    """Return (extent, xlabel, ylabel, ax_ratio) using pyFAI.
    extent = [qx_min, qx_max, qy_min, qy_max] in nm⁻¹, or None on failure."""
    try:
        import pyFAI                                           # noqa: PLC0415
        ai      = pyFAI.load(poni_file)
        q_arr   = ai.array_from_unit((rows, cols), unit="q_nm^-1")
        chi_arr = ai.chiArray((rows, cols))
        qx = q_arr * np.cos(chi_arr)
        qy = q_arr * np.sin(chi_arr)
        qx_rng = float(qx.max() - qx.min())
        qy_rng = float(qy.max() - qy.min())
        extent = [float(qx.min()), float(qx.max()),
                  float(qy.min()), float(qy.max())]
        ratio  = qx_rng / qy_rng if qy_rng > 0 else cols / rows
        return extent, r"$q_x$  (nm$^{-1}$)", r"$q_y$  (nm$^{-1}$)", ratio
    except Exception:
        return None, "Pixel column", "Pixel row", cols / rows


def _render_array(arr: np.ndarray, rows: int, cols: int,
                  cmap: str, log_scale: bool, clip_pct: float,
                  title: str = "", poni_file: str = "") -> dict:
    """Render a 2D array. Returns {"png": base64_str, "svg": svg_str}.
    If poni_file is given, axes show qx/qy in nm⁻¹ instead of pixels.
    Both formats are produced from the same figure in a single render pass."""
    disp = arr.astype(float)
    if log_scale:
        disp   = np.where(disp > 0, np.log10(disp), np.nan)
        clabel = "log₁₀(counts)"
    else:
        clabel = "counts"

    lo = np.nanpercentile(disp, clip_pct)
    hi = np.nanpercentile(disp, 100 - clip_pct)

    # Axes: q-space if PONI supplied, pixel indices otherwise
    if poni_file and Path(poni_file).is_file():
        extent, xlabel, ylabel, ax_ratio = _q_extent(poni_file, rows, cols)
    else:
        extent, xlabel, ylabel, ax_ratio = None, "Pixel column", "Pixel row", cols / rows

    # Figure size preserves the correct aspect ratio (q-space or pixel)
    dpi      = 100
    max_ax   = 7.0                             # max axis dimension in inches
    ax_w_in  = max_ax if ax_ratio >= 1 else max_ax * ax_ratio
    ax_h_in  = max_ax / ax_ratio if ax_ratio >= 1 else max_ax
    fig_w    = ax_w_in + 1.7                   # +colorbar + margins
    fig_h    = ax_h_in + 1.0

    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    im = ax.imshow(disp, aspect="equal", cmap=cmap, origin="lower",
                   interpolation="nearest", vmin=lo, vmax=hi,
                   extent=extent)
    fig.colorbar(im, ax=ax, label=clabel, shrink=0.88)
    if title:
        ax.set_title(title, fontsize=9)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    fig.tight_layout()

    # PNG
    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=dpi, bbox_inches="tight")
    png_buf.seek(0)
    png_b64 = base64.b64encode(png_buf.read()).decode()

    # SVG (vector, same figure — no extra render cost)
    svg_buf = io.BytesIO()
    fig.savefig(svg_buf, format="svg", bbox_inches="tight")
    svg_buf.seek(0)
    svg_str = svg_buf.read().decode("utf-8")

    plt.close(fig)
    del png_buf, svg_buf, disp
    return {"png": png_b64, "svg": svg_str}


def _render_image(file_path: str, rows: int, cols: int,
                  cmap: str, log_scale: bool, clip_pct: float,
                  poni_file: str = "") -> dict:
    """Render a .raw detector file to a base64 PNG. Runs in thread pool.
    If poni_file is given, axes show qx/qy in nm⁻¹ instead of pixels."""
    data = np.fromfile(file_path, dtype=np.int32)
    if data.size != rows * cols:
        return {"error": (
            f"File has {data.size} values but {rows}×{cols}={rows*cols} expected. "
            "Check detector shape selection."
        )}
    data = data.reshape(rows, cols)
    rendered = _render_array(data, rows, cols, cmap, log_scale, clip_pct,
                              title=Path(file_path).name, poni_file=poni_file)
    result = {
        "image":   rendered["png"],   # base64 PNG for browser display
        "shape":   [rows, cols],
        "min":     int(data.min()),
        "max":     int(data.max()),
        "mean":    round(float(data.mean()), 2),
        "nonzero": int(np.count_nonzero(data)),
    }
    del data
    gc.collect()
    return result


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "viewer"})


@app.route("/api/set_project", methods=["POST"])
def set_project():
    """Accept project root from hub."""
    global _project_root
    body    = request.get_json(force=True)
    project = body.get("path", "").strip()
    if project:
        import os
        os.environ["SWAXS_PROJECT"] = project
        _project_root = project
    return jsonify({"ok": True})


@app.route("/api/project")
def api_project():
    """Current project root (set by the hub) — used by the UI to auto-fill paths."""
    return jsonify({"project_root": _project_root})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/browse", methods=["GET"])
def api_browse():
    """Return subdirectories (and optionally files) of a path for the browser modal.
    Query params:
      path       – directory to list (default: home)
      files_ext  – if given (e.g. '.poni'), also return files with that extension
    """
    raw        = request.args.get("path", "").strip()
    files_ext  = request.args.get("files_ext", "").strip().lower()   # e.g. ".poni"
    p          = Path(raw) if raw else Path.home()
    # Gracefully fall back if path doesn't exist
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.is_dir():
        p = Path.home()
    try:
        dirs  = sorted(d.name for d in p.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
        files = sorted(f.name for f in p.iterdir()
                       if f.is_file() and f.name.lower().endswith(files_ext)
                       ) if files_ext else []
    except PermissionError:
        dirs, files = [], []
    return jsonify({
        "current": str(p),
        "parent":  str(p.parent) if p != p.parent else None,
        "dirs":    dirs,
        "files":   files,
    })


@app.route("/api/discover-keywords", methods=["POST"])
def api_discover_keywords():
    """
    Scan 1D output folders and return unique filename-stem prefixes with file counts.
    Useful for discovering sample IDs without typing them from memory.

    Body:
      saxs_folder, waxs_folder  – paths to scan
      strip_first  (int ≥0)     – remove this many tokens from the start of each stem
      strip_last   (int ≥0)     – remove this many tokens from the end of each stem
      delimiter    (str)         – token separator, default "_"
      middle_filter (str)        – case-insensitive substring; only return prefixes that contain it
    Returns: { prefixes: [{label, saxs, waxs, examples}, ...], total, errors }
    """
    body          = request.get_json(force=True)
    saxs_dir      = body.get("saxs_folder",  "").strip()
    waxs_dir      = body.get("waxs_folder",  "").strip()
    strip_first   = max(0, int(body.get("strip_first",  0)))
    strip_last    = max(0, int(body.get("strip_last",   1)))
    delimiter     = body.get("delimiter",    "_") or "_"
    middle_filter = body.get("middle_filter", "").strip().lower()

    def get_prefix(stem: str) -> str | None:
        """Apply start/end trimming. Returns None if nothing would remain."""
        parts = stem.split(delimiter)
        lo    = strip_first
        hi    = len(parts) - strip_last
        if lo >= hi:
            return None          # over-trimmed — skip this file
        return delimiter.join(parts[lo:hi])

    results: dict = {}   # label → {saxs:int, waxs:int, examples:[str]}
    errors:  list = []

    for folder_str, det in [(saxs_dir, "saxs"), (waxs_dir, "waxs")]:
        if not folder_str:
            continue
        p = Path(folder_str)
        if not p.is_dir():
            errors.append(f"{det.upper()}: folder not found — {folder_str}")
            continue
        for f in sorted(p.glob("*.dat")):
            prefix = get_prefix(f.stem)
            if prefix is None:
                continue
            # Apply middle filter (case-insensitive substring on the resulting prefix)
            if middle_filter and middle_filter not in prefix.lower():
                continue
            if prefix not in results:
                results[prefix] = {"saxs": 0, "waxs": 0, "examples": []}
            results[prefix][det] += 1
            if len(results[prefix]["examples"]) < 3:
                results[prefix]["examples"].append(f.name)

    prefixes = [
        {"label": k, "saxs": v["saxs"], "waxs": v["waxs"], "examples": v["examples"]}
        for k, v in sorted(results.items())
    ]
    return jsonify({"prefixes": prefixes, "total": len(prefixes), "errors": errors})


def _numeric_meta(md: dict) -> dict:
    """Return only the numeric (finite, non-NaN) metadata fields as floats."""
    out: dict[str, float] = {}
    for k, v in (md or {}).items():
        try:
            fv = float(v)
            if not np.isnan(fv):
                out[k] = fv
        except (TypeError, ValueError):
            pass
    return out


def _i0_stats(kw_files: list) -> tuple[dict, float | None]:
    """Return (filename→i0 dict, median_i0) for a keyword's file list."""
    all_i0: dict[str, float] = {}
    for fd in kw_files:
        raw = fd["metadata"].get("i0")
        if raw is not None:
            try:
                v = float(raw)
                if np.isfinite(v):
                    all_i0[fd["filename"]] = v
            except (TypeError, ValueError):
                pass
    if not all_i0:
        return all_i0, None
    return all_i0, float(np.median(list(all_i0.values())))


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Count matching .dat files per keyword — fast glob, no data read."""
    body     = request.get_json(force=True)
    keywords = [k.strip() for k in body.get("keywords", []) if k.strip()]
    result   = {}
    for label, raw in [("saxs", body.get("saxs_folder", "")),
                        ("waxs", body.get("waxs_folder", ""))]:
        folder = Path(raw.strip()) if raw else Path("")
        if not folder.exists():
            result[label] = {"_error": f"Not found: {raw}"}
            continue
        dats = list(folder.glob("*.dat"))
        result[label] = {kw: sum(1 for f in dats if kw in f.name)
                         for kw in keywords}
    return jsonify(result)


@app.route("/api/load", methods=["POST"])
def api_load():
    """Load and downsample .dat curves for Plotly display."""
    body          = request.get_json(force=True)
    keywords      = [k.strip() for k in body.get("keywords", []) if k.strip()]
    max_per_kw    = max(1, int(body.get("max_per_kw", 50)))
    n_display     = max(50,  int(body.get("n_display",  300)))
    i0_filter_pct = float(body.get("i0_filter_pct", 0))

    response = {"saxs": {}, "waxs": {}, "errors": []}

    for det, raw in [("saxs", body.get("saxs_folder", "")),
                     ("waxs", body.get("waxs_folder", ""))]:
        folder = Path(raw.strip()) if raw else Path("")
        if not folder.exists():
            if raw.strip():
                response["errors"].append(f"{det.upper()} not found: {raw}")
            continue
        try:
            files = read_folder(folder, keywords=keywords or None)
        except Exception as exc:
            response["errors"].append(f"{det.upper()} load error: {exc}")
            continue

        groups: dict[str, list] = {}
        for f in files:
            groups.setdefault(f["keyword"], []).append(f)

        det_out = {}
        for kw, kw_files in groups.items():
            # ── I0 stats and bad-frame detection ──────────────────────────
            all_i0, median_i0 = _i0_stats(kw_files)
            bad_set: set[str] = set()
            if i0_filter_pct > 0 and median_i0 is not None and abs(median_i0) > 1e-10:
                for fname, i0v in all_i0.items():
                    if abs(i0v - median_i0) / abs(median_i0) * 100 > i0_filter_pct:
                        bad_set.add(fname)

            good_files      = [fd for fd in kw_files if fd["filename"] not in bad_set]
            bad_frames_info = [
                {"name": fd["filename"],
                 "scan_idx": int(fd["scan_idx"]),
                 "i0": all_i0.get(fd["filename"]),
                 "meta": _numeric_meta(fd["metadata"])}   # time fields for axis placement
                for fd in kw_files if fd["filename"] in bad_set
            ]
            # ──────────────────────────────────────────────────────────────

            curves    = []
            meta_rows = []
            for fd in good_files[:max_per_kw]:
                q, I, sig = fd["q"], fd["I"], fd["sigma"]
                mask = (q > 0) & (I > 0)
                if not mask.any():
                    continue
                q, I, sig = q[mask], I[mask], sig[mask]
                if len(q) > n_display:
                    idx = np.round(np.linspace(0, len(q) - 1, n_display)).astype(int)
                    q, I, sig = q[idx], I[idx], sig[idx]
                curves.append({
                    "name":     fd["filename"],
                    "scan_idx": int(fd["scan_idx"]),
                    "q":        q.tolist(),
                    "I":        I.tolist(),
                    "sigma":    sig.tolist(),
                })
                safe = _numeric_meta(fd["metadata"])
                meta_rows.append({"scan_idx": int(fd["scan_idx"]), **safe})

            det_out[kw] = {
                "curves":     curves,
                "meta":       meta_rows,
                "bad_frames": bad_frames_info,
                "median_i0":  median_i0,
                "i0_values":  all_i0,
            }
        response[det] = det_out

    return jsonify(response)


@app.route("/api/average", methods=["POST"])
def api_average():
    """Run average_and_save() and return averaged curves."""
    body          = request.get_json(force=True)
    keywords      = [k.strip() for k in body.get("keywords", []) if k.strip()]
    output_dir    = body.get("output_dir", "").strip() or None
    i0_filter_pct = float(body.get("i0_filter_pct", 0))
    q_min         = body.get("q_min", None)
    q_max         = body.get("q_max", None)
    q_min         = float(q_min) if q_min not in (None, "") else None
    q_max         = float(q_max) if q_max not in (None, "") else None
    response      = {"saxs": {}, "waxs": {}, "errors": [], "saved": []}

    # Manifest mutations are collected and applied atomically (locked) at the end.
    pending = []

    for det, raw in [("saxs", body.get("saxs_folder", "")),
                     ("waxs", body.get("waxs_folder", ""))]:
        folder = Path(raw.strip()) if raw else Path("")
        if not folder.exists():
            if raw.strip():
                response["errors"].append(f"{det.upper()} not found: {raw}")
            continue
        try:
            saved = average_and_save(str(folder), keywords,
                                     output_dir=output_dir,
                                     i0_filter_pct=i0_filter_pct,
                                     q_min=q_min, q_max=q_max)
            for kw, out_path in saved:
                response["saved"].append(str(out_path))
                _, q, I, sigma, _ = read_dat_data_metadata(out_path)
                mask = (q > 0) & (I > 0)
                response[det][kw] = {
                    "q":     q[mask].tolist(),
                    "I":     I[mask].tolist(),
                    "sigma": sigma[mask].tolist(),
                }

                # ── Event bus ──────────────────────────────────────────────
                if _bus is not None:
                    try:
                        _bus.emit_file_averaged(
                            str(out_path),
                            keyword  = kw,
                            n_files  = mask.sum(),
                            detector = det,
                        )
                    except Exception:
                        pass

                # ── Manifest ───────────────────────────────────────────────
                if _project_root:
                    try:
                        prov = make_provenance(
                            "viewer",
                            input_files = [folder],
                            config      = {"keyword": kw, "detector": det,
                                           "i0_filter_pct": i0_filter_pct},
                        )
                        pending.append(lambda m, out_path=out_path, det=det, kw=kw, prov=prov:
                            add_file_entry(
                                m,
                                path       = out_path,
                                stage      = "averaged",
                                detector   = det,
                                keyword    = kw,
                                scan_idx   = 0,
                                provenance = prov,
                            ))
                    except Exception:
                        pass

        except Exception as exc:
            response["errors"].append(f"{det.upper()} averaging error: {exc}")

    if _project_root and pending:
        try:
            update_manifest(_project_root, lambda m: [fn(m) for fn in pending])
        except Exception:
            pass

    return jsonify(response)


# ── Auto-averaging monitor routes ─────────────────────────────────────────────

def _avg_monitor_loop(dets, n_per_batch, interval, i0_filter_pct,
                      n_pts, keywords, label_suffix, q_min=None, q_max=None):
    """
    Continuous auto-averaging loop (runs in a daemon thread).

    Each cycle: scan every detector's Reduction folder, group reduced frames by
    keyword, sort each group by frame index, and — for every full block of
    ``n_per_batch`` frames not yet consumed — average that block and write it to
    the Averaged folder.  State (frames consumed per sample) persists across
    cycles so each frame contributes to exactly one batch (rolling batches).
    """
    global _avg_monitoring
    _avg_emit(f"▶  Auto-averaging started — {n_per_batch} frames/batch, "
              f"every {interval}s", "ok")

    while _avg_monitoring:
        pending: list[dict] = []
        for det, folder, outdir in dets:
            fp = Path(folder)
            if not fp.is_dir():
                continue
            try:
                frames = read_folder(fp, keywords=keywords or None)
            except Exception as exc:
                _avg_emit(f"⚠  {det.upper()} scan error: {exc}", "error")
                continue

            # Group by keyword and order each group by acquisition index. For
            # reactor loop files, group by the role-aware key {recipe_id}_{role}
            # so all sample frames (and all background frames) of a condition
            # average together and stay separable — aligns with the reactor's
            # filename convention. Non-loop files keep their derived keyword.
            groups: dict[str, list[dict]] = {}
            for fd in frames:
                gk = condition_keyword(fd["filename"]) or fd["keyword"]
                groups.setdefault(gk, []).append(fd)

            for kw, grp in groups.items():
                grp.sort(key=lambda d: d.get("scan_idx", 0))
                key = (det, kw)
                consumed = _avg_batch_state.get(key, 0)

                while _avg_monitoring and (len(grp) - consumed) >= n_per_batch:
                    batch    = grp[consumed:consumed + n_per_batch]
                    batch_no = consumed // n_per_batch + 1
                    out_dir  = Path(outdir) if outdir else fp.parent / "Averaged"
                    out_path = (out_dir /
                                f"{kw}_batch{batch_no:03d}_{n_per_batch}files_"
                                f"{label_suffix}.dat")
                    try:
                        written = average_batch(
                            batch, kw, out_path,
                            i0_filter_pct=i0_filter_pct, n_pts=n_pts,
                            q_min=q_min, q_max=q_max,
                        )
                    except Exception as exc:
                        _avg_emit(f"✗  {kw} [{det}] batch {batch_no}: {exc}", "error")
                        break   # stop this group; retry next cycle

                    # Advance regardless so a bad batch can't loop forever.
                    consumed += n_per_batch
                    _avg_batch_state[key] = consumed

                    if written is None:
                        _avg_emit(f"⚠  {kw} [{det}] batch {batch_no}: "
                                  f"no usable frames — skipped", "warn")
                        continue

                    _avg_status["batches"] += 1
                    _avg_status["last"] = written.name
                    _avg_emit(f"✓  {written.name}  ({n_per_batch} frames)", "ok")

                    if _bus is not None:
                        try:
                            _bus.emit_file_averaged(
                                str(written), keyword=kw,
                                n_files=n_per_batch, detector=det,
                            )
                        except Exception:
                            pass

                    if _project_root:
                        try:
                            prov = make_provenance(
                                "viewer",
                                input_files=[fp],
                                config={"keyword": kw, "detector": det,
                                        "auto_average": True,
                                        "frames_per_average": n_per_batch,
                                        "batch": batch_no,
                                        "i0_filter_pct": i0_filter_pct},
                            )
                            pending.append({"path": written, "det": det,
                                            "kw": kw, "batch": batch_no,
                                            "prov": prov})
                        except Exception:
                            pass

        if _project_root and pending:
            try:
                def _apply(m, items=pending):
                    for it in items:
                        add_file_entry(
                            m, path=it["path"], stage="averaged",
                            detector=it["det"], keyword=it["kw"],
                            scan_idx=it["batch"], provenance=it["prov"],
                        )
                update_manifest(_project_root, _apply)
            except Exception as exc:
                _avg_emit(f"⚠  manifest update failed: {exc}", "warn")

        gc.collect()
        time.sleep(interval)

    _avg_monitoring = False
    _avg_status["monitoring"] = False
    _avg_emit("⏹  Auto-averaging stopped", "warn")


@app.route("/api/monitor/start", methods=["POST"])
def monitor_start():
    """Start the auto-averaging monitor.

    Body: { frames_per_average, interval, saxs_folder, waxs_folder,
            output_dir_saxs?, output_dir_waxs?, i0_filter_pct?, n_pts?,
            keywords?, label_suffix? }
    """
    global _avg_monitoring, _avg_monitor_thread, _avg_batch_state
    if _avg_monitoring:
        return jsonify({"ok": False, "error": "Already monitoring"})

    body          = request.get_json(force=True)
    n_per_batch   = max(int(body.get("frames_per_average", 0) or 0), 1)
    interval      = max(int(body.get("interval", 10) or 10), 1)
    saxs_folder   = (body.get("saxs_folder", "") or "").strip()
    waxs_folder   = (body.get("waxs_folder", "") or "").strip()
    out_saxs      = (body.get("output_dir_saxs", "") or "").strip() or None
    out_waxs      = (body.get("output_dir_waxs", "") or "").strip() or None
    i0_filter_pct = float(body.get("i0_filter_pct", 0) or 0)
    n_pts         = int(body.get("n_pts", 1000) or 1000)
    label_suffix  = (body.get("label_suffix", "") or "Average").strip()
    keywords      = [k.strip() for k in body.get("keywords", []) if k.strip()]
    _qn           = body.get("q_min", None)
    _qx           = body.get("q_max", None)
    q_min         = float(_qn) if _qn not in (None, "") else None
    q_max         = float(_qx) if _qx not in (None, "") else None

    dets = []
    if saxs_folder:
        dets.append(("saxs", saxs_folder, out_saxs))
    if waxs_folder:
        dets.append(("waxs", waxs_folder, out_waxs))
    if not dets:
        return jsonify({"ok": False,
                        "error": "No reduction folder provided"}), 400

    _avg_batch_state = {}
    _avg_status.update({"monitoring": True, "batches": 0, "last": None,
                        "frames_per_average": n_per_batch, "interval": interval})
    _avg_monitoring = True

    _avg_monitor_thread = threading.Thread(
        target=_avg_monitor_loop,
        args=(dets, n_per_batch, interval, i0_filter_pct,
              n_pts, keywords, label_suffix, q_min, q_max),
        daemon=True,
    )
    _avg_monitor_thread.start()
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
def monitor_stop():
    global _avg_monitoring
    _avg_monitoring = False
    return jsonify({"ok": True})


@app.route("/api/monitor/status")
def monitor_status():
    return jsonify(_avg_status)


@app.route("/api/monitor/stream")
def monitor_stream():
    """Server-sent-events stream of auto-averaging log lines."""
    from flask import Response

    def _generate():
        last_seq = 0
        # Replay recent backlog so a late-connecting UI sees prior lines.
        while True:
            with _avg_lock:
                new = [(s, ln) for (s, ln) in _avg_log if s > last_seq]
            for s, ln in new:
                last_seq = s
                yield f"data: {json.dumps(ln)}\n\n"
            yield ": keepalive\n\n"
            time.sleep(0.8)

    return Response(_generate(), mimetype="text/event-stream")


# ── 2D raw-file routes ────────────────────────────────────────────────────────

@app.route("/api/list-raw")
def api_list_raw():
    """List .raw detector files in a directory."""
    dir_path = request.args.get("dir", "").strip()
    try:
        p = Path(dir_path)
        if not p.is_dir():
            return jsonify({"files": [], "error": f"Not a directory: {dir_path}"})
        files = sorted(
            f.name for f in p.iterdir()
            if f.suffix.lower() == ".raw" and not f.name.startswith("._")
        )
        return jsonify({"files": files, "dir": str(p)})
    except Exception as exc:
        return jsonify({"files": [], "error": str(exc)})


@app.route("/api/raw-image")
def api_raw_image():
    """
    Render a .raw detector file as a base64 PNG.
    Query params: file, rows, cols, cmap, log (bool), clip (float %),
                  poni (optional path to .poni file — enables qx/qy axes).
    Results are LRU-cached (max 30 entries).
    """
    file_path = request.args.get("file", "").strip()
    rows      = int(request.args.get("rows", 1043))
    cols      = int(request.args.get("cols", 981))
    cmap      = request.args.get("cmap", "viridis")
    log_scale = request.args.get("log", "true").lower() == "true"
    clip_pct  = float(request.args.get("clip", 2.0))
    poni_file = request.args.get("poni", "").strip()

    if not file_path or not Path(file_path).is_file():
        return jsonify({"error": f"File not found: {file_path}"}), 400

    cache_key = hashlib.md5(
        f"{file_path}|{rows}|{cols}|{cmap}|{log_scale}|{clip_pct}|{poni_file}".encode()
    ).hexdigest()

    if cache_key in _img_cache:
        _img_cache.move_to_end(cache_key)
        return jsonify(_img_cache[cache_key])

    try:
        future = _render_pool.submit(
            _render_image, file_path, rows, cols, cmap, log_scale, clip_pct, poni_file
        )
        result = future.result(timeout=60)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if "error" in result:
        return jsonify(result), 400

    _img_cache[cache_key] = result
    if len(_img_cache) > _IMG_CACHE_MAX:
        _img_cache.popitem(last=False)

    return jsonify(result)


# ── 2D save routes ───────────────────────────────────────────────────────────

@app.route("/api/save-image", methods=["POST"])
def api_save_image():
    """
    Save the currently-displayed 2D image as PNG+SVG to {raw_dir}/Saved_2D/.
    Body: { file, rows, cols, cmap, log, clip, poni (optional) }
    """
    body      = request.get_json(force=True)
    file_path = body.get("file", "").strip()
    rows      = int(body.get("rows", 1043))
    cols      = int(body.get("cols", 981))
    cmap      = body.get("cmap", "viridis")
    log_scale = str(body.get("log", "true")).lower() == "true"
    clip_pct  = float(body.get("clip", 2.0))
    poni_file = body.get("poni", "").strip()

    if not file_path or not Path(file_path).is_file():
        return jsonify({"error": f"File not found: {file_path}"}), 400

    # Single render pass → both PNG and SVG (with q-axes if PONI provided)
    rendered = _render_array(
        np.fromfile(file_path, dtype=np.int32).reshape(rows, cols).astype(float),
        rows, cols, cmap, log_scale, clip_pct,
        title=Path(file_path).name, poni_file=poni_file
    )

    save_dir = Path(file_path).parent / "Saved_2D"
    save_dir.mkdir(exist_ok=True)
    stem     = Path(file_path).stem
    png_path = save_dir / f"{stem}.png"
    svg_path = save_dir / f"{stem}.svg"
    png_path.write_bytes(base64.b64decode(rendered["png"]))
    svg_path.write_text(rendered["svg"], encoding="utf-8")

    return jsonify({
        "saved": str(png_path),
        "name":  stem,
        "png":   png_path.name,
        "svg":   svg_path.name,
    })


@app.route("/api/save-average", methods=["POST"])
def api_save_average():
    """
    Average all filtered .raw files pixel-by-pixel and save as PNG+SVG to
    {raw_dir}/Saved_2D/{last_stem}_average_{N}files_{timestamp}.png/.svg
    Body: { dir, files: [...], rows, cols, cmap, log, clip, poni (optional) }
    """
    body      = request.get_json(force=True)
    directory = body.get("dir", "").strip()
    files     = body.get("files", [])
    rows      = int(body.get("rows", 1043))
    cols      = int(body.get("cols", 981))
    cmap      = body.get("cmap", "viridis")
    log_scale = str(body.get("log", "true")).lower() == "true"
    clip_pct  = float(body.get("clip", 2.0))
    poni_file = body.get("poni", "").strip()

    if not files:
        return jsonify({"error": "No files specified"}), 400
    d = Path(directory)
    if not d.is_dir():
        return jsonify({"error": f"Directory not found: {directory}"}), 400

    acc     = np.zeros((rows, cols), dtype=np.float64)
    n_ok    = 0
    skipped = []
    for fname in files:
        fp = d / fname
        if not fp.is_file():
            skipped.append(fname); continue
        raw = np.fromfile(fp, dtype=np.int32)
        if raw.size != rows * cols:
            skipped.append(fname); continue
        acc += raw.reshape(rows, cols)
        n_ok += 1

    if n_ok == 0:
        return jsonify({"error": "No valid files could be read"}), 400

    ts        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    last_stem = Path(files[-1]).stem
    title     = f"Pixel average — {n_ok} files  (last: {Path(files[-1]).name})  [{ts}]"
    rendered  = _render_array(acc / n_ok, rows, cols, cmap, log_scale, clip_pct,
                               title=title, poni_file=poni_file)
    del acc
    gc.collect()

    save_dir  = d / "Saved_2D"
    save_dir.mkdir(exist_ok=True)
    base_name = f"{last_stem}_average_{n_ok}files_{ts}"
    png_path  = save_dir / f"{base_name}.png"
    svg_path  = save_dir / f"{base_name}.svg"
    png_path.write_bytes(base64.b64decode(rendered["png"]))
    svg_path.write_text(rendered["svg"], encoding="utf-8")

    return jsonify({
        "saved":   str(png_path),
        "name":    base_name,
        "png":     png_path.name,
        "svg":     svg_path.name,
        "n_files": n_ok,
        "skipped": skipped,
    })


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    _project_root = os.environ.get("SWAXS_PROJECT", "")
    print("━" * 42)
    print("  SAXS/WAXS Visualization App")
    print("  → http://localhost:5002")
    print("━" * 42)
    app.run(debug=False, port=5002, threaded=True)
