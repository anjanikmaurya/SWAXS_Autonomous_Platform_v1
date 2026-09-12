"""
src/watchdog/probes.py — Read-only HTTP health checks on other apps.

Queries app endpoints to see if a stage's monitor is alive. All I/O is
fire-and-forget with short timeouts; failures are logged but never raised.

Used by diagnose.py to add confidence to stall messages: "subtraction is
stalled AND the background app's monitor is not responding" is more
actionable than just "subtraction is stalled".
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# App ports and their /api/monitor/status endpoint
MONITOR_APPS = {
    "reduction": 5102,
    "average": 5103,
    "background": 5104,
    "quality": 5105,
}

ANALYZER_PORT = 5107
REACTOR_PORT = 5108


def check_monitor_alive(port: int, timeout_s: float = 2.0) -> bool:
    """
    GET :port/api/monitor/status and check if {"monitoring": true}.
    Returns True only if the app responds and is actively monitoring.
    """
    url = f"http://localhost:{port}/api/monitor/status"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read().decode())
            return bool(data.get("monitoring"))
    except Exception as exc:
        logger.debug("monitor check failed for port %d: %s", port, exc)
        return False


def fetch_monitor_status(port: int, timeout_s: float = 2.0) -> dict:
    """
    GET :port/api/monitor/status and return the WHOLE dict ({} on failure).

    ``check_monitor_alive`` answers "is it monitoring?"; this returns everything
    the app publishes about that monitor, so the dashboard can show the numbers
    the app itself computed (e.g. the average app's frames-per-batch gate)
    instead of re-deriving them and risking a different answer.
    """
    url = f"http://localhost:{port}/api/monitor/status"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read().decode())
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("monitor status fetch failed for port %d: %s", port, exc)
        return {}


def check_analyzer_status(timeout_s: float = 2.0) -> dict:
    """
    GET :5107/api/campaign and return status dict.
    Keys: status, n_evaluations, budget, pending.
    Returns empty dict on failure.
    """
    url = f"http://localhost:{ANALYZER_PORT}/api/campaign"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        logger.debug("analyzer check failed: %s", exc)
        return {}


def check_reactor_status(timeout_s: float = 2.0) -> dict:
    """
    GET :5108/api/status and return state dict.
    Keys: state, queue, runs, auto_run, etc.
    Returns empty dict on failure.
    """
    url = f"http://localhost:{REACTOR_PORT}/api/status"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        logger.debug("reactor check failed: %s", exc)
        return {}


def probe_all() -> dict:
    """
    Perform all health checks and return a summary dict.

    Keys:
        monitors: dict of app_id → bool (monitoring status)
        status:   dict of app_id → the app's full /api/monitor/status payload
                  ({} for an app that did not answer)
        analyzer: dict from /api/campaign (empty on fail)
        reactor:  dict from /api/status (empty on fail)

    One HTTP call per monitor app: ``monitors`` is derived from the same payload
    ``status`` carries, so adding the detail cost no extra round trips.
    """
    statuses = {
        app_id: fetch_monitor_status(port)
        for app_id, port in MONITOR_APPS.items()
    }
    return {
        "monitors": {
            app_id: bool(payload.get("monitoring"))
            for app_id, payload in statuses.items()
        },
        "status": statuses,
        "analyzer": check_analyzer_status(),
        "reactor": check_reactor_status(),
    }
