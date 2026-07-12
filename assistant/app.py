"""
assistant/app.py — SWAXS AI Assistant  (port 5005)
====================================================
Flask API for the AI assistant chat interface.
Powered by src.ai.assistant.SWAXSAssistant — full Claude API client with
knowledge base retrieval, 3-layer memory, and tool dispatch.

Endpoints
---------
  GET  /                         — chat UI
  POST /api/chat                 — send a message, get {text, plot, hints, ...}
  GET  /api/history/<session_id> — retrieve conversation history
  POST /api/ingest/pdf           — upload + ingest a PDF into KB
  GET  /api/events/stream        — SSE stream for real-time event-bus hints
  GET  /api/memory/context       — view current memory layers (debug)
  POST /api/memory/clear         — clear session context for this user
  GET  /api/knowledge/stats      — KB collection document counts
  GET  /api/health               — health check

Run:  uv run assistant/app.py
Open: http://localhost:5005
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from threading import Lock

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
)

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Load .env immediately — before any import reads os.environ ────────────────
# Runs regardless of how the assistant is started (hub subprocess, direct
# uv run, IDE, etc.).  Does NOT override variables already set in the shell.
def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

_load_dotenv(_ROOT / ".env")

from src.ai.assistant import SWAXSAssistant   # noqa: E402
from src.ai.hints import HintChecker          # noqa: E402


def _json_default(o):
    """Fallback for json.dumps so NumPy / exotic types never crash the stream.

    Interactive Plotly figures carry NumPy float/array values; the default
    ``json.dumps`` raises ``TypeError`` on those, which would abort the SSE
    'final' event mid-write — the client then loses the full response and the
    figure. This coerces such values to JSON-native ones.
    """
    for attr in ("tolist", "item"):          # numpy arrays, numpy scalars
        fn = getattr(o, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:                 # noqa: BLE001
                pass
    try:
        return float(o)
    except Exception:                         # noqa: BLE001
        return str(o)


def _dumps(obj) -> str:
    """NumPy-safe json.dumps used for every SSE event."""
    return json.dumps(obj, default=_json_default)


def _json_safe(obj):
    """Round-trip an object through the NumPy-safe encoder so Flask's jsonify
    (which uses its own encoder) won't choke on NumPy values either."""
    return json.loads(_dumps(obj))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "[%(asctime)s] %(name)s %(levelname)s  %(message)s",
    datefmt = "%H:%M:%S",
)
logger = logging.getLogger("swaxs_assistant")

# ── Config ────────────────────────────────────────────────────────────────────
PORT          = 5005
HUB_URL       = os.environ.get("SWAXS_HUB_URL",  "ws://localhost:5000/ws")
HUB_API       = os.environ.get("SWAXS_HUB_API",  "http://localhost:5000")
BEAMLINE_ID   = os.environ.get("SWAXS_BEAMLINE",  "ssrl_1-5")
SESSION_TTL_S = 7200   # 2-hour session expiry

app = Flask(__name__, template_folder="templates")

# ── Global state ──────────────────────────────────────────────────────────────
_sessions: dict[str, dict] = {}   # {session_id: {history, user_id, last_active}}
_sessions_lock = Lock()

_hint_events: list[dict] = []     # SSE queue for event-bus hints
_hint_events_lock = Lock()

# ── Lazy assistant singleton ──────────────────────────────────────────────────
_assistant: SWAXSAssistant | None = None
_assistant_lock = Lock()


def _get_assistant() -> SWAXSAssistant:
    global _assistant
    with _assistant_lock:
        if _assistant is None:
            ai_knowledge_dir = _ROOT / "ai_knowledge"
            _assistant = SWAXSAssistant(
                ai_knowledge_dir = ai_knowledge_dir,
                user_id          = _default_user_id(),
                beamline_id      = BEAMLINE_ID,
            )
            _ingest_app_knowledge(_assistant)
    return _assistant


def _default_user_id() -> str:
    uid = os.environ.get("SWAXS_USER_ID", "").strip()
    if uid:
        return uid
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return "default"


def _ingest_app_knowledge(assistant: SWAXSAssistant) -> None:
    """Index all per-app knowledge.md files into the 'apps' KB collection."""
    kb = assistant._get_knowledge_base()
    if kb is None:
        return
    for app_name in ["reduction", "viewer", "background",
                     "analysis", "assistant", "hub"]:
        md_path = _ROOT / app_name / "knowledge.md"
        if md_path.exists():
            try:
                n = kb.ingest_markdown(str(md_path), collection="apps")
                if n:
                    logger.info("Ingested %s/knowledge.md → %d chunks", app_name, n)
            except Exception as exc:
                logger.warning("KB ingest error for %s: %s", app_name, exc)
    # Ingest beamline YAML
    bl_path = _ROOT / "ai_knowledge" / "beamline" / f"{BEAMLINE_ID}.yml"
    if bl_path.exists():
        try:
            kb.ingest_yaml(str(bl_path), collection="beamline")
        except Exception as exc:
            logger.warning("KB ingest beamline YAML error: %s", exc)


# ── Event bus subscription ────────────────────────────────────────────────────

def _setup_event_bus() -> None:
    """Subscribe to hub event bus and convert events to SSE hints."""
    try:
        from src.events import EventBusClient

        bus = EventBusClient("assistant", hub_url=HUB_URL)
        bus.connect(retry=True)

        checker = HintChecker()

        def _on_event(event: dict) -> None:
            # Canonical bus events use the key "type" (see src/events.py).
            etype = event.get("type", "")
            data  = event.get("data", {})
            hints: list = []

            if etype == "file.reduced":
                hints = checker.on_file_reduced(data)
            elif etype == "file.averaged":
                hints = checker.on_file_averaged(data)
            elif etype == "analysis.complete":
                hints = checker.on_analysis(data)
            elif etype == "file.subtracted":
                hints = checker.on_file_subtracted(data)

            with _hint_events_lock:
                for h in hints:
                    _hint_events.append({
                        "id":         str(uuid.uuid4()),
                        "severity":   h.severity,
                        "message":    h.message,
                        "file_path":  h.file_path,
                        "check":      h.check,
                        "event_type": etype,
                    })
                # Cap the queue
                if len(_hint_events) > 200:
                    del _hint_events[:-200]

            # Publish high-severity hints back onto the bus
            for h in hints:
                if h.severity in ("warning", "error"):
                    try:
                        bus.emit_ai_hint(
                            hint      = h.message,
                            file_path = h.file_path,
                            severity  = h.severity,
                        )
                    except Exception:
                        pass

        bus.on_event(_on_event)
        logger.info("[Assistant] Event bus connected to %s", HUB_URL)

    except Exception as exc:
        logger.warning("[Assistant] Event bus unavailable: %s", exc)


# ── Session helpers ────────────────────────────────────────────────────────────

def _get_or_create_session(session_id: str) -> dict:
    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = {
                "history":     [],
                "user_id":     _default_user_id(),
                "last_active": time.time(),
            }
        sess = _sessions[session_id]
        sess["last_active"] = time.time()
        return sess


def _expire_sessions() -> None:
    now = time.time()
    with _sessions_lock:
        dead = [
            sid for sid, s in _sessions.items()
            if now - s["last_active"] > SESSION_TTL_S
        ]
        for sid in dead:
            _sessions.pop(sid, None)
    if dead:
        logger.debug("Expired %d stale sessions", len(dead))


def _resolve_project_root() -> str | None:
    """
    Find the active experiment folder via a robust fallback chain, so the
    assistant doesn't read an empty manifest just because the hub forgot the
    selection:
      1. the hub's live /api/status (folder selected this session)
      2. the SWAXS_PROJECT env var (set when the hub launches this app)
      3. the persisted .hub_state.json (last folder, survives restarts)
    Returns the first path that exists on disk, else None.
    """
    candidates: list[str] = []

    # 1. Live hub status
    try:
        import urllib.request
        with urllib.request.urlopen(f"{HUB_API}/api/status", timeout=1) as r:
            data = json.loads(r.read())
            if data.get("project_root"):
                candidates.append(str(data["project_root"]))
    except Exception:
        pass

    # 2. Env var passed by the hub at launch
    env_root = os.environ.get("SWAXS_PROJECT", "").strip()
    if env_root:
        candidates.append(env_root)

    # 3. Persisted hub state file (project repo root / .hub_state.json)
    try:
        state = Path(__file__).resolve().parent.parent / ".hub_state.json"
        if state.is_file():
            saved = json.loads(state.read_text(encoding="utf-8")).get("project_root", "")
            if saved:
                candidates.append(str(saved))
    except Exception:
        pass

    for c in candidates:
        if c and Path(c).is_dir():
            return c
    return None


# Backwards-compatible alias (older callers).
_hub_project_root = _resolve_project_root


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    POST /api/chat
    Body (JSON): {
        message:      str,          ← required
        session_id?:  str,          ← optional; created if omitted
        user_id?:     str,
        app_id?:      str,
        project_root?: str          ← overrides hub project root
    }
    Response (JSON): {
        text:       str,
        plot:       str|null,       ← base64 PNG
        tool_calls: list,
        hints:      list[str],
        session_id: str
    }
    """
    _expire_sessions()

    body       = request.get_json(force=True, silent=True) or {}
    message    = (body.get("message") or "").strip()
    session_id = body.get("session_id") or str(uuid.uuid4())
    app_id     = body.get("app_id", "assistant")

    if not message:
        return jsonify({"error": "message is required"}), 400

    sess         = _get_or_create_session(session_id)
    user_id      = body.get("user_id") or sess["user_id"]
    project_root = body.get("project_root") or _hub_project_root()
    assistant    = _get_assistant()

    # Per-project chat history: seed a fresh session from the project's saved
    # history so reopening a project restores continuity across restarts.
    if project_root and not sess.get("_preloaded") and not sess["history"]:
        try:
            mem = assistant._get_memory(user_id)
            if mem is not None:
                prior = mem.load_chat(project_root, max_turns=20)
                sess["history"] = [{"role": t["role"], "content": t["text"]}
                                   for t in prior if t.get("text")]
        except Exception as exc:
            logger.debug("preload project history failed: %s", exc)
    sess["_preloaded"] = True

    try:
        result = assistant.chat(
            message      = message,
            user_id      = user_id,
            project_root = project_root,
            app_id       = app_id,
            history      = list(sess["history"]),
        )
    except Exception as exc:
        logger.exception("Chat error: %s", exc)
        return jsonify({"error": str(exc)}), 500

    # Persist history delta
    with _sessions_lock:
        sess["history"] += result.pop("_history_delta", [])

    # Persist this turn to the project's on-disk chat history (text only).
    if project_root:
        try:
            mem = assistant._get_memory(user_id)
            if mem is not None:
                mem.append_chat(project_root, "user", message)
                mem.append_chat(project_root, "assistant", result.get("text", ""))
        except Exception as exc:
            logger.debug("persist project chat failed: %s", exc)

    return jsonify(_json_safe({
        "text":             result.get("text", ""),
        "plot":             result.get("plot"),
        "plot_interactive": result.get("plot_interactive"),
        "tool_calls":       result.get("tool_calls", []),
        "hints":            result.get("hints", []),
        "session_id":       session_id,
    }))


@app.route("/api/chat/stream", methods=["POST"])
def api_chat_stream():
    """
    POST /api/chat/stream — same as /api/chat but streams progress as
    Server-Sent Events so the UI can show intermediate steps live:
        {"type":"thinking","text":...}   interim narration
        {"type":"tool","name":...,"label":...}  a tool is running
        {"type":"final", text, plot, plot_interactive, tool_calls, hints, session_id}
        {"type":"error","error":...}
    """
    import queue as _queue
    import threading as _threading

    _expire_sessions()
    body       = request.get_json(force=True, silent=True) or {}
    message    = (body.get("message") or "").strip()
    session_id = body.get("session_id") or str(uuid.uuid4())
    app_id     = body.get("app_id", "assistant")
    if not message:
        return jsonify({"error": "message is required"}), 400

    sess         = _get_or_create_session(session_id)
    user_id      = body.get("user_id") or sess["user_id"]
    project_root = body.get("project_root") or _hub_project_root()
    assistant    = _get_assistant()

    if project_root and not sess.get("_preloaded") and not sess["history"]:
        try:
            mem = assistant._get_memory(user_id)
            if mem is not None:
                prior = mem.load_chat(project_root, max_turns=20)
                sess["history"] = [{"role": t["role"], "content": t["text"]}
                                   for t in prior if t.get("text")]
        except Exception as exc:
            logger.debug("preload project history failed: %s", exc)
    sess["_preloaded"] = True

    q: "_queue.Queue" = _queue.Queue()
    holder: dict = {}

    def _run():
        try:
            holder["result"] = assistant.chat(
                message=message, user_id=user_id, project_root=project_root,
                app_id=app_id, history=list(sess["history"]),
                emit=lambda ev: q.put(ev),
            )
        except Exception as exc:                       # noqa: BLE001
            holder["error"] = str(exc)
            logger.exception("Streaming chat error: %s", exc)
        finally:
            q.put({"type": "__done__"})

    _threading.Thread(target=_run, daemon=True).start()

    def _generate():
        yield "data: {\"type\": \"start\"}\n\n"
        while True:
            ev = q.get()
            if ev.get("type") == "__done__":
                break
            yield f"data: {_dumps(ev)}\n\n"

        result = holder.get("result")
        if result is None:
            yield f"data: {_dumps({'type':'error','error':holder.get('error','unknown error')})}\n\n"
            return

        with _sessions_lock:
            sess["history"] += result.pop("_history_delta", [])
        if project_root:
            try:
                mem = assistant._get_memory(user_id)
                if mem is not None:
                    mem.append_chat(project_root, "user", message)
                    mem.append_chat(project_root, "assistant", result.get("text", ""))
            except Exception as exc:
                logger.debug("persist project chat failed: %s", exc)

        final = {
            "type":             "final",
            "text":             result.get("text", ""),
            "plot":             result.get("plot"),
            "plot_interactive": result.get("plot_interactive"),
            "tool_calls":       result.get("tool_calls", []),
            "hints":            result.get("hints", []),
            "session_id":       session_id,
        }
        yield f"data: {_dumps(final)}\n\n"

    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/history/<session_id>", methods=["GET"])
def api_history(session_id: str):
    """GET /api/history/<session_id> — return serialisable conversation history."""
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if sess is None:
        return jsonify({"history": [], "session_id": session_id})

    history: list[dict] = []
    for turn in sess.get("history", []):
        role    = turn.get("role")
        content = turn.get("content")
        if isinstance(content, str):
            if content.strip():
                history.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Extract only displayable text blocks; skip tool_use / tool_result
            # plumbing turns so they don't render as empty/garbled bubbles.
            parts = []
            for b in content:
                btype = getattr(b, "type", None)
                if btype is None and isinstance(b, dict):
                    btype = b.get("type")
                if btype == "text":
                    parts.append(getattr(b, "text", None) or (b.get("text", "") if isinstance(b, dict) else ""))
                elif btype is None and isinstance(b, dict) and "text" in b:
                    parts.append(b.get("text", ""))
            texts = " ".join(p for p in parts if p).strip()
            if texts:
                history.append({"role": role, "content": texts})

    return jsonify({"history": history, "session_id": session_id})


@app.route("/api/ingest/pdf", methods=["POST"])
def api_ingest_pdf():
    """
    POST /api/ingest/pdf
    multipart/form-data:
        file:       <pdf file>
        collection: literature | user_papers  (default: user_papers)
    """
    if "file" not in request.files:
        return jsonify({"error": "No file field in request"}), 400

    f          = request.files["file"]
    collection = request.form.get("collection", "user_papers")
    if collection not in ("user_papers", "literature"):
        collection = "user_papers"   # only these two are user-uploadable

    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are accepted"}), 400

    save_dir  = _ROOT / "ai_knowledge" / collection
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f.filename
    f.save(str(save_path))

    try:
        kb = _get_assistant()._get_knowledge_base()
        if kb is None:
            return jsonify({"error": "Knowledge base unavailable (ChromaDB not installed)"}), 503

        n = kb.ingest_pdf(str(save_path), collection=collection, force=False)
        return jsonify({
            "status":     "ok",
            "file":       f.filename,
            "collection": collection,
            "chunks":     n,
            "message":    (
                f"Ingested {f.filename} ({n} chunks) into '{collection}'."
                if n else
                f"{f.filename} already indexed (file unchanged)."
            ),
        })
    except Exception as exc:
        logger.exception("PDF ingest error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/events/stream")
def api_events_stream():
    """
    GET /api/events/stream
    Server-Sent Events stream delivering real-time proactive hints from the
    event bus.  Each event: data: <JSON hint object>

    Connect with:
        const es = new EventSource('/api/events/stream');
        es.onmessage = (e) => { const hint = JSON.parse(e.data); ... };
    """
    cursor = [0]   # per-connection pointer into _hint_events

    def _generate():
        yield "data: {\"type\": \"connected\"}\n\n"
        while True:
            with _hint_events_lock:
                pending = _hint_events[cursor[0]:]
                cursor[0] = len(_hint_events)

            for evt in pending:
                yield f"data: {_dumps(evt)}\n\n"

            time.sleep(1.0)

    return Response(
        _generate(),
        mimetype = "text/event-stream",
        headers  = {
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/memory/context", methods=["GET"])
def api_memory_context():
    """GET /api/memory/context — view current layered memory context."""
    project_root = _hub_project_root()
    mem          = _get_assistant()._get_memory()

    if mem is None:
        return jsonify({"error": "Memory system unavailable"}), 503

    try:
        ctx = mem.load_context(
            project_root = project_root,
            beamline_id  = BEAMLINE_ID,
        )
        return jsonify(ctx)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/memory/clear", methods=["POST"])
def api_memory_clear():
    """POST /api/memory/clear — clear transient session context for current user."""
    mem = _get_assistant()._get_memory()
    if mem is None:
        return jsonify({"error": "Memory system unavailable"}), 503
    try:
        mem.clear_user_context()
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/knowledge/stats", methods=["GET"])
def api_knowledge_stats():
    """GET /api/knowledge/stats — document counts per KB collection."""
    kb = _get_assistant()._get_knowledge_base()
    if kb is None:
        return jsonify({"error": "Knowledge base unavailable (ChromaDB not installed)"}), 503
    try:
        try:
            kb._load_log()        # reflect any writes made since this process started
        except Exception:
            pass
        return jsonify(kb.collection_stats())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/knowledge/list", methods=["GET"])
def api_knowledge_list():
    """GET /api/knowledge/list — every ingested paper/note (works without ChromaDB)."""
    kb = _get_assistant()._get_knowledge_base()
    if kb is None:
        return jsonify({"items": [], "error": "Knowledge base unavailable"}), 200
    try:
        try:
            kb._load_log()        # reflect any writes made since this process started
        except Exception:
            pass
        items = kb.list_ingested()
        try:
            stats = kb.collection_stats()
        except Exception:
            stats = {}
        view = [{"name": it.get("name", it.get("source")),
                 "collection": it["collection"],
                 "chunks": it.get("chunks"),
                 "added": (it.get("ingested_at") or "")[:10]}
                for it in items]
        return jsonify({"items": view, "counts": stats})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/knowledge/note", methods=["POST"])
def api_knowledge_note():
    """POST /api/knowledge/note {name, text, collection?} — save a text note."""
    body = request.get_json(force=True) or {}
    text = (body.get("text") or "").strip()
    name = (body.get("name") or "note").strip()
    col  = body.get("collection", "user_papers")
    if not text:
        return jsonify({"error": "text is required"}), 400
    kb = _get_assistant()._get_knowledge_base()
    if kb is None:
        return jsonify({"error": "Knowledge base unavailable (ChromaDB not installed)"}), 503
    try:
        n = kb.ingest_text(text, name=name, collection=col)
        return jsonify({"status": "ok", "name": name, "chunks": n})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/knowledge/remove", methods=["POST"])
def api_knowledge_remove():
    """POST /api/knowledge/remove {name} — remove a source from the KB + log."""
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    kb = _get_assistant()._get_knowledge_base()
    if kb is None:
        return jsonify({"error": "Knowledge base unavailable"}), 503
    try:
        res = kb.remove_source(name, collection=body.get("collection"))
        code = 404 if "error" in res else 200
        return jsonify(res), code
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/health")
def api_health():
    # Resolve credentials the SAME way the assistant does — this pulls the SLAC
    # gateway token/endpoint from ~/.claude/settings.json into the environment,
    # so the badge reflects reality even before the first chat.
    try:
        from src.ai.assistant import _load_claude_settings_into_env
        _load_claude_settings_into_env()
    except Exception:
        pass
    has_token = bool(os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip())
    has_key   = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    # Last-resort: a project .env may hold ANTHROPIC_API_KEY.
    if not has_token and not has_key:
        try:
            dotenv = _ROOT / ".env"
            if dotenv.is_file():
                for line in dotenv.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("ANTHROPIC_API_KEY=") and \
                            line.split("=", 1)[1].strip().strip('"').strip("'"):
                        has_key = True
                        break
        except Exception:
            pass
    base_url  = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or "default (api.anthropic.com)"
    return jsonify({
        "app":          "assistant",
        "status":       "running",
        "port":         PORT,
        "api_key_set":  has_token or has_key,        # field the UI badge reads
        "credentials":  ("gateway-token" if has_token else "api-key" if has_key else "none"),
        "base_url":     base_url,
        "model":        os.environ.get("ANTHROPIC_MODEL", "").strip() or "default",
        "beamline":     BEAMLINE_ID,
        "project_root": _hub_project_root() or "",     # current folder from the hub
    })


@app.route("/api/project", methods=["GET"])
def api_project():
    """Return the project folder the hub currently has selected (env / live
    status / persisted state), so the assistant UI can track it automatically."""
    return jsonify({"project_root": _hub_project_root() or ""})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _tok  = bool(os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip())
    _key  = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    _base = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or "api.anthropic.com (default)"
    _auth = ("✓ gateway token" if _tok else "✓ API key" if _key
             else "✗ none — set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY")
    print("━" * 54)
    print("  SWAXS AI Assistant")
    print(f"  → http://localhost:{PORT}")
    print(f"  Auth     : {_auth}")
    print(f"  Endpoint : {_base}")
    print(f"  Beamline : {BEAMLINE_ID}")
    print("━" * 54)

    # Warm up the knowledge base in the background so the KB/literature panel
    # populates on first page load instead of only after a manual refresh —
    # the first KB request otherwise pays for ChromaDB + embedding-model init.
    def _warm_kb():
        try:
            kb = _get_assistant()._get_knowledge_base()
            if kb is not None:
                kb.collection_stats()
        except Exception:
            pass
    import threading as _threading
    _threading.Thread(target=_warm_kb, daemon=True).start()

    _setup_event_bus()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
