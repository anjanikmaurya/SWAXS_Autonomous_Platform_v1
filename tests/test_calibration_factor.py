"""
tests/test_calibration_factor.py — per-pump water→fluid flow correction.

The Mitos sensor is water-calibrated and the app bypasses the Dolomite FCC, so the
driver applies a per-pump ``calibration_factor`` (cf = true/water): it COMMANDS the
pump in water units (target/cf) and REPORTS actual in true units (raw × cf).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.reactor.hardware import RealPump, PumpBank   # noqa: E402
from src.reactor.config import PUMP_NAMES             # noqa: E402


class _FakePump:
    """Stand-in for the vendored Py_P_Pump: records the setpoint it was sent."""
    def __init__(self):
        self.sent = None
        self._raw = 0.0
    def set_flow(self, rate, unit="ul/m"):
        self.sent = rate          # water-unit setpoint the driver sent
        self._raw = rate          # pretend the pump reaches it exactly (water units)
    def read_status(self):
        return {"flow_rate_ulmin": self._raw, "chamber_pressure": 0.0,
                "state_code": 1, "error_code": 0}


def _realpump_with_cf(cf, table=None):
    # build a RealPump without opening a serial port
    p = object.__new__(RealPump)
    p.name = "t"; p.max_flow = 1000.0; p.sensor_min = 0.0; p.max_pressure = 1e4
    p._init_cal(calibration_factor=cf, flowrate_table=table)
    p.target = 0.0; p.actual = 0.0; p.pressure = 0.0; p.idle = True
    p.state_code = 0; p.error_code = 0; p.fault = False; p.stale = False
    p._poll_accum = 99.0; p._poll_fails = 0; p.POLL_FAIL_LIMIT = 3
    p._pump = _FakePump()
    return p


def test_setpoint_divided_by_cf_and_readback_multiplied():
    cf = 1.25
    p = _realpump_with_cf(cf)
    p.set_flow(50.0)                      # command TRUE 50 µL/min
    assert p.target == 50.0              # app records the true setpoint
    assert abs(p._pump.sent - 50.0 / cf) < 1e-9   # pump gets water units = 40.0
    p.tick(5.0)                          # poll status (raw water = 40.0)
    assert abs(p.actual - 40.0 * cf) < 1e-9        # reported back as true 50.0


def test_cf_default_is_identity():
    p = _realpump_with_cf(1.0)
    p.set_flow(30.0)
    assert abs(p._pump.sent - 30.0) < 1e-9
    p.tick(5.0)
    assert abs(p.actual - 30.0) < 1e-9


def test_pumpbank_reads_calibration_factor_from_config():
    cfg = {"pumps": {n: {"max_flow": 100.0, "calibration_factor": 1.5} for n in PUMP_NAMES}}
    bank = PumpBank(cfg, backend="mock")
    for n in PUMP_NAMES:
        assert bank.pumps[n].calibration_factor == 1.5


def test_power_law_calibration_wins_and_roundtrips():
    # table where measured = setpoint**2  → fitted exponent a ≈ 2
    p = _realpump_with_cf(1.0, table=[[2, 4], [3, 9], [4, 16], [5, 25]])
    assert p.flow_power is not None and abs(p.flow_power - 2.0) < 0.05
    p.set_flow(16.0)                          # true target 16 → instrument setpt = 16**(1/2) = 4
    assert abs(p._pump.sent - 4.0) < 0.05
    p.tick(5.0)                               # raw 4 → true = 4**2 = 16
    assert abs(p.actual - 16.0) < 0.1


def test_flow_ok_logic():
    from src.reactor.hardware import _flow_ok
    assert _flow_ok(50, 50, 1.0, 0.2)             # on target
    assert not _flow_ok(30, 50, 1.0, 0.2)         # 40% low
    assert _flow_ok(0.4, 0.5, 1.0, 0.2)           # low setpt, abs err < sensitivity → ok
    assert not _flow_ok(3.0, 0.5, 1.0, 0.2)       # low setpt, abs err > sensitivity → not ok


def test_mock_flow_ok_and_volume_limit():
    from src.reactor.hardware import MockPump
    p = MockPump("t", max_flow=100.0, flow_sensitivity=1.0, flow_tol=0.2,
                 bad_flow_tol=2, volume_limit=5.0)
    p.set_flow(60.0)
    for _ in range(8):
        p.tick(1.0)                               # converge to 60 µL/min
    assert p.flow_ok is True and p.v_delivered > 0
    for _ in range(10):
        p.tick(1.0)                               # 60 µL/min → passes 5 µL cap
    assert p.volume_exceeded is True
    p.reset_volume()
    assert p.v_delivered == 0.0 and p.volume_exceeded is False
