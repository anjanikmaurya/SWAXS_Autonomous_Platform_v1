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


from .collection import CollectionMixin   # noqa: E402
from .sequence import SequenceMixin       # noqa: E402
from .supervisor import SupervisorMixin   # noqa: E402


class ReactorController(SequenceMixin, SupervisorMixin, CollectionMixin):
    def __init__(self, cfg: dict, backend: str = "mock", *,
                 log_cb=None, event_cb=None, feedback_cb=None, manifest_cb=None,
                 auto_run_cb=None):
        self.cfg = cfg
        backend = str(backend).strip().lower()
        if backend not in ("mock", "real"):
            raise ValueError(f"backend must be 'mock' or 'real', got {backend!r}")
        self.backend = backend
        self.pumps = PumpBank(cfg, backend=backend)
        # beamline follows the same backend as the pumps (one Mock/Real switch)
        self.beamline = make_beamline(_spec_cfg_for(cfg, backend))
        self._log = log_cb or _noop
        # TempController takes the log too, so a REFUSED temperature command is
        # audible. It used to swallow every exception from beamline
        # .set_temperature(), which made a failed csettemp — including the
        # end-of-run cooldown and the vent-to-zero — completely invisible
        # (audit R21).
        self.temp = TempController(cfg, backend=backend, beamline=self.beamline,
                                   log=self._log)
        self._event = event_cb or _noop
        self._feedback = feedback_cb or _noop
        self._manifest = manifest_cb or _noop
        #: Called whenever auto-run changes for a reason other than the API
        #: route — today only the E-stop, which disables it. Without this the
        #: persisted state kept saying ON after an E-stop had revoked it.
        self._auto_run_changed = auto_run_cb or _noop

        self.state = "idle"
        self.auto_run = False
        self.queue: deque[tuple[Recipe, dict]] = deque()   # (recipe, setpoints)
        self.current: Recipe | None = None
        self.setpoints: dict = {}
        self.history: deque = deque(maxlen=500)   # re-capped from safety.history_max below

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
        # NOTE: there is deliberately NO time compression, in mock or anywhere
        # else. Every duration below is real seconds on every backend, so a mock
        # rehearsal is timed exactly like the beamline run it stands in for. Use
        # short durations in the app if you want a short test.
        self.default_duration = float(run.get("default_duration", 600.0))
        # NOT READ ANYWHERE (OPEN_DEFECTS R4). Kept only so an old config that
        # sets it still loads. Ending a run on measurement is ALWAYS on: a
        # file.averaged signal for the current recipe ends the run
        # (signal_measurement_complete), whatever this key says. Setting it to
        # false does not disable that. Wiring it would change run behaviour, so
        # it is documented as a no-op instead.
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
        self.live_arm_mode: str | None = None         # "temperature" | "timed"
        self.live_arm_wait: float | None = None        # timed-arming wait (s)
        self.live_flush_rate: float | None = None      # flush rate (µL/min)
        self.live_flush_duration: float | None = None  # flush duration (s)
        arm = cfg.get("arming", {})
        self.default_arm_mode = str(arm.get("default_mode", "temperature")).lower()
        self.default_arm_wait = float(arm.get("default_wait_s", 120.0))
        fl = cfg.get("flush", {})
        self.flush_rate = float(fl.get("rate", 100.0))
        self.flush_duration = float(fl.get("duration", 300.0))
        # Short blank when the line is ALREADY clean. In background_when="before"
        # a closed loop does a full post-synthesis flush (clears the product),
        # goes ready, and only then — once the optimizer has proposed the next
        # condition — runs the pre-synthesis blank. The line is still clean from
        # that post-synthesis flush (nothing flowed while waiting), so re-running
        # a FULL flush duration wastes a whole flush per cycle. When the line is
        # clean the blank only needs a brief solvent refresh while the clean-
        # capillary background is collected; arming (heating) then gives that
        # collection ample time to finish before any reagent flows. Falls back to
        # a full flush when the line is NOT clean (cold start, post-abort).
        self.blank_rinse_s = float(fl.get("blank_rinse_s", 30.0))
        #: True once a flush has cleaned the line and nothing has flowed since.
        #: Set at the end of every completed flush; cleared when reagents flow.
        self._line_clean = False
        # Which pump does the flush. Default the dedicated ode_flush; can be switched
        # to a reagent pump (e.g. ode_dilution — same ODE) from the app if ode_flush
        # is unavailable. Reagent flush pumps are capped at their own max_flow.
        fp = str(fl.get("pump", FLUSH_PUMP))
        self._flush_pump = fp if fp in PUMP_NAMES else FLUSH_PUMP
        # cool the reactor to this temperature the moment a synthesis run ends
        # (None / not set = leave temperature as-is)
        _cd = cfg.get("temperature", {}).get("cooldown_c", None)
        self.cooldown_c = None if _cd is None else float(_cd)
        s = cfg.get("safety", {})
        self.T_max = float(s.get("T_max", 320.0))
        self.per_pump_max = float(s.get("per_pump_max", 1000.0))
        self._flow_fault_estop = bool(s.get("flow_fault_estop", False))   # else just warn
        #: History is capped. One record per run is small, but a multi-day
        #: campaign is hundreds of them and nothing ever trimmed the list
        #: (audit R18). 500 keeps well over a week of conditions.
        self.history = deque(maxlen=int(s.get("history_max", 500)))
        self._flow_faulted_prev: set = set()   # for one-shot flow-fault warnings
        # Stale-temperature handling: warn once by default; set
        # safety.temp_stale_estop: true to make it trip the E-stop instead.
        self._temp_stale_warned = False
        #: one-shot note that polling is paused for an acquisition (not a fault)
        self._temp_paused_noted = False
        self._temp_stale_estop = bool(s.get("temp_stale_estop", False))
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
        #: which recipe the CURRENT flush's background belongs to ("" = none)
        self._bkg_recipe_id = ""
        #: recipe staged behind a pre-synthesis blank flush
        self._pending: tuple | None = None
        #: "before" — flush, measure the blank on the clean capillary, THEN run
        #:            the synthesis (background precedes its sample; pairing is
        #:            unambiguous and subtraction can start as soon as the sample
        #:            lands).
        #: "after"  — legacy: measure the blank during the post-synthesis flush.
        self.background_when = str(spec.get("background_when", "before")).strip().lower()
        if self.background_when not in ("before", "after"):
            self.background_when = "before"
        self._last_collect = None       # {role, recipe_id, path, t} of the last SPEC trigger
        #: hub project folder — holds config.yml (poni_files, detector_shapes).
        #: Distinct from _spec_data_dir, which is the 2D save folder.
        self._project_root = str(cfg.get("project_root", "") or "")

        self._lock = threading.RLock()
        self._alive = True
        # Control-loop fault bookkeeping. A loop that has faulted must never look
        # identical to a healthy one in the status payload.
        self._loop_faults = 0
        self._last_fault: str | None = None
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
                # R5: say what was skipped, so the record shows the temperature
                # the pumps actually started at (the UI asks for confirmation).
                try:
                    t_now, t_tgt = float(self.temp.current), float(self.temp.target)
                    why = f" at {t_now:.1f} °C (target {t_tgt:.1f} °C)"
                except Exception:
                    why = ""
                self._log(f"⏩ arming skipped by the operator — starting pumps now{why}", "warn")
                self._enter_running()
                return True
        return False

    def pump_limits(self) -> dict:
        """Current per-pump {sensor_min, max_flow, calibration_factor} (µL/min)."""
        return {name: {"sensor_min": p.sensor_min, "max_flow": p.max_flow,
                       "calibration_factor": getattr(p, "calibration_factor", 1.0)}
                for name, p in self.pumps.pumps.items()}

    def set_pump_limits(self, limits: dict) -> dict:
        """Update per-pump flow limits + calibration_factor live.  ``limits`` =
        {pump: {sensor_min, max_flow, calibration_factor}}.  Limits feed recipe
        validation immediately; calibration_factor scales the water→fluid flow on
        the serial link. Missing keys keep the current value. Raises ValueError on
        a bad range or a non-positive factor."""
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
                if str(lim.get("calibration_factor", "")).strip() != "":
                    cf = float(lim["calibration_factor"])
                    if cf <= 0:
                        raise ValueError(f"{name}: calibration_factor must be > 0 (got {cf})")
                    pc["calibration_factor"] = cf
                    p.calibration_factor = cf
            changed = ", ".join(
                f"{n}[{v.get('sensor_min', '·')}–{v.get('max_flow', '·')} µL/min"
                + (f", cal×{v['calibration_factor']}" if str(v.get("calibration_factor", "")).strip() else "")
                + "]"
                for n, v in (limits or {}).items())
            self._log(f"⚙ pump limits updated — {changed or '(no changes)'}", "info")
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

    def clear_queue(self) -> list[dict]:
        """Empty the pending-recipe queue (does not affect a running recipe).

        Returns ``[{recipe_id, source}, …]`` for what was removed, NOT just a
        count. The caller needs the sources: a folder-sourced condition still
        has its file sitting in the watched folder — the file is only retired
        once the reactor is finished with it — so clearing the queue without
        also retiring those files would leave them to be re-ingested on the
        next restart, and Clear queue would not actually clear anything.
        """
        with self._lock:
            removed = [{"recipe_id": r.recipe_id, "source": r.source}
                       for r, _ in self.queue]
            self.queue.clear()
            if removed:
                self._log(f"🗑 cleared {len(removed)} queued recipe(s): "
                          + ", ".join(d["recipe_id"] for d in removed), "info")
            return removed

    def withdraw_source(self, source: str, reason: str = "") -> list[str]:
        """Drop QUEUED recipes that came from ``source`` (e.g. 'folder:Run3_r004.txt').

        Never touches the recipe that is currently running. Used when the
        producer (the analyzer) withdraws a condition it already wrote, by moving
        the file to Conditions/done/: on a new campaign or an abort those
        conditions belong to a run that no longer exists, and dosing them would
        burn reagent and beam for nothing. Returns the withdrawn recipe ids.
        """
        with self._lock:
            keep, gone = deque(), []
            for item in self.queue:
                if item[0].source == source:
                    gone.append(item[0].recipe_id)
                else:
                    keep.append(item)
            if gone:
                self.queue = keep
                self._log(f"↩ withdrawn {', '.join(gone)}"
                          + (f" ({reason})" if reason else ""), "info")
            return gone

    def set_auto_run(self, on: bool) -> None:
        """Arm / disarm the autonomous loop.

        Turning it OFF never interrupts what is already happening — it lets the
        current condition finish and its line flush, then stops at `ready`
        instead of starting the next one. Say WHEN the pause will take effect,
        because "OFF" on its own reads as "stopping now" and the rig may have
        ten more minutes of synthesis in front of it."""
        with self._lock:
            was = self.auto_run
            self.auto_run = bool(on)
            if on:
                self._log("⚙ auto-run ON", "info")
            elif self.state in ("arming", "running", "flushing"):
                rid = self.current.recipe_id if self.current else "the current condition"
                self._log(f"⏸ auto-run OFF — {rid} will FINISH and its line will "
                          f"flush, then the loop pauses. Nothing is interrupted. "
                          f"New conditions keep queueing; their files stay in the "
                          f"conditions folder until they run.", "warn")
            else:
                self._log("⚙ auto-run OFF" + (f" — {len(self.queue)} condition(s) "
                          f"waiting" if self.queue else ""), "info")
            if on and not was and self.state in ("idle", "ready") and self.queue:
                self._log(f"▶ resuming with {len(self.queue)} queued condition(s) "
                          f"— exposure {self._spec_exposure:g}s ×{self._spec_frames}, "
                          f"lead {self._spec_lead:g}s", "ok")
            if on and self.state in ("idle", "ready") and self.queue:
                self._begin_next()

    def pausing(self) -> bool:
        """True when auto-run is off but a condition is still finishing — the
        loop will stop once this one's flush completes. Distinct from 'paused'
        (already stopped) and from 'running' (will continue). Caller need not
        hold the lock."""
        return (not self.auto_run) and self.state in ("arming", "running", "flushing")

    def set_run_settings(self, d: dict) -> None:
        """Apply live run settings from the app inputs — everything EXCEPT the
        flow fractions / F_tot / temperature (those come from the recipe file).
        Keys: arm_mode, arm_wait_s, run_duration, flush_rate,
        flush_duration, flush_pump.

        Once set, a value STICKS for the whole app session and is used for every
        run / repeat / autonomous run — it wins over the config default AND over
        any recipe-embedded value. A BLANK field is IGNORED (leaves the current
        value unchanged); values reset to config defaults only when the app is
        restarted. A changed run_duration also updates the current run's deadline.

        EVERY NUMBER HERE IS BOUNDED (audit R8). Only ``exposure_s`` was, and
        the rest were coerced with a bare float() and stored verbatim:

          run_duration < 0    ended the run on the first tick
          run_duration = 0    is falsy, so it silently fell back to the config
                              default — typing 0 looked like it did something
          arm_wait_s < 0      put the ready-time in the past, SKIPPING ARMING
                              and starting the pumps immediately
          flush_rate < 0      sent a negative setpoint to the pump driver
          flush_rate = 0      the worst one: 'FLUSH START', the full duration,
                              '✓ flush complete' — and no liquid moved, so the
                              next condition's background was measured on a
                              dirty capillary and nothing said so

        That last one is the Run20 exposure_s=0 failure exactly — a zero that is
        structurally valid and scientifically empty — in the fields next to the
        one that was hardened after Run20. A refused value keeps the working one
        and says why, like set_spec_settings does."""
        def provided(key):
            return key in d and str(d.get(key)).strip() != ""
        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def bounded(key, *, allow_zero: bool, what: str):
            """Parse one field, or return None and log the refusal."""
            v = num(d[key])
            if v is None:
                return None
            if v < 0 or (v == 0 and not allow_zero):
                self._log(f"⚠ {key}={v:g} refused — {what}. Keeping the "
                          f"current value.", "warn")
                return None
            return v

        with self._lock:
            if provided("arm_mode"):
                m = str(d["arm_mode"]).lower()
                if m in ("temperature", "timed"):
                    self.live_arm_mode = m
            if provided("arm_wait_s"):
                v = bounded("arm_wait_s", allow_zero=True,
                            what="a negative wait puts the start time in the past "
                                 "and skips arming altogether")
                if v is not None:
                    self.live_arm_wait = v
            if provided("flush_rate"):
                v = bounded("flush_rate", allow_zero=False,
                            what="a flush at zero or less moves no liquid, so the "
                                 "line is never cleaned and the next background is "
                                 "measured on a dirty capillary")
                if v is not None:
                    self.live_flush_rate = v
            if provided("flush_duration"):
                v = bounded("flush_duration", allow_zero=False,
                            what="a flush of zero seconds does not clean the line")
                if v is not None:
                    self.live_flush_duration = v
            if provided("flush_pump"):
                fp = str(d["flush_pump"]).strip()
                if fp in PUMP_NAMES and fp != self._flush_pump:
                    self._flush_pump = fp
                    if self.state == "flushing":
                        self._log(f"🧼 flush pump → {fp} (applies from the next flush; "
                                  f"this flush keeps running on "
                                  f"{getattr(self, '_flush_active', None) or '?'})", "info")
                    else:
                        self._log(f"🧼 flush pump → {fp}", "info")
            if provided("run_duration"):
                v = bounded("run_duration", allow_zero=False,
                            what="a run of zero or less collects nothing")
                if v is not None:
                    self.live_duration = v
                    if self.state == "running" and self._run_started:
                        self._run_deadline = self._run_started + self.live_duration
                        self._log(f"⏱ run duration → {self.live_duration:g}s (applies to current run)", "info")

    # ── run-end triggers ───────────────────────────────────────────────────────
    def signal_measurement_complete(self, info: str = "", recipe_id: str = "") -> None:
        with self._lock:
            if self.state != "running":
                return
            # Only end THIS recipe's run. In a pipelined campaign a late or
            # duplicate file.averaged from the PREVIOUS recipe (or a background
            # average) could otherwise truncate the recipe now running. When the
            # signal carries no recipe_id (a manual, non-autonomous run), honour
            # it as before for backward compatibility.
            cur = self.current.recipe_id if self.current else ""
            if recipe_id and cur and recipe_id != cur:
                self._log(f"↩ ignoring measurement signal for {recipe_id} — "
                          f"current run is {cur}", "info")
                return
            self._measure_done = True
            self._run_reason = f"SAXS measurement complete{(' — ' + info) if info else ''}"
            self._log(f"📈 measurement signal received — ending run", "ok")

    # ── abort / emergency ──────────────────────────────────────────────────────
    def abort(self) -> tuple[bool, str]:
        """Operator Stop. Returns (acted, reason) so the API can say when the
        button did nothing instead of answering a bare ok:true — pressing Stop
        in idle used to report success and change nothing (audit R16)."""
        with self._lock:
            if self.state in ("arming", "running"):
                self._log("⛔ abort — stopping reagents, going to flush", "warn")
                self._run_reason = "aborted"
                self._end_run(flush=True)
                return True, "run stopped, flushing"
            if self.state == "flushing":
                self._log("⛔ abort during flush — idling all", "warn")
                # R2: a blank flush has a recipe staged behind it. Give it back
                # to the queue before idling, or it is lost and every later
                # condition silently loses its background too.
                self._release_pending("the flush was stopped")
                self._to_idle()
                return True, "flush stopped, idle"
            return False, (f"nothing to stop — the reactor is {self.state}")

    def estop(self) -> list[str]:
        """Emergency stop. Returns the names of any pumps that could NOT be idled
        so the caller (API/UI) can report a partial failure instead of a green
        tick — this is the one path where a false 'success' is unacceptable."""
        # Stop reagents FIRST, WITHOUT waiting on self._lock. The control loop can
        # hold self._lock for seconds while doing a blocking beamline csettemp HTTP
        # or a manifest write (R1: measured 7.8 s), and reagents must not keep
        # flowing for that long. Each pump has its own serial lock, so idle_all()
        # is safe to call here and stops delivery immediately. PUMPS ONLY —
        # deliberately nothing to the beamline/SPEC, so an in-progress X-ray
        # collection finishes on its own; temperature is left exactly as-is.
        failed = self.pumps.idle_all()   # guarded per-pump; never blocks on one
        with self._lock:
            # Record the E-stop state (even if a serial write threw above, the
            # system must not be left without it — _safety_check must see it to
            # stop re-entering).
            self.state = "estop"
            self.current = None
            # Kill the autonomous loop too: otherwise the folder watcher submits
            # the next condition and the rig restarts into the unresolved fault.
            if self.auto_run:
                self.auto_run = False
                self._log("⏸ auto-run DISABLED by the emergency stop — re-enable it "
                          "deliberately after clearing the fault", "warn")
                # Tell the app so it can persist the OFF. Without this the saved
                # state still said auto_run: true, so the restart banner — and
                # run.resume_auto_run — acted on a value the E-stop had already
                # revoked (audit R6b).
                try:
                    self._auto_run_changed(False)
                except Exception:
                    pass
            # R2: a recipe staged behind a blank flush must go back to the
            # queue, not vanish. Doing it here also means _begin_next can never
            # later skip blanks for the whole campaign.
            self._release_pending("the emergency stop fired")
            # Re-idle under the lock to catch anything the control loop may have
            # commanded in the brief window between the idle above and acquiring
            # the lock. idle_all() is idempotent.
            failed = self.pumps.idle_all()
            if failed:
                self._log(f"🛑 EMERGENCY STOP — but could NOT idle: {', '.join(failed)} "
                          f"— CHECK THESE PUMPS/PORTS IMMEDIATELY", "error")
            else:
                self._log("🛑 EMERGENCY STOP — all pumps idle", "error")
            self._event("reactor.estop", {"failed_to_idle": failed})
            return failed

    def reset(self) -> tuple[bool, str]:
        """Clear an E-stop (or a finished run) back to idle. Returns
        (acted, reason): pressed in idle or mid-flush it used to do nothing and
        still answer ok:true, so the operator could not tell (audit R16)."""
        with self._lock:
            if self.state in ("estop", "ready"):
                self.pumps.idle_all()
                self._release_pending("the reactor was reset")
                self.state = "idle"
                self._log("↺ reset to idle", "info")
                return True, "idle"
            if self.state == "idle":
                return False, "already idle — nothing to reset"
            return False, (f"can't reset while {self.state} — press Stop first, "
                           f"then Reset")

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
            # A manual collect is allowed in idle/ready/estop and runs in its own
            # thread. Tearing the beamline down underneath it would release SPEC
            # remote control mid-acquisition, or hand a queued mock collect to
            # real hardware.
            try:
                if self.beamline.is_collecting():
                    return False, ("a 2D collection is in progress — wait for it "
                                   "to finish before switching backend")
            except Exception:
                pass

            # Build EVERYTHING before swapping anything. Previously the new pump
            # bank was installed first, so a later failure (e.g. SpecBeamline's
            # `import requests`) escaped with REAL pumps open while self.backend
            # still said "mock" — the operator would believe they were simulating.
            new_pumps = new_beamline = new_temp = None
            try:
                new_pumps = PumpBank(self.cfg, backend=backend)   # opens ports for 'real'
                new_beamline = make_beamline(_spec_cfg_for(self.cfg, backend))
                new_temp = TempController(self.cfg, backend=backend, beamline=new_beamline)
            except Exception as exc:
                for p in getattr(new_pumps, "pumps", {}).values():
                    try:
                        p.close()
                    except Exception:
                        pass
                try:
                    if new_beamline is not None:
                        new_beamline.close()
                except Exception:
                    pass
                self._log(f"⚠ backend stays {self.backend.upper()} — could not start "
                          f"{backend.upper()}: {exc}", "error")
                return False, str(exc)

            # Only now release the old backend and commit the swap.
            try:
                self.pumps.idle_all()
            except Exception:
                pass
            for p in self.pumps.pumps.values():
                try:
                    p.close()
                except Exception:
                    pass
            try:
                self.beamline.close()      # MockBeamline stops any simulator thread
            except Exception:
                pass
            self.pumps = new_pumps
            self.beamline = new_beamline
            self.temp = new_temp
            self.cfg.setdefault("spec", {})["backend"] = backend
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
        The queue is kept.

        An E-STOP IS LATCHED and is NOT cleared here: venting is the natural
        reflex after a fault, and silently dropping to 'idle' would let the
        folder watcher submit the next recipe straight back into the unresolved
        fault. Use reset() to leave the E-stop deliberately.

        A vent pressed DURING A RUN used to jump straight to idle without going
        through _end_run, so the synthesis left no history entry, no
        <recipe_id>.done.json, no manifest record and no reactor.run_complete —
        the condition simply vanished while its 2D data sat on disk, and the
        optimizer blocked forever on feedback that was never written. The run is
        now closed properly first (audit R10).
        """
        with self._lock:
            was_estop = self.state == "estop"
            if self.state == "running":
                self._log("🟦 vent requested during a run — closing the run "
                          "record first so the condition is not lost", "warn")
                self._run_reason = self._run_reason or "vented by the operator"
                self._end_run(flush=False)     # writes record + feedback + event
            # R2: a recipe staged behind a blank flush goes back to the queue.
            self._release_pending("the pumps were vented")
            failed = self.pumps.idle_all()   # P0 to every pump → chamber → 0
            self.temp.set_temperature(0.0)
            self.setpoints = {}
            if not was_estop:
                self.current = None
                self.state = "idle"
            if failed:
                self._log(f"⚠ vent: could not idle {', '.join(failed)} — check these pumps", "warn")
            self._log("🟦 vented all pumps — chamber pressure reset to 0"
                      + (" (E-STOP still latched — press Reset to clear)" if was_estop else ""),
                      "warn" if was_estop else "info")
            self._event("reactor.vent", {"estop_latched": was_estop,
                                         "failed_to_idle": failed})
            # Returned so the API can report a pump that did NOT idle instead
            # of a green tick, like the E-stop route does (audit R16).
            return failed

    # ── flush ─────────────────────────────────────────────────────────────────
    def flush_now(self, rate: float | None = None, duration: float | None = None,
                  kind: str = "flush") -> bool:
        with self._lock:
            if self.state in ("idle", "ready"):
                self._enter_flush(rate, duration, kind=kind)
                return True
        return False

    # ── background loop ─────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while self._alive:
            # NOTHING may escape this loop. If an unhandled exception killed the
            # thread, _safety_check() would stop running while reagent pumps are
            # still commanded — no over-temperature, over-pressure, volume or
            # flow-fault supervision, and no run deadline. Fail loud and safe.
            try:
                self._tick_once()
            except Exception as exc:                     # noqa: BLE001
                # ORDER MATTERS. This handler used to log first:
                #
                #     logger.exception("control loop fault")   # NameError!
                #     try: self.estop()
                #
                # `logger` was never imported in this module, so the NameError
                # fired BEFORE estop() and then escaped the except block — killing
                # the supervisor thread with reagent pumps still commanded and
                # heaters still on, while `self._alive` remained True so the
                # controller reported itself healthy. Exactly the outcome the
                # comment above forbids.
                #
                # The E-stop now runs FIRST, in its own guard, and no path out of
                # here can skip it.
                try:
                    self.estop()
                except Exception:                        # noqa: BLE001
                    try:
                        self._log("🛑 CONTROL LOOP FAULT and the EMERGENCY STOP "
                                  "ALSO FAILED — go to the hutch and cut power to "
                                  "the pumps and heater.", "error")
                    except Exception:
                        pass
                    try:
                        logger.critical("estop failed inside loop fault handler",
                                        exc_info=True)
                    except Exception:
                        pass
                try:
                    self._log(f"🛑 CONTROL LOOP FAULT — {exc.__class__.__name__}: {exc}. "
                              f"Emergency-stopped so pumps are not left running "
                              f"without supervision.", "error")
                except Exception:
                    pass
                try:
                    logger.exception("control loop fault")
                except Exception:
                    pass
                # Remember it, so status/UI can say the loop faulted rather than
                # implying everything is fine.
                self._loop_faults += 1
                self._last_fault = f"{exc.__class__.__name__}: {exc}"
            time.sleep(0.2)

        # Reaching here means the loop is finished. If _alive is still True the
        # thread is dying for a reason nobody asked for — say so instead of
        # leaving a controller that believes it is supervising.
        if self._alive:
            self._alive = False
            self._last_fault = self._last_fault or "control loop exited unexpectedly"
            try:
                self._log("🛑 CONTROL LOOP EXITED — supervision has stopped. "
                          "Emergency-stopping.", "error")
            except Exception:
                pass
            try:
                self.estop()
            except Exception:
                pass
            try:
                logger.critical("control loop exited unexpectedly")
            except Exception:
                pass

    def _tick_once(self) -> None:
        if getattr(self, "_shutting_down", False):   # never command a flow after the final idle
            return
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
                if self._arm_mode == "timed":
                    # start the pumps once the computed wait elapses; no
                    # temperature gating and no arm timeout in these modes.
                    self._arm_progress(now, timed=True)
                    if now >= self._arm_ready_at:
                        self._enter_running()
                elif self.temp.is_stable():
                    self._enter_running()
                elif now > self._arm_deadline:
                    rid = self.current.recipe_id if self.current else "?"
                    tgt = self.current.T_reac if self.current else float("nan")
                    self._log(
                        f"⚠ ARM TIMEOUT — {rid} aborted after "
                        f"{now - (self._arm_deadline - self.temp.timeout):.0f}s: "
                        f"reactor reached {self.temp.current:.1f}°C but needs "
                        f"{tgt:g}±{self.temp.tolerance:g}°C. Check the heater/"
                        f"thermocouple, raise arming.timeout_s, or use "
                        f"arm_mode='timed' if no thermocouple is wired.", "error")
                    self._run_reason = "arm timeout"
                    self._abandon_condition(rid, "arm timeout", wait_s=self.temp.timeout)
                else:
                    self._arm_progress(now, timed=False)
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
                                     args=(_rid, "sample", self.backend, self.beamline),
                                     daemon=True).start()
                if self._measure_done:
                    self._end_run(flush=True)
                elif now > self._run_deadline:
                    # synthesis duration reached — applies to manual AND auto
                    self._run_reason = self._run_reason or "duration elapsed"
                    self._end_run(flush=True)
                elif (self.advance_on_new and self.queue
                        and (now - self._run_started) >= self.min_dwell
                        # NEVER advance before the 2D collection has fired:
                        # with the shipped 600 s duration / 180 s lead the
                        # sample collect is due at T+420 s but min_dwell is
                        # 60 s, so a queued condition could end the run with
                        # NO DATA — which then stalls the campaign, because
                        # nothing ever reports the measurement.
                        and (self._spec_fired or not self._spec_enabled)):
                    # a newer condition is queued — advance early (before duration)
                    self._run_reason = "next condition available"
                    self._end_run(flush=True)
            elif self.state == "flushing":
                # fire the BACKGROUND 2D collection once, ~lead seconds before the
                # flush ends (pure solvent in the capillary) — only for a real
                # post-synthesis flush that has a recipe to tag it with
                if (self._spec_enabled and not self._bkg_fired
                        and self._bkg_recipe_id
                        and now >= self._flush_deadline - self._spec_lead):
                    self._bkg_fired = True
                    threading.Thread(target=self._fire_spec_collection,
                                     args=(self._bkg_recipe_id, "background",
                                           self.backend, self.beamline),
                                     daemon=True).start()
                if now > self._flush_deadline:
                    self._end_flush()

    #: seconds between "still waiting" progress lines while arming
    ARM_PROGRESS_S = 20.0

    # ── status ──────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            now = time.time()
            elapsed = round(now - self._run_started, 1) if self.state == "running" else None
            eff = self.live_duration or self.default_duration
            dur = ((self.current.run_duration or eff) if self.current else None)
            flush_left = round(self._flush_deadline - now, 1) if self.state == "flushing" else None
            _timed_arm = self.state == "arming" and self._arm_mode == "timed"
            arm_left = round(max(0.0, self._arm_ready_at - now), 1) if _timed_arm else None
            arm_total = round(self._arm_total, 1) if _timed_arm else None
            return {
                "state": self.state,
                "backend": self.backend,
                # A supervisor that has faulted must not look identical to a
                # healthy one. `supervising` is the ground truth: the loop thread
                # is alive AND has not been told to stop.
                "supervising": bool(self._alive and self._thread is not None
                                    and self._thread.is_alive()),
                "loop_faults": self._loop_faults,
                "last_fault": self._last_fault,
                "auto_run": self.auto_run,
                # auto-run is off but a condition is still finishing: the loop
                # stops after this one's flush. The UI must be able to tell
                # "pausing" from "paused" and from "running", or Stop
                # autonomous looks like it did nothing for the next ten minutes.
                "pausing": self.pausing(),
                # paused AND there is work waiting for a deliberate Start
                "paused_with_queue": bool(
                    (not self.auto_run) and self.queue
                    and self.state in ("idle", "ready")),
                "arm_mode": self._arm_mode if self.state == "arming" else None,
                "arm_remaining_s": arm_left,
                "arm_total_s": arm_total,
                "pumps": self.pumps.state(),
                "temperature": {"target": round(self.temp.target, 2),
                                "current": round(self.temp.current, 2),
                                "stable": self.temp.is_stable(),
                                "tolerance": self.temp.tolerance,
                                # Whether `current` is a MEASUREMENT. With no
                                # sensor wired, read() returns the last value
                                # (25 °C ambient) forever and `stale` cannot
                                # see it — there is no source to go stale. A UI
                                # that prints the number without this is
                                # presenting a placeholder as a reading.
                                # See TempController.source.
                                "source": self.temp.source,
                                "stale": self.temp.stale,
                                "age_s": (round(self.temp.age_s(), 1)
                                          if self.temp.source == "beamline" else None),
                                "trustworthy": self.temp.trustworthy,
                                "bstop": self.temp.bstop,
                                "i0": self.temp.i0},
                "last_collect": self._last_collect,
                "spec": {"enabled": self._spec_enabled, "exposure_s": self._spec_exposure,
                         "frames": self._spec_frames, "spec_lead_s": self._spec_lead,
                         "sample_tag": self._spec_sample_tag, "bkg_tag": self._spec_bkg_tag,
                         "data_dir": self._spec_data_dir,
                         # So the UI can grey the fields and say why, rather
                         # than letting the operator type into a box whose
                         # value will be refused.
                         "lock_reason": self.spec_lock_reason(),
                         "locked": bool(self.spec_lock_reason()),
                         "collecting": self.beamline.is_collecting()},
                "current_recipe": self.current.to_dict() if self.current else None,
                "elapsed_s": elapsed, "duration_s": dur,
                "run_duration_setting": self.live_duration or self.default_duration,
                "flush_pump": self._flush_pump,
                # Effective run settings (live value if the operator set one, else
                # the config default). The UI reflects these on load so the fields
                # ALWAYS show what a run would actually use — never a stale value
                # or an HTML default that diverges from the server.
                "run_settings": {
                    "arm_mode":       self.live_arm_mode or self.default_arm_mode,
                    "arm_wait_s":     (self.live_arm_wait if self.live_arm_wait is not None
                                       else self.default_arm_wait),
                    "run_duration":   self.live_duration or self.default_duration,
                    "flush_rate":     (self.live_flush_rate if self.live_flush_rate is not None
                                       else self.flush_rate),
                    "flush_duration": (self.live_flush_duration if self.live_flush_duration is not None
                                       else self.flush_duration),
                    "flush_pump":     self._flush_pump,
                },
                "flush_remaining_s": flush_left,
                "queue": [r.recipe_id for r, _ in self.queue],
                "queue_len": len(self.queue),
                "runs_completed": len(self.history),
            }

    def shutdown(self, collect_wait_s: float = 3.0) -> None:
        """Stop the control loop and leave the rig safe for the next user:
        idle the pumps, close the shutter, and RELEASE SPEC remote control so
        beamline staff can drive SPEC again afterward.

        ORDER MATTERS. The pumps are idled FIRST and unconditionally — that is
        the part that must happen even if everything after it fails.

        Then a bounded wait for an acquisition in flight (audit R19). Shutdown
        used to check ``is_collecting()`` before closing the shutter and then
        release remote control regardless, which is the one action that can
        disturb a live SPEC macro. ``collect_wait_s`` is deliberately short: the
        hub allows 5 s between SIGTERM and SIGKILL, so waiting out a full 100 s
        acquisition is not on offer — this just avoids releasing control in the
        middle of a command that is about to return.
        """
        self._shutting_down = True
        self._alive = False
        try:
            self.pumps.idle_all()
        except Exception:
            pass
        # Pump ports next, while there is time (the hub allows 5 s before SIGKILL;
        # OPEN_DEFECTS R7). Wait briefly for the control loop to see _alive=False,
        # idle AGAIN in case a tick already in progress commanded a flow after the
        # first idle (audit Oct 2026), then close every port so a Windows COM port
        # is not left locked for the next start. A pump that will not close is
        # skipped, never fatal.
        try:
            t = getattr(self, "_thread", None)
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=1.0)
        except Exception:
            pass
        try:
            self.pumps.idle_all()
        except Exception:
            pass
        for p in getattr(self.pumps, "pumps", {}).values():
            try:
                p.close()
            except Exception:
                pass
        deadline = time.time() + max(0.0, float(collect_wait_s))
        try:
            while self.beamline.is_collecting() and time.time() < deadline:
                time.sleep(0.1)
        except Exception:
            pass
        try:
            if not self.beamline.is_collecting():   # never interrupt a live acquisition
                self.beamline.close_shutter()
            else:
                self._log("⚠ shutting down while a 2D acquisition is still "
                          "running — the shutter is being left as-is and SPEC "
                          "control released; check the shutter in the hutch",
                          "warn")
        except Exception:
            pass
        try:
            self.beamline.close()                   # release_remote_control (if held)
        except Exception:
            pass
