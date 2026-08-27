"""
src/notify/slack.py — Slack notifications for autonomous runs.

Design constraints, in order of importance:

1. IT MUST NEVER AFFECT THE RUN. Every send happens on a worker thread with a
   short timeout, and every exception is swallowed. A Slack outage, an expired
   token or a rate limit must be invisible to the reactor. Nothing here may be
   called while the controller lock is held (the caller enforces that by using
   the event callback, which already runs outside it).
2. SIGNAL OVER VOLUME. Events are tiered (alert / progress / quiet) and a
   campaign posts ONE parent message per recipe with everything else threaded
   under it, so a 24 h run reads as one line per condition.
3. NO SECRETS ON DISK. The bot token / webhook URL come from the environment
   (SWAXS_SLACK_BOT_TOKEN, SWAXS_SLACK_WEBHOOK). config.yml holds only the
   channel and which tiers to send — it is in git.

Transport is chosen automatically: a bot token enables threading, message
updates and file uploads; a webhook is the zero-setup fallback (flat messages
only). Only stdlib is used, so there is no new dependency.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
#: env vars holding the credentials — never put these in config.yml
ENV_TOKEN = "SWAXS_SLACK_BOT_TOKEN"
ENV_WEBHOOK = "SWAXS_SLACK_WEBHOOK"

#: event tiers. "alert" = someone should walk to the hutch.
ALERT = "alert"
PROGRESS = "progress"
SESSION = "session"


def _post(url: str, payload: dict, token: str = "", timeout: float = 6.0) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode(errors="replace")
    try:
        return json.loads(body)
    except Exception:
        return {"ok": body.strip() == "ok", "raw": body[:200]}


class SlackNotifier:
    """Tiered, threaded Slack notifications that cannot break the reactor."""

    def __init__(self, cfg: dict | None = None, log=None):
        cfg = dict(cfg or {})
        self.enabled = bool(cfg.get("enabled", False))
        self.channel = str(cfg.get("channel", "") or "")
        self.tiers = {str(t).lower() for t in (cfg.get("tiers")
                                               or [ALERT, PROGRESS, SESSION])}
        self.mention = str(cfg.get("mention_on_alert", "") or "")   # e.g. "<!channel>"
        self.thread_per_recipe = bool(cfg.get("thread_per_recipe", True))
        self.timeout = float(cfg.get("timeout_s", 6.0))
        self.min_interval_s = float(cfg.get("min_interval_s", 0.4))
        #: True → describe what would be sent, touch no network. File uploads go
        #: straight to urllib (not through _post), so they need this flag too.
        self.dry_run = bool(cfg.get("dry_run", False))
        self._log = log or (lambda msg, tag="info": None)

        self.token = os.environ.get(ENV_TOKEN, "").strip()
        self.webhook = os.environ.get(ENV_WEBHOOK, "").strip()
        #: bot token → threading + uploads; webhook → flat messages only
        self.mode = "bot" if self.token else ("webhook" if self.webhook else "")
        #: credentials are present — notifications CAN be turned on at runtime
        self.configured = bool(self.mode)
        if self.enabled and not self.configured:
            self._log(f"⚠ Slack notifications requested but neither {ENV_TOKEN} nor "
                      f"{ENV_WEBHOOK} is set — notifications are OFF", "warn")
            self.enabled = False
        if self.configured and self.mode == "webhook" and self.thread_per_recipe:
            self._log("ℹ Slack: webhook transport cannot thread — posting flat "
                      f"messages. Set {ENV_TOKEN} for threaded updates.", "info")
            self.thread_per_recipe = False

        self._threads: dict[str, str] = {}     # recipe_id → parent message ts
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._alive = True
        self._last_send = 0.0
        self._worker = None
        if self.enabled:
            self._ensure_worker()

    # ── runtime on/off (the "I'm leaving the beamline" switch) ────────────────
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._alive = True
            self._worker = threading.Thread(target=self._run, daemon=True,
                                            name="slack-notifier")
            self._worker.start()

    def enable(self) -> tuple[bool, str]:
        """Turn notifications ON at runtime. Returns (ok, message)."""
        if not self.configured:
            return False, (f"no Slack credentials — export {ENV_TOKEN} (preferred) "
                           f"or {ENV_WEBHOOK} and restart the app")
        if self.mode == "bot" and not self.channel:
            return False, "notify.slack.channel is empty (required in bot mode)"
        self.enabled = True
        self._ensure_worker()
        return True, f"Slack notifications ON ({self.mode} mode)"

    def disable(self) -> tuple[bool, str]:
        """Turn notifications OFF. The worker is left running (idle) so it can be
        re-enabled instantly."""
        self.enabled = False
        return True, "Slack notifications OFF"

    def status(self) -> dict:
        return {"enabled": self.enabled, "configured": self.configured,
                "mode": self.mode, "channel": self.channel,
                "tiers": sorted(self.tiers),
                "thread_per_recipe": self.thread_per_recipe,
                "queued": self._q.qsize(),
                "threads_open": len(self._threads)}

    # ── public API (never blocks, never raises) ───────────────────────────────
    def notify(self, text: str, *, tier: str = PROGRESS, recipe_id: str = "",
               start_thread: bool = False, fields: dict | None = None) -> None:
        if not self.enabled or tier not in self.tiers:
            return
        try:
            self._q.put_nowait({"text": text, "tier": tier, "recipe_id": recipe_id,
                                "start_thread": start_thread, "fields": fields or {}})
        except queue.Full:
            # Dropping a notification is always preferable to blocking the caller.
            logger.warning("slack queue full — dropped a notification")

    def close(self, timeout: float = 2.0) -> None:
        self._alive = False
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        if self._worker is not None:
            self._worker.join(timeout=timeout)

    # ── worker ────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while self._alive:
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._send(item)
            except Exception:
                # Swallow EVERYTHING: Slack must never disturb the reactor.
                logger.exception("slack send failed")

    def _throttle(self) -> None:
        gap = time.time() - self._last_send
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last_send = time.time()

    def _send(self, item: dict) -> None:
        self._throttle()
        text = item["text"]
        if item["tier"] == ALERT and self.mention:
            text = f"{self.mention} {text}"
        if item["fields"]:
            text += "\n" + "\n".join(f"• *{k}*: {v}" for k, v in item["fields"].items())

        rid = item["recipe_id"]
        if self.mode == "webhook":
            _post(self.webhook, {"text": text}, timeout=self.timeout)
            return

        payload = {"channel": self.channel, "text": text}
        # thread the update under this recipe's parent message when we have one
        if self.thread_per_recipe and rid and not item["start_thread"]:
            ts = self._threads.get(rid)
            if ts:
                payload["thread_ts"] = ts
        res = _post(f"{SLACK_API}/chat.postMessage", payload, token=self.token,
                    timeout=self.timeout)
        if not res.get("ok"):
            self._log(f"⚠ Slack rejected a message: {res.get('error', res)}", "warn")
            return
        if item["start_thread"] and rid:
            self._threads[rid] = res.get("ts", "")

    # ── file upload (only used for the "something looks wrong" case) ──────────
    def upload_png(self, path: str, title: str, recipe_id: str = "",
                   comment: str = "") -> None:
        """Attach a plot. Bot token only; silently skipped otherwise."""
        if not self.enabled or self.mode != "bot":
            return
        if self.dry_run:
            self._log(f"[dry-run] would upload {path} as '{title}'"
                      + (f" (thread {recipe_id})" if recipe_id else ""), "info")
            print(f"\n[files.upload]  ↳ THREADED ({recipe_id})\n   {title}\n   {comment}\n"
                  f"   file: {path}")
            return
        threading.Thread(target=self._upload_png_blocking, daemon=True,
                         args=(path, title, recipe_id, comment)).start()

    def _upload_png_blocking(self, path, title, recipe_id, comment) -> None:
        try:
            from pathlib import Path
            p = Path(path)
            if not p.is_file():
                return
            size = p.stat().st_size
            # 1) reserve an upload URL  2) PUT the bytes  3) complete the upload
            res = _post(f"{SLACK_API}/files.getUploadURLExternal",
                        {}, token=self.token, timeout=self.timeout)
            url = (f"{SLACK_API}/files.getUploadURLExternal"
                   f"?filename={p.name}&length={size}")
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.token}"}, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                res = json.loads(r.read().decode())
            if not res.get("ok"):
                return
            put = urllib.request.Request(res["upload_url"], data=p.read_bytes(),
                                         method="POST")
            urllib.request.urlopen(put, timeout=self.timeout * 3).read()
            payload = {"files": [{"id": res["file_id"], "title": title}],
                       "channel_id": self.channel}
            if comment:
                payload["initial_comment"] = comment
            ts = self._threads.get(recipe_id)
            if ts:
                payload["thread_ts"] = ts
            _post(f"{SLACK_API}/files.completeUploadExternal", payload,
                  token=self.token, timeout=self.timeout)
        except Exception as exc:
            # Best-effort only: a missing plot is not worth a scary traceback in
            # the reactor console.
            logger.warning("slack png upload failed: %s: %s",
                           exc.__class__.__name__, exc)
            self._log(f"⚠ Slack plot upload failed ({exc.__class__.__name__}) — "
                      f"the message itself was sent", "warn")

    # ── convenience formatters for the reactor's events ───────────────────────
    def session_start(self, backend: str, project: str = "") -> None:
        self.notify(f":arrow_forward: *Autonomous session started* "
                    f"(backend `{backend}`)", tier=SESSION,
                    fields={"project": project} if project else None)

    def session_end(self, n_runs: int, best: dict | None = None,
                    elapsed_s: float = 0.0) -> None:
        f = {"runs completed": n_runs, "elapsed": _dur(elapsed_s)}
        if best:
            f["best size"] = f"{best.get('size')} nm"
            f["best loss"] = best.get("loss")
        self.notify(":checkered_flag: *Autonomous session finished*",
                    tier=SESSION, fields=f)

    def recipe_applied(self, recipe_id: str, params: dict, duration_s: float) -> None:
        f = {k: _fmt(v) for k, v in params.items()
             if k in ("T_reac", "F_tot", "x_ODE", "x_TOP", "x_oley")}
        f["run duration"] = _dur(duration_s)
        self.notify(f":test_tube: *Recipe applied* — `{recipe_id}`",
                    tier=PROGRESS, recipe_id=recipe_id, start_thread=True, fields=f)

    def run_complete(self, recipe_id: str, reason: str, duration_s: float,
                     result: dict | None = None) -> None:
        f = {"stopped by": reason, "ran": _dur(duration_s)}
        if result:
            for k in ("size", "pdi", "confidence", "loss"):
                if result.get(k) is not None:
                    f[k] = _fmt(result[k])
        self.notify(f":white_check_mark: *Run complete* — `{recipe_id}`",
                    tier=PROGRESS, recipe_id=recipe_id, fields=f)

    def fault(self, title: str, detail: str = "", recipe_id: str = "") -> None:
        self.notify(f":rotating_light: *{title}*" + (f"\n{detail}" if detail else ""),
                    tier=ALERT, recipe_id=recipe_id)


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.4g}"
    return v


def _dur(s: float) -> str:
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"
