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

        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._alive = True
        self._worker: threading.Thread | None = None
        self._last_send = 0.0
        self._warned_no_url = False

        if self.webhook_url:
            self._ensure_worker()
        else:
            if not self._warned_no_url:
                logger.info(
                    "Slack webhook not configured — export %s to enable notifications",
                    ENV_WEBHOOK,
                )
                self._warned_no_url = True

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
        """
        if not self.webhook_url:
            return
        try:
            self._q.put_nowait({"title": title, "text": text, "level": level})
        except queue.Full:
            logger.warning("Slack webhook queue full — dropped a message")

    def close(self, timeout: float = 2.0) -> None:
        """Stop the worker thread."""
        self._alive = False
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        if self._worker is not None:
            self._worker.join(timeout=timeout)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Worker thread: dequeue and send."""
        while self._alive:
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._throttle()
                payload = {"title": item["title"], "text": item["text"],
                           "level": item["level"]}
                result = _post(self.webhook_url, payload, timeout=self.timeout_s)
                if not result.get("ok"):
                    logger.warning("Slack rejected message: %s", result)
            except Exception:
                logger.exception("transport send failed")

    def _throttle(self) -> None:
        gap = time.time() - self._last_send
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last_send = time.time()
