"""
tests/test_restart_recovery.py — the platform must survive a restart.

Before this, a 03:00 restart left everything looking correctly restored (the hub
even remembered the project folder) while doing nothing: every automation loop
was off and the Bayesian campaign was gone. Frames accumulated, nothing was
processed, and all nine cards were green.

One deliberate asymmetry is asserted here: the DATA apps resume themselves, but
the reactor's auto-run does NOT — it moves pumps, and a power blip must not start
reagents flowing with nobody in the hutch.
"""
from __future__ import annotations

import importlib.util as u
import json
import os
import sys
import time
from pathlib import Path

import pytest

from src.runstate import (save_state, load_state, save_monitor, load_monitor,
                          clear_state, resume_disabled, ENV_NO_RESUME)


def _load(tag: str, path: str):
    """Import an app module fresh — the closest thing to a process restart."""
    spec = u.spec_from_file_location(tag, path)
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


# ── the state helper ─────────────────────────────────────────────────────────
def test_state_roundtrip_and_atomicity(tmp_path):
    assert save_state(tmp_path, "thing", {"a": 1}) is True
    assert (tmp_path / ".swaxs_state" / "thing.json").is_file()
    assert load_state(tmp_path, "thing")["a"] == 1
    assert not list((tmp_path / ".swaxs_state").glob("*.tmp")), "temp file left behind"


def test_missing_or_unreadable_state_returns_none(tmp_path):
    assert load_state(tmp_path, "absent") is None
    p = tmp_path / ".swaxs_state" / "bad.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert load_state(tmp_path, "bad") is None      # must not raise


def test_stale_state_is_not_resumed(tmp_path):
    """Resuming a monitor from a state file written days ago — a different sample
    series — is worse than not resuming at all."""
    save_monitor(tmp_path, "viewer", True, {"interval": 5})
    f = tmp_path / ".swaxs_state" / "viewer_monitor.json"
    d = json.loads(f.read_text()); d["_saved_at"] = time.time() - 100 * 3600
    f.write_text(json.dumps(d))
    assert load_monitor(tmp_path, "viewer", max_age_h=48) is None
    assert load_monitor(tmp_path, "viewer", max_age_h=200) == {"interval": 5}


def test_no_resume_env_var_disables_everything(tmp_path, monkeypatch):
    save_monitor(tmp_path, "viewer", True, {"interval": 5})
    monkeypatch.setenv(ENV_NO_RESUME, "1")
    assert resume_disabled() is True
    assert load_monitor(tmp_path, "viewer") is None
    assert load_state(tmp_path, "viewer_monitor") is None


def test_a_stopped_monitor_is_not_resumed(tmp_path):
    save_monitor(tmp_path, "viewer", True, {"interval": 5})
    save_monitor(tmp_path, "viewer", False)
    assert load_monitor(tmp_path, "viewer") is None


def test_no_project_root_is_a_safe_noop():
    assert save_state("", "x", {"a": 1}) is False
    assert load_state("", "x") is None


# ── the data apps resume their own loops ─────────────────────────────────────
@pytest.mark.parametrize("app_name,path,flag", [
    ("quality", "quality/app.py", "_grading"),
])
def test_data_app_resumes_its_monitor_after_restart(tmp_path, monkeypatch,
                                                    app_name, path, flag):
    (tmp_path / "1D" / "SAXS" / "Subtracted").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))

    m1 = _load(f"{app_name}_s1", path)
    body = {"interval": 5, "saxs_folder": str(tmp_path / "1D" / "SAXS" / "Subtracted"),
            "waxs_folder": ""}
    r = m1.app.test_client().post("/api/monitor/start", json=body)
    assert (r.get_json() or {}).get("ok"), r.get_json()
    assert getattr(m1, flag) is True
    saved = tmp_path / ".swaxs_state" / f"{app_name}_monitor.json"
    assert saved.is_file() and json.loads(saved.read_text())["running"] is True

    # simulate the restart
    m2 = _load(f"{app_name}_s2", path)
    assert getattr(m2, flag) is False, "a fresh process should start idle"
    m2._boot_resume_monitor()
    assert getattr(m2, flag) is True, "the monitor did not resume"
    assert any("RESUMED" in l["msg"] for _, l in list(m2._log))


def test_stopping_the_monitor_prevents_a_later_resume(tmp_path, monkeypatch):
    (tmp_path / "1D" / "SAXS" / "Subtracted").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    m1 = _load("q_stop1", "quality/app.py")
    c = m1.app.test_client()
    body = {"interval": 5, "saxs_folder": str(tmp_path / "1D" / "SAXS" / "Subtracted"),
            "waxs_folder": ""}
    c.post("/api/monitor/start", json=body)
    c.post("/api/monitor/stop")
    m2 = _load("q_stop2", "quality/app.py")
    m2._boot_resume_monitor()
    assert m2._grading is False, "resumed a monitor the operator had stopped"


# ── the campaign survives a restart ──────────────────────────────────────────
def test_campaign_is_restored_with_the_same_history(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    (tmp_path / "1D" / "SAXS" / "Subtracted").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))

    a1 = _load("az_r1", "analyzer/app.py")
    a1.app.test_client().post("/api/campaign/start", json={
        "target_size": 4.0, "tolerance": 0.3, "pdi_cap": 0.15,
        "budget": 25, "n_init": 6})
    # deliberately OFF target so the campaign keeps running
    for i in range(3):
        rid = list(a1._pending)[0]
        a1._feed_campaign(f"{rid}_sample_SAXS_subtracted.dat",
                          {"size": {"radius": 8.0 + i}, "pdi": 0.4, "confidence": 0.9})
    assert len(a1._campaign.history) == 3
    assert (tmp_path / ".swaxs_state" / "campaign.json").is_file()

    a2 = _load("az_r2", "analyzer/app.py")
    assert a2._campaign is None, "a fresh process should start with no campaign"
    a2._restore_campaign()
    assert a2._campaign is not None, "campaign was not restored"
    assert len(a2._campaign.history) == 3, "history not replayed"
    assert [round(h["loss"], 6) for h in a2._campaign.history] == \
           [round(h["loss"], 6) for h in a1._campaign.history], "GP state differs"
    assert len(a2._pending) == 1, "the outstanding condition was lost"
    assert any("RESUMED" in l["msg"] for _, l in list(a2._log))


def test_a_finished_campaign_is_not_resumed(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    a1 = _load("az_f1", "analyzer/app.py")
    a1.app.test_client().post("/api/campaign/start", json={
        "target_size": 4.0, "tolerance": 0.3, "pdi_cap": 0.15,
        "budget": 25, "n_init": 6})
    rid = list(a1._pending)[0]
    # on-target + low PDI → converged
    a1._feed_campaign(f"{rid}_sample_SAXS_subtracted.dat",
                      {"size": {"radius": 4.05}, "pdi": 0.02, "confidence": 0.9})
    assert a1._campaign.status_str == "converged"

    a2 = _load("az_f2", "analyzer/app.py")
    a2._restore_campaign()
    assert a2._campaign is None, "a converged campaign must not restart itself"
    assert any("not resuming" in l["msg"] for _, l in list(a2._log))


# ── a proposal whose measurement never arrives must not stall the loop ───────
def test_pending_proposal_expires_into_a_failed_measurement(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_PENDING_TIMEOUT_S", "0.2")
    a = _load("az_exp", "analyzer/app.py")
    a.app.test_client().post("/api/campaign/start", json={
        "target_size": 4.0, "tolerance": 0.3, "pdi_cap": 0.15,
        "budget": 25, "n_init": 6})
    first = list(a._pending)[0]
    assert len(a._campaign.history) == 0
    time.sleep(0.35)
    a._expire_pending()
    assert first not in a._pending, "the stale proposal was not expired"
    assert len(a._campaign.history) == 1, "no failed measurement was recorded"
    assert len(a._pending) == 1, "the loop did not propose a replacement"
    assert any("FAILED measurement" in l["msg"] for _, l in list(a._log))


# ── the reactor is deliberately NOT auto-resumed ─────────────────────────────
def test_reactor_reports_auto_run_but_does_not_resume_pumps(tmp_path, monkeypatch):
    """Auto-run moves pumps. A power blip must not start reagents flowing into a
    hot reactor with nobody present."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")
    r1 = _load("rx1", "reactor/app.py")
    try:
        r1.app.test_client().post("/api/auto_run", json={"on": True})
        st = load_state(str(tmp_path), "reactor_auto")
        assert st and st["auto_run"] is True, "auto-run state was not persisted"
    finally:
        r1._ctrl.shutdown()

    r2 = _load("rx2", "reactor/app.py")
    try:
        r2._restore_auto_run()
        assert r2._ctrl.auto_run is False, "auto-run resumed on its own — unsafe"
        msgs = [l["msg"] for _, l in list(r2._log)]
        assert any("auto-run was ON" in m for m in msgs), msgs[-3:]
    finally:
        r2._ctrl.shutdown()


def test_reactor_can_opt_in_to_resuming_auto_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")
    save_state(str(tmp_path), "reactor_auto", {"auto_run": True})
    r = _load("rx3", "reactor/app.py")
    try:
        r._CFG.setdefault("run", {})["resume_auto_run"] = True
        r._restore_auto_run()
        assert r._ctrl.auto_run is True, "opt-in resume did not take effect"
    finally:
        r._ctrl.shutdown()


# ── the operator's own run durations must survive a restart ───────────────────
# Reported while setting "reach temp + 60 s synthesis + 60 s flush" in the app:
# those live settings were IN-MEMORY ONLY, so after any restart the reactor
# silently fell back to reactor/config.yml (600 s synthesis, 1200 s flush) — and a
# resumed autonomous campaign used the long defaults with nothing to say so.
def test_live_run_settings_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")

    r1 = _load("rx_rs1", "reactor/app.py")
    try:
        assert r1._ctrl.live_duration is None
        r1.app.test_client().post("/api/run_settings",
                                  json={"run_duration": 60, "flush_duration": 60,
                                        "arm_mode": "temperature"})
        assert r1._ctrl.live_duration == 60
        st = load_state(str(tmp_path), "reactor_run_settings")
        assert st and st["run_duration"] == 60, "run settings were not persisted"
    finally:
        r1._ctrl.shutdown()

    # A fresh process restores them on import (the boot block runs
    # _restore_run_settings before auto-run can start anything).
    r2 = _load("rx_rs2", "reactor/app.py")
    try:
        assert r2._ctrl.live_duration == 60, \
            "the operator's synthesis duration reverted to the config default"
        assert r2._ctrl.live_flush_duration == 60
        assert r2._ctrl.live_arm_mode == "temperature"
        msgs = [l["msg"] for _, l in list(r2._log)]
        assert any("run settings restored" in m for m in msgs)
        assert not any("_saved_at" in m for m in msgs), \
            "internal bookkeeping leaked into the operator-facing log"
    finally:
        r2._ctrl.shutdown()


def test_run_settings_are_restored_before_auto_run_can_start(tmp_path, monkeypatch):
    """Order matters: a resumed campaign must use the durations the operator
    chose, not the config defaults it would otherwise pick up first."""
    src = (Path(__file__).resolve().parents[1] / "reactor" / "app.py").read_text()
    # anchor on the module-level boot block, not the earlier callback that
    # happens to start with the same line
    boot = src[src.index("    _sync_data_dir_from_hub(_project_root)"):]
    boot = boot[:boot.index("_restore_auto_run()") + len("_restore_auto_run()")]
    assert "_restore_run_settings()" in boot, \
        "the run settings are not restored before auto-run may start a recipe"
    assert boot.index("_restore_run_settings()") < boot.index("_restore_auto_run()")
