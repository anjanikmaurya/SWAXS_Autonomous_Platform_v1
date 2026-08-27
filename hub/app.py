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

import atexit
import json
import logging
import os
import signal
import socket
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

from src import proc_lifecycle as pl        # noqa: E402  (needs sys.path above)

# ── Load .env into os.environ before anything else reads it ──────────────────
# This makes the hub self-sufficient regardless of how it was launched
# (./start_platform.sh, uv run hub/app.py, IDE, etc.).
def _load_dotenv(dotenv_path: Path) -> None:
    """Minimal .env loader — no external dependencies required."""
    if not dotenv_path.is_file():
        return
    with dotenv_path.open(encoding="utf-8") as _fh:
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

#: The hub's own port. Overridable because 5000 collides with AirPlay Receiver on
#: macOS and with assorted dev servers elsewhere; an operator needs an escape
#: hatch that does not involve editing code.
try:
    _HUB_PORT = int(os.environ.get("SWAXS_HUB_PORT", "5000") or 5000)
except ValueError:
    _HUB_PORT = 5000

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
        with _APPS_YML.open(encoding="utf-8") as fh:
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
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
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
        _STATE_FILE.write_text(json.dumps({"project_root": path}, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not save hub state %s: %s", _STATE_FILE, exc)


_project_root: str = _load_project_state()

# ── WebSocket event bus state ─────────────────────────────────────────────────
_ws_clients: set = set()
_ws_lock     = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _app_by_id(app_id: str) -> dict | None:
    return next((a for a in APPS if a["id"] == app_id), None)


# ── child registry (survives a hub SIGKILL) ──────────────────────────────────
# The hub's atexit handler cannot run if the hub is SIGKILLed or the machine loses
# power. Without a record on disk, the next hub has no idea nine of its apps are
# still running: every port is taken, the UI says "Stopped", and those orphans
# keep polling folders and writing to the project (platform audit O11).
_CHILDREN_FILE = _ROOT / "logs" / "hub_children.json"


def _record_children() -> None:
    """Snapshot which app is which PID. Cheap; called on every start/stop."""
    live = {}
    for a in APPS:
        p = _procs.get(a["id"])
        if p is not None and p.poll() is None:
            live[a["id"]] = {"pid": p.pid, "port": a["port"],
                             "entry": a["entry"], "started": time.time()}
    pl.write_children(_CHILDREN_FILE, live)


def _reap_previous_run() -> list[str]:
    """Kill anything left over from a previous hub, so this hub starts clean."""
    prev = pl.read_children(_CHILDREN_FILE)
    if not prev:
        return []
    apps = {a["id"]: {"port": a["port"], "entry": a["entry"]} for a in APPS}
    notes = pl.reap_orphans(prev, apps, _ROOT)
    for n in notes:
        logger.info("[Hub] reaped orphan from a previous run: %s", n)
    pl.write_children(_CHILDREN_FILE, {})
    return notes


def _is_running(app_id: str) -> bool:
    proc = _procs.get(app_id)
    return proc is not None and proc.poll() is None


def _port_in_use(port: int) -> bool:
    """True if something is already listening on localhost:port (e.g. an orphaned
    app from a previous hub run still holding it)."""
    return pl.port_in_use(port)


def _health_probe(port: int, timeout: float = 1.0) -> tuple[bool, dict | None]:
    """One request to /api/health → (alive, summary).

    This used to be two functions making two HTTP requests to the same endpoint,
    for every app, on every 2 s status tick — 18 requests per tick with nine apps.
    Worse, each blocks up to `timeout`, so a few wedged apps could push a single
    tick past the tick interval and stall the whole status stream. One request,
    and a short timeout, keeps the tick bounded.
    """
    try:
        url = f"http://localhost:{port}/api/health"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return False, None
            body = r.read(65536).decode("utf-8", "replace")
    except Exception:
        return False, None
    try:
        data = json.loads(body or "{}")
        if isinstance(data, dict) and "good" in data and "bad" in data:
            return True, {"good": data.get("good", 0), "bad": data.get("bad", 0),
                          "graded": data.get("graded", 0)}
    except Exception:
        pass                       # answered 200 but not with JSON we understand
    return True, None


def _health_check(port: int, timeout: float = 1.0) -> bool:
    return _health_probe(port, timeout)[0]


def _health_summary(port: int, timeout: float = 1.0) -> dict | None:
    return _health_probe(port, timeout)[1]


def _start_app(app_id: str) -> tuple[bool, str]:
    """Launch the sub-app process FRESH. Returns (success, message).

    "Fresh" means three things, all of which used to be missing:
      * anything still holding the port is dealt with first, not reported as an
        error the operator has no way to fix from the UI;
      * the child gets its own process group, so stopping it later kills the
        whole tree;
      * the previous log is rotated rather than truncated, so a start attempt
        never destroys the traceback from the crash that prompted it.
    """
    meta = _app_by_id(app_id)
    if meta is None:
        return False, f"Unknown app: {app_id}"
    if _is_running(app_id):
        return True, "Already running"

    entry = _ROOT / meta["entry"]
    if not entry.exists():
        return False, f"Entry file not found: {entry}"

    # A held port is normally OUR own orphan: a previous hub run that was killed,
    # or a stop that raced the socket teardown. Reclaim it (only when the holder
    # identifies as this app) instead of dead-ending the operator.
    note = ""
    if pl.port_in_use(meta["port"]):
        freed, why, killed = pl.reclaim_port(meta["port"], entry, _ROOT)
        if not freed:
            return False, why
        note = f" ({why})"
        if killed:
            _hub_emit("app.reclaimed", {"app_id": app_id, "port": meta["port"],
                                        "killed": [k["pid"] for k in killed]})
        logger.info("[Hub] %s: %s", app_id, why)

    env = os.environ.copy()
    if _project_root:
        env["SWAXS_PROJECT"] = _project_root
    # Force UTF-8 in the child so file reads/writes don't blow up on Windows,
    # where the default text encoding is the locale codepage (e.g. cp1252) and
    # chokes on non-ASCII content such as the emoji in knowledge.md / manifest.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # Capture each app's output to a log file (instead of discarding it) so
    # startup failures are diagnosable. See logs/<app_id>.log.
    log_dir = _ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{app_id}.log"
    pl.rotate_log(log_path)          # keep the previous run's traceback (audit O17)

    def _launch(cmd):
        logf = open(log_path, "w", encoding="utf-8")
        return subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            # own process group → stopping the app can signal the whole tree
            **pl.popen_kwargs(),
        )

    # Launch with the SAME interpreter that runs the hub. This guarantees the
    # app uses the identical environment (venv or uv) with all dependencies
    # available. Previously the hub tried `uv run <entry>` first, which spins
    # up a SEPARATE environment without the pip-installed deps when there is no
    # pyproject.toml — so the app died instantly with ModuleNotFoundError and
    # (with output going to DEVNULL) it looked like "nothing happened".
    try:
        proc = _launch([sys.executable, str(entry)])
        _procs[app_id] = proc
        _crashed.pop(app_id, None)
        _last_running[app_id] = True
        _record_children()
        _hub_emit("app.started", {"app_id": app_id, "pid": proc.pid})
        return True, f"Started (PID {proc.pid}){note}"
    except Exception as exc:
        return False, str(exc)


def _stop_app(app_id: str) -> tuple[bool, str]:
    """Stop the app and make sure nothing of it is left running.

    Previously this returned "Not running" whenever the hub's own Popen handle was
    gone — so closing an app that had been started by an EARLIER hub run did
    nothing at all, while that process kept polling folders and writing files. It
    also returned before the listening socket was released, so an immediate
    restart could fail to bind.
    """
    meta = _app_by_id(app_id)
    port = meta["port"] if meta else None
    entry = (_ROOT / meta["entry"]) if meta else ""
    proc = _procs.get(app_id)
    notes = []

    if proc is not None and proc.poll() is None:
        notes.append(pl.kill_tree(proc, grace=5.0))
    _procs[app_id] = None

    # Whatever the handle said, the port is the ground truth. If something is
    # still listening and it identifies as this app, it is an orphan of ours.
    if port is not None and pl.port_in_use(port):
        if not pl.wait_port_free(port, timeout=3.0):
            freed, why, killed = pl.reclaim_port(port, entry, _ROOT)
            notes.append(why)
            if killed:
                logger.info("[Hub] %s: %s", app_id, why)
            if not freed:
                _record_children()
                return False, "; ".join(n for n in notes if n)

    # A deliberate stop is not a crash: clear the edge state so the next status
    # tick sees no running→dead transition, and drop any stale crash badge.
    _last_running[app_id] = False
    _crashed.pop(app_id, None)
    _record_children()
    _hub_emit("app.stopped", {"app_id": app_id})
    detail = "; ".join(n for n in notes if n and n != "already-gone")
    if not detail:
        return True, "Not running"
    return True, f"Stopped ({detail})" if detail != "terminated" else "Stopped"


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


def _app_status() -> dict:
    """Per-app status, built once and used by both /api/status and the SSE stream.
    Two copies of this loop had already drifted apart — the stream reported
    crashes and the snapshot did not, so a page load right after a crash showed a
    plain "Stopped"."""
    out = {}
    for a in APPS:
        aid = a["id"]
        running = _is_running(aid)
        alive, summary = _health_probe(a["port"]) if running else (False, None)
        proc = _procs.get(aid)
        out[aid] = {
            "running": running,
            "healthy": alive,
            "port":    a["port"],
            "pid":     proc.pid if (running and proc is not None) else None,
            "summary": summary,
            "crashed": _crashed.get(aid),
        }
    return out


@app.route("/api/status")
def api_status():
    """Snapshot status of all apps (used for initial page load)."""
    _detect_crashes()
    out = _app_status()
    return jsonify({
        "apps":           out,
        "project_root":   _project_root,
        "event_bus":      _SOCK_AVAILABLE,
        "ws_clients":     len(_ws_clients),
    })


#: app_id → (exit_code, when) for a child that died on its own
_crashed: dict = {}
#: app_id → last observed running state, for edge detection
_last_running: dict = {}


def _disk_free_gb():
    """Free space on the project volume. A 24 h run writes thousands of 4 MB
    frames; filling the disk truncates .dat files that then look perfectly
    stable to every downstream watcher."""
    try:
        import shutil
        target = _project_root or str(_ROOT)
        return round(shutil.disk_usage(target).free / 1e9, 1)
    except Exception:
        return None


def _exit_reason(code) -> str:
    """A human exit reason. `None` used to be rendered as the word "null", which
    is what an operator saw on the card and could do nothing with."""
    if code is None:
        return "unknown"
    if code < 0:                                   # POSIX: killed by a signal
        try:
            return f"killed by {signal.Signals(-code).name}"
        except Exception:
            return f"killed by signal {-code}"
    return f"exit {code}"


def _log_tail(app_id: str, lines: int = 12) -> list[str]:
    """The last few log lines, so the crash can be diagnosed without leaving the
    hub. Reading a whole log would be wasteful; a tail is what is actually read."""
    try:
        p = _ROOT / "logs" / f"{app_id}.log"
        if not p.is_file():
            return []
        with p.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8192))
            txt = fh.read().decode("utf-8", "replace")
        return [ln.rstrip() for ln in txt.splitlines() if ln.strip()][-lines:]
    except Exception:
        return []


def _detect_crashes() -> None:
    """Notice a child that exited ON ITS OWN and say so LOUDLY.

    Previously nothing watched the subprocesses: if the reduction app died at
    02:00, frames kept landing, nothing processed them, the campaign advanced
    zero steps, and the hub card just went grey. An unattended run needs the
    crash surfaced the moment it happens.

    But "not running any more" is not the same as "crashed". `_stop_app` clears
    `_procs[aid]`, so the previous version saw a running→dead transition on every
    deliberate Stop, reported a crash, and — because the handle was already gone —
    had no exit code to show. That is where **"⚠ CRASHED (exit null)"** came from:
    the operator pressing Stop. A crash is now only ever reported when we STILL
    HOLD the handle and that process has exited by itself.
    """
    for a in APPS:
        aid = a["id"]
        proc = _procs.get(aid)
        if proc is None:
            # Deliberately stopped, or never started. Not a crash, and the state
            # was already cleared by _stop_app.
            _last_running[aid] = False
            continue
        code = proc.poll()
        running = code is None
        was = _last_running.get(aid)
        _last_running[aid] = running
        if was and not running:                     # exited without being asked
            reason = _exit_reason(code)
            _crashed[aid] = {"exit_code": code, "reason": reason,
                             "at": time.time(), "tail": _log_tail(aid)}
            _procs[aid] = None                      # reap; the handle is spent
            logger.error("APP CRASHED: %s %s — see logs/%s.log", aid, reason, aid)
            for ln in _crashed[aid]["tail"][-4:]:
                logger.error("    %s | %s", aid, ln[:160])
            try:
                _hub_emit("app.crashed", {"app": aid, "exit_code": code,
                                          "reason": reason,
                                          "log": f"logs/{aid}.log"})
            except Exception:
                pass
        elif running and aid in _crashed:
            _crashed.pop(aid, None)                 # started again by the operator


@app.route("/api/status/stream")
def api_status_stream():
    """SSE stream — pushes a status JSON every 2 seconds.

    Every tick is guarded. One unhandled exception in here used to end the
    generator, which closes the stream: the page then froze on its last frame and
    kept showing whatever was true minutes ago — a status display that lies is
    worse than one that is obviously broken. Now a failed tick reports itself and
    the stream carries on.
    """
    def generate():
        fails = 0
        while True:
            try:
                _detect_crashes()
                payload = json.dumps({
                    "apps":         _app_status(),
                    "project_root": _project_root,
                    "ws_clients":   len(_ws_clients),
                    # so the UI can warn when the bus is down (fit reports + the
                    # measurement-complete signal depend on it)
                    "event_bus":    _SOCK_AVAILABLE,
                    "disk_free_gb": _disk_free_gb(),
                    "hub_error":    None,
                })
                fails = 0
            except Exception as exc:
                fails += 1
                logger.exception("[Hub] status tick failed (%d in a row)", fails)
                payload = json.dumps({"apps": {}, "project_root": _project_root,
                                      "hub_error": f"{type(exc).__name__}: {exc}"})
            yield f"data: {payload}\n\n"
            # back off a little if we are failing, so a persistent fault does not
            # spin the CPU or flood the log
            time.sleep(2 if fails == 0 else min(2 + fails * 2, 15))

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


@app.route("/api/restart/<app_id>", methods=["POST"])
def api_restart(app_id: str):
    """Stop then start, in one call. Not exposed as a button — the card keeps to
    Start / Stop / Open — but useful from a script, and it is the same sequence
    those two buttons perform: stop waits for the port to be released, start
    reclaims it if anything is still holding it."""
    ok, stop_msg = _stop_app(app_id)
    if not ok:
        return jsonify({"ok": False, "message": f"could not stop: {stop_msg}"})
    ok, start_msg = _start_app(app_id)
    return jsonify({"ok": ok, "message": f"{stop_msg} → {start_msg}"})


@app.route("/api/stop_all", methods=["POST"])
def api_stop_all():
    """Close every app and leave no process behind."""
    results = {}
    for a in APPS:
        try:
            ok, msg = _stop_app(a["id"])
        except Exception as exc:
            ok, msg = False, str(exc)
        results[a["id"]] = {"ok": ok, "message": msg}
    stuck = [k for k, v in results.items() if not v["ok"]]
    return jsonify({"ok": not stuck, "results": results, "stuck": stuck})


@app.route("/api/ports")
def api_ports():
    """Who is holding each app's port. The answer to "why won't it start?"."""
    out = []
    for a in APPS:
        busy = pl.port_in_use(a["port"])
        row = {"app_id": a["id"], "port": a["port"], "in_use": busy,
               "managed": _is_running(a["id"]), "holders": []}
        if busy and not row["managed"]:
            entry = _ROOT / a["entry"]
            for h in pl.listeners(a["port"]):
                info = pl.describe(h)
                info["ours"] = pl.is_our_app(h, entry, _ROOT)
                row["holders"].append(info)
        out.append(row)
    return jsonify({"ports": out})


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


def _list_drives() -> list:
    """Available filesystem roots: drive letters on Windows, '/' on POSIX."""
    if sys.platform.startswith("win"):
        from string import ascii_uppercase
        return [f"{c}:\\" for c in ascii_uppercase if Path(f"{c}:\\").exists()]
    return ["/"]


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
        "drives":  _list_drives(),   # jump to any drive / root
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

_shutdown_done = threading.Event()


def _shutdown_all_apps() -> None:
    """Stop every sub-app so none is orphaned. Idempotent + best-effort.

    Closing the hub must close the apps: an orphan keeps its port, keeps polling
    the project folder and keeps writing files, while nothing in the UI can reach
    it. Registered on atexit AND called from the SIGTERM/SIGINT handler, guarded
    so the two paths cannot fight.
    """
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    for app_id in list(_procs):
        try:
            if _is_running(app_id):
                ok, msg = _stop_app(app_id)
                logger.info("[Hub] shutdown %s: %s", app_id, msg)
        except Exception:
            pass
    try:
        pl.write_children(_CHILDREN_FILE, {})
    except Exception:
        pass


# Run on normal exit / Ctrl-C, and on SIGTERM (kill) where supported.
atexit.register(_shutdown_all_apps)
def _on_signal(signum, _frame):
    """Ctrl-C / kill must take the children with it.

    `sys.exit(0)` alone relied on atexit, which does not run reliably from a
    signal handler while Flask's request threads are alive — so a Ctrl-C in the
    launching terminal used to leave nine apps running.
    """
    logger.info("[Hub] signal %s — stopping all apps", signum)
    _shutdown_all_apps()
    os._exit(0)


for _sig in ("SIGTERM", "SIGINT", "SIGHUP"):
    try:
        signal.signal(getattr(signal, _sig), _on_signal)
    except (ValueError, AttributeError, OSError):
        pass   # not in main thread / not supported on this OS


def _hub_port_error(reason: str) -> None:
    """Explain an unusable hub port and give the operator a way out.

    A bare `OSError: [Errno 48] Address already in use` is a poor answer to
    "start the platform", especially on macOS where port 5000 belongs to AirPlay
    Receiver by default.
    """
    print()
    print(f"  ✗ {reason}")
    for h in pl.listeners(_HUB_PORT):
        d = pl.describe(h)
        print(f"    held by PID {d['pid']} ({d['name']}): {d['cmdline'][:110]}")
    if _HUB_PORT == 5000 and sys.platform == "darwin":
        print("    On macOS, port 5000 is also used by AirPlay Receiver.")
        print("    Turn it off in System Settings → General → AirDrop & Handoff,")
        print("    or run the hub on a different port:")
    else:
        print("    Free that port, or run the hub on a different one:")
    print("        SWAXS_HUB_PORT=5100 ./start_platform.sh")
    print()


if __name__ == "__main__":
    # Before anything else: clean up after a hub that did not exit cleanly, so
    # this run starts from a known state instead of colliding with its own ghosts.
    _reaped = _reap_previous_run()

    # The hub's OWN port. If a previous hub is still bound to it, take it back;
    # otherwise say nothing and let Flask bind.
    #
    # This must NEVER refuse to start on the basis of the process table. On macOS,
    # AirPlay Receiver (ControlCenter) listens on port 5000 and a Flask app binds
    # 127.0.0.1:5000 alongside it perfectly happily — so "port 5000 is held by
    # ControlCenter" is not a reason to give up. `can_bind` asks the kernel the
    # only question that matters, and if the answer is still no, Flask's own
    # error is more trustworthy than anything we could infer.
    if not pl.can_bind(_HUB_PORT):
        _freed, _why, _killed = pl.reclaim_port(_HUB_PORT, _HERE / "app.py", _ROOT)
        if _killed:
            print(f"  port {_HUB_PORT}: {_why}")
        # Re-test with the SAME question Flask will ask. If we still cannot bind,
        # this is a fact rather than an inference, and worth failing on with
        # instructions instead of letting werkzeug print a bare socket error.
        if not pl.can_bind(_HUB_PORT):
            _hub_port_error(f"port {_HUB_PORT} is not available")
            sys.exit(1)

    print("━" * 58)
    if _reaped:
        print(f"  Reaped {len(_reaped)} orphaned app(s) from a previous run:")
        for _n in _reaped:
            print(f"      {_n}")
    print("  SWAXS Platform Hub")
    print(f"  → http://localhost:{_HUB_PORT}")
    if _SOCK_AVAILABLE:
        print(f"  → ws://localhost:{_HUB_PORT}/ws  (event bus)")
    print(f"  → {len(APPS)} app(s) registered from apps.yml")
    for a in APPS:
        print(f"      {a['icon']}  {a['name']}  :{a['port']}")
    print("━" * 58)
    try:
        app.run(debug=False, port=_HUB_PORT, threaded=True)
    except OSError as exc:                     # belt and braces
        _hub_port_error(str(exc))
        sys.exit(1)
    finally:
        _shutdown_all_apps()   # ensure children die when the hub exits
