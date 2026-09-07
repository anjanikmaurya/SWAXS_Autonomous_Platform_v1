"""
tests/test_restart_param_divergence.py — displayed values must equal executed values.

The bug: after a hub restart while apps were stale, an app fell back to its INTERNAL
defaults while the input fields still showed the operator's values, silently. Two
concrete instances are fixed here (reactor + background); the fix has three parts:

  RC1  the UI reflects the server's effective settings on load (status now carries
       run_settings; the truncation panel already re-fetches), so displayed==executed.
  RC2  restoring the setting VALUES is decoupled from resuming the loop: values come
       back on EVERY restart (honour_no_resume=False), while auto-run / the monitor
       loop stay behind SWAXS_RESUME. Previously the (correct-looking) restore code
       was routed through load_state's resume gate and was INERT unless SWAXS_RESUME=1.
  RC3  params that were never persisted at all (background _TRUNC) are now persisted;
       if a saved value cannot be restored, a LOUD banner names it — never a silent
       default.

Every test here runs with SWAXS_RESUME UNSET (the shipped default), because the whole
point is that the operator's values survive a restart WITHOUT opting into resume.
"""
from __future__ import annotations

import importlib.util as u
import json
import sys
import time
from pathlib import Path

import pytest

from src.runstate import save_state, load_state, state_path, save_monitor


def _load(tag: str, path: str):
    """Import an app module fresh — the closest thing to a process restart."""
    spec = u.spec_from_file_location(tag, path)
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _fresh_start_default(monkeypatch):
    """Assert the SHIPPED default: resume OFF. The fix must work here regardless."""
    monkeypatch.delenv("SWAXS_RESUME", raising=False)
    monkeypatch.delenv("SWAXS_NO_RESUME", raising=False)
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")     # no monitor/boot threads in tests


# ══ REACTOR ══════════════════════════════════════════════════════════════════
def test_reactor_run_settings_restore_with_resume_OFF(tmp_path, monkeypatch):
    """(a) After a restart the operator's run settings come back — displayed values
    a run would use match what was set — even though resume is OFF by default."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")

    r1 = _load("rxpd_1", "reactor/app.py")
    try:
        r1.app.test_client().post("/api/run_settings",
                                  json={"run_duration": 45, "flush_duration": 90,
                                        "flush_rate": 120, "arm_mode": "temperature",
                                        "arm_wait_s": 200})
        assert r1._ctrl.live_duration == 45
    finally:
        r1._ctrl.shutdown()

    # A fresh process, resume still OFF: the values must be restored anyway.
    r2 = _load("rxpd_2", "reactor/app.py")
    try:
        assert r2._ctrl.live_duration == 45, "run duration reverted to config default"
        assert r2._ctrl.live_flush_duration == 90
        assert r2._ctrl.live_flush_rate == 120
        assert r2._ctrl.live_arm_wait == 200
        # RC1: what the UI reflects (status.run_settings) == what a run executes.
        rs = r2._ctrl.status()["run_settings"]
        assert rs["run_duration"] == 45 and rs["flush_duration"] == 90
        assert rs["arm_mode"] == "temperature" and rs["arm_wait_s"] == 200
        # RC banner: calm "restored" tier.
        assert r2._RESTART_NOTICE["level"] == "restored"
        assert r2._RESTART_NOTICE["params"], "restored banner names nothing"
    finally:
        r2._ctrl.shutdown()


def test_reactor_arm_mode_does_not_revert_timed_on_restart(tmp_path, monkeypatch):
    """(c) The reported hazard: operator chose temperature-gated arming; a restart
    must NOT silently revert to a fixed timed wait (which would start pumps before
    the reactor reached T_reac → synthesis at the wrong temperature)."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")

    r1 = _load("rxpd_arm1", "reactor/app.py")
    try:
        r1.app.test_client().post("/api/run_settings", json={"arm_mode": "temperature"})
        assert r1._ctrl.live_arm_mode == "temperature"
    finally:
        r1._ctrl.shutdown()

    r2 = _load("rxpd_arm2", "reactor/app.py")
    try:
        assert r2._ctrl.live_arm_mode == "temperature", "arm mode was lost on restart"
        assert r2._ctrl.status()["run_settings"]["arm_mode"] == "temperature", \
            "a run would arm 'timed' while the UI shows temperature — the divergence"
    finally:
        r2._ctrl.shutdown()


def test_reactor_default_arm_mode_is_temperature(tmp_path, monkeypatch):
    """Even with NOTHING ever set, the effective default arm mode is temperature —
    it matches the UI default and controller._arm_mode, so an untouched reactor does
    not silently arm 'timed'. (config.yml default_mode aligned to 'temperature'.)"""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")
    r = _load("rxpd_def", "reactor/app.py")
    try:
        assert r._ctrl.live_arm_mode is None, "no live value should be set"
        assert r._ctrl.default_arm_mode == "temperature"
        assert r._ctrl.status()["run_settings"]["arm_mode"] == "temperature"
        assert r._RESTART_NOTICE["level"] == "none", "fresh start must not raise a banner"
    finally:
        r._ctrl.shutdown()


def test_reactor_unrestorable_settings_shout_not_silently_default(tmp_path, monkeypatch):
    """(b) A saved settings file that cannot be restored (stale > 48 h) must NOT be
    applied silently as defaults — the loud 'lost' banner fires and names the params."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")

    save_state(str(tmp_path), "reactor_run_settings",
               {"run_duration": 45, "arm_mode": "temperature"})
    f = state_path(str(tmp_path), "reactor_run_settings")
    d = json.loads(f.read_text()); d["_saved_at"] = time.time() - 100 * 3600
    f.write_text(json.dumps(d))

    r = _load("rxpd_lost", "reactor/app.py")
    try:
        assert r._ctrl.live_duration is None, "a stale file must not be applied"
        assert r._RESTART_NOTICE["level"] == "lost", "no loud banner for lost settings"
        assert "run_duration" in r._RESTART_NOTICE["params"]
    finally:
        r._ctrl.shutdown()


# ══ BACKGROUND (the _TRUNC grid shapes EVERY written file) ═══════════════════
def test_background_trunc_survives_restart_with_resume_OFF(tmp_path, monkeypatch):
    """(a)+RC3 The truncation grid is persisted and restored with resume OFF, and
    what the panel shows (GET /api/truncation) equals what a write uses (_TRUNC)."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))

    b1 = _load("bgpd_1", "background/app.py")
    b1.app.test_client().post("/api/truncation",
                              json={"q_min": 0.05, "q_max": 0.5, "n_points": 200,
                                    "spacing": "log", "q_unit": "nm"})
    assert state_path(str(tmp_path), "background_truncation").is_file()

    b2 = _load("bgpd_2", "background/app.py")   # import runs _restore_trunc()
    assert b2._TRUNC["q_min"] == 0.05, "truncation q_min reverted to the default grid"
    assert b2._TRUNC["q_max"] == 0.5 and b2._TRUNC["n_points"] == 200
    assert b2._TRUNC["spacing"] == "log" and b2._TRUNC["q_unit"] == "nm"
    # displayed (panel) == executed (write) : GET returns exactly what _TRUNC holds.
    shown = b2.app.test_client().get("/api/truncation").get_json()
    assert shown["q_min"] == b2._TRUNC["q_min"] and shown["n_points"] == b2._TRUNC["n_points"]
    assert b2._TRUNC_NOTICE["level"] == "restored"


def test_background_trunc_unrestorable_shouts_not_silently_default(tmp_path, monkeypatch):
    """(b)+RC3 A stale saved grid must not silently apply; every file would write on
    the DEFAULT grid, so the loud banner fires and names the grid. _TRUNC stays at
    the module defaults (not the stale values)."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    save_state(str(tmp_path), "background_truncation",
               {"enabled": True, "q_min": 0.05, "q_max": 0.5, "n_points": 200,
                "spacing": "log", "q_unit": "nm"})
    f = state_path(str(tmp_path), "background_truncation")
    d = json.loads(f.read_text()); d["_saved_at"] = time.time() - 100 * 3600
    f.write_text(json.dumps(d))

    b = _load("bgpd_lost", "background/app.py")
    assert b._TRUNC["q_min"] == 0.03 and b._TRUNC["n_points"] == 549, \
        "a stale grid was silently applied instead of the visible defaults"
    assert b._TRUNC_NOTICE["level"] == "lost"
    assert b._TRUNC_NOTICE["params"], "lost banner names nothing"


def test_background_fresh_start_no_banner_and_defaults_shown(tmp_path, monkeypatch):
    """A genuine fresh start (nothing saved) must NOT raise a banner — the module
    defaults are exactly what the panel shows, so displayed==executed already."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    b = _load("bgpd_fresh", "background/app.py")
    assert b._TRUNC_NOTICE["level"] == "none"
    shown = b.app.test_client().get("/api/truncation").get_json()
    assert shown["q_min"] == 0.03 and shown["q_max"] == 0.6 and shown["n_points"] == 549


# ══ AVERAGE (the app the operator originally observed) ═══════════════════════
_AA_BODY = {"frames_per_average": 12, "interval": 7, "keywords": ["Run5"],
            "i0_filter_pct": 3, "q_min": 0.1, "q_max": 2.0,
            "saxs_folder": "x", "resume": True}


def test_average_settings_restore_with_resume_OFF(tmp_path, monkeypatch):
    """(a) After a restart the LAST auto-average settings are exposed for the UI to
    display, with resume OFF, so a manual Start runs what the operator sees — not the
    30-frame / all-keywords HTML defaults the observed bug fell back to."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    # a crash while running: state persisted with running=True
    save_monitor(str(tmp_path), "average", True, _AA_BODY)

    m = _load("avpd_1", "average/app.py")   # import runs _restore_settings()
    assert m._LAST_SETTINGS["frames_per_average"] == 12
    assert m._LAST_SETTINGS["interval"] == 7
    assert m._LAST_SETTINGS["keywords"] == ["Run5"]
    assert m._AVG_NOTICE["level"] == "restored"
    # displayed source: status carries last_settings for the UI to fill the fields.
    st = m.app.test_client().get("/api/monitor/status").get_json()
    assert st["last_settings"]["frames_per_average"] == 12
    assert st["last_settings"]["keywords"] == ["Run5"]


def test_average_running_status_reflects_all_params(tmp_path, monkeypatch):
    """RC1: a running monitor's status must expose the FULL param set (keywords, i0,
    q-range — not just batch size/interval) so the UI reflects exactly what runs."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    (tmp_path / "red").mkdir()
    m = _load("avpd_run", "average/app.py")
    c = m.app.test_client()
    try:
        r = c.post("/api/monitor/start", json={
            "frames_per_average": 9, "interval": 1, "keywords": ["A", "B"],
            "i0_filter_pct": 4, "q_min": 0.2, "q_max": 3.0,
            "saxs_folder": str(tmp_path / "red")})
        assert (r.get_json() or {}).get("ok"), r.get_json()
        st = c.get("/api/monitor/status").get_json()
        assert st["frames_per_average"] == 9 and st["interval"] == 1
        assert st["keywords"] == ["A", "B"] and st["i0_filter_pct"] == 4
        assert st["q_min"] == 0.2 and st["q_max"] == 3.0
    finally:
        c.post("/api/monitor/stop")
        m._avg_monitoring = False


def test_average_lost_settings_shout_not_silently_default(tmp_path, monkeypatch):
    """(b) A stale saved-running file must NOT be applied silently; the loud banner
    fires and _LAST_SETTINGS stays empty (fields will show visible defaults)."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    save_monitor(str(tmp_path), "average", True, _AA_BODY)
    f = state_path(str(tmp_path), "average_monitor")
    d = json.loads(f.read_text()); d["_saved_at"] = time.time() - 100 * 3600
    f.write_text(json.dumps(d))

    m = _load("avpd_lost", "average/app.py")
    assert m._LAST_SETTINGS == {}, "a stale monitor body was applied silently"
    assert m._AVG_NOTICE["level"] == "lost"
    assert m._AVG_NOTICE["params"]


def test_average_stopped_monitor_no_banner(tmp_path, monkeypatch):
    """A monitor the operator STOPPED must not raise a banner on the next start —
    there is nothing to restore, and the fields correctly show defaults."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    save_monitor(str(tmp_path), "average", True, _AA_BODY)
    save_monitor(str(tmp_path), "average", False)     # operator stopped it
    m = _load("avpd_stopped", "average/app.py")
    assert m._AVG_NOTICE["level"] == "none"
    assert m._LAST_SETTINGS == {}
