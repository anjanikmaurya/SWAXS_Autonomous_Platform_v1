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

STATES = ["idle", "arming", "running", "flushing", "ready", "estop"]


def _noop(*a, **k):
    return None


class ReactorController:
    def __init__(self, cfg: dict, backend: str = "mock", *,
                 log_cb=None, event_cb=None, feedback_cb=None, manifest_cb=None):
        self.cfg = cfg
        self.pumps = PumpBank(cfg, backend=backend)
        self.temp = TempController(cfg, backend=backend)
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
        self._arm_ready_at = 0.0         # when timed-arming completes
        self._flush_deadline = 0.0
        self._flush_kind = "flush"
        self._measure_done = False
        self._stop_flag = False
        self._run_reason = ""

        run = cfg.get("run", {})
        self.default_duration = float(run.get("default_duration", 600.0))
        self.end_on_measurement = bool(run.get("end_on_measurement", True))
        arm = cfg.get("arming", {})
        self.default_arm_mode = str(arm.get("default_mode", "temperature")).lower()
        self.default_arm_wait = float(arm.get("default_wait_s", 120.0))
        fl = cfg.get("flush", {})
        self.flush_rate = float(fl.get("rate", 100.0))
        self.flush_duration = float(fl.get("duration", 300.0))
        s = cfg.get("safety", {})
        self.T_max = float(s.get("T_max", 320.0))
        self.per_pump_max = float(s.get("per_pump_max", 1000.0))

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

    # ── run-end triggers ───────────────────────────────────────────────────────
    def signal_measurement_complete(self, info: str = "") -> None:
        with self._lock:
            if self.state == "running":
                self._measure_done = True
                self._run_reason = f"SAXS measurement complete{(' — ' + info) if info else ''}"
                self._log(f"📈 measurement signal received — ending run", "ok")

    def stop(self) -> bool:
        """Operator manual stop of the running synthesis (→ flush)."""
        with self._lock:
            if self.state == "running":
                self._stop_flag = True
                self._run_reason = "manual stop"
                return True
        return False

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
            self.pumps.idle_all()
            self.temp.set_temperature(0.0)
            self.state = "estop"
            self.current = None
            self._log("🛑 EMERGENCY STOP — all pumps idle", "error")
            self._event("reactor.estop", {})

    def reset(self) -> None:
        with self._lock:
            if self.state in ("estop", "ready"):
                self.pumps.idle_all()
                self.state = "idle"
                self._log("↺ reset to idle", "info")

    # ── flush / prime ───────────────────────────────────────────────────────────
    def flush_now(self, rate: float | None = None, duration: float | None = None,
                  kind: str = "flush") -> bool:
        with self._lock:
            if self.state in ("idle", "ready"):
                self._enter_flush(rate, duration, kind=kind)
                return True
        return False

    def prime(self, rate: float | None = None, duration: float | None = None) -> bool:
        return self.flush_now(rate, duration, kind="prime")

    # ── internal transitions (call with lock held) ─────────────────────────────
    def _begin_next(self) -> None:
        if not self.queue:
            self._to_idle()
            return
        recipe, setpoints = self.queue.popleft()
        self.current = recipe
        self.setpoints = setpoints
        self._measure_done = False
        self._stop_flag = False
        self._run_reason = ""
        self.temp.set_temperature(recipe.T_reac)   # recorded for display / gating
        self._arm_mode = (recipe.arm_mode or self.default_arm_mode).lower()
        now = time.time()
        self.state = "arming"
        if self._arm_mode == "timed":
            wait = (recipe.arm_wait_s if recipe.arm_wait_s is not None
                    else self.default_arm_wait)
            self._arm_ready_at = now + float(wait)
            self._arm_deadline = 0.0    # no temperature timeout in timed mode
            self._log(f"⏲ arming {recipe.recipe_id}: timed wait {float(wait):g}s "
                      f"before pumps start (temperature gate off)", "info")
        else:
            self._arm_ready_at = 0.0
            self._arm_deadline = now + self.temp.timeout
            self._log(f"🌡 arming {recipe.recipe_id}: waiting for {recipe.T_reac:g}°C "
                      f"(±{self.temp.tolerance:g})", "info")

    def _enter_running(self) -> None:
        self.pumps.set_all(self.setpoints)
        self._run_started = time.time()
        dur = self.current.run_duration or self.default_duration
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
        # stop reagents immediately
        for p in REAGENT_PUMPS:
            self.pumps.set_pump_flow(p, 0.0)
        record = {
            "recipe_id": rec.recipe_id if rec else None,
            "recipe": rec.to_dict() if rec else None,
            "setpoints": self.setpoints,
            "started": self._run_started, "ended": ended,
            "duration_s": round(ended - self._run_started, 1) if self._run_started else None,
            "reason": reason, "status": "ran",
        }
        self.history.append(record)
        self._log(f"⏹ run {record['recipe_id']} ended ({reason})", "ok")
        try:
            self._manifest(record)
            self._feedback(record["recipe_id"], record)
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
                   else self.flush_rate))
        d = float(duration if duration is not None else
                  (self.current.flush_duration if self.current and self.current.flush_duration
                   else self.flush_duration))
        for p in REAGENT_PUMPS:                 # zero the 4 reagent pumps first
            self.pumps.set_pump_flow(p, 0.0)
        self.pumps.set_pump_flow(FLUSH_PUMP, r)
        self.state = "flushing"
        self._flush_kind = kind
        self._flush_deadline = time.time() + d
        self._log(f"🧼 {kind}: ode_flush {r:g} µL/min for {d:g}s "
                  f"(new recipes blocked)", "info")

    def _end_flush(self) -> None:
        self.pumps.set_pump_flow(FLUSH_PUMP, 0.0)
        self._log(f"✓ {self._flush_kind} complete", "ok")
        if self.current is not None:
            self._event("reactor.ready", {"recipe_id": self.current.recipe_id})
        # always auto-advance to the next queued recipe (per spec)
        if self.queue:
            self._begin_next()
        else:
            self.state = "ready"
            self.current = None
            self.setpoints = {}

    def _to_idle(self) -> None:
        self.pumps.idle_all()
        self.state = "idle"
        self.current = None
        self.setpoints = {}

    # ── background loop ─────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while self._alive:
            now = time.time()
            dt = now - self._last
            self._last = now
            with self._lock:
                self.pumps.tick(dt)
                self.temp.tick(dt)
                self._safety_check()
                if self.state == "arming":
                    if self._arm_mode == "timed":
                        # start the pumps once the fixed wait elapses; no
                        # temperature gating and no arm timeout in this mode.
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
                    if self._measure_done or self._stop_flag:
                        self._end_run(flush=True)
                    elif now > self._run_deadline:
                        self._run_reason = self._run_reason or "duration elapsed (fallback)"
                        self._end_run(flush=True)
                elif self.state == "flushing":
                    if now > self._flush_deadline:
                        self._end_flush()
            time.sleep(0.2)

    def _safety_check(self) -> None:
        if self.state == "estop":
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
            dur = (self.current.run_duration or self.default_duration) if self.current else None
            flush_left = round(self._flush_deadline - now, 1) if self.state == "flushing" else None
            arm_left = (round(max(0.0, self._arm_ready_at - now), 1)
                        if self.state == "arming" and self._arm_mode == "timed" else None)
            return {
                "state": self.state,
                "auto_run": self.auto_run,
                "arm_mode": self._arm_mode if self.state == "arming" else None,
                "arm_remaining_s": arm_left,
                "pumps": self.pumps.state(),
                "temperature": {"target": round(self.temp.target, 1),
                                "current": round(self.temp.current, 1),
                                "stable": self.temp.is_stable(),
                                "tolerance": self.temp.tolerance},
                "current_recipe": self.current.to_dict() if self.current else None,
                "elapsed_s": elapsed, "duration_s": dur,
                "flush_remaining_s": flush_left,
                "queue": [r.recipe_id for r, _ in self.queue],
                "queue_len": len(self.queue),
                "runs_completed": len(self.history),
            }

    def shutdown(self) -> None:
        self._alive = False
