#!/usr/bin/env python3
"""
reduction/app.py — Reduction & Correction Flask backend (port 5001)
====================================================================
Launch:
    uv run reduction/app.py          (recommended)
    python reduction/app.py

Then open  http://localhost:5001  in any browser.

For multi-day continuous operation use gunicorn instead of the dev server:
    uv run gunicorn -w 1 --threads 8 -b 127.0.0.1:5001 "reduction.app:app"

Routes
------
  GET  /                     → main SPA
  GET  /api/browse           → directory listing for file browser
  POST /api/run              → one-shot processing run (background thread)
  POST /api/run/stop         → request graceful stop of active one-shot run
  GET  /api/stream           → Server-Sent Events — live log
  POST /api/monitor/start    → start continuous file watching
  POST /api/monitor/stop     → stop watching
  GET  /api/monitor/status   → {"monitoring": bool}
  POST /api/reset            → clear processed-files list
  GET  /api/list-dat         → .dat files in a directory
  GET  /api/list-raw         → .raw files in a directory
  GET  /api/dat-data         → q/I/err for one .dat file
  GET  /api/raw-image        → detector image as base64 PNG
  POST /api/save-config      → write config to YAML
  POST /api/load-config      → read config from YAML

Design notes
------------
* The Experiment object (PyFAI integrators) is created ONCE and cached
  globally.  It is reused across all Run Now calls and across all poll
  cycles of the monitor, so PONI files are loaded only once per session.
  The cache is invalidated automatically if the config hash changes.

* Files are processed strictly one at a time.  src.reduction.core
  frees detector arrays after each file; app.py never accumulates results.

* The monitor loop catches per-file exceptions without crashing the loop,
  so a single bad file never stops continuous acquisition.

* matplotlib backend is set once at import time (never inside a handler).
"""

from __future__ import annotations   # allow `X | None` type hints on Python 3.9

import base64
import collections
import gc
import getpass
import hashlib
import io
import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import yaml

# ── matplotlib: set backend ONCE at module level ──────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from flask import (
    Flask, Response, jsonify, render_template,
    request, stream_with_context,
)

# ── sys.path: add project root so src.* imports resolve ──────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.reduction import core as reduction_core  # noqa: E402
from src.manifest import (                        # noqa: E402
    update_manifest, add_file_entry, make_provenance, set_project_meta,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_user(explicit: str | None = None) -> str:
    """
    Determine who is running the reduction:
    explicit value from the UI  →  SWAXS_USER_ID env  →  OS login  →  'unknown'.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    uid = os.environ.get("SWAXS_USER_ID", "").strip()
    if uid:
        return uid
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def _record_run_meta(project_root, operator: str, mode: str) -> None:
    """
    Record run/operator metadata in manifest['project_meta'] so the experiment
    captures WHO ran a reduction and WHEN. Appends the operator to the unique
    users list and stamps last_run_by / last_run_at / last_run_app.
    Errors are swallowed so a manifest issue never blocks a run.
    """
    def _mut(m):
        users = list(m.get("project_meta", {}).get("users", []))
        if operator and operator not in users:
            users.append(operator)
        set_project_meta(
            m,
            users        = users,
            last_run_by  = operator,
            last_run_at  = _now_iso(),
            last_run_app = "reduction",
            last_run_mode = mode,
        )

    try:
        update_manifest(project_root, _mut)
    except Exception as exc:
        _emit(f"  [manifest] could not record run metadata: {exc}", "warn")

# ── Event bus (graceful degradation: no crash if hub is down) ─────────────────
try:
    from src.events import EventBusClient as _EventBusClient
    _bus = _EventBusClient("reduction").connect(retry=True)
except Exception:
    _bus = None

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
logging.basicConfig(level=logging.WARNING)

# ─────────────────────────────────────────────────────────────────────────────
# SSE state
# ─────────────────────────────────────────────────────────────────────────────

_sse_clients: list = []
_sse_lock    = threading.Lock()
_log_buffer: collections.deque = collections.deque(maxlen=500)


def _emit(msg: str, tag: str = "info"):
    """Push one log line to all connected SSE clients and the replay buffer."""
    item = {"msg": msg, "tag": tag}
    _log_buffer.append(item)
    with _sse_lock:
        for q in list(_sse_clients):
            try:
                q.put_nowait(item)
            except queue.Full:
                pass   # slow client — drop the message


# ─────────────────────────────────────────────────────────────────────────────
# Experiment cache
# ─────────────────────────────────────────────────────────────────────────────
# Keeps PyFAI AzimuthalIntegrators alive across runs/poll cycles.
# Loading PONI files is slow; recreating the Experiment every call
# was the main source of sluggishness.

_exp_lock        = threading.Lock()
_cached_exp      = None   # src.reduction.core.Experiment instance
_cached_exp_hash = None   # md5 of the config that produced it


def _config_hash(config: dict) -> str:
    return hashlib.md5(
        json.dumps(config, sort_keys=True, default=str).encode()
    ).hexdigest()


def _get_experiment(config: dict):
    """Return the cached Experiment, creating a new one only if config changed."""
    global _cached_exp, _cached_exp_hash
    h = _config_hash(config)
    with _exp_lock:
        if _cached_exp is None or _cached_exp_hash != h:
            _emit("Loading PyFAI integrators (first time or config changed)…", "info")
            _cached_exp      = reduction_core.Experiment(config, log_callback=_emit)
            _cached_exp_hash = h
            _emit("Integrators ready.", "ok")
        return _cached_exp


def _invalidate_experiment():
    """Call this when an unrecoverable error has likely corrupted the state."""
    global _cached_exp, _cached_exp_hash
    with _exp_lock:
        _cached_exp      = None
        _cached_exp_hash = None


# ─────────────────────────────────────────────────────────────────────────────
# Monitoring state
# ─────────────────────────────────────────────────────────────────────────────

_processed_files: set = set()
_monitoring           = False
_monitor_thread       = None

# Stop event for one-shot /api/run calls.
# Cleared at the start of every new run; set by /api/run/stop.
_run_stop_event = threading.Event()

# ─────────────────────────────────────────────────────────────────────────────
# Image rendering: thread pool + LRU cache
# ─────────────────────────────────────────────────────────────────────────────

_render_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="img-render")
_img_cache: collections.OrderedDict = collections.OrderedDict()
_IMG_CACHE_MAX = 30


def _render_image(file_path: str, rows: int, cols: int,
                  cmap: str, log_scale: bool, clip_pct: float) -> dict:
    """Render a .raw file to base64 PNG.  Runs in the thread pool."""
    data = np.fromfile(file_path, dtype=np.int32)
    if data.size != rows * cols:
        return {"error": (
            f"File has {data.size} values but {rows}×{cols}={rows*cols} expected."
        )}
    data = data.reshape(rows, cols)

    disp = data.astype(float)
    if log_scale:
        disp   = np.where(disp > 0, np.log10(disp), np.nan)
        clabel = "log₁₀(counts)"
    else:
        clabel = "counts"

    lo = np.nanpercentile(disp, clip_pct)
    hi = np.nanpercentile(disp, 100 - clip_pct)

    fig, ax = plt.subplots(figsize=(7, 5.5), dpi=100)
    im = ax.imshow(disp, aspect="auto", cmap=cmap, origin="lower",
                   interpolation="nearest", vmin=lo, vmax=hi)
    fig.colorbar(im, ax=ax, label=clabel, shrink=0.88)
    ax.set_title(Path(file_path).name, fontsize=9)
    ax.set_xlabel("Pixel column")
    ax.set_ylabel("Pixel row")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode()
    del buf

    result = {
        "image": img_b64,
        "shape": [rows, cols],
        "min":   int(data.min()),
        "max":   int(data.max()),
        "mean":  round(float(data.mean()), 2),
    }
    del data, disp
    gc.collect()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Event bus + manifest helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_raw_kw_idx(raw_path: Path) -> tuple:
    """
    Extract (keyword, scan_idx) from a raw file stem.
    e.g.  BSA_10mg_001.raw  →  keyword='BSA_10mg', scan_idx=1
    Falls back to (full_stem, 0) if the stem doesn't end with digits.
    """
    stem  = raw_path.stem            # strip .raw
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return stem, 0


def _register_reduced(result: dict, raw_path: Path, detector: str,
                      experiment, config: dict, user: str = "") -> None:
    """
    Called after each successful file reduction.
    1. Emits a ``file.reduced`` event on the hub event bus.
    2. Registers the output .dat in manifest.json (v2 schema).

    All errors are caught locally so that a bad manifest write never
    interrupts the reduction pipeline.
    """
    kw, scan_idx = _parse_raw_kw_idx(raw_path)
    out_path = (experiment.output_dir_1d
                / detector.upper() / "Reduction"
                / result["filename"])

    # ── 1. Event bus ────────────────────────────────────────────────────────
    if _bus is not None:
        try:
            _bus.emit_file_reduced(
                str(out_path),
                keyword  = kw,
                scan_idx = scan_idx,
                detector = detector,
            )
        except Exception:
            pass

    # ── 2. Manifest (locked read-modify-write — safe across apps) ────────────
    try:
        project_root = experiment.data_directory.parent
        prov = make_provenance(
            "reduction",
            input_files = [raw_path],
            user = user,
            config = {
                k: config[k] for k in
                ("npt_radial", "error_model", "mode", "compound")
                if k in config
            },
        )
        corr = result.get("corrections", {})
        update_manifest(project_root, lambda m: add_file_entry(
            m,
            path          = out_path,
            stage         = "reduced",
            detector      = detector,
            keyword       = kw,
            scan_idx      = scan_idx,
            metadata      = {k: float(v) for k, v in corr.items()
                             if isinstance(v, (int, float))},
            provenance    = prov,
        ))
    except Exception as exc:
        _emit(f"  [manifest] write error: {exc}", "warn")


# ─────────────────────────────────────────────────────────────────────────────
# Routes — pages
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "reduction"})


@app.route("/api/set_project", methods=["POST"])
def set_project():
    """Accept project root from hub; no-op if reduction manages its own config."""
    body = request.get_json(force=True)
    project = body.get("path", "").strip()
    if project:
        os.environ["SWAXS_PROJECT"] = project
    return jsonify({"ok": True})


@app.route("/api/project")
def api_project():
    """Current project root (set by the hub) — used by the UI to auto-fill paths."""
    return jsonify({"project_root": os.environ.get("SWAXS_PROJECT", "")})


@app.route("/")
def index():
    return render_template("index.html")


# ─────────────────────────────────────────────────────────────────────────────
# Routes — file browser
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/browse")
def browse():
    requested = request.args.get("path", str(Path.home()))
    try:
        p = Path(requested).expanduser().resolve()
        if not p.is_dir():
            p = p.parent if p.parent.is_dir() else Path.home()

        def _safe(items):
            return sorted(
                [x.name for x in items
                 if not x.name.startswith(".") and x.exists()],
                key=str.lower,
            )

        return jsonify({
            "path":   str(p),
            "parent": str(p.parent) if p != p.parent else None,
            "dirs":   _safe(x for x in p.iterdir() if x.is_dir()),
            "files":  _safe(x for x in p.iterdir() if x.is_file()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Routes — pipeline control
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def run():
    """One-shot run: find all new files, process one at a time, stream progress."""
    config = request.json or {}
    if not config:
        return jsonify({"ok": False, "error": "No config provided"}), 400

    # Operator is metadata, not an Experiment parameter — pop it BEFORE the
    # config is hashed/cached so changing operator never reloads PyFAI. (user capture)
    operator = _current_user(config.pop("operator", None))

    # Clear any previous stop request before starting a fresh run
    _run_stop_event.clear()

    def _worker():
        try:
            _emit("=" * 56, "header")
            _emit("▶  Processing run started", "header")
            _emit(f"   Operator: {operator}   ·   {_now_iso()}", "info")
            _emit("=" * 56, "header")

            # Reuse cached integrators
            try:
                experiment = _get_experiment(config)
            except Exception as e:
                _emit(f"Failed to load integrators: {e}", "error")
                _emit("__RUN_DONE__", "sentinel")
                return

            # Record who ran this and when, into the manifest project metadata.
            _record_run_meta(experiment.data_directory.parent, operator, "run")

            counts = reduction_core.run_pipeline(
                config,
                log_callback      = _emit,
                processed_files   = _processed_files,
                experiment        = experiment,          # pass cached — not reloaded
                stop_event        = _run_stop_event,     # allows graceful mid-run stop
                file_done_callback = lambda result, raw_path, det: (
                    _register_reduced(result, raw_path, det, experiment, config, operator)
                ),
            )
            if counts.get("stopped"):
                _emit(
                    f"\n⏹  Run stopped by user — "
                    f"{counts['saxs_count']} SAXS + {counts['waxs_count']} WAXS processed",
                    "warn",
                )
            else:
                _emit(
                    f"\n✓  Done — {counts['saxs_count']} SAXS + "
                    f"{counts['waxs_count']} WAXS files processed",
                    "ok",
                )
        except Exception:
            _emit(traceback.format_exc(), "error")
            _invalidate_experiment()           # stale state — force reload next run
        finally:
            _emit("__RUN_DONE__", "sentinel")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/run/stop", methods=["POST"])
def run_stop():
    """
    Request a graceful stop of the currently active one-shot run.
    The pipeline finishes the file it is currently processing, then halts.
    Has no effect if no run is active.
    """
    _run_stop_event.set()
    _emit("⏹  Stop requested — finishing current file then halting…", "warn")
    return jsonify({"ok": True})


@app.route("/api/monitor/start", methods=["POST"])
def monitor_start():
    global _monitoring, _monitor_thread
    if _monitoring:
        return jsonify({"ok": False, "error": "Already monitoring"})

    data     = request.json or {}
    config   = data.get("config", {})
    interval = max(int(data.get("interval", 10)), 1)
    operator = _current_user(data.get("operator") or config.pop("operator", None))

    if not config:
        return jsonify({"ok": False, "error": "No config provided"}), 400

    _monitoring = True
    _emit(f"👁  Monitoring started — checking every {interval} s  ·  Operator: {operator}", "ok")

    def _loop():
        """
        Continuous monitor loop designed to run for days without crashing.

        Key properties:
        - Experiment created ONCE at loop start, reused every poll cycle.
        - Files processed strictly one at a time.
        - Per-file exceptions are caught and logged; the loop continues.
        - If the Experiment itself fails, it is recreated on the next cycle
          (with exponential back-off to avoid rapid retry storms).
        - gc.collect() is called after every file and every poll cycle.
        """
        experiment    = None
        backoff       = interval   # seconds to wait after a setup error
        MAX_BACKOFF   = 300        # cap at 5 minutes

        while _monitoring:
            # ── Ensure we have a working Experiment ──────────────────────
            if experiment is None:
                try:
                    experiment = _get_experiment(config)
                    backoff = interval   # reset back-off on success
                    _record_run_meta(experiment.data_directory.parent, operator, "monitor")
                except Exception as e:
                    _emit(f"⚠  Cannot load integrators: {e}", "error")
                    _emit(f"   Retrying in {backoff} s…", "warn")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    continue

            # ── Scan for new files ────────────────────────────────────────
            try:
                saxs_new, waxs_new = reduction_core.find_new_raw_files(
                    config, _processed_files
                )
            except Exception as e:
                _emit(f"⚠  File scan error: {e}", "error")
                time.sleep(interval)
                continue

            if not saxs_new and not waxs_new:
                _emit("  (no new files)", "info")
            else:
                _emit(
                    f"  New: {len(saxs_new)} SAXS + {len(waxs_new)} WAXS — processing…",
                    "ok",
                )
                # Notify event bus of newly discovered raw files
                if _bus is not None:
                    for f in saxs_new:
                        try:
                            _bus.emit_watch_new_raw(str(f), "saxs")
                        except Exception:
                            pass
                    for f in waxs_new:
                        try:
                            _bus.emit_watch_new_raw(str(f), "waxs")
                        except Exception:
                            pass

            # ── Process SAXS files — one at a time ────────────────────────
            for f in saxs_new:
                if not _monitoring:
                    break
                _emit(f"  SAXS  {f.name}", "info")
                try:
                    result = experiment.process_saxs_file(f)   # frees arrays inside
                    _processed_files.add(str(f))
                    _emit(reduction_core._fmt_result_line(result), "ok")
                    _register_reduced(result, f, "saxs", experiment, config, operator)
                except Exception as e:
                    _emit(f"  ✗  {f.name}: {e}", "error")
                    # Single bad file — log and continue; don't kill the loop
                gc.collect()

            # ── Process WAXS files — one at a time ────────────────────────
            for f in waxs_new:
                if not _monitoring:
                    break
                _emit(f"  WAXS  {f.name}", "info")
                try:
                    result = experiment.process_waxs_file(f)
                    _processed_files.add(str(f))
                    _emit(reduction_core._fmt_result_line(result), "ok")
                    _register_reduced(result, f, "waxs", experiment, config, operator)
                except Exception as e:
                    _emit(f"  ✗  {f.name}: {e}", "error")
                gc.collect()

            # ── End-of-cycle cleanup ──────────────────────────────────────
            gc.collect()
            time.sleep(interval)

        _emit("⏹  Monitoring stopped", "warn")

    _monitor_thread = threading.Thread(target=_loop, daemon=True)
    _monitor_thread.start()
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
def monitor_stop():
    global _monitoring
    _monitoring = False
    return jsonify({"ok": True})


@app.route("/api/monitor/status")
def monitor_status():
    return jsonify({"monitoring": _monitoring})


@app.route("/api/reset", methods=["POST"])
def reset_processed():
    _processed_files.clear()
    _emit("♻  Processed-files list cleared — all files will reprocess on next run", "warn")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# Routes — SSE
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/stream")
def stream():
    client_q: queue.Queue = queue.Queue(maxsize=500)
    for item in list(_log_buffer)[-100:]:
        client_q.put_nowait(item)
    with _sse_lock:
        _sse_clients.append(client_q)

    def _generate():
        try:
            while True:
                try:
                    item = client_q.get(timeout=20)
                    yield f"data: {json.dumps(item)}\n\n"
                except queue.Empty:
                    yield "data: {\"tag\": \"ping\"}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(client_q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routes — config YAML
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/save-config", methods=["POST"])
def save_config():
    data = request.json or {}
    cfg  = data.get("config", {})
    path = data.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "No path specified"}), 400
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/load-config", methods=["POST"])
def load_config():
    path = (request.json or {}).get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "No path specified"}), 400
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return jsonify({"ok": True, "config": cfg})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Routes — 1-D data
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/list-dat")
def list_dat():
    dir_path = request.args.get("dir", "")
    try:
        p = Path(dir_path)
        if not p.is_dir():
            return jsonify({"files": [], "error": f"Not a directory: {dir_path}"})
        files = sorted(
            f.name for f in p.iterdir()
            if f.suffix.lower() == ".dat" and not f.name.startswith("._")
        )
        return jsonify({"files": files, "dir": str(p)})
    except Exception as e:
        return jsonify({"files": [], "error": str(e)})


@app.route("/api/dat-data")
def dat_data():
    file_path = request.args.get("file", "")
    try:
        p = Path(file_path)
        data_rows, metadata, in_meta = [], {}, False

        with open(p, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    if "METADATA INFORMATION" in line:
                        in_meta = True
                        continue
                    if in_meta and ":" in line:
                        key, _, val = line[1:].strip().partition(":")
                        try:
                            metadata[key.strip()] = float(val.strip())
                        except ValueError:
                            pass
                    continue
                parts = line.split()
                if parts and any(c.isalpha() for c in parts[0]):
                    continue
                if len(parts) >= 2:
                    try:
                        row = [float(x) for x in parts[:3]]
                        if len(row) == 2:
                            row.append(0.0)
                        data_rows.append(row)
                    except ValueError:
                        pass

        if not data_rows:
            return jsonify({"error": f"No numeric data in {p.name}"}), 400

        arr = np.array(data_rows)
        result = {
            "name":      p.name,
            "q":         arr[:, 0].tolist(),
            "intensity": arr[:, 1].tolist(),
            "error":     arr[:, 2].tolist(),
            "metadata":  metadata,
            "n_points":  len(data_rows),
        }
        del arr, data_rows
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Routes — 2-D detector image
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/list-raw")
def list_raw():
    dir_path = request.args.get("dir", "")
    try:
        p = Path(dir_path)
        if not p.is_dir():
            return jsonify({"files": [], "error": f"Not a directory: {dir_path}"})
        files = sorted(
            f.name for f in p.iterdir()
            if f.suffix.lower() == ".raw" and not f.name.startswith("._")
        )
        return jsonify({"files": files, "dir": str(p)})
    except Exception as e:
        return jsonify({"files": [], "error": str(e)})


@app.route("/api/raw-image")
def raw_image():
    """
    Render a .raw detector file as base64 PNG.
    - Render runs in a dedicated thread pool (not a Flask request thread).
    - Results are cached (LRU, max 30) so replaying the same file is instant.
    - Debouncing on the frontend ensures we don't queue renders on every
      slider pixel.
    """
    file_path = request.args.get("file", "")
    rows      = int(request.args.get("rows", 1043))
    cols      = int(request.args.get("cols", 981))
    cmap      = request.args.get("cmap", "viridis")
    log_scale = request.args.get("log", "true").lower() == "true"
    clip_pct  = float(request.args.get("clip", 2.0))

    cache_key = hashlib.md5(
        f"{file_path}|{rows}|{cols}|{cmap}|{log_scale}|{clip_pct}".encode()
    ).hexdigest()

    if cache_key in _img_cache:
        _img_cache.move_to_end(cache_key)
        return jsonify(_img_cache[cache_key])

    try:
        future = _render_pool.submit(
            _render_image, file_path, rows, cols, cmap, log_scale, clip_pct
        )
        result = future.result(timeout=60)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if "error" in result:
        return jsonify(result), 400

    _img_cache[cache_key] = result
    if len(_img_cache) > _IMG_CACHE_MAX:
        _img_cache.popitem(last=False)

    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("  ┌─────────────────────────────────────────────────────────────┐")
    print("  │   SWAXS Reduction & Correction App                          │")
    print("  │   Open  http://localhost:5001  in a browser                 │")
    print("  │                                                             │")
    print("  │   For multi-day runs, use gunicorn instead:                 │")
    print("  │   uv run gunicorn -w 1 --threads 8 -b 127.0.0.1:5001 \\     │")
    print("  │       'reduction.app:app'                                   │")
    print("  │                                                             │")
    print("  │   Press  Ctrl-C  to stop                                    │")
    print("  └─────────────────────────────────────────────────────────────┘")
    print()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
