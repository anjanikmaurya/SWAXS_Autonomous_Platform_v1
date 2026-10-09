"""
src/reactor/supervisor.py — part of ReactorController (split out October 2026).

The safety supervisor: temperature, pressure, delivered volume and flow, during the run and the flush.

The methods below were moved VERBATIM from controller.py; ReactorController
inherits SupervisorMixin, so every call and every self.* attribute is unchanged.
Proven identical by tests/test_reactor_golden_run.py.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from .config import REAGENT_PUMPS, FLUSH_PUMP, PUMP_NAMES
from .hardware import PumpBank, TempController
from .recipe import Recipe, RecipeError, recipe_to_setpoints, validate
from ..beamline import make_beamline

#: Module logger. Its absence used to be a safety defect, not a cosmetic one:
#: the control-loop fault handler called `logger.exception(...)` BEFORE the
#: emergency stop, so the resulting NameError skipped the E-stop and killed the
#: supervisor thread with the pumps still commanded.
logger = logging.getLogger("swaxs_platform")

STATES = ["idle", "arming", "running", "flushing", "ready", "estop"]


def _noop(*a, **k):
    return None


def _spec_cfg_for(cfg: dict, backend: str) -> dict:
    """cfg with spec.backend forced to ``backend`` — so the pump Mock/Real choice
    also governs the beamline (one switch covers both)."""
    spec = dict(cfg.get("spec", {})); spec["backend"] = backend
    return {**cfg, "spec": spec}




class SupervisorMixin:
    def _safety_check(self) -> None:
        if self.state == "estop":
            return
        # A pump reporting ERROR state (3) while we're trying to run/arm — e.g.
        # low air supply, blockage, or flow-sensor loss — trips the E-stop so it
        # can't silently deliver the wrong (or no) flow.
        if self.state in ("arming", "running", "flushing"):
            faulted = [n for n, p in self.pumps.pumps.items()
                       if getattr(p, "fault", False)]
            if faulted:
                self._log(f"🛑 SAFETY E-STOP: pump(s) {', '.join(faulted)} report "
                          f"ERROR/lost state while {self.state} — check air supply, "
                          f"blockage, flow sensor and serial connection", "error")
                self._event("reactor.safety", {"check": "pump fault",
                            "detail": f"pumps in ERROR/lost state: {', '.join(faulted)}",
                            "pumps": faulted, "state": self.state})
                self.estop()
                return
        # A dead temperature source is NOT "25 °C, all good": if read_state()
        # keeps failing, `current` freezes at the ambient default and the T_max
        # comparison below can never be true — the thermal interlock would be
        # silently disabled while reagents flow through a hot reactor.
        # A pause WE caused is not a sensor fault. While a 2D acquisition holds the
        # SPEC lock, read_state() returns {} by design, so the age climbs on every
        # single collection (100 s with the shipped exposure × frames). Reporting
        # that as "🛑 SAFETY … check spec.temp_counter" sent the operator to
        # inspect a counter that was working perfectly. Say what is actually
        # happening, once per run, and name the remedy that actually fixes it.
        if self.state in ("arming", "running", "flushing") and self.temp.polling_paused:
            if not self._temp_paused_noted:
                self._temp_paused_noted = True
                if self.temp.blind_during_collect:
                    self._log(
                        "ℹ temperature polling is paused for the duration of the 2D "
                        "acquisition (the collect holds the SPEC lock). The "
                        "over-temperature interlock has no fresh reading until it "
                        "finishes. To keep it live during data collection set "
                        "spec.read_source: \"epics\", or spec.read_during_collect: "
                        "true.", "warn")
        elif not self.temp.polling_paused:
            self._temp_paused_noted = False

        if self.state in ("arming", "running", "flushing") and self.temp.stale:
            if not self._temp_stale_warned:
                self._temp_stale_warned = True
                # Two different faults reach here and they need different
                # remedies, so name which one it is (audit R3). An overrun
                # means a 2D acquisition is STILL holding the SPEC lock long
                # past its own exposure × frames — the reading is frozen
                # because of a hang, not because the counter is broken.
                overrun = self.temp.collect_overrun_s
                if overrun > 0:
                    exp = 0.0
                    try:
                        exp = float(self.beamline.collect_expected_s())
                    except Exception:
                        pass
                    self._log(
                        f"🛑 SAFETY: the 2D acquisition has been holding the SPEC "
                        f"lock for {overrun:.0f}s longer than the {exp:g}s it "
                        f"should take. The temperature reading has been frozen "
                        f"that whole time, so the over-temperature interlock "
                        f"cannot protect you. Check the detector and SPEC — it "
                        f"is not reporting the macro finished.", "error")
                    self._event("reactor.safety",
                                {"check": "2D acquisition overrun",
                                 "detail": f"collect has overrun by {overrun:.0f}s "
                                           f"(expected {exp:g}s); the "
                                           f"over-temperature interlock is blind"})
                else:
                    self._log(f"🛑 SAFETY: temperature reading is STALE "
                              f"({self.temp.age_s():.0f}s since the last successful read, "
                              f"and no acquisition is running) — the over-temperature "
                              f"interlock cannot protect you. Check the SPEC/EPICS "
                              f"temperature source "
                              f"(spec.temp_counter / spec.epics_pvs).", "error")
                    self._event("reactor.safety", {"check": "temperature reading stale",
                                "detail": f"no successful read for {self.temp.age_s():.0f}s — "
                                          f"the over-temperature interlock is blind"})
                if self._temp_stale_estop:
                    self.estop()
                    return
        elif not self.temp.stale:
            self._temp_stale_warned = False

        if self.temp.current > self.T_max + 0.5:
            self._log(f"🛑 SAFETY E-STOP: reactor {self.temp.current:.1f}°C exceeds "
                      f"T_max {self.T_max:g}°C — all pumps idled", "error")
            self._event("reactor.safety", {"check": "over-temperature",
                        "detail": f"{self.temp.current:.1f}°C > T_max {self.T_max:g}°C"})
            self.estop()
            return
        for name, p in self.pumps.pumps.items():
            if p.target > self.per_pump_max + 1e-6:
                self._log(f"🛑 SAFETY E-STOP: {name} setpoint {p.target:.1f} µL/min "
                          f"exceeds per_pump_max {self.per_pump_max:g} µL/min "
                          f"— check the recipe or safety.per_pump_max", "error")
                self._event("reactor.safety", {"check": "pump setpoint over limit",
                            "detail": f"{name} {p.target:.1f} > {self.per_pump_max:g} µL/min"})
                self.estop()
                return
            # A pump's OWN max_flow, not just the platform-wide ceiling. This
            # was checked at intake and never again (audit R9), so narrowing a
            # limit while a recipe sat in the queue — the natural reaction to
            # noticing a pump misbehaving — did not apply to that recipe. On the
            # shipped config that is a 20× gap (50 vs 1000 µL/min) on the three
            # small-sensor reagent pumps, with nothing watching it at runtime.
            own_max = float(getattr(p, "max_flow", 0.0) or 0.0)
            if own_max and p.target > own_max + 1e-6:
                self._log(f"🛑 SAFETY E-STOP: {name} setpoint {p.target:.1f} µL/min "
                          f"exceeds its own max_flow {own_max:g} µL/min — the "
                          f"limit was narrowed after this recipe was accepted, "
                          f"or the sensor was reconfigured", "error")
                self._event("reactor.safety", {"check": "pump setpoint over its own max",
                            "detail": f"{name} {p.target:.1f} > max_flow {own_max:g} µL/min"})
                self.estop()
                return
            # pump pressure must never exceed the pump's pressure ceiling
            pmax = getattr(p, "max_pressure", 0.0)
            if pmax and getattr(p, "pressure", 0.0) > pmax + 1e-6:
                self._log(f"🛑 SAFETY E-STOP: {name} at {p.pressure:.0f} mbar exceeds "
                          f"its {pmax:.0f} mbar ceiling — likely a blockage "
                          f"downstream; check the line and capillary", "error")
                self._event("reactor.safety", {"check": "over-pressure",
                            "detail": f"{name} {p.pressure:.0f} > {pmax:.0f} mbar"})
                self.estop()
                return
        # delivered-volume limit + sustained-bad-flow checks (during a run)
        if self.state == "running":
            vex = self.pumps.volume_exceeded()
            if vex:
                det = ", ".join(
                    f"{n} {getattr(self.pumps.pumps.get(n), 'v_delivered', 0.0):.0f}/"
                    f"{getattr(self.pumps.pumps.get(n), 'volume_limit', 0.0):.0f} µL"
                    for n in vex)
                self._log(f"🛑 SAFETY: delivered-volume limit reached ({det}) "
                          f"— ending the run early and flushing", "warn")
                self._run_reason = "volume limit exceeded"
                self._end_run(flush=True)
                return
            ff = set(self.pumps.flow_faults())
            if ff and self._flow_fault_estop:
                self._log(f"🛑 SAFETY E-STOP: {', '.join(sorted(ff))} flow is far from "
                          f"setpoint — check supply pressure, blockage and the flow "
                          f"sensor (set safety.flow_fault_estop=false to warn only)",
                          "error")
                self.estop()
                return
            for nm in ff - self._flow_faulted_prev:      # warn once per onset
                p = self.pumps.pumps.get(nm)
                act, tgt = getattr(p, "actual", 0.0), getattr(p, "target", 0.0)
                self._log(f"⚠ {nm}: measured {act:.1f} vs setpoint {tgt:.1f} µL/min "
                          f"— check supply/blockage/sensor (run continues)", "warn")
            self._flow_faulted_prev = ff
        elif self.state == "flushing":
            self._check_flush_safety()

    #: µL a non-flush pump may still deliver in a flush (its ramp-down tail)
    #: before a volume cap breach counts as "it never stopped".
    FLUSH_TAIL_UL = 5.0

    def _check_flush_safety(self) -> None:
        """Supervise the flush (OPEN_DEFECTS R6). Caller holds _lock.

        These checks used to run only while ``running``, so the flush, the
        longest phase (20 min shipped), went unwatched. Same rules as a run, so
        nothing new can surprise the operator:

        * Flow fault on a commanded pump (the flush pump is not delivering, the
          line may not be clean): warn once per onset; E-stop only when
          ``safety.flow_fault_estop`` is true, exactly as during a run.
        * A pump that is NOT the flush pump passing its optional
          ``volume_limit`` by more than its ramp-down tail during the flush has
          not stopped when told to: E-stop. Pumps without a ``volume_limit``
          (the shipped config sets none) are not volume-checked, as in a run.
        """
        flush_pump = getattr(self, "_flush_active", None) or getattr(self, "_flush_pump", None)
        v0 = getattr(self, "_flush_v0", {}) or {}
        # A pump that should be idle but is still flowing (audit Oct 2026). The
        # volume check below needs an optional volume_limit, which the shipped
        # config does not set, and an idle pump (target 0) is never judged on
        # flow by the pump health logic. So: after the ramp-down grace
        # (safety.flow_settle_s), a non-flush pump measuring more than a few
        # µL/min for safety.bad_flow_s is "not stopping". Same rule as a flow
        # fault: warn once, E-stop only with safety.flow_fault_estop: true.
        sc = (self.cfg.get("safety") or {}) if isinstance(getattr(self, "cfg", None), dict) else {}
        settle = float(sc.get("flow_settle_s", 10.0) or 0.0)
        hold = float(sc.get("bad_flow_s", 12.0) or 12.0)
        thresh = max(2.0, 2.0 * float(sc.get("flow_sensitivity", 1.0) or 1.0))
        now = time.time()
        since = getattr(self, "_idle_flow_since", None)
        if since is None:
            since = self._idle_flow_since = {}
        not_stopping = []
        if now - float(getattr(self, "_flush_started_at", now)) >= settle:
            for n, p in self.pumps.pumps.items():
                if n == flush_pump:
                    continue
                act = float(getattr(p, "actual", 0.0) or 0.0)
                if act > thresh:
                    since.setdefault(n, now)
                    if now - since[n] >= hold:
                        not_stopping.append((n, act))
                else:
                    since.pop(n, None)
        new = [(n, a) for n, a in not_stopping if n not in getattr(self, "_idle_flow_warned", set())]
        if not_stopping and self._flow_fault_estop:
            det = ", ".join(f"{n} {a:.1f} µL/min" for n, a in not_stopping)
            self._log(f"🛑 SAFETY E-STOP: {det} — a pump that should be idle during the "
                      f"flush is still flowing; check that it stopped", "error")
            self._event("reactor.safety", {"check": "flush-idle-flow", "detail": det})
            self.estop()
            return
        for n, a in new:
            self._log(f"⚠ {n} should be idle during the flush but measures {a:.1f} µL/min "
                      f"for over {hold:g}s — check that it stopped (flush continues)", "warn")
        self._idle_flow_warned = {n for n, _ in not_stopping}
        stuck = []
        for n in self.pumps.volume_exceeded():
            if n == flush_pump:
                continue
            p = self.pumps.pumps.get(n)
            during = float(getattr(p, "v_delivered", 0.0) or 0.0) - v0.get(n, 0.0)
            if during > self.FLUSH_TAIL_UL:
                stuck.append(f"{n} +{during:.0f} µL during the flush")
        if stuck:
            self._log(f"🛑 SAFETY E-STOP: {', '.join(stuck)} — a pump that should be "
                      f"idle during the flush kept delivering past its volume limit; "
                      f"check that it stopped", "error")
            self._event("reactor.safety", {"check": "flush-volume", "detail": ", ".join(stuck)})
            self.estop()
            return
        ff = set(self.pumps.flow_faults())
        if ff and self._flow_fault_estop:
            self._log(f"🛑 SAFETY E-STOP: {', '.join(sorted(ff))} flow is far from "
                      f"setpoint during the flush — check supply pressure, blockage and "
                      f"the flow sensor (set safety.flow_fault_estop=false to warn only)",
                      "error")
            self.estop()
            return
        for nm in ff - self._flow_faulted_prev:          # warn once per onset
            p = self.pumps.pumps.get(nm)
            act, tgt = getattr(p, "actual", 0.0), getattr(p, "target", 0.0)
            self._log(f"⚠ {nm}: measured {act:.1f} vs setpoint {tgt:.1f} µL/min during "
                      f"the flush — the line may not be getting cleaned (flush continues)",
                      "warn")
        self._flow_faulted_prev = ff
