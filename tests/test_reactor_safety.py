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
        pass


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
