"""
src/reactor/sequence.py — part of ReactorController (split out October 2026).

The run order: begin the next condition, blank flush, arm, run, end, flush, idle.

The methods below were moved VERBATIM from controller.py; ReactorController
inherits SequenceMixin, so every call and every self.* attribute is unchanged.
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




class SequenceMixin:
    # ── internal transitions (call with lock held) ─────────────────────────────
    def _release_pending(self, why: str) -> None:
        """Return a recipe staged behind a blank flush to the front of the queue.

        ``_pending`` holds the recipe whose pre-synthesis blank is being
        collected. Only ``_end_flush`` used to clear it, so ANY other way out of
        that flush — Stop, E-stop, Vent, a to-idle — stranded the recipe there
        forever, and the damage was in two parts:

          1. the staged condition was silently lost (no run, no record, no
             feedback file, so the optimizer waited for a `.done.json` that was
             never coming);
          2. far worse, ``_begin_next`` only stages a blank when ``_pending is
             None``, so with a stranded value EVERY LATER CONDITION RAN WITH NO
             BACKGROUND. The run log looked perfectly normal throughout and the
             damage only surfaced in the subtraction app, conditions later.

        Nothing cleared it — not reset, not vent, not estop, not clear_queue.
        Only restarting the app recovered. Audit finding R2.

        Caller must hold _lock.
        """
        if self._pending is None:
            return
        recipe, setpoints = self._pending
        self._pending = None
        self._bkg_recipe_id = ""
        self.queue.appendleft((recipe, setpoints))
        self._log(f"↩ {recipe.recipe_id} was staged behind its blank when {why} — "
                  f"put back at the front of the queue (it has not run)", "warn")

    def _begin_next(self) -> None:
        # Defence in depth for R2, and BEFORE the empty-queue check so a
        # stranded recipe is recovered even when nothing else is waiting. If
        # _pending is still set here, a blank would be skipped for this
        # condition AND every one after it. Say so loudly and recover, rather
        # than running the whole campaign without backgrounds in silence.
        if self._pending is not None:
            self._log("⚠ a recipe was still staged behind a blank that never "
                      "completed — recovering it before starting the next "
                      "condition (this would otherwise have skipped every "
                      "background for the rest of the session)", "warn")
            self._release_pending("the blank did not complete")

        if not self.queue:
            self._to_idle()
            return

        recipe, setpoints = self.queue.popleft()

        # ── background BEFORE synthesis (background_when: "before") ───────────
        # Flush the line, measure the blank on the clean capillary, THEN run the
        # synthesis. The blank is tagged with the UPCOMING recipe_id, so the
        # pairing is unambiguous and the background is already on disk when the
        # sample frames land — subtraction can start immediately.
        if (self.background_when == "before" and self._spec_enabled
                and self._pending is None):
            self._pending = (recipe, setpoints)
            dur = self._blank_flush_duration()
            if dur is not None:
                self._log(f"🧪 line already clean — short blank rinse ({dur:g}s) + "
                          f"background for {recipe.recipe_id}, then the synthesis "
                          f"(no redundant full flush)", "info")
            else:
                self._log(f"🧪 blank first for {recipe.recipe_id} — flushing, then a "
                          f"background collection, then the synthesis", "info")
            self._enter_flush(kind="blank", duration=dur,
                              bkg_recipe_id=recipe.recipe_id)
            return

        self._start_recipe(recipe, setpoints)

    def _start_recipe(self, recipe, setpoints) -> None:
        """Arm and run a recipe (the part of _begin_next after any blank flush)."""
        self.current = recipe
        self.setpoints = setpoints
        self._measure_done = False
        self._run_reason = ""
        # Clear the PREVIOUS run's measurements now, not in _enter_running — an
        # abort during arming would otherwise report the last run's flows.
        self._run_started = 0.0
        self._meas_sum = {}
        self._meas_n = 0
        self._meas_series = []
        # Hand the recipe to the beamline backend. No-op on real SPEC; in mock
        # mode this is what makes the SIMULATED 2D data reflect this recipe, so
        # the optimizer sees a real landscape instead of constant results.
        try:
            self.beamline.set_recipe(recipe)
            if self._spec_data_dir:
                # The PROJECT ROOT (where config.yml with poni_files lives) —
                # NOT the 2D data folder. Passing data_dir here meant the
                # simulator never found the project config and silently fell
                # back to synthetic geometry.
                self.beamline.set_project_root(self._project_root or self._spec_data_dir)
        except Exception:
            pass
        self.temp.set_temperature(recipe.T_reac)   # recorded for display / gating
        # Precedence: the APP (live_*) value wins once the user has set it, then any
        # recipe-embedded value, then the config default. So whatever is in the app
        # UI is used for every run/repeat/autonomous run until the app is restarted.
        self._arm_mode = (self.live_arm_mode or recipe.arm_mode or self.default_arm_mode).lower()
        now = time.time()
        self.state = "arming"
        if self._arm_mode == "timed":
            wait = (self.live_arm_wait if self.live_arm_wait is not None
                    else recipe.arm_wait_s if recipe.arm_wait_s is not None
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
            # Say NOW, not in 900 s, that this cannot succeed (audit R13).
            # Temperature arming gates on a reading. If there is no live source
            # the gate can never open, so the condition waits out the full
            # arming timeout and is then abandoned — once per condition, all
            # night, with the reason only visible at the very end of each wait.
            if not self.temp.trustworthy:
                why = {"unwired": "no temperature source is wired — `current` is "
                                  "the ambient default and never changes",
                       "mock": "this is the simulated ramp of Simulation mode, "
                               "not a measurement"}.get(self.temp.source,
                                                  "the reading is stale")
                self._log(f"⚠ arm_mode is 'temperature' but {why}. This run will "
                          f"wait the full {self.temp.timeout:g}s and then be "
                          f"abandoned. Switch the Timing card to a fixed wait, or "
                          f"fix the temperature source, before leaving it "
                          f"running.", "warn")

    def _enter_running(self) -> None:
        self.pumps.reset_volumes()          # start counting delivered volume for this run
        self._line_clean = False            # reagents about to flow — line is now dirty
        self._flow_faulted_prev = set()
        failed_set = self.pumps.set_all(self.setpoints)
        if failed_set:
            # Some pumps are flowing and others refused the command — the mixture
            # is wrong and unsupervised. Stop rather than run a bad composition.
            self._log(f"🛑 could not command pump(s) {', '.join(failed_set)} — "
                      f"emergency stop (the remaining pumps would deliver the "
                      f"wrong composition)", "error")
            self.estop()
            return
        self._meas_sum = {name: 0.0 for name in self.pumps.pumps}
        self._meas_n = 0
        self._meas_series = []
        self._run_started = time.time()
        self._meas_last_sample = self._run_started
        self._spec_fired = False
        dur = (self.live_duration or self.current.run_duration or self.default_duration)
        if float(dur) <= 0:      # see the R8 note on set_run_settings
            self._log(f"⚠ run duration {float(dur):g}s is not usable — falling back "
                      f"to the configured {self.default_duration:g}s", "warn")
            dur = self.default_duration
        self._run_deadline = self._run_started + float(dur)
        self.state = "running"
        sp = ", ".join(f"{k}={v:g}" for k, v in self.setpoints.items() if v)
        self._log(f"▶ RUN START {self.current.recipe_id} — {sp} µL/min "
                  f"@ {self.current.T_reac:g}°C for {float(dur):g}s", "ok")

        # Spell out what will END the run, so an unexpectedly short/long run can
        # be traced to the trigger that actually fired.
        enders = [f"duration {float(dur):g}s", "measurement-complete signal"]
        if self.advance_on_new:
            enders.append(f"next queued condition (after {self.min_dwell:g}s dwell)")
        self._log(f"   ends on: first of — {'; '.join(enders)}", "info")

        # And when the 2D collection is due. Silence here was the main reason a
        # missing acquisition was hard to diagnose.
        if not self._spec_enabled:
            self._log("   📷 2D collection DISABLED (spec.enabled = false) — "
                      "no data will be written this run", "warn")
        elif not self._spec_data_dir:
            self._log("   ⚠ 2D collection enabled but SPEC data_dir is UNSET — "
                      "set the Save folder or it will fail", "warn")
        elif self.backend != "real" and getattr(self.beamline, "simulator", None) is None:
            self._log("   ⚠ Simulation mode with the 2D simulator OFF: collections "
                      "will be logged but NO files written. Set "
                      "spec.simulator.enabled: true to generate synthetic data.",
                      "warn")
        else:
            if self.backend != "real":
                self._check_mock_dir_writable()
            fire_in = float(dur) - self._spec_lead
            if fire_in <= 0:
                self._log(f"   📷 2D collection fires IMMEDIATELY — lead "
                          f"{self._spec_lead:g}s ≥ run duration {float(dur):g}s "
                          f"(reduce spec_lead_s to collect later in the run)", "warn")
            else:
                self._log(f"   📷 2D collection due at T+{fire_in:g}s "
                          f"({self._spec_lead:g}s before the end) → "
                          f"{self._spec_data_dir}", "info")
        self._event("reactor.run_start",
                    {"recipe_id": self.current.recipe_id,
                     "setpoints": self.setpoints, "T_reac": self.current.T_reac,
                     # full recipe + planned duration so notifiers can report the
                     # conditions without reaching back into the controller
                     "recipe": self.current.to_dict(),
                     "duration_s": float(dur), "backend": self.backend})

    def _end_run(self, flush: bool = True) -> None:
        rec = self.current
        ended = time.time()
        reason = self._run_reason or "ended"
        # A run that never left `arming` produced no flow and no measurement.
        # Emitting a record here used to copy the PREVIOUS run's measured_flows
        # and flow_series into a <recipe_id>.done.json marked status="ran" — the
        # optimizer would then train on a synthesis that never happened.
        never_ran = not self._run_started
        if never_ran:
            self._log(f"⏹ {rec.recipe_id if rec else '?'} ended before the pumps "
                      f"started ({reason}) — no run record written", "info")
            self.current = None
            self.setpoints = {}
            if flush:
                self._enter_flush()
            else:
                self._to_idle()
            return
        # stop reagents immediately (guarded per-pump so one failure can't leave
        # the others flowing)
        failed = self.pumps.zero_pumps(REAGENT_PUMPS)
        if failed:
            self._log(f"⚠ could not zero reagent pump(s): {', '.join(failed)} — check them", "warn")
        # synthesis is over: cool the reactor to room temperature (if configured)
        if self.cooldown_c is not None:
            try:
                self.temp.set_temperature(self.cooldown_c)
                self._log(f"🌡 synthesis complete — cooling to {self.cooldown_c:g} °C", "info")
            except Exception as exc:
                self._log(f"⚠ cooldown command failed: {exc}", "warn")
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
            # mock | real (OPEN_DEFECTS R8): a campaign resuming from
            # manifest.json must be able to tell simulated runs from real ones.
            "backend": self.backend,
        }
        self.history.append(record)

        # Elapsed vs planned makes "why was my synthesis shorter than I set?"
        # answerable from the log alone.
        planned = (self._run_deadline - self._run_started) if self._run_started else None
        actual = record["duration_s"]
        timing = f"{actual:g}s" if actual is not None else "?"
        if planned and actual is not None:
            delta = actual - planned
            timing = (f"{actual:g}s of {planned:g}s planned"
                      + (f" ({delta:+.0f}s)" if abs(delta) >= 1 else ""))
        self._log(f"⏹ RUN END {record['recipe_id']} — ran {timing}; "
                  f"stopped by: {reason}", "ok")
        if planned and actual is not None and actual < planned - 1 and reason != "duration elapsed":
            self._log(f"   ↳ ended EARLY by {planned - actual:.0f}s because "
                      f"'{reason}' fired before the {planned:g}s duration", "info")

        # The most confusing failure mode: the run finished but no 2D data exists.
        if self._spec_enabled and not self._spec_fired:
            self._log("   ⚠ NO 2D collection fired this run — the run ended before "
                      f"T+{max(0.0, (planned or 0) - self._spec_lead):g}s "
                      f"(spec_lead_s={self._spec_lead:g}s). Shorten spec_lead_s or "
                      "lengthen the run.", "warn")
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
            # In "before" mode the post-synthesis clean-out and the next run's
            # blank are the SAME flush: the line ends up clean either way, so
            # running two back-to-back would waste a full flush duration between
            # every pair of runs. Stage the next recipe and collect its blank at
            # the end of this one.
            # `and self.auto_run`: with the loop paused, do NOT stage the next
            # condition's blank into this flush. Staging it would collect a
            # background for a condition that is not going to run until the
            # operator re-arms — possibly after they have changed the exposure,
            # which would pair a sample with a blank taken under different
            # settings. Paused means a plain clean-out; the next condition gets
            # its own blank when it actually starts.
            if (self.background_when == "before" and self._spec_enabled
                    and self.queue and self._pending is None and self.auto_run):
                nxt_recipe, nxt_setpoints = self.queue.popleft()
                self._pending = (nxt_recipe, nxt_setpoints)
                self._log(f"🧪 this flush doubles as the blank for the next "
                          f"condition ({nxt_recipe.recipe_id})", "info")
                self._enter_flush(kind="blank", bkg_recipe_id=nxt_recipe.recipe_id)
            else:
                self._enter_flush(kind="flush")
        else:
            self._to_idle()

    def _blank_flush_duration(self) -> float | None:
        """Duration for a pre-synthesis blank. Returns the short rinse when the
        line is already clean (skips a redundant full flush), else None so
        _enter_flush uses the full flush duration. `blank_rinse_s <= 0` disables
        the shortcut (always full flush). The subsequent arming period keeps the
        capillary clean while the background collection finishes, so the short
        rinse does not risk contaminating the background."""
        if self._line_clean and self.blank_rinse_s > 0:
            return float(self.blank_rinse_s)
        return None

    def _enter_flush(self, rate: float | None = None, duration: float | None = None,
                     kind: str = "flush", bkg_recipe_id: str = "") -> None:
        """kind: "flush" (post-synthesis clean-out) | "blank" (pre-synthesis
        background) | anything else (manual). ``bkg_recipe_id`` is the recipe the
        background belongs to — the UPCOMING one for a blank, the just-finished
        one for a post-run flush."""
        # explicit arg (Flush-now) first, then the APP value, then recipe, then config
        r = float(rate if rate is not None else
                  (self.live_flush_rate if self.live_flush_rate is not None
                   else self.current.flush_rate if self.current and self.current.flush_rate
                   else self.flush_rate))
        d = float(duration if duration is not None else
                  (self.live_flush_duration if self.live_flush_duration is not None
                   else self.current.flush_duration if self.current and self.current.flush_duration
                   else self.flush_duration))
        # Last line of defence before a number reaches the pump driver. The
        # callers are all bounded now (audit R8), but a flush is the one
        # operation whose failure is invisible — it announces START, waits the
        # full duration, announces complete, and may have moved nothing. Never
        # command a non-positive rate; fall back to the config value and say so.
        if r <= 0:
            self._log(f"⚠ flush rate {r:g} µL/min is not usable — falling back to "
                      f"the configured {self.flush_rate:g} µL/min so the line is "
                      f"actually cleaned", "warn")
            r = float(self.flush_rate)
        if d <= 0:
            self._log(f"⚠ flush duration {d:g}s is not usable — falling back to "
                      f"the configured {self.flush_duration:g}s", "warn")
            d = float(self.flush_duration)
        flush_pump = self._flush_pump
        # zero every present reagent EXCEPT the one we're flushing with, plus the
        # dedicated ode_flush if it exists and we're flushing with a reagent instead
        present = self.pumps.pumps
        to_zero = [p for p in REAGENT_PUMPS if p != flush_pump and p in present]
        if flush_pump != FLUSH_PUMP and FLUSH_PUMP in present:
            to_zero.append(FLUSH_PUMP)
        failed = self.pumps.zero_pumps(to_zero)
        if failed:
            self._log(f"⚠ could not zero pump(s): {', '.join(failed)} — check them", "warn")
        # clamp the flush rate to the chosen pump's own max_flow (a reagent pump has a
        # smaller sensor than ode_flush — e.g. ode_dilution maxes at 50 µL/min)
        maxf = float(self.cfg.get("pumps", {}).get(flush_pump, {}).get("max_flow", r))
        if r > maxf:
            self._log(f"⚠ flush rate {r:g} µL/min exceeds {flush_pump}'s max "
                      f"{maxf:g} µL/min — clamped to {maxf:g} (flush will take longer)",
                      "warn")
            r = maxf
        self.pumps.set_pump_flow(flush_pump, r)
        self.state = "flushing"
        # The pump THIS flush runs on. The operator can change the flush pump in
        # the app at any time; that choice applies from the next flush. Ending
        # and supervising this flush must use the pump that is actually running
        # (audit Oct 2026: stopping the NEW pump left the running one flowing).
        self._flush_active = flush_pump
        self._flush_started_at = time.time()
        self._idle_flow_since = {}
        # R6: the flush is supervised too. Remember each pump's delivered volume
        # now, so only volume delivered DURING the flush is judged (a pump that
        # ended the run near its cap must not trip on its ramp-down tail).
        self._flush_v0 = {n: float(getattr(p, "v_delivered", 0.0) or 0.0)
                          for n, p in self.pumps.pumps.items()}
        self._flow_faulted_prev = set()
        self._flush_kind = kind
        self._flush_deadline = time.time() + d
        self._bkg_fired = False        # arm the background acquisition for this flush
        # Which recipe this flush's background belongs to. Empty = collect none.
        if bkg_recipe_id:
            self._bkg_recipe_id = str(bkg_recipe_id)
        elif kind == "flush" and self.background_when == "after" and self.current is not None:
            self._bkg_recipe_id = self.current.recipe_id
        else:
            self._bkg_recipe_id = ""
        self._log(f"🧼 {'BLANK' if kind == 'blank' else 'FLUSH'} START ({kind}) — "
                  f"{flush_pump} at {r:g} µL/min for {d:g}s; "
                  f"new recipes are blocked until it finishes", "info")
        if self._spec_enabled and self._bkg_recipe_id:
            when = d - self._spec_lead
            if when > 0:
                self._log(f"   📷 background for {self._bkg_recipe_id} due at "
                          f"T+{when:g}s of the flush (on the clean capillary)", "info")
            else:
                self._log(f"   📷 background for {self._bkg_recipe_id} fires immediately "
                          f"(lead {self._spec_lead:g}s ≥ flush {d:g}s) — the line may "
                          f"not be fully flushed yet", "warn")

    def _end_flush(self) -> None:
        active = getattr(self, "_flush_active", None) or self._flush_pump
        self.pumps.set_pump_flow(active, 0.0)
        # A completed flush leaves the line clean; it stays clean until reagents
        # flow again (_enter_running). This lets the NEXT pre-synthesis blank skip
        # a redundant full flush — see _blank_flush_duration.
        self._line_clean = True

        # A "blank" flush is the FIRST half of starting a recipe: the line is now
        # clean and the background has been collected, so run the synthesis it
        # was staged for.
        if self._flush_kind == "blank" and self._pending is not None:
            recipe, setpoints = self._pending
            self._pending = None
            got_bkg = self._bkg_fired or not self._spec_enabled
            self._log(f"✓ blank complete for {recipe.recipe_id}"
                      + ("" if got_bkg else " (⚠ no background was collected)")
                      + " — starting the synthesis", "ok" if got_bkg else "warn")
            self._start_recipe(recipe, setpoints)
            return

        # THE AUTONOMOUS LOOP ADVANCES ONLY WHEN AUTO-RUN IS ON.
        #
        # This check did not exist, and its absence is what made "Stop
        # autonomous" do nothing: the flag was only ever read at INTAKE
        # (submit) and on the re-arm, never here — so once a campaign was
        # rolling, _end_flush → _begin_next → … chained through the entire
        # queue whatever the toggle said. Turning it off mid-campaign changed
        # precisely one thing: newly arriving conditions no longer auto-started
        # if the reactor happened to be idle at that moment. The rig kept
        # going.
        #
        # What the operator asked for, and what this gives: pause AFTER the
        # current condition has run and its line has been flushed. The rig is
        # left clean and idle at `ready`, the queue is kept, and the
        # data-collection settings unlock (spec_lock_reason clears once
        # auto-run is off and the state is quiet) so exposure / frames / lead
        # can be changed. Re-arming picks the queue up with the new values.
        advancing = bool(self.queue) and self.auto_run
        if self.queue and not self.auto_run:
            nxt = (f"⏸ PAUSED — autonomous mode is off. {len(self.queue)} "
                   f"condition(s) waiting; the line is flushed and the pumps "
                   f"are idle. Data-collection settings can be changed now. "
                   f"Press Start for one, or Run autonomously for all.")
        else:
            nxt = (f"starting next of {len(self.queue)} queued condition(s)"
                   if self.queue else "no conditions queued — going idle")
        self._log(f"✓ {self._flush_kind} complete ({active} stopped) — {nxt}",
                  "warn" if (self.queue and not self.auto_run) else "ok")
        if self.current is not None:
            self._event("reactor.ready", {"recipe_id": self.current.recipe_id,
                                          "paused": not self.auto_run,
                                          "queued": len(self.queue)})
        # advance to the next queued recipe, or idle/vent the pumps and wait
        if advancing:
            self._begin_next()
        else:
            self.pumps.idle_all()   # vent all pumps (P0) — not just hold flow 0
            self.state = "ready"
            self.current = None
            self.setpoints = {}
            if not self.queue:
                self._log("💤 no more conditions — pumps idled, waiting for next", "info")

    def _to_idle(self) -> None:
        failed = self.pumps.idle_all()
        if failed:
            self._log(f"⚠ could not idle {', '.join(failed)} — check these pumps", "warn")
        self.state = "idle"
        self.current = None
        self.setpoints = {}

    def _abandon_condition(self, recipe_id: str, reason: str,
                           wait_s: float | None = None) -> None:
        """A condition that can never run (today: an arm timeout). Close it
        LOUDLY instead of just going idle.

        Going quietly to idle was a three-part silence (audit R11):
          * no bus event, so Auto Watch could not report it;
          * no feedback file, so the optimizer waited forever on a condition
            that had already been abandoned;
          * no call to _begin_next, so anything else queued sat there until the
            next file happened to arrive — and if the ML side was itself
            waiting on the missing feedback, that was never.

        With the shipped temperature arming this fires on EVERY condition on a
        rig whose thermocouple is not reporting, 900 s apart, in silence.
        Caller must hold _lock.
        """
        rec = self.current
        record = {
            "recipe_id": recipe_id or (rec.recipe_id if rec else None),
            "recipe": rec.to_dict() if rec else None,
            "setpoints": self.setpoints,
            "measured_flows": {},
            "started": None, "ended": time.time(),
            "duration_s": None,
            "waited_s": round(float(wait_s), 1) if wait_s else None,
            "reason": reason,
            "status": "abandoned",
            "backend": self.backend,                # mock | real (OPEN_DEFECTS R8)
        }
        self.history.append(record)
        try:
            self._feedback(record["recipe_id"], record)
            self._event("reactor.run_abandoned", record)
        except Exception as exc:
            self._log(f"⚠ could not report the abandoned condition: {exc}", "warn")
        # DELIBERATELY NOT written to manifest.json as a run: nothing was
        # synthesised, and a record there reads as a completed run to every
        # other app. The feedback file and the event are how the optimizer and
        # Auto Watch learn about it.
        self._to_idle()
        # Keep the campaign moving. One condition that cannot arm must not
        # stall the queue behind it.
        if self.auto_run and self.queue:
            self._log(f"↪ {len(self.queue)} condition(s) still queued — "
                      f"continuing with the next one", "info")
            self._begin_next()

    def _arm_progress(self, now: float, timed: bool) -> None:
        """Periodic 'still arming' line. Without this the app looks frozen while
        the reactor heats, and there is nothing in the log to show whether the
        temperature was actually climbing."""
        last = getattr(self, "_arm_last_log", 0.0)
        if now - last < self.ARM_PROGRESS_S:
            return
        self._arm_last_log = now
        rid = self.current.recipe_id if self.current else "?"
        if timed:
            left = max(0.0, self._arm_ready_at - now)
            self._log(f"⏲ arming {rid} — {left:.0f}s left of the timed wait "
                      f"(reactor {self.temp.current:.1f}°C)", "info")
        else:
            tgt = self.current.T_reac if self.current else float("nan")
            left = max(0.0, self._arm_deadline - now)
            self._log(f"🌡 arming {rid} — {self.temp.current:.1f}°C → "
                      f"{tgt:g}±{self.temp.tolerance:g}°C "
                      f"(Δ{tgt - self.temp.current:+.1f}°C, {left:.0f}s before timeout)",
                      "info")
