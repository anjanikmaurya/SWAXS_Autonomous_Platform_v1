"""
src/events.py — Event Bus Client
=================================
Lightweight WebSocket client that connects to the Hub's event broker
at ws://localhost:5000/ws.  All SWAXS platform apps use this module to
publish events and subscribe to events from other apps.

Quick start (in any app.py)
----------------------------
    from src.events import EventBusClient

    # Create once at module level:
    bus = EventBusClient(app_id="reduction")
    bus.on_event(lambda e: print("received:", e))
    bus.connect()          # non-blocking — starts a daemon background thread

    # Publish when something happens:
    bus.emit_file_reduced(
        file_path="/abs/path/sample_0001_SAXS.dat",
        keyword="sample_A",
        scan_idx=1,
        detector="saxs",
    )

    # On app shutdown (Flask teardown / atexit):
    bus.disconnect()

Graceful degradation
---------------------
If the Hub is not running, or ``websocket-client`` is not installed,
:meth:`connect` silently no-ops.  :meth:`publish` drops the event with a
DEBUG log entry.  Apps never crash because of a missing bus connection.

Event types
-----------
Publishers              Event type
──────────────────────  ──────────────────────
reduction               file.reduced
viewer                  file.averaged, file.stitched
background              file.subtracted
analysis                analysis.complete
reduction (watch mode)  watch.new_raw
assistant               ai.hint
hub                     app.started, app.stopped
any                     app.connected
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger("swaxs_platform")

_HUB_WS_URL  = "ws://localhost:5000/ws"
_RETRY_DELAY  = 5    # seconds between reconnect attempts
_PING_INTERVAL = 30  # seconds between WebSocket keepalive pings
_PING_TIMEOUT  = 10  # seconds to wait for ping response


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(event_type: str, source_app: str, data: dict) -> dict:
    """
    Build a canonical SWAXS event dict.
    Useful when constructing events manually before publishing.
    """
    return {
        "type":         event_type,
        "source_app":   source_app,
        "timestamp":    _now(),
        "data":         data,
        "ai_triggered": False,
    }


class EventBusClient:
    """
    WebSocket client for the SWAXS Hub event bus.

    One instance per app.  Call :meth:`connect` once at startup; the client
    runs a daemon thread that maintains the connection and reconnects
    automatically if the Hub restarts.
    """

    def __init__(
        self,
        app_id:  str,
        hub_url: str = _HUB_WS_URL,
    ) -> None:
        self._app_id    = app_id
        self._url       = hub_url
        self._ws        = None           # active WebSocketApp instance
        self._thread: threading.Thread | None = None
        self._running   = False
        self._connected = False
        self._callbacks: list[Callable[[dict], None]] = []

    # ── Public API ─────────────────────────────────────────────────────────────

    def connect(self, retry: bool = True) -> "EventBusClient":
        """
        Start the background connection thread.  Non-blocking.

        Parameters
        ----------
        retry : bool
            If True (default) the thread reconnects indefinitely after
            any disconnect.  Set False for single-attempt mode (useful in
            tests).

        Returns self for fluent chaining::

            bus = EventBusClient("reduction").connect()
        """
        if self._running:
            return self
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop,
            args=(retry,),
            daemon=True,
            name=f"swaxs-eventbus-{self._app_id}",
        )
        self._thread.start()
        logger.debug("[EventBus:%s] Connection thread started → %s",
                     self._app_id, self._url)
        return self

    def disconnect(self) -> None:
        """Cleanly close the WebSocket connection and stop the thread."""
        self._running   = False
        self._connected = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.debug("[EventBus:%s] Disconnected", self._app_id)

    @property
    def connected(self) -> bool:
        """True if the WebSocket is currently open."""
        return self._connected

    def publish(self, event_type: str, data: dict) -> bool:
        """
        Publish an event to the bus.

        Returns True if the event was sent, False if the client is not
        connected (event is dropped silently with a DEBUG log).
        """
        if not self._connected or self._ws is None:
            logger.debug("[EventBus:%s] Not connected — dropping %s",
                         self._app_id, event_type)
            return False
        event = make_event(event_type, self._app_id, data)
        try:
            self._ws.send(json.dumps(event))
            logger.debug("[EventBus:%s] ↑ %s", self._app_id, event_type)
            return True
        except Exception as exc:
            logger.warning("[EventBus:%s] Publish failed: %s", self._app_id, exc)
            self._connected = False
            self._ws = None
            return False

    def on_event(self, callback: Callable[[dict], None]) -> "EventBusClient":
        """
        Register a callback invoked for every incoming event (from other apps).
        The callback receives the full event dict.
        Returns self for fluent chaining.
        """
        self._callbacks.append(callback)
        return self

    # ── Convenience publishers ─────────────────────────────────────────────────

    def emit_file_reduced(
        self,
        file_path: str,
        keyword:   str,
        scan_idx:  int,
        detector:  str = "saxs",
    ) -> bool:
        """Emit ``file.reduced`` after the reduction app writes a .dat file."""
        return self.publish("file.reduced", {
            "file_path": str(file_path),
            "keyword":   keyword,
            "scan_idx":  scan_idx,
            "detector":  detector,
        })

    def emit_file_averaged(
        self,
        file_path: str,
        keyword:   str,
        n_files:   int,
        detector:  str = "saxs",
    ) -> bool:
        """Emit ``file.averaged`` after the viewer app averages scans."""
        return self.publish("file.averaged", {
            "file_path": str(file_path),
            "keyword":   keyword,
            "n_files":   n_files,
            "detector":  detector,
        })

    def emit_file_stitched(
        self,
        file_path:    str,
        keyword:      str,
        scale_factor: float,
    ) -> bool:
        """Emit ``file.stitched`` after SAXS+WAXS auto-stitching."""
        return self.publish("file.stitched", {
            "file_path":    str(file_path),
            "keyword":      keyword,
            "scale_factor": scale_factor,
        })

    def emit_file_subtracted(
        self,
        file_path: str,
        keyword:   str,
        scale:     float,
        mode:      str,
    ) -> bool:
        """Emit ``file.subtracted`` after background subtraction."""
        return self.publish("file.subtracted", {
            "file_path": str(file_path),
            "keyword":   keyword,
            "scale":     scale,
            "mode":      mode,
        })

    def emit_analysis_complete(
        self,
        analysis_type: str,
        file_path:     str,
        results:       dict,
    ) -> bool:
        """Emit ``analysis.complete`` after any analysis run finishes."""
        return self.publish("analysis.complete", {
            "analysis_type": analysis_type,
            "file_path":     str(file_path),
            "results":       results,
        })

    def emit_watch_new_raw(
        self,
        file_path: str,
        detector:  str,
    ) -> bool:
        """Emit ``watch.new_raw`` when the reduction watcher detects a new file."""
        return self.publish("watch.new_raw", {
            "file_path": str(file_path),
            "detector":  detector,
        })

    def emit_ai_hint(
        self,
        hint:      str,
        file_path: str | None = None,
        severity:  str = "info",
    ) -> bool:
        """
        Emit ``ai.hint`` so all app UIs can display a proactive AI hint.

        ``severity`` — "info" | "warning" | "error"
        """
        return self.publish("ai.hint", {
            "hint":      hint,
            "file_path": str(file_path) if file_path else None,
            "severity":  severity,
        })

    # ── Internal ───────────────────────────────────────────────────────────────

    def _run_loop(self, retry: bool) -> None:
        """Background thread: connect → receive → reconnect on drop."""
        while self._running:
            try:
                self._connect_once()
            except Exception as exc:
                logger.debug("[EventBus:%s] Loop error: %s", self._app_id, exc)
            if not self._running:
                break
            if not retry:
                logger.debug("[EventBus:%s] retry=False — stopping", self._app_id)
                break
            logger.debug("[EventBus:%s] Reconnecting in %ds…",
                         self._app_id, _RETRY_DELAY)
            time.sleep(_RETRY_DELAY)

    def _connect_once(self) -> None:
        """Open a single WebSocket connection and block until it closes."""
        try:
            import websocket  # websocket-client package
        except ImportError:
            logger.warning(
                "[EventBus:%s] 'websocket-client' not installed — event bus "
                "disabled. Install with: pip install websocket-client",
                self._app_id,
            )
            self._running = False
            return

        ws = websocket.WebSocketApp(
            self._url,
            on_open    =self._on_open,
            on_message =self._on_message,
            on_error   =self._on_error,
            on_close   =self._on_close,
        )
        self._ws = ws
        # run_forever blocks until the connection is closed
        ws.run_forever(
            ping_interval=_PING_INTERVAL,
            ping_timeout =_PING_TIMEOUT,
        )
        self._ws        = None
        self._connected = False

    def _on_open(self, ws) -> None:
        self._connected = True
        logger.info("[EventBus:%s] Connected to hub event bus at %s",
                    self._app_id, self._url)
        # Announce presence so the hub knows which app is listening
        event = make_event("app.connected", self._app_id, {"app_id": self._app_id})
        try:
            ws.send(json.dumps(event))
        except Exception:
            pass

    def _on_message(self, ws, message: str) -> None:
        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            logger.debug("[EventBus:%s] Received non-JSON message", self._app_id)
            return
        logger.debug("[EventBus:%s] ↓ %s from %s",
                     self._app_id, event.get("type"), event.get("source_app"))
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as exc:
                logger.warning("[EventBus:%s] Callback error: %s",
                               self._app_id, exc)

    def _on_error(self, ws, error) -> None:
        logger.debug("[EventBus:%s] WS error: %s", self._app_id, error)
        self._connected = False

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.debug("[EventBus:%s] WS closed (code=%s msg=%s)",
                     self._app_id, close_status_code, close_msg)
        self._connected = False
        self._ws = None
