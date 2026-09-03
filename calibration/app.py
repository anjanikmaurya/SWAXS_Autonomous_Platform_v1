#!/usr/bin/env python3
"""
calibration/app.py — Calibration & raw-prep Flask backend (port 5101)
=====================================================================
Pre-reduction utility:
  1. find calibrant .raw files by keyword and convert them to CBF (fabio),
  2. preview the pattern (log scale) to check the rings, and
  3. drive pyFAI to make the .poni files reduction uses — either by launching
     pyFAI-calib2 (interactive) or a best-effort headless auto-refine.

Thin shell: all logic is in src/preprocess.
"""
from __future__ import annotations

import base64
import collections
import io
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import numpy as np
import yaml
from flask import Flask, render_template, request, jsonify

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.preprocess import (                                    # noqa: E402
    DEFAULT_SHAPES, find_raw_files, read_raw, convert_dir,
    CALIBRANTS, launch_calib2, list_poni_files,
    SftpSync, sftp_test, sftp_load_config, sftp_save_config,
)

app = Flask(__name__)
_project_root: str = os.environ.get("SWAXS_PROJECT", "")

# ── SFTP data-copy state (left panel) ─────────────────────────────────────────
_sync = None                                   # active SftpSync thread
_sync_lock = threading.Lock()
_sync_log = collections.deque(maxlen=400)      # (seq, level, msg)
_sync_seq = 0
_sync_status = {"text": "Not running", "color": "muted"}

_log_lock = threading.Lock()

def _sync_log_cb(level, msg):
    """Called from several transfer worker threads — must be serialised."""
    global _sync_seq
    with _log_lock:
        _sync_seq += 1
        _sync_log.append((_sync_seq, level, msg))

def _sync_status_cb(text, color):
    _sync_status["text"] = text; _sync_status["color"] = color


# ── project config helpers ────────────────────────────────────────────────────
def _project_cfg() -> dict:
    """Load the project config.yml (detector_shapes, energy_keV, poni_directory)."""
    if not _project_root:
        return {}
    p = Path(_project_root) / "config.yml"
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _shapes() -> dict:
    ds = (_project_cfg().get("detector_shapes") or {})
    out = {}
    for k, v in ds.items():
        try:
            out[k.upper()] = (int(v[0]), int(v[1]))
        except Exception:
            pass
    return out or DEFAULT_SHAPES


def _poni_dir() -> Path:
    cfg = _project_cfg()
    pd = cfg.get("poni_directory")
    if pd:
        return Path(pd)
    return Path(_project_root) / "poni" if _project_root else Path.cwd() / "poni"


def _preview_png(data, title) -> str:
    """Log-scale PNG (base64) of a 2-D pattern for the gallery."""
    d = np.asarray(data, float)
    fig = plt.figure(figsize=(3.2, 3.0), dpi=90)
    vmin = max(1.0, float(np.nanmin(d[d > 0])) if np.any(d > 0) else 1.0)
    plt.imshow(d, cmap="hot", norm=LogNorm(vmin=vmin, vmax=max(vmin + 1, float(np.nanmax(d)))))
    plt.title(title, fontsize=8); plt.axis("off")
    plt.tight_layout(pad=0.2)
    buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ── routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "calibration"})


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    p = (request.get_json(force=True).get("path", "") or "").strip()
    if p:
        os.environ["SWAXS_PROJECT"] = p
        _project_root = p
    return jsonify({"ok": True})


@app.route("/api/project")
def api_project():
    return jsonify({"project_root": _project_root, "poni_dir": str(_poni_dir()),
                    "shapes": {k: list(v) for k, v in _shapes().items()},
                    "energy_keV": _project_cfg().get("energy_keV")})


@app.route("/api/calibrants")
def api_calibrants():
    return jsonify({"calibrants": CALIBRANTS})


@app.route("/api/list_raw", methods=["POST"])
def api_list_raw():
    b = request.get_json(force=True)
    raw_dir = Path((b.get("raw_dir", "") or "").strip())
    if not raw_dir.is_dir():
        return jsonify({"error": f"folder not found: {raw_dir}"}), 400
    kws = b.get("keywords") or []
    shapes = _shapes()
    files = []
    for name in find_raw_files(raw_dir, kws):
        size = (raw_dir / name).stat().st_size // 4      # int32
        det = None
        for d, sh in shapes.items():
            if size == sh[0] * sh[1]:
                det = d; break
        files.append({"name": name, "path": str(raw_dir / name),
                      "pixels": int(size), "detector": det or "?"})
    return jsonify({"files": files, "count": len(files)})


@app.route("/api/convert", methods=["POST"])
def api_convert():
    b = request.get_json(force=True)
    raw_dir = Path((b.get("raw_dir", "") or "").strip())
    if not raw_dir.is_dir():
        return jsonify({"error": f"folder not found: {raw_dir}"}), 400
    kws = b.get("keywords") or []
    shapes = _shapes()
    results, out_dir = convert_dir(raw_dir, kws, shapes)
    if b.get("preview", True):
        for r in results:
            if r.get("ok"):
                try:
                    _, data = read_raw(raw_dir / r["file"], shapes)
                    r["png"] = _preview_png(data, f"{r['detector']} · {r['file']}")
                except Exception:
                    pass
    return jsonify({"results": results, "out_dir": out_dir})


@app.route("/api/calibrate/launch", methods=["POST"])
def api_calib_launch():
    """Open the pyFAI-calib2 GUI preloaded with the image, calibrant and energy.
    The GUI's cwd is the project poni/ folder so its save dialog lands there."""
    b = request.get_json(force=True)
    cbf = (b.get("cbf", "") or "").strip()
    if not cbf:
        return jsonify({"ok": False, "error": "no image given"}), 400
    if not Path(cbf).is_file():
        return jsonify({"ok": False, "error": f"file not found: {cbf}"}), 400
    poni_dir = _poni_dir()
    ok, msg, cmd = launch_calib2(cbf, b.get("calibrant", "AgBehenate"),
                                 b.get("energy_keV", 12.0),
                                 pixel_um=float(b.get("pixel_um", 172.0)),
                                 workdir=str(poni_dir))
    return jsonify({"ok": ok, "message": msg, "command": cmd, "poni_dir": str(poni_dir)})


@app.route("/api/poni")
def api_poni():
    return jsonify({"poni_dir": str(_poni_dir()), "files": list_poni_files(_poni_dir())})


@app.route("/api/browse")
def api_browse():
    """List sub-directories of ``path`` so the UI can pick an absolute folder.
    Defaults to the project root (else the user's home)."""
    raw = (request.args.get("path", "") or "").strip()
    base = Path(raw) if raw else (Path(_project_root) if _project_root else Path.home())
    try:
        base = base.expanduser().resolve()
    except Exception:
        base = Path.home()
    if not base.is_dir():
        base = base.parent if base.parent.is_dir() else Path.home()
    dirs, files = [], []
    try:
        for c in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if c.name.startswith(".") or c.name == "__pycache__":
                continue
            (dirs if c.is_dir() else files).append(c.name)
    except PermissionError:
        pass
    return jsonify({"path": str(base), "parent": str(base.parent), "dirs": dirs, "files": files})


# ── SFTP data copy ────────────────────────────────────────────────────────────
@app.route("/api/sftp/config")
def api_sftp_config():
    cfg = sftp_load_config()
    return jsonify({"config": cfg, "running": _sync is not None and _sync.is_alive()})


@app.route("/api/sftp/test", methods=["POST"])
def api_sftp_test():
    ok, msg = sftp_test(request.get_json(force=True) or {})
    _sync_log_cb("OK" if ok else "ERR", f"[{datetime.now():%H:%M:%S}] {msg}")
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/sftp/start", methods=["POST"])
def api_sftp_start():
    global _sync
    cfg = request.get_json(force=True) or {}
    missing = [k for k in ("host", "username", "remote_dir", "local_dir") if not str(cfg.get(k, "")).strip()]
    if missing:
        return jsonify({"ok": False, "error": f"missing: {', '.join(missing)}"}), 400
    if not Path(cfg["local_dir"]).is_dir():
        return jsonify({"ok": False, "error": "local folder does not exist"}), 400
    with _sync_lock:
        if _sync is not None and _sync.is_alive():
            return jsonify({"ok": False, "error": "already running"}), 400
        sftp_save_config(cfg)                       # persists everything except the password
        _sync = SftpSync(cfg, log_cb=_sync_log_cb, status_cb=_sync_status_cb)
        _sync.start()
    return jsonify({"ok": True})


@app.route("/api/sftp/stop", methods=["POST"])
def api_sftp_stop():
    global _sync
    with _sync_lock:
        if _sync is not None:
            _sync.stop()
            _sync.join(timeout=3.0)      # wait for the poll loop to exit before
            _sync = None                 # releasing the handle (prevents overlap)
    _sync_status_cb("Stopped", "muted")
    return jsonify({"ok": True})


@app.route("/api/sftp/status")
def api_sftp_status():
    since = int(request.args.get("since", 0))
    new = [{"seq": s, "level": lv, "msg": m} for (s, lv, m) in list(_sync_log) if s > since]
    sync = _sync
    return jsonify({"running": sync is not None and sync.is_alive(),
                    "status": _sync_status, "logs": new, "seq": _sync_seq,
                    "progress": sync.progress() if sync is not None else None})


if __name__ == "__main__":
    _project_root = os.environ.get("SWAXS_PROJECT", "")
    print("━" * 52)
    print("  Calibration & raw-prep  ·  http://localhost:5101")
    print("━" * 52)
    app.run(host="127.0.0.1", port=5101, debug=False, threaded=True)
