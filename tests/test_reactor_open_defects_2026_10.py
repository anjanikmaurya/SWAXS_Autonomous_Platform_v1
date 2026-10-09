"""
tests/test_reactor_open_defects_2026_10.py — three reactor risks from
docs/audits/OPEN_DEFECTS.md ("Reactor & beamline — 8 residual risks"),
re-checked against the code in October 2026 and closed here:

  R7  shutdown() closes the pump serial ports (after idling them)
  R8  every run record says which backend produced it (mock | real)
  R4  run.end_on_measurement is a documented no-op: measurement-end is always on

All in Simulation (mock backend); no setting in reactor/config.yml is changed.
"""
from __future__ import annotations

import time

import yaml

from src.reactor.controller import ReactorController

RECIPE = {"T_reac": 240, "F_tot": 80, "x_ODE": 0.3, "x_TOP": 0.15,
          "x_oley": 0.1, "recipe_id": "od1"}


def _cfg(tmp_path):
    cfg = yaml.safe_load(open("reactor/config.yml"))
    cfg["spec"]["data_dir"] = str(tmp_path / "proj")
    cfg["spec"]["simulator"].update(enabled=False)
    cfg["spec"].update(exposure_s=0.1, frames=1, spec_lead_s=0.2)
    cfg["flush"]["duration"] = 0.3
    cfg["arming"] = {**cfg.get("arming", {}), "default_mode": "timed", "default_wait_s": 0.1}
    return cfg


def _wait(pred, t=8.0):
    t0 = time.time()
    while time.time() - t0 < t:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ── R7 ────────────────────────────────────────────────────────────────────────
def test_shutdown_idles_then_closes_every_pump_port(tmp_path):
    ctl = ReactorController(_cfg(tmp_path), backend="mock")
    calls = []
    for name, p in ctl.pumps.pumps.items():
        orig_close = p.close
        p.close = (lambda n=name, f=orig_close: (calls.append(("close", n)), f()))
    orig_idle = ctl.pumps.idle_all
    ctl.pumps.idle_all = lambda: (calls.append(("idle", None)), orig_idle())[1]
    ctl.shutdown()
    names = sorted(ctl.pumps.pumps)
    assert sorted(n for k, n in calls if k == "close") == names, "a pump port was left open"
    first_close = next(i for i, c in enumerate(calls) if c[0] == "close")
    assert calls.index(("idle", None)) < first_close, "ports closed before the pumps were idled"
    assert not ctl._thread.is_alive(), "control loop still running after shutdown"


def test_a_pump_that_will_not_close_does_not_stop_the_others(tmp_path):
    ctl = ReactorController(_cfg(tmp_path), backend="mock")
    pumps = list(ctl.pumps.pumps.values())
    closed = []
    def boom():
        raise OSError("port busy")
    pumps[0].close = boom
    for p in pumps[1:]:
        p.close = (lambda p=p: closed.append(p))
    ctl.shutdown()                                   # must not raise
    assert len(closed) == len(pumps) - 1


# ── R8 ────────────────────────────────────────────────────────────────────────
def test_run_record_and_manifest_entry_carry_the_backend(tmp_path):
    seen = []
    ctl = ReactorController(_cfg(tmp_path), backend="mock", manifest_cb=seen.append)
    try:
        ctl.submit({**RECIPE, "run_duration": 0.6})
        ctl.start()
        assert _wait(lambda: ctl.history), "run never finished"
    finally:
        ctl.shutdown()
    assert ctl.history[-1]["backend"] == "mock"
    assert seen and seen[-1]["backend"] == "mock", "manifest record has no backend"


def test_abandoned_record_carries_the_backend_too():
    src = "".join(open(f"src/reactor/{f}").read() for f in ("controller.py", "sequence.py"))
    i = src.index('"status": "abandoned"')
    assert '"backend": self.backend' in src[i: i + 200]


# ── R4 ────────────────────────────────────────────────────────────────────────
def test_end_on_measurement_is_documented_as_a_no_op():
    src = open("src/reactor/controller.py").read()
    assert "NOT READ ANYWHERE (OPEN_DEFECTS R4)" in src
    kn = open("reactor/knowledge.md").read()
    assert "`run.end_on_measurement` is NOT read by the code" in kn
    assert "PRIMARY end condition is `run.end_on_measurement: true`" not in kn


def test_measurement_signal_ends_the_run_even_if_the_key_is_false(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["run"]["end_on_measurement"] = False
    ctl = ReactorController(cfg, backend="mock")
    try:
        ctl.submit({**RECIPE, "run_duration": 30})
        ctl.start()
        assert _wait(lambda: ctl.state == "running"), "never started running"
        ctl.signal_measurement_complete("test", recipe_id="od1")
        assert _wait(lambda: ctl.state != "running", 5), "measurement signal did not end the run"
        assert "measurement" in (ctl.history[-1]["reason"] if ctl.history else "")
    finally:
        ctl.shutdown()


# ── R6: the flush is supervised too ──────────────────────────────────────────
def _flushing(tmp_path, estop_on_flow=False):
    cfg = _cfg(tmp_path)
    cfg["safety"]["flow_fault_estop"] = estop_on_flow
    cfg["flush"]["duration"] = 30
    logs = []
    ctl = ReactorController(cfg, backend="mock", log_cb=lambda m, t="info": logs.append((t, m)))
    assert ctl.flush_now()
    assert ctl.state == "flushing"
    return ctl, logs


def test_flow_fault_during_flush_warns_once_and_does_not_estop_by_default(tmp_path):
    ctl, logs = _flushing(tmp_path)
    try:
        fp = ctl._flush_pump
        ctl.pumps.flow_faults = lambda: [fp]
        with ctl._lock:
            ctl._check_flush_safety(); ctl._check_flush_safety()
        warns = [m for t, m in logs if "during the flush" in m and t == "warn"]
        assert len(warns) == 1 and ctl.state == "flushing"
    finally:
        ctl.shutdown()


def test_flow_fault_during_flush_estops_when_configured(tmp_path):
    ctl, logs = _flushing(tmp_path, estop_on_flow=True)
    try:
        fp = ctl._flush_pump
        ctl.pumps.flow_faults = lambda: [fp]
        with ctl._lock:
            ctl._check_flush_safety()
        assert ctl.state == "estop"
    finally:
        ctl.shutdown()


def test_a_reagent_pump_that_keeps_delivering_in_the_flush_estops(tmp_path):
    ctl, logs = _flushing(tmp_path)
    try:
        other = next(n for n in ctl.pumps.pumps if n != ctl._flush_pump)
        p = ctl.pumps.pumps[other]
        p.v_delivered = ctl._flush_v0.get(other, 0.0) + 40.0      # 40 µL during the flush
        ctl.pumps.volume_exceeded = lambda: [other]
        with ctl._lock:
            ctl._check_flush_safety()
        assert ctl.state == "estop"
        assert any("kept delivering" in m for _, m in logs)
    finally:
        ctl.shutdown()


def test_ramp_down_tail_after_a_run_does_not_trip_the_flush(tmp_path):
    ctl, logs = _flushing(tmp_path)
    try:
        other = next(n for n in ctl.pumps.pumps if n != ctl._flush_pump)
        p = ctl.pumps.pumps[other]
        p.v_delivered = ctl._flush_v0.get(other, 0.0) + 1.0       # tiny tail, already over cap
        ctl.pumps.volume_exceeded = lambda: [other]
        with ctl._lock:
            ctl._check_flush_safety()
        assert ctl.state == "flushing", "a normal ramp-down tail must not E-stop"
    finally:
        ctl.shutdown()


def test_flush_pump_volume_is_not_capped_during_its_own_flush(tmp_path):
    ctl, logs = _flushing(tmp_path)
    try:
        fp = ctl._flush_pump
        ctl.pumps.pumps[fp].v_delivered = 10_000.0
        ctl.pumps.volume_exceeded = lambda: [fp]
        with ctl._lock:
            ctl._check_flush_safety()
        assert ctl.state == "flushing"
    finally:
        ctl.shutdown()


# ── R5: skipping the temperature wait is confirmed and recorded ──────────────
def test_start_now_logs_the_temperature_it_started_at(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["arming"] = {**cfg["arming"], "default_mode": "temperature"}
    logs = []
    ctl = ReactorController(cfg, backend="mock", log_cb=lambda m, t="info": logs.append((t, m)))
    try:
        ctl.submit({**RECIPE, "run_duration": 30})
        ctl.start()
        assert _wait(lambda: ctl.state == "arming"), "never armed"
        assert ctl.start_now() and ctl.state == "running"
        msg = next(m for t, m in logs if "arming skipped by the operator" in m)
        assert "°C (target" in msg
    finally:
        ctl.shutdown()


def test_ui_asks_before_skipping_the_temperature_wait():
    html = open("reactor/templates/index.html").read()
    i = html.index("function startClicked(")
    body = html[i: html.index("\n}", i)]
    assert "confirm(" in body and "/api/start_now" in body
    assert body.index("confirm(") < body.index("/api/start_now"), "must ask before posting"


# ── audit Oct 2026, follow-ups ───────────────────────────────────────────────
def test_changing_the_flush_pump_mid_flush_stops_the_pump_that_is_running(tmp_path):
    cfg = _cfg(tmp_path); cfg["flush"]["duration"] = 0.8
    ctl = ReactorController(cfg, backend="mock")
    try:
        assert ctl.flush_now()
        running = ctl._flush_active
        other = next(n for n in ("ode_flush", "ode_dilution") if n != running)
        ctl.set_run_settings({"flush_pump": other})
        assert ctl._flush_active == running and ctl._flush_pump == other
        assert _wait(lambda: ctl.state in ("ready", "idle"), 5)
        assert ctl.pumps.pumps[running].target == 0.0, "the pump that flushed was left running"
    finally:
        ctl.shutdown()


def _frozen_flush(tmp_path, estop_on_flow=False):
    ctl, logs = _flushing(tmp_path, estop_on_flow)
    ctl._alive = False                  # freeze the loop so the test owns the pump values
    time.sleep(0.2)
    ctl._flush_started_at = time.time() - 100
    return ctl, logs


def test_an_idle_pump_still_flowing_in_the_flush_warns_once(tmp_path):
    ctl, logs = _frozen_flush(tmp_path)
    try:
        other = next(n for n in ctl.pumps.pumps if n != ctl._flush_active)
        ctl.pumps.pumps[other].actual = 20.0
        ctl._idle_flow_since = {other: time.time() - 100}
        with ctl._lock:
            ctl._check_flush_safety(); ctl._check_flush_safety()
        warns = [m for t, m in logs if "should be idle during the flush" in m]
        assert len(warns) == 1 and ctl.state == "flushing"
    finally:
        ctl.shutdown()


def test_an_idle_pump_still_flowing_estops_when_configured(tmp_path):
    ctl, logs = _frozen_flush(tmp_path, estop_on_flow=True)
    try:
        other = next(n for n in ctl.pumps.pumps if n != ctl._flush_active)
        ctl.pumps.pumps[other].actual = 20.0
        ctl._idle_flow_since = {other: time.time() - 100}
        with ctl._lock:
            ctl._check_flush_safety()
        assert ctl.state == "estop"
    finally:
        ctl.shutdown()


def test_a_short_blip_or_the_ramp_down_does_not_count(tmp_path):
    ctl, logs = _frozen_flush(tmp_path)
    try:
        other = next(n for n in ctl.pumps.pumps if n != ctl._flush_active)
        ctl.pumps.pumps[other].actual = 20.0
        with ctl._lock:
            ctl._check_flush_safety()          # first sighting only starts the clock
        ctl._flush_started_at = time.time()    # and inside the ramp-down grace nothing is judged
        ctl._idle_flow_since = {other: time.time() - 100}
        with ctl._lock:
            ctl._check_flush_safety()
        assert not any("should be idle" in m for _, m in logs) and ctl.state == "flushing"
    finally:
        ctl.shutdown()


def test_shutdown_idles_again_after_the_loop_stops(tmp_path):
    ctl = ReactorController(_cfg(tmp_path), backend="mock")
    n = {"idle": 0}
    orig = ctl.pumps.idle_all
    ctl.pumps.idle_all = lambda: (n.__setitem__("idle", n["idle"] + 1), orig())[1]
    ctl.shutdown()
    assert n["idle"] >= 2, "pumps must be idled again after the loop has stopped"


def test_a_tick_after_shutdown_commands_nothing(tmp_path):
    ctl = ReactorController(_cfg(tmp_path), backend="mock")
    ctl.shutdown()
    called = []
    ctl.pumps.set_all = lambda *a, **k: called.append(a)
    ctl._tick_once()
    assert called == []


def test_checklist_beamline_fails_on_stale_and_errors_stay_inside_a_line():
    from src.reactor import checks
    st = {"backend": "real", "pumps": {}, "temperature": {"source": "beamline", "stale": True,
                                                          "i0": 1e5, "bstop": 4e4}}
    r = checks.run_check("beamline", st, {})
    assert not r.ok and "stale" in r.value
    bad = {"pumps": {"x": {"fault": False, "stale": False}}, "temperature": None}
    out = checks.run_all(bad, {"pumps": {}})            # must not raise
    assert len(out) == len(checks.CHECKS)
