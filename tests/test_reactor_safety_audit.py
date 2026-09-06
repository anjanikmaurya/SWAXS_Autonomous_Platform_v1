"""
tests/test_reactor_safety_audit.py — regression tests for the reactor findings
from the September 2026 pipeline robustness audit.

Each test locks down one fix:
  1. is_stable() returns False on a STALE/frozen temperature reading, so the
     arming->running gate cannot open on a dead sensor (reagents at unknown T).
  2. The E-stop path (idle_all / RealPump.confirm_idle) reports a pump that has
     silently dropped to manual mode as failed instead of falsely "idled", and
     tick() flags a manual-mode pump as faulted.
  3. signal_measurement_complete() only ends the recipe it is FOR — a late or
     duplicate file.averaged from a different recipe can't truncate the run.
  4. _fire_spec_collection() honours the backend captured at DISPATCH time, so a
     backend switch between dispatch and execution cancels the collect.

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

from src.reactor.config import PUMP_NAMES                        # noqa: E402
from src.reactor.controller import ReactorController            # noqa: E402
from src.reactor.drivers import Py_P_Pump                       # noqa: E402
from src.reactor.hardware import PumpBank, RealPump, TempController  # noqa: E402


BASE_CFG = {
    "pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES},
    "bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
               "x_each": [0, 0.3], "x_sum_max": 0.9},
    "run": {"default_duration": 5.0},
    "spec": {"enabled": False},
}


def _ctl(**over):
    return ReactorController({**BASE_CFG, **over}, backend="mock")


# ── 1: a stale temperature reading is never "stable" ──────────────────────────

class _DeadableBL:
    """Fake beamline whose temperature source can be killed mid-run."""
    def __init__(self, temp): self._temp = temp; self.dead = False; self._collecting = False
    def is_collecting(self): return self._collecting
    def read_state(self):
        return {} if self.dead else {"temperature": self._temp, "bstop": 1.0, "i0": 2.0}
    def set_temperature(self, T): pass


def test_stale_temperature_never_reads_as_stable():
    bl = _DeadableBL(240.0)
    tc = TempController(
        {"temperature": {"read_interval_s": 0.0, "tolerance": 5.0, "stable_hold": 0.0}},
        backend="real", beamline=bl)
    tc.set_temperature(240.0)
    tc.tick(1.0)                       # reads 240 -> in band, stable_hold 0 -> stable
    assert tc.stale is False
    assert tc.is_stable() is True

    bl.dead = True                     # the SPEC/EPICS source dies
    tc.tick(1.0)                       # read_state() {} -> current frozen in-band
    tc._last_read_ok -= 100            # age past the 15 s stale limit
    assert tc.stale is True
    assert tc.is_stable() is False, \
        "a frozen/stale reading must not open the arming gate"


# ── 2: E-stop confirms the pump actually stopped ──────────────────────────────

class _FakeSerialPump:
    """Vendored-driver stand-in exposing a settable mode (1=remote, 0=manual)."""
    def __init__(self, *a, **k):
        self.mode = 1
        self.state_code = 1
        self.remote_ok = True
    def enter_remote(self): return self.remote_ok
    def read_status(self):
        return {"flow_rate_ulmin": 0.0, "chamber_pressure": 0.0,
                "state_code": self.state_code, "error_code": 0, "mode": self.mode}
    def set_flow(self, *a, **k): pass
    def set_idle(self): pass
    def close(self): pass


def _real_pump(monkeypatch, name="p"):
    monkeypatch.setattr(Py_P_Pump, "P_pump", _FakeSerialPump)
    return RealPump(name, "COM_FAKE", pump_id=0, max_flow=1000.0)


def test_confirm_idle_detects_a_pump_dropped_to_manual(monkeypatch):
    rp = _real_pump(monkeypatch)
    rp.idle_now()
    assert rp.confirm_idle() is True          # mode 1 (remote) -> confirmed stopped
    rp._pump.mode = 0                          # silently dropped to manual/local
    assert rp.confirm_idle() is False, \
        "a P0 ignored by a manual-mode pump must not confirm as idle"
    assert rp.fault is True


def test_tick_flags_a_manual_mode_pump(monkeypatch):
    rp = _real_pump(monkeypatch)
    rp.tick(3.0)                               # mode 1, state 1 -> no fault
    assert rp.fault is False
    rp._pump.mode = 0
    rp.tick(3.0)
    assert rp.fault is True, "a pump silently in manual mode is a fault"


def test_idle_all_reports_unconfirmed_pump(monkeypatch):
    bank = PumpBank({"pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES}}, backend="mock")
    victim = PUMP_NAMES[0]
    rp = _real_pump(monkeypatch, victim)
    rp._pump.mode = 0                          # this pump won't accept the idle
    bank.pumps[victim] = rp
    failed = bank.idle_all()
    assert victim in failed, "an unconfirmed pump must be surfaced on the E-stop path"
    # a confirmed real pump is NOT reported
    rp._pump.mode = 1
    assert bank.idle_all() == []


# ── 3: measurement-complete only ends its own recipe ──────────────────────────

def test_measurement_complete_ignores_other_recipes():
    ctl = _ctl()
    ctl.state = "running"
    ctl.current = types.SimpleNamespace(recipe_id="r002")

    ctl.signal_measurement_complete("late.dat", recipe_id="r001")
    assert ctl._measure_done is False, "a signal for another recipe must be ignored"

    ctl.signal_measurement_complete("mine.dat", recipe_id="r002")
    assert ctl._measure_done is True


def test_measurement_complete_without_recipe_id_still_honoured():
    ctl = _ctl()
    ctl.state = "running"
    ctl.current = types.SimpleNamespace(recipe_id="r002")
    ctl.signal_measurement_complete("manual.dat")     # manual run, no id
    assert ctl._measure_done is True


# ── 4: backend switch between dispatch and execution cancels the collect ──────

class _RecordingBL:
    def __init__(self): self.collect_calls = 0
    def collect(self, **k): self.collect_calls += 1


def test_fire_spec_collection_cancels_on_backend_switch():
    ctl = _ctl()                               # backend == "mock"
    fake = _RecordingBL()
    # dispatched under "real", but the controller is now "mock" -> cancel
    ctl._fire_spec_collection("r1", "sample", backend_at_dispatch="real", bl=fake)
    assert fake.collect_calls == 0

    # dispatched under the current backend -> proceeds
    ctl._fire_spec_collection("r1", "sample", backend_at_dispatch="mock", bl=fake)
    assert fake.collect_calls == 1
