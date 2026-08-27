"""
tests/test_reactor_safety.py — safety-critical behaviour of the reactor.

These lock down the fixes for the pump-control safety review:
  1. idle_all / zero_pumps guard each pump — one failed serial write can never
     stop the others from being idled (the emergency-stop path).
  2. estop() records the estop state even if a pump's idle write throws.
  3. RealPump.tick() marks a pump faulted+stale after repeated status-poll
     failures, so a lost/hung pump trips the safety E-stop instead of coasting
     on stale, healthy-looking readings.
  4. Serial matching is exact (tolerating an FTDI 'a'/'b' channel suffix), never
     a loose substring, and refuses to start on an ambiguous or missing serial
     instead of driving a possibly-wrong pump.

All run in mock mode / with fakes — no hardware or real serial ports needed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.reactor.config import PUMP_NAMES, REAGENT_PUMPS  # noqa: E402
from src.reactor.drivers import Py_P_Pump  # noqa: E402
from src.reactor.hardware import MockPump, PumpBank, RealPump  # noqa: E402


# ── 1 + 2: guarded idling / emergency stop ────────────────────────────────────

class _FailingMock(MockPump):
    """A mock pump whose flow command always fails (simulates a dead port)."""
    def set_flow(self, rate):
        raise RuntimeError("serial down")


def _mock_bank():
    cfg = {"pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES}}
    return PumpBank(cfg, backend="mock")


def test_idle_all_guards_each_pump():
    bank = _mock_bank()
    victim = PUMP_NAMES[1]
    bank.pumps[victim] = _FailingMock(victim, 1000.0)
    # command everyone to flow, then idle
    for n in PUMP_NAMES:
        try:
            bank.pumps[n].set_flow(100.0)
        except Exception:
            pass
    failed = bank.idle_all()
    assert failed == [victim]                       # the bad pump is reported
    for n in PUMP_NAMES:                             # every *other* pump is idled
        if n != victim:
            assert bank.pumps[n].target == 0.0


def test_zero_pumps_guards_each_pump():
    bank = _mock_bank()
    victim = REAGENT_PUMPS[0]
    bank.pumps[victim] = _FailingMock(victim, 1000.0)
    for n in REAGENT_PUMPS:
        try:
            bank.pumps[n].set_flow(50.0)
        except Exception:
            pass
    failed = bank.zero_pumps(REAGENT_PUMPS)
    assert failed == [victim]
    for n in REAGENT_PUMPS:
        if n != victim:
            assert bank.pumps[n].target == 0.0


def test_estop_records_state_even_if_a_pump_fails():
    from src.reactor.controller import ReactorController
    logs = []
    cfg = {"pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES}}
    ctl = ReactorController(cfg, backend="mock",
                            log_cb=lambda m, t="info": logs.append((t, m)))
    try:
        victim = PUMP_NAMES[2]
        ctl.pumps.pumps[victim] = _FailingMock(victim, 1000.0)
        ctl.estop()
        assert ctl.state == "estop"                 # estop recorded despite failure
        assert any(t == "error" for t, _ in logs)   # and surfaced loudly
        for n in PUMP_NAMES:                         # other pumps idled
            if n != victim:
                assert ctl.pumps.pumps[n].target == 0.0
    finally:
        ctl.shutdown()


# ── 3: lost / hung pump detection ──────────────────────────────────────────────

class _FakeSerialPump:
    """Stand-in for the vendored driver: read_status fails until .ok is set."""
    def __init__(self, *a, **k):
        self.ok = False
        self.remote_ok = True
        self.closed = False

    def enter_remote(self):
        return self.remote_ok

    def read_status(self):
        if not self.ok:
            raise IOError("no reply")
        return {"flow_rate_ulmin": 1.0, "chamber_pressure": 10.0,
                "state_code": 1, "error_code": 0}

    def set_flow(self, *a, **k):
        pass

    def set_idle(self):
        pass

    def close(self):
        self.closed = True


def test_realpump_requires_remote_control(monkeypatch):
    created = {}

    class _NoRemote(_FakeSerialPump):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.remote_ok = False
            created["p"] = self

    monkeypatch.setattr(Py_P_Pump, "P_pump", _NoRemote)
    with pytest.raises(RuntimeError):
        RealPump("p", "COM_FAKE", pump_id=0, max_flow=1000.0)
    assert created["p"].closed is True        # port released, not left blind & locked


def test_lost_pump_becomes_faulted_then_recovers(monkeypatch):
    monkeypatch.setattr(Py_P_Pump, "P_pump", _FakeSerialPump)
    rp = RealPump("p", "COM_FAKE", pump_id=0, max_flow=1000.0)
    assert rp.fault is False
    # each tick(3.0) triggers exactly one status poll; fails accumulate
    for _ in range(rp.POLL_FAIL_LIMIT):
        rp.tick(3.0)
    assert rp.fault is True and rp.stale is True     # lost pump -> safety can trip
    # once the pump answers again, the flags clear
    rp._pump.ok = True
    rp.tick(3.0)
    assert rp.fault is False and rp.stale is False


# ── 4: serial matching ─────────────────────────────────────────────────────────

def _fake_ports(pairs):
    return [types.SimpleNamespace(device=dev, serial_number=sn) for dev, sn in pairs]


def test_serial_match_exact_and_suffix(monkeypatch):
    monkeypatch.setattr(Py_P_Pump.list_ports, "comports",
                        lambda: _fake_ports([("COM3", "PUMPA1"), ("COM4", "PUMPB2b")]))
    assert Py_P_Pump.find_port_by_serial("PUMPA1") == "COM3"
    assert Py_P_Pump.find_port_by_serial("PUMPB2") == "COM4"   # trailing 'b' tolerated


def test_serial_no_loose_substring(monkeypatch):
    monkeypatch.setattr(Py_P_Pump.list_ports, "comports",
                        lambda: _fake_ports([("COM3", "XXPUMPA1XX")]))
    assert Py_P_Pump.find_port_by_serial("PUMPA1") is None     # substring must NOT match


def test_serial_ambiguous_raises(monkeypatch):
    monkeypatch.setattr(Py_P_Pump.list_ports, "comports",
                        lambda: _fake_ports([("COM3", "DUP"), ("COM7", "DUP")]))
    with pytest.raises(RuntimeError):
        Py_P_Pump.find_port_by_serial("DUP")


def test_real_bank_refuses_when_serial_missing(monkeypatch):
    monkeypatch.setattr(Py_P_Pump.list_ports, "comports", lambda: _fake_ports([]))
    cfg = {"pumps": {n: {"serial": "NOPE", "max_flow": 1000.0} for n in PUMP_NAMES}}
    with pytest.raises(RuntimeError):
        PumpBank(cfg, backend="real")               # never opens a wrong port


# ── control-button cleanup (audit follow-up) ───────────────────────────────────

def test_dead_stop_control_removed():
    from src.reactor.controller import ReactorController
    ctl = ReactorController({"pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES}},
                            backend="mock")
    try:
        assert not hasattr(ctl, "stop")     # dead duplicate of abort() removed
        assert hasattr(ctl, "abort")        # the real end-run control stays
    finally:
        ctl.shutdown()


def test_prime_removed_flush_still_works():
    from src.reactor.controller import ReactorController
    ctl = ReactorController({"pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES}},
                            backend="mock")
    try:
        assert not hasattr(ctl, "prime")                  # Prime removed per request
        assert ctl.flush_now(rate=50, duration=30) is True
        assert ctl.state == "flushing"
    finally:
        ctl.shutdown()


def test_done_file_includes_flow_series_at_bottom():
    import time
    from src.reactor.controller import ReactorController
    cap = {}
    cfg = {"pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES},
           "bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
                      "x_each": [0, 0.3], "x_sum_max": 0.9},
           "run": {"default_duration": 1.0, "log_interval_s": 0.2},
           # This test is about the done-file contents, not 2D collection. With
           # collection on, background_when="before" inserts a blank flush ahead
           # of the run and it wouldn't finish inside the 2 s window.
           "spec": {"enabled": False}}
    ctl = ReactorController(cfg, backend="mock",
                            feedback_cb=lambda rid, payload: cap.update({rid: payload}))
    try:
        ctl.set_run_settings({"arm_mode": "timed", "arm_wait_s": "0", "run_duration": "1"})
        ctl.submit({"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1})
        ctl.start()
        time.sleep(2.0)
        assert cap, "no feedback (done) payload was produced"
        payload = next(iter(cap.values()))
        assert list(payload.keys())[-1] == "flow_series"     # appended at the bottom
        assert len(payload["flow_series"]) >= 1
        s0 = payload["flow_series"][0]
        assert "t_s" in s0 and set(PUMP_NAMES) <= set(s0["flows"])
    finally:
        ctl.shutdown()


def test_backend_switch_guards():
    from src.reactor.controller import ReactorController
    cfg = {"pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES}}
    ctl = ReactorController(cfg, backend="mock")
    try:
        assert ctl.backend == "mock"
        assert ctl.switch_backend("mock") == (True, "already mock")
        # real has no serial/hardware here → must fail and STAY on mock
        ok, _ = ctl.switch_backend("real")
        assert ok is False and ctl.backend == "mock"
        assert ctl.status()["backend"] == "mock"
        # never switch mid-run
        ctl.state = "running"
        ok, msg = ctl.switch_backend("real")
        assert ok is False and "run" in msg.lower()
    finally:
        ctl.shutdown()


def test_recipe_arm_modes_are_temperature_or_timed_only():
    from src.reactor.recipe import Recipe, RecipeError
    base = {"T_reac": 240, "F_tot": 100, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1}
    # temperature + timed are accepted
    assert Recipe.from_dict({**base, "arm_mode": "timed"}).arm_mode == "timed"
    assert Recipe.from_dict({**base, "arm_mode": "temperature"}).arm_mode == "temperature"
    # ramp is no longer a valid mode
    with pytest.raises(RecipeError):
        Recipe.from_dict({**base, "arm_mode": "ramp"})


# ── folder-watcher intake: no partial-write drops ──────────────────────────────

def test_intake_waits_until_file_is_stable():
    from src.reactor.intake import decide_intake
    handled: dict = {}
    last: dict = {}
    k = "/recipes/cond.dat"
    # first sight → wait (record signature)
    assert decide_intake(k, (10, 111), handled, last) == "wait"
    last[k] = (10, 111)
    # still being written (signature changed) → wait
    assert decide_intake(k, (20, 222), handled, last) == "wait"
    last[k] = (20, 222)
    # unchanged since previous poll → stable → go
    assert decide_intake(k, (20, 222), handled, last) == "go"


def test_intake_skips_already_handled_version():
    from src.reactor.intake import decide_intake
    k = "/recipes/cond.dat"
    assert decide_intake(k, (20, 222), {k: (20, 222)}, {k: (20, 222)}) == "skip"


def test_intake_reprocesses_a_corrected_rewrite():
    from src.reactor.intake import decide_intake
    # a bad version (20,222) was rejected earlier; a corrected rewrite has a new sig
    handled = {"/recipes/cond.dat": (20, 222)}
    last: dict = {}
    k = "/recipes/cond.dat"
    assert decide_intake(k, (30, 333), handled, last) == "wait"   # new sig, wait for stable
    last[k] = (30, 333)
    assert decide_intake(k, (30, 333), handled, last) == "go"     # then re-ingested


# ── temperature staleness must not blame the sensor for our own acquisition ───
# Reported from the beamline as:
#   🛑 SAFETY: temperature reading is STALE (15s since the last successful read)
#   — the over-temperature interlock cannot protect you. Check the SPEC/EPICS
#   temperature source (spec.temp_counter / spec.epics_pvs).
# Nothing was wrong with the counter. With read_source "spec" a collection holds
# the SPEC lock, read_state() returns {} by design for the whole acquisition, and
# the shipped exposure_s 10 x frames 10 makes that 100 s — so the alarm fired on
# EVERY acquisition and sent the operator to inspect working hardware.
class _Bl:
    """Minimal beamline stand-in with a controllable collecting flag."""

    def __init__(self, read_source="spec", during=False, temp=200.0):
        self.cfg = {"read_source": read_source, "read_during_collect": during}
        self._collecting = False
        self._temp = temp

    def is_collecting(self):
        return self._collecting

    def read_state(self):
        if self._collecting and self.cfg["read_source"] != "epics" \
                and not self.cfg["read_during_collect"]:
            return {}                      # the SPEC lock is held — by design
        return {"temperature": self._temp, "bstop": 1.0, "i0": 2.0}

    def set_temperature(self, T):
        pass


def _tc(bl):
    from src.reactor.hardware import TempController
    return TempController({"temperature": {"read_interval_s": 1.0}},
                          backend="real", beamline=bl)


def test_a_collection_does_not_make_the_temperature_look_broken():
    bl = _Bl()
    tc = _tc(bl)
    tc.tick(1.0)
    assert tc.stale is False and tc.polling_paused is False

    bl._collecting = True
    tc.tick(1.0)
    tc._last_read_ok -= 100          # a long acquisition, as shipped (100 s)
    assert tc.polling_paused is True
    assert tc.stale is False, \
        "our own acquisition is still reported as a stale sensor"
    assert tc.blind_during_collect is True, "the config trade-off is not detected"

    bl._collecting = False           # acquisition over, reads resume
    tc.tick(1.0)
    assert tc.stale is False and tc.polling_paused is False
    assert tc.age_s() < 2.0


def test_a_genuinely_dead_source_is_still_reported():
    """The other half: the alarm must not be silenced, only correctly attributed."""
    bl = _Bl()
    bl.read_state = lambda: {}       # broken counter, nothing collecting
    tc = _tc(bl)
    tc.tick(1.0)
    tc._last_read_ok -= 100
    assert tc.polling_paused is False
    assert tc.stale is True, "a dead temperature source is no longer flagged"


def test_epics_keeps_reading_during_a_collection():
    """The remedy the message now recommends has to actually work."""
    bl = _Bl(read_source="epics")
    tc = _tc(bl)
    bl._collecting = True
    tc.tick(1.0)
    assert tc.blind_during_collect is False
    assert tc.age_s() < 2.0, "epics reads did not survive the collection"
    assert tc.stale is False


def test_read_during_collect_also_clears_the_blind_window():
    bl = _Bl(during=True)
    tc = _tc(bl)
    bl._collecting = True
    tc.tick(1.0)
    assert tc.blind_during_collect is False
    assert tc.age_s() < 2.0


def test_the_two_messages_are_distinguishable():
    """An operator must be able to tell "we are mid-acquisition" from "your
    sensor is dead" — the old wording only ever said the latter."""
    src = (_ROOT / "src" / "reactor" / "controller.py").read_text() \
        if "ROOT" in dir() else \
        (__import__("pathlib").Path(__file__).resolve().parents[1]
         / "src" / "reactor" / "controller.py").read_text()
    assert "temperature polling is paused" in src
    assert 'spec.read_source: \\"epics\\"' in src or 'read_source: "epics"' in src, \
        "the note does not name the remedy"
    stale = src[src.index("temperature reading is STALE"):][:400]
    assert "no acquisition is running" in stale, \
        "the safety message does not rule out the benign cause"


# ── timing is never compressed ───────────────────────────────────────────────
# A mock time-scale was tried and then removed at the operator's request: a mock
# rehearsal must be timed exactly like the beamline run it stands in for, so a
# 60 s synthesis takes 60 s everywhere. This test keeps it out.
def test_no_time_compression_exists_anywhere_in_the_reactor():
    src = (_ROOT / "src" / "reactor" / "controller.py").read_text()
    for banned in ("time_scale", "_scaled(", "mock_time_scale"):
        assert banned not in src, \
            f"time compression is back in the controller ({banned})"
    cfg = (_ROOT / "reactor" / "config.yml").read_text()
    assert "mock_time_scale" not in cfg


def test_durations_are_used_verbatim():
    """arming / synthesis / flush must equal exactly what the config says."""
    from src.reactor import ReactorController, load_config
    cfg = load_config()
    cfg.setdefault("spec", {})["backend"] = "mock"
    cfg["spec"].setdefault("simulator", {})["enabled"] = False
    c = ReactorController(cfg, backend="mock")
    try:
        run = cfg.get("run", {}); fl = cfg.get("flush", {}); arm = cfg.get("arming", {})
        assert c.default_duration == float(run.get("default_duration", 600.0))
        assert c.flush_duration == float(fl.get("duration", 300.0))
        assert c.default_arm_wait == float(arm.get("default_wait_s", 120.0))
    finally:
        c.shutdown()
