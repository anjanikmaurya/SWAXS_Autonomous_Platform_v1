"""
src/reactor/collection.py — part of ReactorController (split out October 2026).

SPEC data collection: acquisition settings, Collect now, firing the background and sample shots.

The methods below were moved VERBATIM from controller.py; ReactorController
inherits CollectionMixin, so every call and every self.* attribute is unchanged.
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




class CollectionMixin:
    def spec_lock_reason(self) -> str:
        """Why the data-collection settings cannot be changed right now, or ''.

        These define what an acquisition IS — exposure, frame count, the tags
        that become filenames, the folder frames land in. Changing them while
        the loop is collecting does not just risk a bad value (Run20 lost a
        whole condition to an exposure_s of 0); even a VALID change makes the
        conditions before and after it incomparable, which is the one thing a
        campaign cannot tolerate, since the optimizer treats every point as a
        measurement of the same experiment.

        So they are frozen for the duration and the operator stops the run to
        change them. Caller must hold _lock.
        """
        if self.auto_run:
            return ("auto-run is ON — data-collection settings are frozen so "
                    "every condition in the campaign is collected identically. "
                    "Turn auto-run off to change them.")
        if self.state not in ("idle", "ready", "estop"):
            return (f"a run is in progress ({self.state}) — data-collection "
                    f"settings are frozen until it finishes.")
        return ""

    def set_spec_settings(self, d: dict) -> tuple[bool, str]:
        """Live SPEC data-collection settings from the app: exposure_s, frames,
        spec_lead_s, sample_tag, bkg_tag, data_dir. Blank/missing keys are left
        unchanged. Values apply to the NEXT acquisition (read when a collect fires).

        Returns (ok, message) like collect_now/set_backend. Refused outright
        while a run or campaign is in flight — see spec_lock_reason.
        """
        def _tag(v):
            # keep filename-safe tokens only (letters/digits/_-)
            return "".join(c for c in str(v).strip() if c.isalnum() or c in "_-")
        with self._lock:
            locked = self.spec_lock_reason()
            if locked:
                self._log(f"⚙ data-collection settings NOT changed — {locked}",
                          "warn")
                return False, locked
            if str(d.get("exposure_s", "")).strip():
                # A non-positive exposure is silently catastrophic: the mock
                # detector multiplies its intensity map by exposure_s, so 0
                # yields a correctly-sized, entirely BLANK .raw — which reduces
                # to a structurally perfect .dat full of zeros, and only
                # surfaces four stages later as "no usable frames" in the
                # average app. On a real rig it is a zero-second count, i.e.
                # burnt beamtime. `frames` beside it has always been clamped
                # with max(1, ...); exposure_s was not, so a 0 typed into the
                # UI field was accepted verbatim and every later condition
                # collected nothing. Refuse it and keep the working value.
                try:
                    v = float(d["exposure_s"])
                    if v > 0:
                        self._spec_exposure = v
                    else:
                        self._log(f"⚠ exposure_s={v:g} refused — an exposure of "
                                  f"zero or less collects blank frames. Keeping "
                                  f"{self._spec_exposure:g}s.", "warn")
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
            if str(d.get("data_dir", "")).strip():
                self._spec_data_dir = str(d["data_dir"]).strip()
            msg = (f"exp {self._spec_exposure:g}s ×{self._spec_frames}, "
                   f"lead {self._spec_lead:g}s, tags {self._spec_sample_tag}/"
                   f"{self._spec_bkg_tag}, dir {self._spec_data_dir or '(unset)'}")
            self._log(f"⚙ data-collection: {msg}", "info")
            return True, msg

    def set_project_root(self, path: str) -> None:
        """Tell the controller (and the 2D simulator) where the hub project
        folder is. That folder's config.yml supplies poni_files / detector_shapes,
        so the simulated frames use the SAME geometry the reduction app will."""
        with self._lock:
            self._project_root = str(path or "").strip()
        try:
            self.beamline.set_project_root(self._project_root)
        except Exception:
            pass

    def set_data_dir(self, path: str) -> None:
        """Force the SPEC save folder (used when the hub switches project folder —
        overwrites whatever was there so data_dir follows the hub)."""
        with self._lock:
            p = str(path).strip()
            if p and p != self._spec_data_dir:
                self._spec_data_dir = p
                self._log(f"📁 SPEC data_dir → {p}", "info")

    def default_data_dir(self, path: str) -> None:
        """Set data_dir ONLY if it isn't already set (used to seed it from the hub
        project folder without clobbering a config value or the user's UI entry)."""
        with self._lock:
            if path and not self._spec_data_dir:
                self._spec_data_dir = str(path).strip()

    def collect_now(self, role: str = "sample") -> tuple[bool, str]:
        """Fire a one-off SPEC 2D acquisition on demand (dry-run/verify), OUTSIDE a
        run. Allowed only when idle/ready/estop and not already collecting, so it
        never interferes with the automated sample/background collects."""
        with self._lock:
            if self.state not in ("idle", "ready", "estop"):
                return False, f"can't manually collect while {self.state} — the run manages collection"
            if not self._spec_enabled:
                return False, "data collection is disabled (spec.enabled = false)"
            if self.beamline.is_collecting():
                return False, "a collection is already in progress"
            role = "background" if str(role).lower().startswith("b") else "sample"
            rid = "manual_" + time.strftime("%Y%m%d_%H%M%S")
        threading.Thread(target=self._fire_spec_collection,
                         args=(rid, role, self.backend, self.beamline),
                         daemon=True).start()
        self._log(f"📷 manual collect requested ({role}) — {rid}", "info")
        return True, rid

    def _fire_spec_collection(self, recipe_id: str, role: str,
                              backend_at_dispatch: str | None = None,
                              bl=None) -> None:
        """Trigger a SPEC 2D acquisition. ``role`` is 'sample' (during the run) or
        'background' (during the flush). The filename is
        ``{recipe_id}_{tag}`` so averaging separates the two and background
        subtraction pairs them by the shared recipe_id. Runs in its own thread —
        blocking SPEC I/O must not stall the control loop.

        ``backend_at_dispatch`` and ``bl`` are captured by the DISPATCHER at
        Thread-construction time and passed in, so the guard below can detect a
        backend switch that happened between dispatch and this thread running.
        (They used to be read here, inside the thread, and compared to themselves
        one line later — the check could never fire, so a mock-initiated collect
        could execute on real hardware, or vice versa.)"""
        t_start = time.time()
        if backend_at_dispatch is None:
            backend_at_dispatch = self.backend
        if bl is None:
            bl = self.beamline
        try:
            if backend_at_dispatch != self.backend:
                self._log(f"📷 2D {role} collect CANCELLED — backend changed from "
                          f"{backend_at_dispatch} to {self.backend} before it ran", "warn")
                return
            tag = self._spec_sample_tag if role == "sample" else self._spec_bkg_tag
            prefix = f"{recipe_id}_{tag}"
            path = (f"{self._spec_data_dir.rstrip('/')}/{prefix}"
                    if self._spec_data_dir else prefix)
            total_s = self._spec_exposure * self._spec_frames
            self._log(f"📷 2D {role.upper()} collect START — {self._spec_frames} frame(s) "
                      f"× {self._spec_exposure:g}s = {total_s:g}s total "
                      f"[{self.backend.upper()}]", "ok")
            self._log(f"   → {path}_*.raw", "info")
            self._last_collect = {"role": role, "recipe_id": recipe_id, "path": path,
                                  "t": time.time()}
            bl.collect(recipe_id=recipe_id, role=role, path=path,
                       sample=prefix, main_folder=self._spec_data_dir,
                       temperature=self.temp.target,
                       exposure=self._spec_exposure, frames=self._spec_frames)
            # Report what ACTUALLY landed on disk. In mock mode without the
            # simulator, collect() is a no-op — saying "DONE" there is a lie that
            # sends you hunting for files that were never written.
            sim = getattr(bl, "simulator", None)
            recs = getattr(bl, "collections", None) or []
            sim_err = recs[-1].get("simulator_error") if recs else None
            if backend_at_dispatch != "real" and sim is None:
                self._log(f"📷 2D {role} collect — NO FILES WRITTEN. The simulated "
                          f"beamline only records the request; set "
                          f"spec.simulator.enabled: true in reactor/config.yml to "
                          f"generate synthetic 2D data.", "warn")
            elif sim_err:
                self._log(f"📷 2D {role} collect — NO FILES WRITTEN: {sim_err}",
                          "error")
            elif sim is not None and getattr(sim, "last", None):
                last = sim.last
                self._log(f"📷 2D {role.upper()} collect DONE — {last['n_frames']} "
                          f"frame(s) written to {last['detector_dir']} "
                          f"({time.time() - t_start:.0f}s)", "ok")
            else:
                self._log(f"📷 2D {role.upper()} collect DONE — {recipe_id} "
                          f"({time.time() - t_start:.0f}s)", "ok")
            self._event("reactor.spec_collect",
                        {"recipe_id": recipe_id, "role": role, "path": path})
        except Exception as exc:
            self._log(f"⚠ 2D {role} collect FAILED for {recipe_id}: {exc}", "error")
            self._log(f"   check: SPEC/bServer reachable, save folder writable "
                      f"({self._spec_data_dir or 'UNSET'}), detector armed", "error")

    def _check_mock_dir_writable(self) -> bool:
        """In mock mode the simulator writes with plain file I/O, so the save
        folder must exist locally. The shipped config points data_dir at the
        BEAMLINE path (/msd_data/...), which is unwritable on a laptop — catch
        that here instead of at the first frame."""
        from pathlib import Path                                  # noqa: PLC0415
        d = Path(self._spec_data_dir)
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".swaxs_write_test"
            probe.write_text("")
            probe.unlink()
            return True
        except Exception as exc:
            self._log(f"   ⛔ save folder is NOT writable: {d} ({exc.__class__.__name__}) "
                      f"— synthetic data cannot be saved. Point the Save folder at a "
                      f"local directory, or set spec.mock_data_dir in "
                      f"reactor/config.yml.", "error")
            return False
