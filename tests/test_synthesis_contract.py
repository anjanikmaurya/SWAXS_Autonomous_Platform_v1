"""Phase 0 of docs/design/SYNTHESIS_PLATFORM_PLAN.md: the instrument contract.

Exercised end to end with the simulated ToyMixer. The last tests guard the
promise that phase 0 leaves the running reactor alone: nothing in reactor/,
src/reactor/ or src/beamline/ imports src.synthesis, and no instrument is
registered by default (robot and well plate routes are future work).
"""
import re
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.synthesis import (Channel, Description, GateError, InstrumentSession, Parameter,  # noqa: E402
                           Plan, Refusal, State, Step, TestSpec, registry)
from src.synthesis.toy import ToyMixer  # noqa: E402


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def ready_session(**kw):
    clk = Clock()
    s = InstrumentSession(ToyMixer(**kw), clock=clk)
    s.connect()
    s.run_test("ports"); s.run_test("sensor")
    s.setup("prime")
    return s, clk


# ── declarations ────────────────────────────────────────────────────────────
def test_description_rejects_bad_role_and_duplicates():
    with pytest.raises(ValueError):
        Description(id="x", title="x", role="robotics")
    with pytest.raises(ValueError):
        Description(id="x", title="x", role="probe", tests=(TestSpec("a", "A"), TestSpec("a", "A2")))


def test_parameter_and_channel_limits():
    p = Parameter("T", "°C", 180, 300)
    assert p.check(240) is None and "below" in p.check(100) and "not a number" in p.check("hot")
    c = Channel("P", "mbar", safe_max=10000, warn_max=8000)
    assert (c.level(5000), c.level(9000), c.level(12000)) == ("ok", "warn", "trip")


# ── gating: Test → Setup → Ready → Run ──────────────────────────────────────
def test_run_is_locked_until_required_tests_and_setup_pass():
    s = InstrumentSession(ToyMixer(), clock=Clock())
    with pytest.raises(GateError):
        s.run_test("ports")                     # not connected yet
    s.connect()
    assert s.state == State.CONNECTED
    assert set(s.missing()) == {"test: Ports open", "test: Sensor reads", "setup: Prime the line"}
    s.run_test("ports"); s.run_test("sensor")
    assert s.state == State.TESTED              # optional leak test not needed
    with pytest.raises(GateError):
        s.run(Plan((Step("mix"),)))
    s.setup("prime")
    assert s.state == State.READY and s.missing() == []


def test_a_failed_test_blocks_ready_and_says_how_to_fix_it():
    s = InstrumentSession(ToyMixer(fail_on=("sensor",)), clock=Clock())
    s.connect(); s.run_test("ports"); s.setup("prime")
    r = s.run_test("sensor")
    assert not r.ok and "cable" in r.hint and s.state != State.READY


def test_an_expired_test_must_be_rerun():
    s, clk = ready_session()
    clk.t += 3601                               # sensor test is valid for 1 h
    assert "test expired: Sensor reads" in s.missing()
    with pytest.raises(GateError):
        s.run(Plan((Step("mix"),)))


# ── compile: refuse before anything moves ───────────────────────────────────
def test_compile_refuses_out_of_limit_recipes_without_touching_hardware():
    s, _ = ready_session()
    toy = s.instrument
    r = s.compile({"flow": 500, "time_s": 10})
    assert isinstance(r, Refusal) and r.parameter == "flow"
    assert isinstance(s.compile({"flow": 20}), Refusal)          # the module's own rule
    assert toy.ran == [] and toy.flow == 0.0 and s.state == State.READY


def test_a_good_plan_runs_and_returns_to_ready():
    s, _ = ready_session()
    plan = s.compile({"flow": 40, "time_s": 30})
    assert isinstance(plan, Plan) and plan.duration_s == 35
    assert s.run(plan).ok and s.instrument.ran == ["mix", "flush"] and s.state == State.READY


# ── failures always end in safe state ───────────────────────────────────────
def test_a_failing_step_trips_safe_state_and_fault():
    s, _ = ready_session(fail_on=("flush",))
    r = s.run(s.compile({"flow": 40, "time_s": 30}))
    assert not r.ok and s.state == State.FAULT and s.instrument.safe_calls == 1
    assert s.instrument.flow == 0.0 and "flush" in s.fault
    with pytest.raises(GateError):
        s.run(Plan((Step("mix"),)))
    s.reset()
    # setup (priming) is kept; the tests must pass again before Ready
    assert s.state == State.SET_UP and s.tests == {} and "test: Ports open" in s.missing()


def test_estop_works_from_any_state_and_never_raises():
    for setup in (lambda s: None, lambda s: s.connect()):
        s = InstrumentSession(ToyMixer(), clock=Clock()); setup(s)
        s.estop()
        assert s.state == State.FAULT and s.instrument.estops == 1 and s.instrument.safe_calls == 1


def test_estop_during_a_run_stops_the_remaining_steps():
    s, _ = ready_session()
    gate = threading.Event()
    orig = s.instrument.execute
    def slow(step):
        if step.capability == "mix":
            gate.wait(2)
        return orig(step)
    s.instrument.execute = slow
    t = threading.Thread(target=lambda: s.run(s.compile({"flow": 40, "time_s": 30})))
    t.start(); s.estop(); gate.set(); t.join(3)
    assert s.state == State.FAULT and "flush" not in s.instrument.ran


def test_status_and_event_log_are_reported():
    s, _ = ready_session()
    st = s.status()
    assert st["state"] == "ready" and st["simulated"] is True and st["role"] == "synthesis"
    kinds = [e["kind"] for e in s.events]
    assert kinds.count("test") == 2 and "connect" in kinds and "setup" in kinds


# ── registry ────────────────────────────────────────────────────────────────
def test_registry_is_empty_by_default_and_registers_explicitly():
    assert registry.available() == [], "no instrument should be registered in phase 0"
    registry.register("toy_mixer", ToyMixer)
    try:
        assert registry.available() == ["toy_mixer"] and isinstance(registry.create("toy_mixer"), ToyMixer)
        with pytest.raises(ValueError):
            registry.register("toy_mixer", lambda: ToyMixer())
    finally:
        registry.unregister("toy_mixer")
    with pytest.raises(KeyError):
        registry.create("liquid_handling_robot")                # future route, not built


# ── phase 0 leaves the running reactor alone ────────────────────────────────
def test_reactor_and_hardware_code_do_not_import_the_new_package():
    files = list((ROOT / "reactor").rglob("*.py")) + list((ROOT / "src" / "reactor").rglob("*.py")) \
          + list((ROOT / "src" / "beamline").rglob("*.py"))
    # The one allowed user is the READ-ONLY hardware checklist (src/reactor/checks.py,
    # October 2026). The controller, the pump code and the beamline driver must not
    # depend on the contract until phase 1 is done behind its own tests.
    allowed = {"src/reactor/checks.py"}
    users = [str(f.relative_to(ROOT)) for f in files
             if re.search(r"\bsrc\.synthesis\b|from \.\.synthesis|import synthesis", f.read_text(errors="ignore"))]
    assert set(users) <= allowed, f"only the read-only checklist may import src.synthesis: {users}"


def test_the_package_never_drives_hardware():
    src = "\n".join(p.read_text() for p in (ROOT / "src" / "synthesis").glob("*.py"))
    for forbidden in ("serial", "requests", "epics", "src.reactor", "src.beamline", "Py_P_Pump"):
        assert not re.search(rf"^\s*(import|from)\s+{re.escape(forbidden)}", src, re.M), forbidden
