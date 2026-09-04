"""
tests/test_event_naming_and_health_debounce.py

Two real bugs, both caught live by a user running the platform, not by any
prior test:

1. The analyzer and the Data Analysis app both published a bus event named
   `analysis.complete`, with two completely different payload shapes. The
   hub's event log was written for the Data Analysis app's shape
   (`analysis_type`, `file_path`) and rendered the analyzer's events (which
   have `file`, `size`, `pdi`, ...) as the literal text "undefined on ?". The
   reactor's Slack notifier had the same problem in reverse: it listened for
   `analysis.complete` expecting the analyzer's shape, so it would also fire
   a garbage message for every Data Analysis app result. Fixed by renaming
   the analyzer's event to `fit.complete`.

2. The hub's status tick flashed a running app's card to "not responding" and
   back within one tick whenever a single `/api/health` probe missed its 1 s
   timeout -- which the assistant's one-time startup knowledge-base warm-up
   (embedding every knowledge.md into ChromaDB) can do easily, since that
   work can hold the GIL past the timeout. Fixed with a 2-consecutive-miss
   debounce before reporting an app as not responding.
"""
from __future__ import annotations

import importlib.util as u
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(tag: str, path: str):
    spec = u.spec_from_file_location(tag, path)
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


# ── 1. the event name must not collide ──────────────────────────────────────
def test_the_analyzer_does_not_reuse_the_data_analysis_apps_event_name():
    """The analyzer must publish fit.complete, never analysis.complete -- that
    name is the Data Analysis app's, with an incompatible payload shape."""
    src = (ROOT / "analyzer" / "app.py").read_text()
    assert '_bus.publish("fit.complete"' in src, \
        "the analyzer must publish fit.complete"
    assert '_bus.publish("analysis.complete"' not in src, \
        "the analyzer must not reuse the Data Analysis app's event name"


def test_the_reactor_listens_for_the_analyzers_actual_event_name():
    """The reactor's Slack notifier must key off fit.complete -- keying off
    analysis.complete would also fire (with a garbage/empty message) on every
    Guinier/Porod/Kratky/peak/model result from the Data Analysis app, which
    publishes genuine analysis.complete events with a different shape."""
    src = (ROOT / "reactor" / "app.py").read_text()
    assert 'etype == "fit.complete"' in src
    assert 'etype == "analysis.complete"' not in src


def test_the_hub_can_render_a_fit_complete_event_without_undefined_fields():
    """Regression for the literal bug report: the hub log rendered the
    analyzer's event as "undefined on ?". The template's fit.complete branch
    must read the analyzer's actual field names (file/size/pdi/confidence),
    not the Data Analysis app's (analysis_type/file_path)."""
    html = (ROOT / "hub" / "templates" / "index.html").read_text()
    m = re.search(r"case 'fit\.complete':(.*?)case '", html, re.S)
    assert m, "no fit.complete case in the hub's event-summary switch"
    branch = m.group(1)
    assert "data.file" in branch and "data.size" in branch and "data.confidence" in branch
    assert "data.analysis_type" not in branch, \
        "fit.complete branch is reading the wrong event's field names"


# ── 2. health-check debounce ─────────────────────────────────────────────────
def test_a_single_missed_health_probe_does_not_flip_a_running_app_to_not_responding(monkeypatch):
    """One slow /api/health tick (e.g. the assistant's KB warm-up holding the
    GIL) must not flash the card to "not responding". Two consecutive misses
    should."""
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    h = _load("hub_debounce", "hub/app.py")

    aid = h.APPS[0]["id"]
    monkeypatch.setattr(h, "_is_running", lambda a: True)

    calls = {"n": 0}
    def flaky_probe(port, timeout=1.0):
        calls["n"] += 1
        return (False, None) if calls["n"] == 1 else (True, None)
    monkeypatch.setattr(h, "_health_probe", flaky_probe)

    out1 = h._app_status()
    assert out1[aid]["healthy"] is True, \
        "a single missed probe must not report the app as not responding"

    out2 = h._app_status()
    assert out2[aid]["healthy"] is True, "probe recovered — should read healthy"


def test_two_consecutive_missed_probes_do_report_not_responding(monkeypatch):
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    h = _load("hub_debounce2", "hub/app.py")

    aid = h.APPS[0]["id"]
    monkeypatch.setattr(h, "_is_running", lambda a: True)
    monkeypatch.setattr(h, "_health_probe", lambda port, timeout=1.0: (False, None))

    h._app_status()                      # miss 1 -- still optimistic
    out = h._app_status()                # miss 2 -- now report it
    assert out[aid]["healthy"] is False, \
        "a genuinely stuck app must still be reported, just not on the first miss"


def test_a_stopped_app_is_never_reported_healthy_regardless_of_streak(monkeypatch):
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    h = _load("hub_debounce3", "hub/app.py")
    aid = h.APPS[0]["id"]
    monkeypatch.setattr(h, "_is_running", lambda a: False)
    out = h._app_status()
    assert out[aid]["healthy"] is False
    assert out[aid]["running"] is False
