"""
reactor/app.py — Flow Synthesis (port 5007)
============================================
Pump-control / execution layer for the 5-pump continuous-flow nanoparticle
reactor (Fong et al., J. Chem. Phys. 154, 224201, 2021).  Receives an
already-predicted recipe (folder / JSON API / form) and drives the pumps; the
BO/SAXS optimization itself lives elsewhere.

All hardware + run logic is in src/reactor/.  This file is a thin Flask shell:
routes, SSE, the recipes-folder watcher, and the hub event-bus wiring.

Run:  uv run reactor/app.py    Open: http://localhost:5007
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response

# ── sys.path ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.reactor import load_config, ReactorController, RecipeError   # noqa: E402
from src.manifest import update_manifest, add_reactor_run            # noqa: E402

# ── Event bus (graceful degradation) ─────────────────────────────────────────
try:
    from src.events import EventBusClient as _EventBusClient
    _bus = _EventBusClient("reactor").connect(retry=True)
except Exception:
    _bus = None

app = Flask(__name__)

_project_root: str = ""
_CFG = load_config()
_BACKEND = os.environ.get("SWAXS_REACTOR_BACKEND", "mock")   # "mock" | "real"

# ── log buffer (fed by the controller, streamed over SSE) ─────────────────────
_log: collections.deque = collections.deque(maxlen=500)
_seq = 0
_log_lock = threading.Lock()


def _emit(msg: str, tag: str = "info") -> None:
    global _seq
    with _log_lock:
        _seq += 1
        _log.append((_seq, {"ts": datetime.datetime.now().strftime("%H:%M:%S"),
                            "msg": msg, "tag": tag}))


def _resolve(folder_key: str) -> Path:
    """Resolve a config folder against the project root (or CWD)."""
    rel = _CFG.get("folders", {}).get(folder_key, folder_key)
    p = Path(rel)
    if not p.is_absolute():
        base = Path(_project_root) if _project_root else Path.cwd()
        p = base / rel
    return p


# ── controller callbacks ──────────────────────────────────────────────────────
def _event_cb(etype: str, data: dict) -> None:
    if _bus is not None:
        try:
            _bus.publish(etype, data)
        except Exception:
            pass


def _feedback_cb(recipe_id: str, payload: dict) -> None:
    """Write <recipe_id>.done.json so the BO/SAXS side knows the run finished."""
    try:
        fb = _resolve("feedback")
        fb.mkdir(parents=True, exist_ok=True)
        (fb / f"{recipe_id}.done.json").write_text(json.dumps(payload, indent=2, default=str))
    except Exception as exc:
        _emit(f"⚠ could not write feedback file: {exc}", "warn")


def _manifest_cb(record: dict) -> None:
    if _project_root:
        try:
            update_manifest(_project_root, lambda m: add_reactor_run(m, record=record))
        except Exception as exc:
            _emit(f"⚠ manifest update failed: {exc}", "warn")


_ctrl = ReactorController(_CFG, backend=_BACKEND, log_cb=_emit,
                          event_cb=_event_cb, feedback_cb=_feedback_cb,
                          manifest_cb=_manifest_cb)
_emit(f"Flow Synthesis ready — backend={_BACKEND}", "ok")


# ── hub bus: end the run when SAXS produces a new averaged file ───────────────
def _on_bus_event(event: dict) -> None:
    etype = event.get("type") or event.get("event_type") or ""
    if etype == "file.averaged":
        data = event.get("data", event)
        _ctrl.signal_measurement_complete(str(data.get("file_path", "")))


if _bus is not None:
    try:
        _bus.on_event(_on_bus_event)
    except Exception:
        pass


# ── recipes-folder watcher (backstop to the API) ─────────────────────────────
_watch_seen: set = set()


def _folder_watcher() -> None:
    interval = float(_CFG.get("poll_interval", 3.0))
    while True:
        try:
            rdir = _resolve("recipes")
            if rdir.is_dir():
                for f in sorted(rdir.glob("*.json")):
                    if str(f) in _watch_seen:
                        continue
                    _watch_seen.add(str(f))
                    try:
                        data = json.loads(f.read_text() or "{}")
                        _ctrl.submit(data, source=f"folder:{f.name}")
                        done = _resolve("processed"); done.mkdir(parents=True, exist_ok=True)
                        f.rename(done / f.name)
                    except RecipeError as e:
                        _emit(f"✗ rejected {f.name}: {e}", "error")
                    except Exception as e:
                        _emit(f"⚠ {f.name}: {e}", "warn")
        except Exception:
            pass
        time.sleep(interval)


threading.Thread(target=_folder_watcher, daemon=True).start()


# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    s = _ctrl.status()
    return jsonify({"status": "ok", "app": "reactor",
                    "state": s["state"], "queue": s["queue_len"],
                    "runs": s["runs_completed"]})


def _limits_path() -> Path | None:
    return Path(_project_root) / "reactor_limits.json" if _project_root else None


def _save_limits(limits: dict) -> None:
    p = _limits_path()
    if p is None:
        return
    try:
        p.write_text(json.dumps({"limits": limits}, indent=2))
    except Exception as exc:
        _emit(f"⚠ could not save reactor_limits.json: {exc}", "warn")


def _load_limits() -> None:
    p = _limits_path()
    if p is None or not p.is_file():
        return
    try:
        data = json.loads(p.read_text() or "{}").get("limits", {})
        if data:
            _ctrl.set_pump_limits(data)
            _emit(f"loaded saved pump flow limits for {len(data)} pump(s)", "info")
    except Exception as exc:
        _emit(f"⚠ could not load reactor_limits.json: {exc}", "warn")


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    body = request.get_json(force=True)
    p = (body.get("path", "") or "").strip()
    if p:
        os.environ["SWAXS_PROJECT"] = p
        _project_root = p
        _load_limits()          # pick up saved per-pump flow limits
    return jsonify({"ok": True})


@app.route("/api/pumps", methods=["GET", "POST"])
def api_pumps():
    """GET current per-pump flow limits; POST {limits:{pump:{sensor_min,max_flow}}}."""
    if request.method == "POST":
        body = request.get_json(force=True)
        try:
            out = _ctrl.set_pump_limits(body.get("limits", {}))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        _save_limits(out)
        return jsonify({"ok": True, "limits": out})
    return jsonify({"limits": _ctrl.pump_limits()})


@app.route("/api/project")
def api_project():
    return jsonify({"project_root": _project_root})


@app.route("/api/config")
def api_config():
    """Expose bounds / pump names / flush defaults for the UI form."""
    from src.reactor.config import PUMP_NAMES
    return jsonify({"pumps": PUMP_NAMES, "bounds": _CFG.get("bounds", {}),
                    "flush": _CFG.get("flush", {}), "backend": _BACKEND})


@app.route("/api/recipe", methods=["POST"])
def api_recipe():
    """Submit a recipe as JSON (BO/SAXS push) or form fields."""
    data = request.get_json(silent=True) or request.form.to_dict()
    src = "form" if request.form else "api"
    try:
        out = _ctrl.submit(data, source=src)
        return jsonify({"ok": True, **out})
    except RecipeError as e:
        _emit(f"✗ rejected recipe: {e}", "error")
        return jsonify({"ok": False, "error": str(e)}), 400


def _simple(fn):
    try:
        return jsonify({"ok": bool(fn())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/start", methods=["POST"])
def api_start():   return _simple(_ctrl.start)


@app.route("/api/stop", methods=["POST"])
def api_stop():    return _simple(_ctrl.stop)


@app.route("/api/abort", methods=["POST"])
def api_abort():   _ctrl.abort();  return jsonify({"ok": True})


@app.route("/api/estop", methods=["POST"])
def api_estop():   _ctrl.estop();  return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():   _ctrl.reset();  return jsonify({"ok": True})


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    return jsonify({"ok": True, "cleared": _ctrl.clear_queue()})


@app.route("/api/flush", methods=["POST"])
def api_flush():
    b = request.get_json(silent=True) or {}
    rate = b.get("rate"); dur = b.get("duration")
    ok = _ctrl.flush_now(float(rate) if rate else None, float(dur) if dur else None)
    return jsonify({"ok": ok})


@app.route("/api/prime", methods=["POST"])
def api_prime():
    b = request.get_json(silent=True) or {}
    rate = b.get("rate"); dur = b.get("duration")
    ok = _ctrl.prime(float(rate) if rate else None, float(dur) if dur else None)
    return jsonify({"ok": ok})


@app.route("/api/auto_run", methods=["POST"])
def api_auto_run():
    b = request.get_json(force=True)
    _ctrl.set_auto_run(bool(b.get("on", False)))
    return jsonify({"ok": True, "auto_run": _ctrl.auto_run})


@app.route("/api/status")
def api_status():
    return jsonify(_ctrl.status())


@app.route("/api/stream")
def api_stream():
    """SSE: pushes {status, logs[]} ~2×/s."""
    def gen():
        last = 0
        while True:
            with _log_lock:
                new = [ln for (s, ln) in _log if s > last]
                if _log:
                    last = _log[-1][0]
            yield "data: " + json.dumps({"status": _ctrl.status(), "logs": new}) + "\n\n"
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _project_root = os.environ.get("SWAXS_PROJECT", "")
    print("━" * 52)
    print("  Flow Synthesis (reactor)  ·  http://localhost:5007")
    print(f"  backend = {_BACKEND}   (set SWAXS_REACTOR_BACKEND=real for hardware)")
    print("━" * 52)
    app.run(host="127.0.0.1", port=5007, debug=False, threaded=True)
