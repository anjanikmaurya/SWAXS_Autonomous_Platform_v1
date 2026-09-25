"""Collapse the redundant double flush in background_when='before' closed loops.

Reported from the reactor UI: the flush after a synthesis looked ~2x the set
duration. Cause: in a closed loop the next condition is not queued when synthesis
ends (it depends on this run's result), so the "flush doubles as the next blank"
merge never fires. The reactor does a full post-synthesis flush (clears product),
goes ready, and then — once the optimizer proposes — runs ANOTHER full flush as
the pre-synthesis blank. Two full flushes per cycle.

Fix: the line is already clean coming out of the post-synthesis flush (nothing
flows while waiting), so the pre-synthesis blank uses a short rinse
(flush.blank_rinse_s) instead of a second full flush. Arming afterwards keeps the
capillary clean while the background collection finishes.
"""
from __future__ import annotations

import importlib

import pytest

from src.reactor.config import PUMP_NAMES
from src.reactor.controller import ReactorController
from src.reactor.recipe import Recipe

_CFG = {
    "pumps": {n: {"max_flow": 1000.0, "sensor_min": 1.0} for n in PUMP_NAMES},
    "bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
               "x_each": [0, 0.3], "x_sum_max": 0.9},
    "run": {"default_duration": 5.0},
    "spec": {"enabled": True, "background_when": "before"},
    "flush": {"rate": 50.0, "duration": 1200.0, "blank_rinse_s": 30.0,
              "pump": "ode_dilution"},
}


def _ctl():
    c = ReactorController(_CFG, backend="mock")
    c._spec_enabled = True                       # exercise the before-mode blank path
    return c


def _recipe(rid):
    return Recipe(T_reac=240, F_tot=80, x_ODE=0.2, x_TOP=0.1, x_oley=0.1, recipe_id=rid)


def test_blank_uses_short_rinse_when_line_is_clean(monkeypatch):
    c = _ctl()
    try:
        calls = []
        monkeypatch.setattr(c, "_enter_flush",
                            lambda **kw: calls.append(kw))
        c._line_clean = True                     # e.g. just finished a post-synthesis flush
        c.queue.append((_recipe("Run7_r002"), {"ode": 10.0}))
        c._begin_next()
        assert len(calls) == 1
        assert calls[0]["kind"] == "blank"
        assert calls[0]["duration"] == 30.0, "clean line should get the short rinse"
    finally:
        c.shutdown()


def test_blank_uses_full_flush_when_line_is_dirty(monkeypatch):
    """Cold start / post-abort: nothing has cleaned the line, so a full flush."""
    c = _ctl()
    try:
        calls = []
        monkeypatch.setattr(c, "_enter_flush", lambda **kw: calls.append(kw))
        c._line_clean = False
        c.queue.append((_recipe("Run7_r001"), {"ode": 10.0}))
        c._begin_next()
        assert calls[0]["kind"] == "blank"
        assert calls[0]["duration"] is None, "dirty line must do a full flush (None)"
    finally:
        c.shutdown()


def test_line_clean_toggles_with_flow_and_flush():
    c = _ctl()
    try:
        c._line_clean = True
        c.current = _recipe("Run7_r003")
        c.setpoints = {n: 0.0 for n in c.pumps.pumps}
        c._enter_running()                       # reagents flow → dirty
        assert c._line_clean is False
        c._flush_kind = "flush"
        c._end_flush()                           # a completed flush → clean
        assert c._line_clean is True
    finally:
        c.shutdown()


def test_blank_rinse_zero_disables_the_shortcut():
    c = _ctl()
    try:
        c.blank_rinse_s = 0.0
        c._line_clean = True
        assert c._blank_flush_duration() is None, "0 disables the short rinse"
    finally:
        c.shutdown()


def test_default_blank_rinse_present_in_config():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "reactor" / "config.yml").read_text())
    assert float(cfg["flush"]["blank_rinse_s"]) > 0
