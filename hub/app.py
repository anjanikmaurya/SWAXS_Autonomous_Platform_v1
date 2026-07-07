"""
hub/app.py — SWAXS Platform Hub (port 5000)
============================================
Central launcher: reads apps.yml to discover sub-apps, starts/stops them as
independent subprocesses, streams live status via SSE, lets the user pick
the project folder, and serves the WebSocket event bus at /ws.

Run:  uv run hub/app.py
Open: http://localhost:5000

Event bus
---------
All sub-apps connect to ws://localhost:5000/ws on startup.
The hub broadcasts every incoming message to all other connected apps and
appends it to manifest["events"] (rolling last 100) if a project is active.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template, request

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Load .env into os.environ before anything else reads it ──────────────────
# This makes the hub self-sufficient regardless of how it was launched
# (./start_platform.sh, uv run hub/app.py, IDE, etc.).
def _load_dotenv(dotenv_path: Path) -> None:
    """Minimal .env loader — no external dependencies required."""
    if not dotenv_path.is_file():
        return
    with dotenv_path.open() as _fh:
        for _line in _fh:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key = _key.strip()
            _val = _val.strip().strip('"').strip("'")
            if _key and _key not in os.environ:   # don't override real env vars
                os.environ[_key] = _val

_load_dotenv(_ROOT / ".env")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [hub] %(levelname)s %(message)s")
logger = logging.getLogger("swaxs_platform")

app = Flask(__name__)

# ── flask-sock (WebSocket event bus) ─────────────────────────────────────────
try:
    from flask_sock import Sock
    sock = Sock(app)
    _SOCK_AVAILABLE = True
except ImportError:
    sock = None
    _SOCK_AVAILABLE = False
    logger.warning(
        "flask-sock not installed — WebSocket event bus unavailable. "
        "Install with: pip install flask-sock"
    )

# ── App registry ──────────────────────────────────────────────────────────────

_APPS_YML = _ROOT / "apps.yml"

# Defaults applied to any apps.yml entry that omits optional fields
_APP_DEFAULTS: dict = {
    "description": "",
    "icon":        "🔧",
    "icon_image":  None,    # optional path/URL to an image icon (overrides emoji)
    "color":       "#455A64",
    "knowledge":   None,
    "manifest_key": None,
}


def _load_apps() -> list[dict]:
    """
    Load the app registry from apps.yml.
    Falls back to an empty list (with a warning) if the file is missing.
    """
    if not _APPS_YML.exists():
        logger.warning(
            "apps.yml not found at %s — no sub-apps registered. "
            "Create apps.yml to register apps.", _APPS_YML
        )
        return []
    try:
        with _APPS_YML.open() as fh:
            cfg = yaml.safe_load(fh)
        entries = cfg.get("apps", [])
        # Apply defaults for any omitted optional fields
        return [{**_APP_DEFAULTS, **entry} for entry in entries]
    except Exception as exc:
        logger.error("Failed to parse apps.yml: %s", exc)
        return []


# Load once at startup
APPS: list[dict] = _load_apps()

# Runtime process table  {app_id: Popen | None}
_procs: dict[str, subprocess.Popen | None] = {a["id"]: None for a in APPS}

# Currently selected project root (set via /api/set_project).
# Persisted to a small state file so the hub REMEMBERS the folder across
# restarts (otherwise every restart forgets it and manifests look "empty").
_STATE_FILE = _ROOT / ".hub_state.json"


def _load_project_state() -> str:
    """Return the last-used project_root if it still exists, else ''."""
    try:
        if _STATE_FILE.is_file():
            data = json.loads(_STATE_FILE.read_text())
            path = str(data.get("project_root", "")).strip()
            if path and Path(path).is_dir():
                logger.info("Restored project folder from state: %s", path)
                return path
            if path:
                logger.warning("Saved project folder no longer exists: %s", path)
    except Exception as exc:
        logger.debug("Could not read hub state %s: %s", _STATE_FILE, exc)
    return ""


def _save_project_state(path: str) -> None:
    """Persist the selected project_root so it survives a hub restart."""
    try:
        _STATE_FILE.write_text(json.dumps({"project_root": path}, indent=2))
    except Exception as exc:
        logger.warning("Could not save hub state %s: %s", _STATE_FILE, exc)


_project_root: str = _load_project_state()

# ── WebSocket event bus state ─────────────────────────────────────────────────
_ws_clients: set = set()
_ws_lock     = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _app_by_id(app_id: str) -> dict | None:
    return next((a for a in APPS if a["id"] == app_id), None)


def _is_running(app_id: str) -> bool:
    proc = _procs.get(app_id)
    return proc is not None and proc.poll() is None


def _health_check(port: int, timeout: float = 1.0) -> bool:
    try:
        url = f"http://localhost:{port}/api/health"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _health_summary(port: int, timeout: float = 1.0) -> dict | None:
    """Return a short status summary an app exposes on /api/health (e.g. the
    Quality Gate's good/bad counts), or None.  Best-effort, never raises."""
    try:
        url = f"http://localhost:{port}/api/health"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode() or "{}")
        if "good" in data and "bad" in data:
            return {"good": data.get("good", 0), "bad": data.get("bad", 0),
                    "graded": data.get("graded", 0)}
    except Exception:
        pass
    return None


def _start_app(app_id: str) -> tuple[bool, str]:
    """Launch the sub-app process. Returns (success, message)."""
    meta = _app_by_id(app_id)
    if meta is None:
        return False, f"Unknown app: {app_id}"
    if _is_running(app_id):
        return True, "Already running"

    entry = _ROOT / meta["entry"]
    if not entry.exists():
        return False, f"Entry file not found: {entry}"

    env = os.environ.copy()
    if _project_root:
        env["SWAXS_PROJECT"] = _project_root

    import shutil
    uv_path = shutil.which("uv")

    def _launch(cmd):
        return subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if uv_path:
        try:
            proc = _launch([uv_path, "run", str(entry)])
            _procs[app_id] = proc
            _hub_emit("app.started", {"app_id": app_id, "pid": proc.pid})
            return True, f"Started (PID {proc.pid})"
        except Exception:
            pass

    try:
        proc = _launch([sys.executable, str(entry)])
        _procs[app_id] = proc
        _hub_emit("app.started", {"app_id": app_id, "pid": proc.pid})
        return True, f"Started (PID {proc.pid})"
    except Exception as exc:
        return False, str(exc)


def _stop_app(app_id: str) -> tuple[bool, str]:
    proc = _procs.get(app_id)
    if proc is None or proc.poll() is not None:
        _procs[app_id] = None
        return True, "Not running"
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    _procs[app_id] = None
    _hub_emit("app.stopped", {"app_id": app_id})
    return True, "Stopped"


# ── Event bus helpers ─────────────────────────────────────────────────────────

def _broadcast(event: dict, exclude=None) -> None:
    """Broadcast an event dict to all connected WebSocket clients."""
    dead: set = set()
    with _ws_lock:
        clients = set(_ws_clients)
    for client in clients:
        if client is exclude:
            continue
        try:
            client.send(json.dumps(event))
        except Exception:
            dead.add(client)
    if dead:
        with _ws_lock:
            _ws_clients.difference_update(dead)


def _append_event_to_manifest(event: dict) -> None:
    """Write an event to manifest["events"] if a project is active."""
    if not _project_root:
        return
    try:
        from src.manifest import update_manifest, add_event
        update_manifest(_project_root, lambda m: add_event(
            m,
            event_type  = event.get("type", "unknown"),
            source_app  = event.get("source_app", "unknown"),
            data        = event.get("data", {}),
            ai_triggered= event.get("ai_triggered", False),
        ))
    except Exception as exc:
        logger.debug("Failed to append event to manifest: %s", exc)


def _hub_emit(event_type: str, data: dict) -> None:
    """Publish a hub-originated event onto the bus and into the manifest."""
    from datetime import datetime, timezone
    event = {
        "type":         event_type,
        "source_app":   "hub",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "data":         data,
        "ai_triggered": False,
    }
    _broadcast(event)
    _append_event_to_manifest(event)


# ── WebSocket event bus endpoint ──────────────────────────────────────────────

if _SOCK_AVAILABLE and sock is not None:
    @sock.route("/ws")
    def ws_event_bus(ws):
        """
        WebSocket event broker.
        Each connected app sends events here; hub broadcasts to all others
        and writes to manifest["events"].
        """
        with _ws_lock:
            _ws_clients.add(ws)
        logger.debug("[Hub WS] Client connected (total=%d)", len(_ws_clients))
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                try:
                    event = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                logger.debug("[Hub WS] ← %s from %s",
                             event.get("type"), event.get("source_app"))
                # Persist to manifest and broadcast
                _append_event_to_manifest(event)
                _broadcast(event, exclude=ws)
        except Exception as exc:
            logger.debug("[Hub WS] Client disconnected: %s", exc)
        finally:
            with _ws_lock:
                _ws_clients.discard(ws)
            logger.debug("[Hub WS] Client removed (total=%d)", len(_ws_clients))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", apps=APPS)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "hub"})


@app.route("/api/status")
def api_status():
    """Snapshot status of all apps (used for initial page load)."""
    out = {}
    for a in APPS:
        running = _is_running(a["id"])
        alive   = _health_check(a["port"]) if running else False
        out[a["id"]] = {
            "running": running,
            "healthy": alive,
            "port":    a["port"],
            "pid":     _procs[a["id"]].pid if running else None,
            "summary": _health_summary(a["port"]) if alive else None,
        }
    return jsonify({
        "apps":           out,
        "project_root":   _project_root,
        "event_bus":      _SOCK_AVAILABLE,
        "ws_clients":     len(_ws_clients),
    })


@app.route("/api/status/stream")
def api_status_stream():
    """SSE stream — pushes a status JSON every 2 seconds."""
    def generate():
        while True:
            out = {}
            for a in APPS:
                running = _is_running(a["id"])
                alive   = _health_check(a["port"]) if running else False
                out[a["id"]] = {
                    "running": running,
                    "healthy": alive,
                    "port":    a["port"],
                    "pid":     _procs[a["id"]].pid if running else None,
                    "summary": _health_summary(a["port"]) if alive else None,
                }
            payload = json.dumps({
                "apps":         out,
                "project_root": _project_root,
                "ws_clients":   len(_ws_clients),
            })
            yield f"data: {payload}\n\n"
            time.sleep(2)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/start/<app_id>", methods=["POST"])
def api_start(app_id: str):
    ok, msg = _start_app(app_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/stop/<app_id>", methods=["POST"])
def api_stop(app_id: str):
    ok, msg = _stop_app(app_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/set_project", methods=["POST"])
def api_set_project():
    global _project_root
    body = request.get_json(force=True)
    path = body.get("path", "").strip()
    if path and Path(path).is_dir():
        _project_root = path
        _save_project_state(path)   # remember across hub restarts
        # Propagate to already-running sub-apps
        for a in APPS:
            if _is_running(a["id"]):
                try:
                    url  = f"http://localhost:{a['port']}/api/set_project"
                    data = json.dumps({"path": path}).encode()
                    req  = urllib.request.Request(
                        url, data=data,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=1)
                except Exception:
                    pass
        _hub_emit("project.set", {"path": _project_root})
        return jsonify({"ok": True, "path": _project_root})
    return jsonify({"ok": False, "message": "Invalid path"}), 400


@app.route("/api/browse")
def api_browse():
    """Directory browser for the project picker."""
    raw = request.args.get("path", "").strip()
    p   = Path(raw) if raw else Path.home()
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.is_dir():
        p = Path.home()
    try:
        dirs = sorted(
            d.name for d in p.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except PermissionError:
        dirs = []
    return jsonify({
        "current": str(p),
        "parent":  str(p.parent) if p != p.parent else None,
        "dirs":    dirs,
    })


@app.route("/api/apps/reload", methods=["POST"])
def api_reload_apps():
    """
    Reload apps.yml without restarting the Hub.
    New apps are added to the registry; removed apps are left in _procs
    (still manageable until their process exits).
    """
    global APPS, _procs
    new_apps = _load_apps()
    existing_ids = {a["id"] for a in APPS}
    for a in new_apps:
        if a["id"] not in existing_ids:
            _procs[a["id"]] = None
            logger.info("[Hub] Registered new app: %s (port %d)", a["id"], a["port"])
    APPS = new_apps
    return jsonify({"ok": True, "apps": [a["id"] for a in APPS]})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("━" * 58)
    print("  SWAXS Platform Hub")
    print("  → http://localhost:5000")
    if _SOCK_AVAILABLE:
        print("  → ws://localhost:5000/ws  (event bus)")
    print(f"  → {len(APPS)} app(s) registered from apps.yml")
    for a in APPS:
        print(f"      {a['icon']}  {a['name']}  :{a['port']}")
    print("━" * 58)
    app.run(debug=False, port=5000, threaded=True)
