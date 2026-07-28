"""
tests/test_reactor_logging.py — the reactor log must explain what happened.

These assert on log CONTENT because the log is the primary troubleshooting tool
during beamtime: if a 2D collection never fires, or a run ends earlier than the
duration set in the app, the operator has to be able to see why without reading
the source.
"""
from __future__ import annotations

import time

import pytest
import yaml

from src.reactor.controller import ReactorController

BASE_RECIPE = {"T_reac": 240, "F_tot": 80, "x_ODE": 0.3, "x_TOP": 0.15,
               "x_oley": 0.1, "recipe_id": "d1"}


def _cfg(tmp_path, **spec_over):
    cfg = yaml.safe_load(open("reactor/config.yml"))
    cfg["spec"]["data_dir"] = str(tmp_path / "proj")
    cfg["spec"]["simulator"].update(enabled=True, speed_factor=0)
    cfg["spec"].update(exposure_s=0.1, frames=1, spec_lead_s=0.6)
    cfg["spec"].update(spec_over)
    cfg["flush"]["duration"] = 0.4
    cfg["arming"] = {**cfg.get("arming", {}), "default_mode": "timed",
                     "default_wait_s": 0.1}
    return cfg


def _run(cfg, duration=1.2, wait=8.0):
    logs: list = []
    ctl = ReactorController(cfg, backend="mock",
                            log_cb=lambda m, t="info": logs.append((t, m)))
    try:
        ctl.submit({**BASE_RECIPE, "run_duration": duration})
        ctl.start()
        t0 = time.time()
        while time.time() - t0 < wait and not (ctl.state in ("ready", "idle")
                                               and time.time() - t0 > 0.8):
            time.sleep(0.05)
    finally:
        # ALWAYS stop the control loop: a leaked controller keeps ticking pumps
        # in a background thread and destabilises every test that follows.
        ctl.shutdown()
    return ctl, [m for _, m in logs], logs


@pytest.fixture(scope="module")
def happy(tmp_path_factory):
    """One nominal run shared by every assertion about a healthy log."""
    return _run(_cfg(tmp_path_factory.mktemp("happy")))


def _has(msgs, needle):
    return any(needle in m for m in msgs)


# ── run start states the plan ─────────────────────────────────────────────────
def test_run_start_states_setpoints_temperature_and_duration(happy):
    _, msgs, _ = happy
    start = [m for m in msgs if "RUN START" in m]
    assert start, msgs
    assert "µL/min" in start[0] and "240" in start[0] and "1.2s" in start[0]


def test_run_start_lists_what_will_end_the_run(happy):
    _, msgs, _ = happy
    assert _has(msgs, "ends on: first of"), "run-end triggers not explained"
    assert _has(msgs, "duration") and _has(msgs, "measurement-complete signal")


def test_run_start_says_when_the_2d_collection_is_due(happy):
    _, msgs, _ = happy
    assert _has(msgs, "2D collection due at T+"), msgs


# ── misconfiguration is announced, not silent ─────────────────────────────────
def test_disabled_collection_is_flagged_at_run_start(tmp_path):
    _, msgs, logs = _run(_cfg(tmp_path, enabled=False))
    assert _has(msgs, "2D collection DISABLED")
    assert any(t == "warn" and "DISABLED" in m for t, m in logs)


def test_unset_data_dir_is_flagged(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["spec"]["data_dir"] = ""
    _, msgs, _ = _run(cfg)
    assert _has(msgs, "data_dir is UNSET")


def test_lead_longer_than_the_run_is_flagged(tmp_path):
    """spec_lead_s ≥ duration makes the collection fire at T+0 — easy to miss."""
    _, msgs, _ = _run(_cfg(tmp_path, spec_lead_s=10.0), duration=1.0)
    assert _has(msgs, "fires IMMEDIATELY"), msgs
    assert _has(msgs, "reduce spec_lead_s")


# ── run end explains the timing ───────────────────────────────────────────────
def test_run_end_reports_actual_versus_planned_duration(happy):
    _, msgs, _ = happy
    end = [m for m in msgs if "RUN END" in m]
    assert end, msgs
    assert "planned" in end[0] and "stopped by:" in end[0]


def test_run_end_names_the_trigger_that_stopped_it(happy):
    _, msgs, _ = happy
    assert _has(msgs, "stopped by: duration elapsed")


def test_collection_start_and_done_are_both_logged(happy):
    _, msgs, _ = happy
    assert _has(msgs, "2D SAMPLE collect START")
    assert _has(msgs, "2D SAMPLE collect DONE")
    assert _has(msgs, "frame(s) ×")            # frames × exposure spelled out


def test_flush_start_names_pump_rate_and_duration(happy):
    _, msgs, _ = happy
    flush = [m for m in msgs if "FLUSH START" in m]
    assert flush and "µL/min" in flush[0] and "blocked" in flush[0]


# ── no false alarms ───────────────────────────────────────────────────────────
def test_no_flow_fault_warnings_during_pump_ramp_up(tmp_path):
    """Pumps can't hit a new setpoint instantly. Warning during the ramp trains
    the operator to ignore a genuinely important message."""
    _, msgs, _ = _run(_cfg(tmp_path))
    spurious = [m for m in msgs if "flow" in m.lower() and "setpoint" in m.lower()]
    assert not spurious, f"false flow-fault alarms during ramp: {spurious}"


def test_settle_window_suppresses_then_re_enables_flow_checks():
    """Unit-level: the grace period must expire, not disable the check forever."""
    from src.reactor.hardware import MockPump
    p = MockPump("t", max_flow=100.0, flow_settle_s=5.0, bad_flow_tol=0)
    p.set_flow(50.0)
    p.actual = 0.0                       # nowhere near setpoint
    p._update_health(1.0)
    assert p.flow_ok and not p.flow_fault, "settle window did not suppress"
    p._update_health(10.0)               # window expires
    p._update_health(1.0)
    assert not p.flow_ok and p.flow_fault, "check never re-enabled after settling"


def test_settle_window_restarts_on_a_setpoint_change():
    from src.reactor.hardware import MockPump
    p = MockPump("t", max_flow=100.0, flow_settle_s=5.0, bad_flow_tol=0)
    p.set_flow(50.0)
    p._update_health(10.0)               # burn through the window
    p.set_flow(80.0)                     # new setpoint → fresh grace
    p.actual = 0.0
    p._update_health(1.0)
    assert p.flow_ok, "settle window did not restart on a setpoint change"


# ── the log must never claim data was written when it wasn't ─────────────────
def test_mock_without_simulator_says_no_files_written(tmp_path):
    """Regression: mock collect() is a no-op, but the log used to report DONE —
    sending the operator hunting for files that never existed."""
    cfg = _cfg(tmp_path)
    cfg["spec"]["simulator"]["enabled"] = False
    _, msgs, logs = _run(cfg)
    assert _has(msgs, "NO FILES WRITTEN"), msgs
    assert _has(msgs, "spec.simulator.enabled: true"), "fix not suggested"
    assert not _has(msgs, "collect DONE"), "log falsely claimed success"
    assert any(t == "warn" and "NO FILES" in m for t, m in logs)


def test_run_start_warns_when_mock_simulator_is_off(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["spec"]["simulator"]["enabled"] = False
    _, msgs, _ = _run(cfg)
    assert _has(msgs, "MOCK backend with the 2D simulator OFF")


def test_unwritable_save_folder_is_reported_before_the_first_frame(tmp_path):
    """The shipped data_dir is the BEAMLINE path, unwritable on a laptop."""
    cfg = _cfg(tmp_path)
    cfg["spec"]["data_dir"] = "/proc/definitely/not/writable"
    _, msgs, logs = _run(cfg)
    assert _has(msgs, "NOT writable"), msgs
    assert _has(msgs, "mock_data_dir"), "no remedy offered"
    assert any(t == "error" for t, m in logs if "NOT writable" in m)


def test_successful_collection_reports_frame_count_and_folder(happy):
    _, msgs, _ = happy
    done = [m for m in msgs if "collect DONE" in m]
    assert done, msgs
    assert "frame(s) written to" in done[0] and "SAXS" in done[0]


# ── safety messages carry the numbers needed to act ───────────────────────────
def test_safety_messages_include_the_limit_that_was_breached():
    import inspect
    src = inspect.getsource(ReactorController._safety_check)
    assert "T_max {self.T_max" in src, "temperature trip omits the limit value"
    assert "per_pump_max {self.per_pump_max" in src, "pump trip omits the limit"
    assert "mbar ceiling" in src, "pressure trip omits the ceiling"
