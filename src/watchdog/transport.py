"""
src/watchdog/transport.py — Fire-and-forget Slack webhook POST.

Sends {"title", "text", "level"} to SWAXS_SLACK_WEBHOOK_URL via a worker thread
with throttling, timeout, and all errors swallowed. Based on the queue/worker
pattern from src/notify/slack.py, but vastly simpler: webhook transport only,
no threading or file uploads, no retry logic.
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

ENV_WEBHOOK = "SWAXS_SLACK_WEBHOOK_URL"

#: Distinct from None (the shutdown sentinel) and from a real message, so the
#: worker can tell "nothing queued right now" from "stop".
_EMPTY = object()


def _post(url: str, payload: dict, timeout: float = 6.0) -> dict:
    """POST JSON to a URL. Returns the parsed JSON response or an error dict."""
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json; charset=utf-8"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode(errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": body.strip() == "ok", "raw": body[:200]}
    except Exception as exc:
        logger.warning("POST to Slack webhook failed: %s", exc)
        return {"ok": False, "error": str(exc)}


class WebhookTransport:
    """
    Queue-based Slack webhook sender. Thread-safe, fire-and-forget, never blocks
    or raises. All errors are logged and swallowed.
    """

    def __init__(self, min_interval_s: float = 3.0, timeout_s: float = 6.0):
        self.webhook_url = os.environ.get(ENV_WEBHOOK, "").strip()
        self.min_interval_s = float(min_interval_s)
        self.timeout_s = float(timeout_s)

        # TWO queues, faults drained first and never throttled. With one queue
        # and a 3 s throttle the drain rate is 20 messages/minute, so a full
        # 200-deep queue is ten minutes long: an E-stop queued behind a burst
        # of progress messages arrived ten minutes late, or was dropped on
        # queue.Full with nothing but a log line to say so. A fault must never
        # wait behind an info message, and must never be the one that is
        # dropped — the info queue is what gets shed under pressure.
        self._fault_q: queue.Queue = queue.Queue(maxsize=200)
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._alive = True
        self._draining = False
        self._worker: threading.Thread | None = None
        self._last_send = 0.0

        # Delivery outcome, for the UI. A webhook that is revoked or mistyped
        # otherwise fails invisibly: _post logs a warning and the app records
        # the message as sent, so the operator sees a healthy send history
        # while nothing at all reaches Slack.
        self.last_error: str = ""
        self.n_failed: int = 0
        self.n_sent: int = 0
        self.n_dropped: int = 0

        if self.webhook_url:
            self._ensure_worker()
        else:
            logger.info(
                "Slack webhook not configured — export %s to enable notifications",
                ENV_WEBHOOK,
            )

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._alive = True
            self._worker = threading.Thread(
                target=self._run, daemon=True, name="watchdog-transport"
            )
            self._worker.start()

    def send(self, title: str, text: str, level: str) -> None:
        """
        Queue a message for delivery. Never blocks, never raises.
        Returns immediately.

        ``level == "fault"`` goes on the priority queue: drained first, sent
        without the throttle, and never dropped in favour of an info message.
        """
        if not self.webhook_url:
            return
        item = {"title": title, "text": text, "level": level}
        if level == "fault":
            try:
                self._fault_q.put_nowait(item)
                return
            except queue.Full:
                # 200 unsent faults is itself the emergency; keep the NEWEST.
                try:
                    self._fault_q.get_nowait()
                    self._fault_q.put_nowait(item)
                    self.n_dropped += 1
                    logger.error("fault queue full — dropped the oldest fault")
                except Exception:
                    pass
                return
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self.n_dropped += 1
            logger.warning("Slack webhook queue full — dropped a message")

    def close(self, timeout: float = 2.0) -> None:
        """Stop the worker, draining what is already queued first.

        Setting _alive False before queueing the sentinel let the worker exit
        on its `while self._alive` check without draining, so the last
        message before a shutdown — often the E-stop or stall that caused it
        — was silently discarded. The worker now drains, bounded by `timeout`.
        """
        self._draining = True
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        if self._worker is not None:
            self._worker.join(timeout=timeout)
        self._alive = False

    # ── Internal ───────────────────────────────────────────────────────────────

    def _next(self) -> dict | None | object:
        """Faults first, then info. Returns the sentinel/None as-is, or
        _EMPTY when both queues are empty."""
        try:
            return self._fault_q.get_nowait()
        except queue.Empty:
            pass
        try:
            return self._q.get(timeout=0.5)
        except queue.Empty:
            return _EMPTY

    def _run(self) -> None:
        """Worker thread: dequeue and send. Faults bypass the throttle."""
        while self._alive:
            item = self._next()
            if item is _EMPTY:
                if self._draining and self._fault_q.empty() and self._q.empty():
                    break
                continue
            if item is None:
                # Shutdown sentinel — but never leave a fault unsent.
                if not self._fault_q.empty():
                    self._draining = True
                    continue
                break
            try:
                if item["level"] != "fault":
                    self._throttle()
                else:
                    self._last_send = time.time()
                payload = {"title": item["title"], "text": item["text"],
                           "level": item["level"]}
                result = _post(self.webhook_url, payload, timeout=self.timeout_s)
                if result.get("ok"):
                    self.n_sent += 1
                    self.last_error = ""
                else:
                    self.n_failed += 1
                    self.last_error = str(result.get("error") or result)[:300]
                    logger.warning("Slack rejected message: %s", result)
            except Exception as exc:
                self.n_failed += 1
                self.last_error = str(exc)[:300]
                logger.exception("transport send failed")

    def _throttle(self) -> None:
        gap = time.time() - self._last_send
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last_send = time.time()
