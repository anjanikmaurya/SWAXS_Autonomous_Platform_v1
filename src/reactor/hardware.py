"""
src/reactor/hardware.py — pump + temperature hardware interface.

Two backends, selected by ``backend=`` on PumpBank:
  • "mock" (default) — in-memory state; flows converge toward setpoints so the
    dashboard and the run/flush logic behave realistically with no hardware.
  • "real"           — drives the Dolomite Mitos P-pump via the vendored
    ``Py_P_Pump`` SDK.  The exact SDK call sites are marked  ⟵ REAL DRIVER.

The single real call point is ``PumpBank.set_pump_flow(pump, rate_uL_min)``.

Temperature is *read-only* here (gate-only): this app does NOT command a heater.
``TempController`` exposes a target (the recipe's T_reac, used only to decide
when the reactor is "at temperature") and a current reading.  In mock mode the
reading ramps toward the target; the real reading comes from an external
controller via the marked hook.
"""

from __future__ import annotations

import threading
import time


# ── Pumps ─────────────────────────────────────────────────────────────────────

class MockPump:
    """In-memory pump: `actual` chases `target` so flows look real."""

    def __init__(self, name: str, max_flow: float, sensor_min: float = 0.0,
                 max_pressure: float = 10000.0):
        self.name = name
        self.max_flow = float(max_flow)
        self.sensor_min = float(sensor_min)
        self.max_pressure = float(max_pressure)
        self.target = 0.0        # commanded µL/min
        self.actual = 0.0        # observed µL/min (mock)
        self.pressure = 0.0      # chamber pressure (mbar, mock)
        self.idle = True

    def set_flow(self, rate: float) -> None:
        self.target = float(rate)
        self.idle = (rate == 0.0)

    def tick(self, dt: float) -> None:
        # first-order approach to the setpoint (~2 s time constant)
        self.actual += (self.target - self.actual) * min(1.0, dt / 2.0)
        if abs(self.actual - self.target) < 1e-3:
            self.actual = self.target
        # mock chamber pressure: rises with flow demand, well under the ceiling
        frac = (self.actual / self.max_flow) if self.max_flow else 0.0
        self.pressure = round(0.6 * self.max_pressure * max(0.0, min(1.0, frac)), 1)


class RealPump:
    """Adapter around the vendored Py_P_Pump SDK (one serial connection)."""

    def __init__(self, name: str, address: str, pump_id: int,
                 max_flow: float, sensor_min: float = 0.0,
                 max_pressure: float = 10000.0):
        self.name = name
        self.max_flow = float(max_flow)
        self.sensor_min = float(sensor_min)
        self.max_pressure = float(max_pressure)
        self.target = 0.0
        self.actual = 0.0
        self.pressure = 0.0
        self.idle = True
        self._p_ctr = 0          # throttle counter for pressure reads
        # ⟵ REAL DRIVER: open the serial connection to the Mitos P-pump.
        from .drivers import Py_P_Pump            # noqa: PLC0415
        self._pump = Py_P_Pump.P_pump(address, name=name, pump_id=pump_id, verbose=False)

    def set_flow(self, rate: float) -> None:
        self.target = float(rate)
        self.idle = (rate == 0.0)
        # ⟵ REAL DRIVER: flow-control mode, µL/min ('ul/m'), indefinite hold.
        # A 0 setpoint sends flow = 0 (per config decision), not set_idle().
        self._pump.set_flow(rate, unit="ul/m", hold="00:00:00:00")

    def idle_now(self) -> None:
        self.target = 0.0
        self.idle = True
        # ⟵ REAL DRIVER: vent chamber + stop flow.
        self._pump.set_idle()

    def tick(self, dt: float) -> None:
        # ⟵ REAL DRIVER (optional): read the live flow from the sensor, e.g.
        #     self.actual = self._pump.get_target() ...  For now mirror target.
        self.actual = self.target
        # ⟵ REAL DRIVER: read chamber pressure, throttled to ~1 s (serial is slow).
        # get_pressure() returns [atmospheric, supply, chamber] in mbar.
        self._p_ctr += 1
        if self._p_ctr >= 5:
            self._p_ctr = 0
            try:
                self.pressure = float(self._pump.get_pressure()[2])   # chamber
            except Exception:
                pass


class PumpBank:
    """The five named pumps + the marked hardware call points."""

    def __init__(self, cfg: dict, backend: str = "mock"):
        from .config import PUMP_NAMES
        self.backend = backend
        self.pumps: dict[str, object] = {}
        pumps_cfg = cfg.get("pumps", {})
        pmax_global = float(cfg.get("safety", {}).get("max_pressure", 10000.0))
        for name in PUMP_NAMES:
            pc = pumps_cfg.get(name, {})
            mx = float(pc.get("max_flow", 1000.0))
            mn = float(pc.get("sensor_min", 0.0))
            pp = float(pc.get("max_pressure", pmax_global))
            if backend == "real":
                self.pumps[name] = RealPump(name, pc.get("address", ""),
                                            int(pc.get("pump_id", 0)), mx, mn, pp)
            else:
                self.pumps[name] = MockPump(name, mx, mn, pp)

    # ── the single real call point ──────────────────────────────────────────
    def set_pump_flow(self, pump: str, rate: float) -> None:
        """Command one pump to ``rate`` µL/min.  ⟵ swap mock↔real here."""
        if pump not in self.pumps:
            raise KeyError(f"unknown pump: {pump}")
        self.pumps[pump].set_flow(rate)

    def set_all(self, setpoints: dict) -> None:
        for name in self.pumps:
            self.set_pump_flow(name, float(setpoints.get(name, 0.0)))

    def idle_all(self) -> None:
        for name, p in self.pumps.items():
            if isinstance(p, RealPump):
                p.idle_now()
            else:
                p.set_flow(0.0)

    def tick(self, dt: float) -> None:
        for p in self.pumps.values():
            p.tick(dt)

    def state(self) -> dict:
        return {name: {"target": round(p.target, 3), "actual": round(p.actual, 3),
                       "max_flow": p.max_flow, "sensor_min": p.sensor_min,
                       "pressure": round(getattr(p, "pressure", 0.0), 1),
                       "max_pressure": getattr(p, "max_pressure", 0.0),
                       "idle": p.idle}
                for name, p in self.pumps.items()}


# ── Temperature (read-only / gate-only) ───────────────────────────────────────

class TempController:
    """Reactor-temperature *reader* with a target setpoint used only to gate the
    run.  This app does not drive a heater."""

    def __init__(self, cfg: dict, backend: str = "mock"):
        t = cfg.get("temperature", {})
        self.backend = backend
        self.tolerance = float(t.get("tolerance", 2.0))
        self.stable_hold = float(t.get("stable_hold", 5.0))
        self.timeout = float(t.get("timeout", 900.0))
        self._mock_ramp = float(t.get("mock_ramp", 5.0))
        self.target = 0.0
        self.current = 25.0           # ambient
        self._in_band_since: float | None = None
        self._lock = threading.Lock()

    def set_temperature(self, T: float) -> None:
        """Record the desired reactor temperature (the external controller is
        expected to drive the heater).  ⟵ NOTE: no heater command issued here."""
        with self._lock:
            self.target = float(T)
            self._in_band_since = None

    def read(self) -> float:
        # ⟵ REAL: read the reactor thermocouple / external controller here.
        return self.current

    def tick(self, dt: float) -> None:
        if self.backend != "mock":
            self.current = self.read()
        else:
            step = self._mock_ramp * dt
            if abs(self.target - self.current) <= step:
                self.current = self.target
            else:
                self.current += step if self.target > self.current else -step
        # track how long we've been within tolerance of the target
        if self.target > 0 and abs(self.current - self.target) <= self.tolerance:
            if self._in_band_since is None:
                self._in_band_since = time.time()
        else:
            self._in_band_since = None

    def is_stable(self) -> bool:
        return (self.target > 0 and self._in_band_since is not None
                and (time.time() - self._in_band_since) >= self.stable_hold)
