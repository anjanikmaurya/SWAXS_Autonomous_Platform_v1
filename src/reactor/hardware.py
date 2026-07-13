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
        self.state_code = 0      # 0 IDLE / 1 CONTROLLING / 3 ERROR (mock: never faults)
        self.error_code = 0
        self.fault = False

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

    def close(self) -> None:
        pass

    def tare_pressure(self) -> None:
        pass

    def tare_flow(self) -> None:
        pass


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
        self.state_code = 0      # 0 IDLE / 1 CONTROLLING / 2 TARE / 3 ERROR / 4 LEAK
        self.error_code = 0
        self.fault = False       # True when the pump reports ERROR state (3)
        self._poll_accum = 0.0   # seconds since last status poll (keepalive)
        # ⟵ REAL DRIVER: open the serial connection to the Mitos P-pump.
        # The driver opens at 57600 8N1 and enters REMOTE control (A1) so the
        # pump accepts flow commands. Status is polled in tick() — that both
        # updates the readings and keeps the pump from dropping out of control
        # (it exits control after ~30 s without a command).
        from .drivers import Py_P_Pump            # noqa: PLC0415
        try:
            self._pump = Py_P_Pump.P_pump(address, name=name, pump_id=pump_id, verbose=False)
        except Exception as exc:
            raise RuntimeError(
                f"pump '{name}': could not open {address!r} ({exc}). "
                f"Is the Dolomite GUI open, or is another program using {address}?"
            ) from exc

    def close(self) -> None:
        try:
            self._pump.close()
        except Exception:
            pass

    def tare_pressure(self) -> None:
        # ⟵ REAL DRIVER: pump pressure tare ('R0'). Air must be disconnected.
        self._pump.tare()

    def tare_flow(self) -> None:
        # ⟵ REAL DRIVER: flow-sensor tare ('R1'). Ensure no flow.
        self._pump.tare_flow()

    def set_flow(self, rate: float) -> None:
        self.target = float(rate)
        self.idle = (rate == 0.0)
        # ⟵ REAL DRIVER: set the flow-rate setpoint in µL/min (driver converts
        # to pl/s and sends 'F<pl_s>'). A 0 setpoint sends flow = 0.
        self._pump.set_flow(rate, unit="ul/m")

    def idle_now(self) -> None:
        self.target = 0.0
        self.idle = True
        # ⟵ REAL DRIVER: idle / vent the chamber ('P0').
        self._pump.set_idle()

    def tick(self, dt: float) -> None:
        # ⟵ REAL DRIVER: poll status ~every 3 s. This updates the live flow +
        # chamber pressure AND keeps the pump in control mode (any command,
        # including the status query 's', resets the pump's 30 s timeout).
        self._poll_accum += dt
        if self._poll_accum >= 3.0:
            self._poll_accum = 0.0
            try:
                st = self._pump.read_status()
                self.actual = st["flow_rate_ulmin"]
                self.pressure = st["chamber_pressure"]
                self.state_code = st["state_code"]
                self.error_code = st["error_code"]
                self.fault = (st["state_code"] == 3)   # 3 = ERROR
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
                try:
                    self.pumps[name] = RealPump(name, pc.get("address", ""),
                                                int(pc.get("pump_id", 0)), mx, mn, pp)
                except Exception:
                    # release any ports already opened before reporting
                    for p in self.pumps.values():
                        p.close()
                    raise
            else:
                self.pumps[name] = MockPump(name, mx, mn, pp)

    # ── the single real call point ──────────────────────────────────────────
    def set_pump_flow(self, pump: str, rate: float) -> None:
        """Command one pump to ``rate`` µL/min.  ⟵ swap mock↔real here."""
        if pump not in self.pumps:
            raise KeyError(f"unknown pump: {pump}")
        self.pumps[pump].set_flow(rate)

    def tare(self, name: str, kind: str = "pressure") -> None:
        """Tare one pump. kind='pressure' sends the pump pressure tare (R0).
        (A separate flow-sensor tare command is not yet confirmed.)"""
        p = self.pumps.get(name)
        if p is None:
            raise KeyError(f"unknown pump: {name}")
        if kind == "pressure":
            p.tare_pressure()
        elif kind == "flow":
            p.tare_flow()
        elif kind == "both":
            p.tare_pressure()
            p.tare_flow()
        else:
            raise ValueError(f"unknown tare kind '{kind}'")

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
                       "idle": p.idle,
                       "fault": bool(getattr(p, "fault", False)),
                       "state_code": int(getattr(p, "state_code", 0))}
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
        # ⟵ REAL DRIVER HOOK — reactor temperature source.
        #
        # This stub returns the last value (ambient) so nothing crashes. Until
        # you wire a sensor, use *timed* arming mode (config `arming.default_mode:
        # timed`, or per recipe) so runs don't depend on this reading.
        #
        # When a temperature source is available, replace the body with ONE of:
        #   • USB/serial thermocouple:
        #       return float(self._thermo.read_celsius())     # your reader object
        #   • external controller writing a file:
        #       return float(Path("/path/to/reactor_temp.txt").read_text().strip())
        #   • network/PLC/Modbus query, etc.
        # Return the current reactor temperature in °C. Keep it fast and
        # non-blocking — this is polled ~5×/second by the control loop.
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
