"""
tests/test_hub_crash_reporting.py — a Stop is not a crash.

THE BUG
-------
The hub card read **"⚠ CRASHED (exit null) · logs/reduction.log · press Start"**
after the operator pressed Stop. Nothing had crashed.

`_stop_app` sets `_procs[aid] = None`, and `_detect_crashes` computed

    running = proc is not None and proc.poll() is None

so a deliberate stop looked exactly like a running→dead transition. It then read
the exit code off a handle it had already discarded, got `None`, and the UI
rendered that as the string "null" — an alarming message, about an event that did
not happen, with no information in it.

Locked down here:
  1. A deliberate stop is never reported as a crash.
  2. A real crash IS reported, with a usable reason ("exit 1", "killed by
     SIGKILL") and the tail of the app's log.
  3. Starting again clears the badge.
  4. The status snapshot and the SSE stream agree — they used to be two copies of
     the same loop and only one of them knew about crashes.
  5. A failing status tick cannot kill the stream.
"""
from __future__ import annotations

import importlib.util as u
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import proc_lifecycle as pl                  # noqa: E402

psutil = pytest.importorskip("psutil")
APP_ID = "quality"


def _hub(tag="hubcrash"):
    spec = u.spec_from_file_location(tag, str(ROOT / "hub" / "app.py"))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


def _wait(pred, timeout=25.0, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def hub():
    h = _hub()
    yield h
    for a in h.APPS:
        try:
            h._stop_app(a["id"])
        except Exception:
            pass


# ── the exit-reason formatter ────────────────────────────────────────────────
def test_an_unknown_exit_code_never_renders_as_null(hub):
    """"exit null" was the literal message an operator was asked to act on."""
    assert hub._exit_reason(None) == "unknown"
    assert "null" not in hub._exit_reason(None)
    assert hub._exit_reason(0) == "exit 0"
    assert hub._exit_reason(1) == "exit 1"
    assert hub._exit_reason(-signal.SIGKILL) == "killed by SIGKILL"
    assert hub._exit_reason(-signal.SIGTERM) == "killed by SIGTERM"
    assert "signal" in hub._exit_reason(-99) or "killed" in hub._exit_reason(-99)


def test_log_tail_is_bounded_and_never_raises(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "x.log").write_text("\n".join(f"line {i}" for i in range(400)))
    tail = hub._log_tail("x", lines=12)
    assert len(tail) == 12 and tail[-1] == "line 399"
    assert hub._log_tail("absent") == []          # must not raise


# ── a stop is not a crash ────────────────────────────────────────────────────
@pytest.mark.slow
def test_a_deliberate_stop_is_not_reported_as_a_crash(hub):
    port = hub._app_by_id(APP_ID)["port"]
    if pl.port_in_use(port):
        pytest.skip(f"port {port} busy on this machine")
    ok, msg = hub._start_app(APP_ID)
    assert ok, msg
    assert _wait(lambda: pl.port_in_use(port))
    hub._detect_crashes()
    assert hub._crashed.get(APP_ID) is None

    hub._stop_app(APP_ID)
    for _ in range(3):                      # several status ticks
        hub._detect_crashes()
    assert hub._crashed.get(APP_ID) is None, \
        f"pressing Stop reported a crash: {hub._crashed.get(APP_ID)}"
    assert hub._app_status()[APP_ID]["crashed"] is None
    assert hub._app_status()[APP_ID]["running"] is False


@pytest.mark.slow
def test_a_real_crash_is_reported_with_a_reason_and_a_log_tail(hub):
    port = hub._app_by_id(APP_ID)["port"]
    if pl.port_in_use(port):
        pytest.skip(f"port {port} busy on this machine")
    assert hub._start_app(APP_ID)[0]
    assert _wait(lambda: pl.port_in_use(port))
    hub._detect_crashes()

    os.kill(hub._procs[APP_ID].pid, signal.SIGKILL)     # died on its own
    assert _wait(lambda: (hub._detect_crashes(), hub._crashed.get(APP_ID))[1] is not None,
                 timeout=15)
    c = hub._crashed[APP_ID]
    assert c["exit_code"] == -signal.SIGKILL
    assert c["reason"] == "killed by SIGKILL"
    assert isinstance(c["tail"], list) and c["tail"], "no log tail captured"
    assert hub._procs[APP_ID] is None, "the spent handle was not reaped"
    # and the crash is visible through both status paths
    assert hub._app_status()[APP_ID]["crashed"]["reason"] == "killed by SIGKILL"

    assert hub._start_app(APP_ID)[0]
    assert hub._crashed.get(APP_ID) is None, "Start did not clear the crash badge"


@pytest.mark.slow
def test_an_app_that_dies_at_startup_reports_its_traceback(hub, tmp_path):
    """The case that matters: a missing .poni, a bad config. The reason must be
    the exit code and the tail must contain the actual error line."""
    entry = tmp_path / "boom_app.py"
    entry.write_text("import sys\n"
                     "print('loading config', flush=True)\n"
                     "raise SystemExit('config file missing: poni/atT_SAXS.poni')\n")
    hub.APPS.append({"id": "boom", "port": 5199, "entry": str(entry),
                     "icon": "x", "name": "Boom", "color": "#000"})
    hub._procs["boom"] = None
    ok, _ = hub._start_app("boom")
    assert ok
    assert _wait(lambda: (hub._detect_crashes(), hub._crashed.get("boom"))[1] is not None,
                 timeout=15)
    c = hub._crashed["boom"]
    assert c["exit_code"] == 1 and c["reason"] == "exit 1"
    assert any("atT_SAXS.poni" in ln for ln in c["tail"]), c["tail"]


# ── the two status paths agree ───────────────────────────────────────────────
def test_snapshot_and_stream_report_the_same_shape(hub):
    hub._crashed["quality"] = {"exit_code": 1, "reason": "exit 1",
                               "at": time.time(), "tail": ["boom"]}
    snap = hub.app.test_client().get("/api/status").get_json()
    assert snap["apps"]["quality"]["crashed"]["reason"] == "exit 1", \
        "the snapshot used to omit crashes entirely, so a page load hid them"
    gen = hub.app.view_functions["api_status_stream"]().response
    frame = json.loads(next(iter(gen)).split("data: ", 1)[1])
    assert frame["apps"]["quality"]["crashed"]["reason"] == "exit 1"
    assert set(frame["apps"]["quality"]) == set(snap["apps"]["quality"]), \
        "the stream and the snapshot describe an app differently"
    gen.close()


def test_a_failing_status_tick_does_not_kill_the_stream(hub, monkeypatch):
    """An unhandled exception here ended the generator, which closes the SSE
    stream — the page then froze showing minutes-old state."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(hub, "_app_status", flaky)
    gen = hub.app.view_functions["api_status_stream"]().response
    it = iter(gen)
    first = json.loads(next(it).split("data: ", 1)[1])
    assert first["hub_error"] and "boom" in first["hub_error"]
    assert first["apps"] == {}
    second = json.loads(next(it).split("data: ", 1)[1])      # stream survived
    assert second["hub_error"] is None
    it.close()


def test_one_health_request_per_app_per_tick(hub, monkeypatch):
    """It used to make two requests to the same endpoint for every app on every
    2 s tick, and each could block for the full timeout."""
    seen = []

    class R:
        status = 200
        def read(self, *a): return b'{"status":"ok"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(hub.urllib.request, "urlopen",
                        lambda url, timeout=None: (seen.append(url), R())[1])
    monkeypatch.setattr(hub, "_is_running", lambda aid: True)
    monkeypatch.setattr(hub, "_procs", {a["id"]: None for a in hub.APPS})
    hub._app_status()
    assert len(seen) == len(hub.APPS), \
        f"{len(seen)} health requests for {len(hub.APPS)} apps"


def test_a_health_endpoint_returning_junk_is_still_alive(hub, monkeypatch):
    class R:
        status = 200
        def read(self, *a): return b"<html>not json</html>"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(hub.urllib.request, "urlopen", lambda *a, **k: R())
    alive, summary = hub._health_probe(1234)
    assert alive is True and summary is None
