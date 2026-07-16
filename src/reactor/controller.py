"""
src/reactor/controller.py — run / flush state machine for the 5-pump reactor.

State flow:
    idle ──Start/auto──▶ arming ──temp stable──▶ running ──run ends──▶ flushing ──▶ ready
      ▲                    │ timeout                                   ▲  (auto-advance to
      └──────────────── (abort)  ◀── Abort ───────────────────────────┘   next queued recipe)
    Emergency stop (estop) idles everything from any state.

A run ends when (in priority order): a SAXS measurement signal arrives
(``signal_measurement_complete``), the operator Stops, or the fallback run
duration elapses.

This module is pure Python (no Flask).  The app injects callbacks:
    log_cb(msg, tag)            — push a line to the UI log
    event_cb(event_type, data)  — publish on the hub event bus
    feedback_cb(recipe_id, payload) — write <id>.done.json for the BO side
    manifest_cb(record)         — persist a run record in manifest.json
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .config import REAGENT_PUMPS, FLUSH_PUMP
from .hardware import PumpBank, TempController
from .recipe import Recipe, RecipeError, recipe_to_setpoints, validate
from ..beamline import make_beamline

STATES = ["idle", "arming", "running", "flushing", "ready", "estop"]

RAMP_START_TEMP_C = 25.0    # assumed ambient start for ramp-mode arming


def ramp_wait_seconds(t_final: float, rate_c_per_min: float,
                      t_start: float = RAMP_START_TEMP_C) -> float:
    """Seconds to ramp from ``t_start`` (default 25 °C) up to ``t_final`` at
    ``rate_c_per_min`` °C/min: ``(t_final − t_start) / rate × 60``. Returns 0 if
    the target is at/below the start or the rate is not positive."""
    if rate_c_per_min and rate_c_per_min > 0 and t_final > t_start:
        return (t_final - t_start) / rate_c_per_min * 60.0
    return 0.0


def _noop(*a, **k):
    return None


def _spec_cfg_for(cfg: dict, backend: str) -> dict:
    """cfg with spec.backend forced to ``backend`` — so the pump Mock/Real choice
    also governs the beamline (one switch covers both)."""
    spec = dict(cfg.get("spec", {})); spec["backend"] = backend
    return {**cfg, "spec": spec}


class ReactorController:
    def __init__(self, cfg: dict, backend: str = "mock", *,
                 log_cb=None, event_cb=None, feedback_cb=None, manifest_cb=None):
        self.cfg = cfg
        self.backend = backend
        self.pumps = PumpBank(cfg, backend=backend)
        # beamline follows the same backend as the pumps (one Mock/Real switch)
        self.beamline = make_beamline(_spec_cfg_for(cfg, backend))
        self.temp = TempController(cfg, backend=backend, beamline=self.beamline)
        self._log = log_cb or _noop
        self._event = event_cb or _noop
        self._feedback = feedback_cb or _noop
        self._manifest = manifest_cb or _noop

        self.state = "idle"
        self.auto_run = False
        self.queue: deque[tuple[Recipe, dict]] = deque()   # (recipe, setpoints)
        self.current: Recipe | None = None
        self.setpoints: dict = {}
        self.history: list[dict] = []

        # timers
        self._run_started = 0.0
        self._run_deadline = 0.0
        self._arm_deadline = 0.0
        self._arm_mode = "temperature"   # active recipe's arming mode
        self._arm_ready_at = 0.0         # when timed/ramp arming completes
        self._arm_total = 0.0            # full timed/ramp wait (s), for the UI bar
        self._flush_deadline = 0.0
        self._flush_kind = "flush"
        self._measure_done = False
        self._run_reason = ""
        self._meas_sum: dict = {}    # accumulates measured flow per pump during a run
        self._meas_n = 0             # number of samples accumulated
        self._meas_series: list = []  # sampled per-pump flow trace over the run
        self._meas_last_sample = 0.0  # time of the last trace sample

        run = cfg.get("run", {})
        self.default_duration = float(run.get("default_duration", 600.0))
        self.end_on_measurement = bool(run.get("end_on_measurement", True))
        # how often (s) to sample the delivered-flow trace saved in the done file
        self.meas_sample_s = float(run.get("log_interval_s", 2.0))
        # Autonomous loop: hold the current condition (steady flow) until the
        # next recipe is queued (e.g. a new param file lands), then advance.
        self.advance_on_new = bool(run.get("advance_on_new_file", False))
        self.min_dwell = float(run.get("min_dwell_s", 60.0))
        # Live run settings from the app inputs. These apply to BOTH manual and
        # autonomous runs for everything EXCEPT the flow fractions / F_tot /
        # temperature (which come from the recipe / predicted folder file).
        # None = fall back to the config default.
        self.live_duration: float | None = None      # synthesis run duration (s)
        self.live_arm_mode: str | None = None         # "temperature" | "timed" | "ramp"
        self.live_arm_wait: float | None = None        # timed-arming wait (s)
        self.live_arm_ramp: float | None = None        # ramp-arming rate (°C/min)
        self.live_flush_rate: float | None = None      # flush rate (µL/min)
        self.live_flush_duration: float | None = None  # flush duration (s)
        arm = cfg.get("arming", {})
        self.default_arm_mode = str(arm.get("default_mode", "temperature")).lower()
        self.default_arm_wait = float(arm.get("default_wait_s", 120.0))
        self.default_ramp_rate = float(arm.get("default_ramp_rate", 0.0) or 0.0)
        self.ramp_start_temp = float(arm.get("start_temp_c", RAMP_START_TEMP_C))
        fl = cfg.get("flush", {})
        self.flush_rate = float(fl.get("rate", 100.0))
        self.flush_duration = float(fl.get("duration", 300.0))
        s = cfg.get("safety", {})
        self.T_max = float(s.get("T_max", 320.0))
        self.per_pump_max = float(s.get("per_pump_max", 1000.0))
        # SPEC data-collection: fire a 2D acquisition this long before the run ends
        spec = cfg.get("spec", {})
        self._spec_enabled = bool(spec.get("enabled", True))
        self._spec_lead = float(spec.get("spec_lead_s", 180.0))
        self._spec_exposure = float(spec.get("exposure_s", 1.0))
        self._spec_frames = int(spec.get("frames", 1))
        self._spec_data_dir = str(spec.get("data_dir", ""))
        self._spec_sample_tag = str(spec.get("sample_tag", "sample"))
        self._spec_bkg_tag = str(spec.get("bkg_tag", "bkg"))
        self._spec_fired = False        # sample acquisition fired this run
        self._bkg_fired = False         # background acquisition fired this flush
        self._last_collect = None       # {role, recipe_id, path, t} of the last SPEC trigger

        self._lock = threading.RLock()
        self._alive = True
        self._last = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ── intake ────────────────────────────────────────────────────────────────
    def submit(self, data: dict, source: str = "api") -> dict:
        """Validate + convert a recipe and enqueue it.  Raises RecipeError on a
        bad/unsafe recipe (nothing is sent to hardware).  Auto-starts only if the
        Auto-run toggle is on and the system is free."""
        recipe = Recipe.from_dict(data)
        recipe.source = source
        setpoints = recipe_to_setpoints(recipe, self.cfg)   # validates + clamps
        with self._lock:
            self.queue.append((recipe, setpoints))
            self._log(f"📥 queued {recipe.recipe_id} "
                      f"(T={recipe.T_reac:g}°C, F_tot={recipe.F_tot:g} µL/min) "
                      f"via {source}", "info")
            if self.auto_run and self.state in ("idle", "ready"):
                self._begin_next()
        return {"recipe": recipe.to_dict(), "setpoints": setpoints,
                "queued": len(self.queue)}

    def start(self) -> bool:
        """Operator Start: begin the next queued recipe if free."""
        with self._lock:
            if self.state in ("idle", "ready") and self.queue:
                self._begin_next()
                return True
        return False

    def start_now(self) -> bool:
        """Skip the remaining arming wait and start the pumps immediately."""
        with self._lock:
            if self.state == "arming":
                self._log("⏩ arming skipped — starting pumps now", "info")
                self._enter_running()
                return True
        return False

    def pump_limits(self) -> dict:
        """Current per-pump {sensor_min, max_flow} (µL/min)."""
        return {name: {"sensor_min": p.sensor_min, "max_flow": p.max_flow}
                for name, p in self.pumps.pumps.items()}

    def set_pump_limits(self, limits: dict) -> dict:
        """Update per-pump flow limits live.  ``limits`` = {pump: {sensor_min,
        max_flow}}.  These feed recipe validation/rejection immediately and the
        dashboard %-of-max bars.  Raises ValueError on a bad range."""
        with self._lock:
            for name, lim in (limits or {}).items():
                p = self.pumps.pumps.get(name)
                if p is None:
                    continue
                smin = float(lim.get("sensor_min", p.sensor_min))
                smax = float(lim.get("max_flow", p.max_flow))
                if smin < 0 or smax <= smin:
                    raise ValueError(f"{name}: need 0 ≤ min < max (got {smin}, {smax})")
                pc = self.cfg.setdefault("pumps", {}).setdefault(name, {})
                pc["sensor_min"] = smin
                pc["max_flow"] = smax
                p.sensor_min = smin
                p.max_flow = smax
            self._log("⚙ pump flow limits updated", "info")
            return self.pump_limits()

    def tare_pump(self, name: str, kind: str = "pressure") -> tuple[bool, str]:
        """Tare one pump's pressure (kind='pressure' -> R0). Only when idle, so
        it never interferes with a run. Disconnect the air supply first."""
        with self._lock:
            if self.state not in ("idle", "ready", "estop"):
                return False, f"can't tare while {self.state} (stop the run first)"
            try:
                self.pumps.tare(name, kind=kind)
            except Exception as exc:
                self._log(f"⚠ tare {name} ({kind}) failed: {exc}", "warn")
                return False, str(exc)
            note = {"pressure": "air disconnected", "flow": "no flow",
                    "both": "air disconnected + no flow"}.get(kind, "")
            self._log(f"🔧 tared {name} ({kind}) — needs {note}", "info")
            return True, "ok"

    def clear_queue(self) -> int:
        """Empty the pending-recipe queue (does not affect a running recipe).
        Returns the number of recipes removed."""
        with self._lock:
            n = len(self.queue)
            self.queue.clear()
            if n:
                self._log(f"🗑 cleared {n} queued recipe(s)", "info")
            return n

    def set_auto_run(self, on: bool) -> None:
        with self._lock:
            self.auto_run = bool(on)
            self._log(f"⚙ auto-run {'ON' if on else 'OFF'}", "info")
            if on and self.state in ("idle", "ready") and self.queue:
                self._begin_next()

    def set_run_settings(self, d: dict) -> None:
        """Apply live run settings from the app inputs — everything EXCEPT the
        flow fractions / F_tot / temperature (those come from the recipe file).
        Keys: arm_mode, arm_wait_s, run_duration, flush_rate, flush_duration.
        Blank/empty clears back to the config default. A changed run_duration
        updates the current run's deadline live."""
        def num(v):
            if v in (None, ""):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        with self._lock:
            if "arm_mode" in d:
                m = str(d.get("arm_mode") or "").lower()
                self.live_arm_mode = m if m in ("temperature", "timed", "ramp") else None
            if "arm_wait_s" in d:
                self.live_arm_wait = num(d.get("arm_wait_s"))
            if "arm_ramp_rate" in d:
                self.live_arm_ramp = num(d.get("arm_ramp_rate"))
            if "flush_rate" in d:
                self.live_flush_rate = num(d.get("flush_rate"))
            if "flush_duration" in d:
                self.live_flush_duration = num(d.get("flush_duration"))
            if "run_duration" in d:
                self.live_duration = num(d.get("run_duration"))
                if self.state == "running" and self._run_started and self.live_duration:
                    self._run_deadline = self._run_started + self.live_duration
                    self._log(f"⏱ run duration → {self.live_duration:g}s (applies to current run)", "info")

    def set_spec_settings(self, d: dict) -> None:
        """Live SPEC data-collection settings from the app: exposure_s, frames,
        spec_lead_s, sample_tag, bkg_tag. Blank/missing keys are left unchanged.
        Values apply to the NEXT acquisition (they're read when a collect fires)."""
        def _tag(v):
            # keep filename-safe tokens only (letters/digits/_-)
            return "".join(c for c in str(v).strip() if c.isalnum() or c in "_-")
        with self._lock:
            if str(d.get("exposure_s", "")).strip():
                try: self._spec_exposure = float(d["exposure_s"])
                except (TypeError, ValueError): pass
            if str(d.get("frames", "")).strip():
                try: self._spec_frames = max(1, int(float(d["frames"])))
                except (TypeError, ValueError): pass
            if str(d.get("spec_lead_s", "")).strip():
                try: self._spec_lead = float(d["spec_lead_s"])
                except (TypeError, ValueError): pass
            if _tag(d.get("sample_tag", "")):
                self._spec_sample_tag = _tag(d["sample_tag"])
            if _tag(d.get("bkg_tag", "")):
                self._spec_bkg_tag = _tag(d["bkg_tag"])
            self._log(f"⚙ data-collection: exp {self._spec_exposure:g}s ×{self._spec_frames}, "
                      f"lead {self._spec_lead:g}s, tags {self._spec_sample_tag}/{self._spec_bkg_tag}", "info")

    # ── run-end triggers ───────────────────────────────────────────────────────
    def signal_measurement_complete(self, info: str = "") -> None:
        with self._lock:
            if self.state == "running":
                self._measure_done = True
                self._run_reason = f"SAXS measurement complete{(' — ' + info) if info else ''}"
                self._log(f"📈 measurement signal received — ending run", "ok")

    # ── abort / emergency ──────────────────────────────────────────────────────
    def abort(self) -> None:
        with self._lock:
            if self.state in ("arming", "running"):
                self._log("⛔ abort — stopping reagents, going to flush", "warn")
                self._run_reason = "aborted"
                self._end_run(flush=True)
            elif self.state == "flushing":
                self._log("⛔ abort during flush — idling all", "warn")
                self._to_idle()

    def estop(self) -> None:
        with self._lock:
            # Record the E-stop FIRST: even if a serial write below throws, the
            # system must not be left without the estop state (and _safety_check
            # must see it to stop re-entering).
            self.state = "estop"
            self.current = None
            failed = self.pumps.idle_all()   # guarded per-pump; never blocks on one
            # PUMPS ONLY: deliberately send NOTHING to the beamline/SPEC here, so
            # an in-progress X-ray collection finishes on its own and SPEC is not
            # disturbed. Temperature is left exactly as-is.
            if failed:
                self._log(f"🛑 EMERGENCY STOP — but could NOT idle: {', '.join(failed)} "
                          f"— CHECK THESE PUMPS/PORTS IMMEDIATELY", "error")
            else:
                self._log("🛑 EMERGENCY STOP — all pumps idle", "error")
            self._event("reactor.estop", {"failed_to_idle": failed})

    def reset(self) -> None:
        with self._lock:
            if self.state in ("estop", "ready"):
                self.pumps.idle_all()
                self.state = "idle"
                self._log("↺ reset to idle", "info")

    def switch_backend(self, backend: str) -> tuple[bool, str]:
        """Switch the hardware backend ('mock'|'real') live. Only allowed when
        idle/ready/estop — never mid-run. Builds the new hardware FIRST and only
        swaps it in if that succeeds, so a failed real-pump connection leaves the
        current (working) backend untouched. Session-only: not persisted."""
        backend = str(backend).lower()
        if backend not in ("mock", "real"):
            return False, f"unknown backend {backend!r}"
        with self._lock:
            if backend == self.backend:
                return True, f"already {backend}"
            if self.state not in ("idle", "ready", "estop"):
                return False, f"can't switch backend while {self.state} — stop the run first"
            try:
                new_pumps = PumpBank(self.cfg, backend=backend)   # opens ports for 'real'
            except Exception as exc:
                self._log(f"⚠ backend stays {self.backend.upper()} — could not start "
                          f"{backend.upper()}: {exc}", "error")
                return False, str(exc)
            # release the old backend's pumps, then swap in the new one
            try:
                self.pumps.idle_all()
            except Exception:
                pass
            for p in self.pumps.pumps.values():
                try:
                    p.close()
                except Exception:
                    pass
            self.pumps = new_pumps
            # switch the beamline to the same mode (one toggle covers pumps + SPEC)
            try:
                self.beamline.close()
            except Exception:
                pass
            self.beamline = make_beamline(_spec_cfg_for(self.cfg, backend))
            self.cfg.setdefault("spec", {})["backend"] = backend
            self.temp = TempController(self.cfg, backend=backend, beamline=self.beamline)
            self.backend = backend
            self.state = "idle"
            self.current = None
            self.setpoints = {}
            self._log(f"⚙ backend switched to {backend.upper()} — pumps + beamline"
                      + (" are LIVE" if backend == "real" else " (simulation)"),
                      "warn" if backend == "real" else "info")
            self._event("reactor.backend", {"backend": backend})
            return True, backend

    def vent_all(self) -> None:
        """Vent every pump so chamber pressure returns to 0, from ANY state and
        in either mode (manual or autonomous). Does NOT stop the autonomous loop
        — auto-run is left as-is, so the next condition file will run normally.
        The queue is kept."""
        with self._lock:
            failed = self.pumps.idle_all()   # P0 to every pump → chamber → 0
            self.temp.set_temperature(0.0)
            self.current = None
            self.setpoints = {}
            self.state = "idle"
            if failed:
                self._log(f"⚠ vent: could not idle {', '.join(failed)} — check these pumps", "warn")
            self._log("🟦 vented all pumps — chamber pressure reset to 0", "info")
            self._event("reactor.vent", {})

    # ── flush ─────────────────────────────────────────────────────────────────
    def flush_now(self, rate: float | None = None, duration: float | None = None,
                  kind: str = "flush") -> bool:
        with self._lock:
            if self.state in ("idle", "ready"):
                self._enter_flush(rate, duration, kind=kind)
                return True
        return False

    # ── internal transitions (call with lock held) ─────────────────────────────
    def _begin_next(self) -> None:
        if not self.queue:
            self._to_idle()
            return
        recipe, setpoints = self.queue.popleft()
        self.current = recipe
        self.setpoints = setpoints
        self._measure_done = False
        self._run_reason = ""
        self.temp.set_temperature(recipe.T_reac)   # recorded for display / gating
        self._arm_mode = (recipe.arm_mode or self.live_arm_mode or self.default_arm_mode).lower()
        now = time.time()
        self.state = "arming"
        if self._arm_mode == "ramp":
            rate = (recipe.arm_ramp_rate if recipe.arm_ramp_rate is not None
                    else self.live_arm_ramp if self.live_arm_ramp is not None
                    else self.default_ramp_rate)
            wait = ramp_wait_seconds(recipe.T_reac, float(rate or 0.0), self.ramp_start_temp)
            self._arm_total = float(wait)
            self._arm_ready_at = now + float(wait)
            self._arm_deadline = 0.0    # no temperature timeout in ramp mode
            if rate and rate > 0:
                self._log(f"📈 arming {recipe.recipe_id}: ramp {self.ramp_start_temp:g}→"
                          f"{recipe.T_reac:g}°C at {float(rate):g}°C/min → wait "
                          f"{float(wait):g}s before pumps start", "info")
            else:
                self._log(f"⚠ arming {recipe.recipe_id}: no positive ramp rate set — "
                          f"pumps start immediately", "warn")
        elif self._arm_mode == "timed":
            wait = (recipe.arm_wait_s if recipe.arm_wait_s is not None
                    else self.live_arm_wait if self.live_arm_wait is not None
                    else self.default_arm_wait)
            self._arm_total = float(wait)
            self._arm_ready_at = now + float(wait)
            self._arm_deadline = 0.0    # no temperature timeout in timed mode
            self._log(f"⏲ arming {recipe.recipe_id}: timed wait {float(wait):g}s "
                      f"before pumps start (temperature gate off)", "info")
        else:
            self._arm_total = 0.0
            self._arm_ready_at = 0.0
            self._arm_deadline = now + self.temp.timeout
            self._log(f"🌡 arming {recipe.recipe_id}: waiting for {recipe.T_reac:g}°C "
                      f"(±{self.temp.tolerance:g})", "info")

    def _enter_running(self) -> None:
        self.pumps.set_all(self.setpoints)
        self._meas_sum = {name: 0.0 for name in self.pumps.pumps}
        self._meas_n = 0
        self._meas_series = []
        self._run_started = time.time()
        self._meas_last_sample = self._run_started
        self._spec_fired = False
        dur = (self.current.run_duration or self.live_duration or self.default_duration)
        self._run_deadline = self._run_started + float(dur)
        self.state = "running"
        sp = ", ".join(f"{k}={v:g}" for k, v in self.setpoints.items() if v)
        self._log(f"▶ running {self.current.recipe_id}: {sp} µL/min", "ok")
        self._event("reactor.run_start",
                    {"recipe_id": self.current.recipe_id,
                     "setpoints": self.setpoints, "T_reac": self.current.T_reac})

    def _end_run(self, flush: bool = True) -> None:
        rec = self.current
        ended = time.time()
        reason = self._run_reason or "ended"
        # stop reagents immediately (guarded per-pump so one failure can't leave
        # the others flowing)
        failed = self.pumps.zero_pumps(REAGENT_PUMPS)
        if failed:
            self._log(f"⚠ could not zero reagent pump(s): {', '.join(failed)} — check them", "warn")
        # mean measured flow per pump over the run (from the flow sensors)
        measured = ({nm: round(self._meas_sum.get(nm, 0.0) / self._meas_n, 4)
                     for nm in self._meas_sum} if self._meas_n
                    else {nm: round(getattr(p, "actual", 0.0), 4)
                          for nm, p in self.pumps.pumps.items()})
        record = {
            "recipe_id": rec.recipe_id if rec else None,
            "recipe": rec.to_dict() if rec else None,
            "setpoints": self.setpoints,
            "measured_flows": measured,
            "started": self._run_started, "ended": ended,
            "duration_s": round(ended - self._run_started, 1) if self._run_started else None,
            "reason": reason, "status": "ran",
        }
        self.history.append(record)
        self._log(f"⏹ run {record['recipe_id']} ended ({reason})", "ok")
        try:
            self._manifest(record)
            # the full delivered-flow trace goes ONLY in the done file (kept out
            # of manifest.json / events to avoid bloat); appended at the bottom.
            feedback_payload = {
                **record,
                "flow_series_note": (f"delivered flow (µL/min) per pump, sampled every "
                                     f"{self.meas_sample_s:g}s over the synthesis run"),
                "flow_series": self._meas_series,
            }
            self._feedback(record["recipe_id"], feedback_payload)
            self._event("reactor.run_complete", record)
        except Exception as exc:
            self._log(f"⚠ feedback/manifest error: {exc}", "warn")
        if flush:
            self._enter_flush(kind="flush")
        else:
            self._to_idle()

    def _enter_flush(self, rate: float | None = None, duration: float | None = None,
                     kind: str = "flush") -> None:
        r = float(rate if rate is not None else
                  (self.current.flush_rate if self.current and self.current.flush_rate
                   else self.live_flush_rate if self.live_flush_rate is not None
                   else self.flush_rate))
        d = float(duration if duration is not None else
                  (self.current.flush_duration if self.current and self.current.flush_duration
                   else self.live_flush_duration if self.live_flush_duration is not None
                   else self.flush_duration))
        failed = self.pumps.zero_pumps(REAGENT_PUMPS)   # zero the 4 reagent pumps first
        if failed:
            self._log(f"⚠ could not zero reagent pump(s): {', '.join(failed)} — check them", "warn")
        self.pumps.set_pump_flow(FLUSH_PUMP, r)
        self.state = "flushing"
        self._flush_kind = kind
        self._flush_deadline = time.time() + d
        self._bkg_fired = False        # arm the background acquisition for this flush
        self._log(f"🧼 {kind}: ode_flush {r:g} µL/min for {d:g}s "
                  f"(new recipes blocked)", "info")

    def _end_flush(self) -> None:
        self.pumps.set_pump_flow(FLUSH_PUMP, 0.0)
        self._log(f"✓ {self._flush_kind} complete", "ok")
        if self.current is not None:
            self._event("reactor.ready", {"recipe_id": self.current.recipe_id})
        # advance to the next queued recipe, or idle/vent the pumps and wait
        if self.queue:
            self._begin_next()
        else:
            self.pumps.idle_all()   # vent all pumps (P0) — not just hold flow 0
            self.state = "ready"
            self.current = None
            self.setpoints = {}
            self._log("💤 no more conditions — pumps idled, waiting for next", "info")

    def _to_idle(self) -> None:
        failed = self.pumps.idle_all()
        if failed:
            self._log(f"⚠ could not idle {', '.join(failed)} — check these pumps", "warn")
        self.state = "idle"
        self.current = None
        self.setpoints = {}

    def _fire_spec_collection(self, recipe_id: str, role: str) -> None:
        """Trigger a SPEC 2D acquisition. ``role`` is 'sample' (during the run) or
        'background' (during the flush). The filename is
        ``{recipe_id}_{tag}`` so averaging separates the two and background
        subtraction pairs them by the shared recipe_id. Runs in its own thread —
        blocking SPEC I/O must not stall the control loop."""
        try:
            tag = self._spec_sample_tag if role == "sample" else self._spec_bkg_tag
            prefix = f"{recipe_id}_{tag}"
            path = (f"{self._spec_data_dir.rstrip('/')}/{prefix}"
                    if self._spec_data_dir else prefix)
            self._log(f"📷 SPEC {role} collect → {path} "
                      f"(exp {self._spec_exposure:g}s ×{self._spec_frames})", "ok")
            self._last_collect = {"role": role, "recipe_id": recipe_id, "path": path,
                                  "t": time.time()}
            self.beamline.collect(recipe_id=recipe_id, role=role, path=path,
                                  temperature=self.temp.target,
                                  exposure=self._spec_exposure, frames=self._spec_frames)
            self._log(f"📷 SPEC {role} collect complete: {recipe_id}", "ok")
            self._event("reactor.spec_collect",
                        {"recipe_id": recipe_id, "role": role, "path": path})
        except Exception as exc:
            self._log(f"⚠ SPEC {role} collect failed ({recipe_id}): {exc}", "error")

    # ── background loop ─────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while self._alive:
            now = time.time()
            dt = now - self._last
            self._last = now
            # Poll hardware OUTSIDE the controller lock: pumps.tick() does
            # blocking serial I/O and must not delay an operator estop()/abort()/
            # stop() that is waiting on the lock. The driver serializes per-pump
            # serial access with its own lock, so a concurrent set_flow/idle is
            # safe; the loop thread is the only writer of the cached readings.
            self.pumps.tick(dt)
            self.temp.tick(dt)
            with self._lock:
                self._safety_check()
                if self.state == "arming":
                    if self._arm_mode in ("timed", "ramp"):
                        # start the pumps once the computed wait elapses; no
                        # temperature gating and no arm timeout in these modes.
                        if now >= self._arm_ready_at:
                            self._enter_running()
                    elif self.temp.is_stable():
                        self._enter_running()
                    elif now > self._arm_deadline:
                        self._log(f"⚠ arm timeout — {self.current.recipe_id if self.current else '?'} "
                                  f"never reached temperature; aborting", "error")
                        self._run_reason = "arm timeout"
                        self._to_idle()
                elif self.state == "running":
                    for _nm, _p in self.pumps.pumps.items():
                        self._meas_sum[_nm] = self._meas_sum.get(_nm, 0.0) + getattr(_p, "actual", 0.0)
                    self._meas_n += 1
                    # sample the delivered flow trace (saved to the done file)
                    if now - self._meas_last_sample >= self.meas_sample_s:
                        self._meas_last_sample = now
                        self._meas_series.append({
                            "t_s": round(now - self._run_started, 1),
                            "flows": {nm: round(getattr(p, "actual", 0.0), 4)
                                      for nm, p in self.pumps.pumps.items()},
                        })
                    # fire the SPEC 2D collection once, ~lead seconds before the run ends
                    if (self._spec_enabled and not self._spec_fired
                            and now >= self._run_deadline - self._spec_lead):
                        self._spec_fired = True
                        _rid = self.current.recipe_id if self.current else "run"
                        threading.Thread(target=self._fire_spec_collection,
                                         args=(_rid, "sample"), daemon=True).start()
                    if self._measure_done:
                        self._end_run(flush=True)
                    elif now > self._run_deadline:
                        # synthesis duration reached — applies to manual AND auto
                        self._run_reason = self._run_reason or "duration elapsed"
                        self._end_run(flush=True)
                    elif self.advance_on_new and self.queue and (now - self._run_started) >= self.min_dwell:
                        # a newer condition is queued — advance early (before duration)
                        self._run_reason = "next condition available"
                        self._end_run(flush=True)
                elif self.state == "flushing":
                    # fire the BACKGROUND 2D collection once, ~lead seconds before the
                    # flush ends (pure solvent in the capillary) — only for a real
                    # post-synthesis flush that has a recipe to tag it with
                    if (self._spec_enabled and not self._bkg_fired
                            and self._flush_kind == "flush" and self.current is not None
                            and now >= self._flush_deadline - self._spec_lead):
                        self._bkg_fired = True
                        threading.Thread(target=self._fire_spec_collection,
                                         args=(self.current.recipe_id, "background"),
                                         daemon=True).start()
                    if now > self._flush_deadline:
                        self._end_flush()
            time.sleep(0.2)

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
                self._log(f"🛑 SAFETY: pump(s) {', '.join(faulted)} in ERROR/lost state "
                          f"(check air supply / blockage / flow sensor / connection) — emergency stop",
                          "error")
                self.estop()
                return
        if self.temp.current > self.T_max + 0.5:
            self._log(f"🛑 SAFETY: temperature {self.temp.current:.0f}°C > T_max", "error")
            self.estop()
            return
        for name, p in self.pumps.pumps.items():
            if p.target > self.per_pump_max + 1e-6:
                self._log(f"🛑 SAFETY: {name} target {p.target:.0f} > per_pump_max", "error")
                self.estop()
                return
            # pump pressure must never exceed the pump's pressure ceiling
            pmax = getattr(p, "max_pressure", 0.0)
            if pmax and getattr(p, "pressure", 0.0) > pmax + 1e-6:
                self._log(f"🛑 SAFETY: {name} pressure {p.pressure:.0f} mbar "
                          f"> max {pmax:.0f} mbar — emergency stop", "error")
                self.estop()
                return

    # ── status ──────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            now = time.time()
            elapsed = round(now - self._run_started, 1) if self.state == "running" else None
            eff = self.live_duration or self.default_duration
            dur = ((self.current.run_duration or eff) if self.current else None)
            flush_left = round(self._flush_deadline - now, 1) if self.state == "flushing" else None
            _timed_arm = self.state == "arming" and self._arm_mode in ("timed", "ramp")
            arm_left = round(max(0.0, self._arm_ready_at - now), 1) if _timed_arm else None
            arm_total = round(self._arm_total, 1) if _timed_arm else None
            return {
                "state": self.state,
                "backend": self.backend,
                "auto_run": self.auto_run,
                "arm_mode": self._arm_mode if self.state == "arming" else None,
                "arm_remaining_s": arm_left,
                "arm_total_s": arm_total,
                "pumps": self.pumps.state(),
                "temperature": {"target": round(self.temp.target, 1),
                                "current": round(self.temp.current, 1),
                                "stable": self.temp.is_stable(),
                                "tolerance": self.temp.tolerance,
                                "bstop": self.temp.bstop,
                                "i0": self.temp.i0},
                "last_collect": self._last_collect,
                "spec": {"enabled": self._spec_enabled, "exposure_s": self._spec_exposure,
                         "frames": self._spec_frames, "spec_lead_s": self._spec_lead,
                         "sample_tag": self._spec_sample_tag, "bkg_tag": self._spec_bkg_tag,
                         "data_dir": self._spec_data_dir,
                         "collecting": self.beamline.is_collecting()},
                "current_recipe": self.current.to_dict() if self.current else None,
                "elapsed_s": elapsed, "duration_s": dur,
                "run_duration_setting": self.live_duration or self.default_duration,
                "flush_remaining_s": flush_left,
                "queue": [r.recipe_id for r, _ in self.queue],
                "queue_len": len(self.queue),
                "runs_completed": len(self.history),
            }

    def shutdown(self) -> None:
        self._alive = False
