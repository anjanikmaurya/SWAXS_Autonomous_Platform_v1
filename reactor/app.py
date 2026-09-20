"""
reactor/app.py — Autonomous Synthesis (port 5108)
==================================================
Pump-control / execution layer for the 5-pump continuous-flow nanoparticle
reactor (Fong et al., J. Chem. Phys. 154, 224201, 2021).  Receives an
already-predicted recipe (folder / JSON API / form) and drives the pumps; the
BO/SAXS optimization itself lives elsewhere.

All hardware + run logic is in src/reactor/.  This file is a thin Flask shell:
routes, SSE, the recipes-folder watcher, and the hub event-bus wiring.

Run:  python reactor/app.py    Open: http://localhost:5108
      (from the activated venv — see CLAUDE.md)
"""

from __future__ import annotations

import collections
import datetime
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response

# ── sys.path ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.favicon import register_favicon               # noqa: E402
from src.reactor import load_config, ReactorController, RecipeError   # noqa: E402
from src.reactor.config import hub_to_spec_dir                        # noqa: E402
from src.reactor.recipe import parse_param_file                       # noqa: E402
from src.reactor.intake import decide_intake                          # noqa: E402
from src.loop_naming import split_role, is_background                 # noqa: E402
from src.manifest import update_manifest, add_reactor_run            # noqa: E402

# ── Event bus (graceful degradation) ─────────────────────────────────────────
try:
    from src.events import EventBusClient as _EventBusClient
    _bus = _EventBusClient("reactor").connect(retry=True)
except Exception:
    _bus = None

app = Flask(__name__)

# Per-app browser-tab icon, from apps.yml — ten apps on ten ports
# otherwise give ten identical tabs. See src/favicon.py.
register_favicon(app, "reactor")

_project_root: str = os.environ.get("SWAXS_PROJECT", "")   # folder selected in the hub
_CFG = load_config()
# Normalise and validate: the pump layer and the beamline layer used to compare
# this string differently ("== 'real'" vs ".lower() == 'real'"), so a value like
# "REAL" gave a LIVE beamline with SIMULATED pumps. Fail closed instead.
_BACKEND = os.environ.get("SWAXS_REACTOR_BACKEND", "mock").strip().lower()
if _BACKEND not in ("mock", "real"):
    raise SystemExit(
        f"SWAXS_REACTOR_BACKEND must be 'mock' or 'real' (got "
        f"{os.environ.get('SWAXS_REACTOR_BACKEND')!r}). Refusing to start rather "
        f"than guess — an ambiguous value can mean live hardware.")

# ── logging to logs/reactor.log ──────────────────────────────────────────────
# The hub captures each app's stderr into logs/<app>.log. This app never
# configured logging, so the only logger with a handler was werkzeug's and the
# file held nothing but HTTP access lines. Two consequences, both found the
# hard way while diagnosing a lost condition:
#
#   * src.beamline.driver already passes the 2D simulator a
#     `log=lambda m: logger.info(...)` callback, so every acquisition announces
#     itself with the prefix, frame count, exposure and the TRUE R/PDI it is
#     generating — and every one of those lines was dropped by Python's
#     last-resort handler, which is WARNING-level. Run20's blank frames took a
#     folder-walking diagnostic tool to explain; one grep would have done it.
#   * the operator log (collect START/DONE, arming, faults, E-stop) lived only
#     in a 500-entry in-memory deque served over SSE, so it existed only while
#     a browser was watching. Nothing survived the night.
#
# Root stays at WARNING so pyFAI/matplotlib/urllib3 do not flood the file; only
# our own packages are raised to INFO.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
for _name in ("src.beamline", "src.simulator", "src.reactor", "reactor"):
    logging.getLogger(_name).setLevel(logging.INFO)

logger = logging.getLogger("reactor")

#: Operator-log tag → log level. Warnings and errors must not land in the file
#: as INFO, or grepping the overnight log for trouble finds nothing.
_TAG_LEVEL = {"error": logging.ERROR, "err": logging.ERROR,
              "warn": logging.WARNING, "warning": logging.WARNING}

# ── log buffer (fed by the controller, streamed over SSE) ─────────────────────
_log: collections.deque = collections.deque(maxlen=500)
_seq = 0
_log_lock = threading.Lock()


def _emit(msg: str, tag: str = "info") -> None:
    global _seq
    with _log_lock:
        _seq += 1
        _log.append((_seq, {"ts": datetime.datetime.now().strftime("%H:%M:%S"),
                            "msg": msg, "tag": tag}))
    # Tee to the file. Outside the lock: a blocked write must not stall the
    # controller thread that called us, and never raise for the same reason.
    try:
        logger.log(_TAG_LEVEL.get(str(tag).lower(), logging.INFO), "%s", msg)
    except Exception:
        pass


def _sync_data_dir_from_hub(folder: str) -> None:
    """When the hub folder changes, update the SPEC data_dir to follow it. The hub
    folder is a Windows path; SPEC needs the matching Linux path, so translate via
    spec.hub_path_map. If data_dir_from_hub is off or the path can't be mapped,
    fall back to seeding data_dir only if it's still unset (never send SPEC a bad path).

    MOCK backend: no SPEC is involved and the 2D simulator writes with local file
    I/O, so the beamline path translation is skipped entirely and the hub folder
    is used verbatim. Translating it would hand the simulator a Linux beamline
    path like /msd_data/... that doesn't exist on this machine.
    """
    spec = _CFG.get("spec", {}) or {}
    if not folder:
        return

    if str(getattr(_ctrl, "backend", "mock")).lower() != "real":
        override = str(spec.get("mock_data_dir", "") or "").strip()
        target = override or folder
        _ctrl.set_data_dir(target)
        return

    if spec.get("data_dir_from_hub", True):
        mapped = hub_to_spec_dir(folder, spec.get("hub_path_map"))
        if mapped:
            _ctrl.set_data_dir(mapped)          # follow the hub (translated)
            return
        _emit("⚠ hub folder changed but couldn't map it to a SPEC path "
              "(check spec.hub_path_map) — data_dir left as-is", "warn")
    _ctrl.default_data_dir(folder)              # fallback: seed only if unset


def _resolve(folder_key: str) -> Path:
    """Resolve a config folder against the project root (or CWD)."""
    rel = _CFG.get("folders", {}).get(folder_key, folder_key)
    p = Path(rel)
    if not p.is_absolute():
        base = Path(_project_root) if _project_root else Path.cwd()
        p = base / rel
    return p





# ── recipes-folder watcher state ─────────────────────────────────────────────
# Declared HERE, above every function that touches it. _clear_stale_conditions
# runs during module execution — i.e. before the bottom of this file has been
# reached — so with these defined further down it raised NameError straight
# into a broad `except`, which reported "could not clear leftover conditions"
# and swallowed the fact that the files HAD already been moved. A start-up
# helper cannot reference state declared after it.
#
# A file is ingested only once it is STABLE (size+mtime unchanged across two
# polls), so a recipe still being written by the ML pipeline is never parsed
# mid-write and lost. Handled files are remembered by signature, so a corrected
# re-write of the same filename is picked up again.
_watch_handled: dict = {}    # path -> signature of the version already ingested/rejected
_watch_lastsig: dict = {}    # path -> signature seen on the previous poll


# ── controller callbacks ──────────────────────────────────────────────────────
def _event_cb(etype: str, data: dict) -> None:
    if _bus is not None:
        try:
            _bus.publish(etype, data)
        except Exception:
            pass


def _retire_condition_file(source: str, why: str = "") -> Path | None:
    """Move a consumed condition file out of the watched folder into processed/.

    THE FILE IS THE DURABLE QUEUE. It used to be moved the instant the watcher
    parsed it, which had two consequences the operator hit directly:

      * turning **Run autonomously** OFF did not stop the reactor taking work.
        The watcher runs regardless of the toggle (by design — the toggle
        decides whether a recipe STARTS, not whether it is read), so conditions
        kept being swallowed out of Conditions/ into done/ while nothing ran;
      * those queued conditions then existed ONLY in memory. The source files
        were already in done/, so an app restart lost them from both places,
        silently.

    So a file now leaves Conditions/ when the reactor is FINISHED with it —
    it ran, it was abandoned, or the operator cleared it from the queue. Until
    then it stays on disk, in order, and a restart simply re-reads it.

    ``source`` is the recipe's ``folder:<name>``; anything else (the manual
    form, POST /api/recipe) has no file and is a no-op. Returns the new path.
    """
    if not str(source).startswith("folder:"):
        return None
    name = str(source).split("folder:", 1)[1]
    try:
        src_file = _resolve("recipes") / name
        done_dir = _resolve("processed")
        done_dir.mkdir(parents=True, exist_ok=True)
        dest = done_dir / name
        if src_file.is_file():
            # replace() (not rename()) overwrites an existing dest — rename()
            # raises on Windows if done/<name> already exists.
            src_file.replace(dest)
            _watch_handled.pop(str(src_file), None)   # moved away; a re-drop is new
            _watch_lastsig.pop(str(src_file), None)
            if why:
                _emit(f"📁 {name} → done/ ({why})", "info")
        return dest if dest.is_file() else None
    except Exception as exc:
        _emit(f"⚠ could not move {name} to done/: {exc}", "warn")
        return None


def _feedback_cb(recipe_id: str, payload: dict) -> None:
    """Write <recipe_id>.done.json so the BO/SAXS side knows the run finished."""
    try:
        fb = _resolve("feedback")
        fb.mkdir(parents=True, exist_ok=True)
        (fb / f"{recipe_id}.done.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        # The reactor is finished with this condition (it ran, or it was
        # abandoned), so retire its file now — see _retire_condition_file.
        rec = payload.get("recipe") or {}
        status = str(payload.get("status", "ran"))
        done_file = _retire_condition_file(str(rec.get("source", "")), status)
        # append the measured flow-sensor readings as a footer to the consumed
        # condition file — commanded vs delivered.
        if done_file is not None:
            sp = payload.get("setpoints", {})
            meas = payload.get("measured_flows", {})
            head = ("# ── RESULT (measured, appended by reactor) ──────────────"
                    if status == "ran" else
                    "# ── NOT RUN (appended by reactor) ───────────────────────")
            foot = ["", head,
                    f"# ended:       {datetime.datetime.now().isoformat(timespec='seconds')}",
                    f"# status:      {status}",
                    f"# duration_s:  {payload.get('duration_s')}",
                    f"# reason:      {payload.get('reason')}"]
            for pump in sp:
                foot.append(f"# {pump}: setpoint={sp.get(pump)} measured={meas.get(pump)} uL/min")
            with done_file.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(foot) + "\n")
    except Exception as exc:
        _emit(f"⚠ could not write feedback file: {exc}", "warn")


def _manifest_cb(record: dict) -> None:
    if _project_root:
        try:
            update_manifest(_project_root, lambda m: add_reactor_run(m, record=record))
        except Exception as exc:
            _emit(f"⚠ manifest update failed: {exc}", "warn")


try:
    _ctrl = ReactorController(_CFG, backend=_BACKEND, log_cb=_emit,
                              event_cb=_event_cb, feedback_cb=_feedback_cb,
                              manifest_cb=_manifest_cb,
                              # The E-stop disables auto-run. Without this the
                              # saved state still said ON, so the restart
                              # banner — and run.resume_auto_run — acted on a
                              # value the E-stop had already revoked.
                              auto_run_cb=lambda on: _persist_auto_run(on))
except Exception as exc:
    print("\n[Autonomous Synthesis] Startup failed:\n  " + str(exc) +
          "\n\nIn real mode, close the Dolomite GUI and any other program using "
          "the pump COM ports, then restart.\n", file=sys.stderr)
    sys.exit(1)
_emit(f"Autonomous Synthesis ready — backend={_BACKEND}", "ok")

# ── handing the rig back on exit ─────────────────────────────────────────────
# shutdown() idles the pumps, closes the shutter and releases SPEC remote
# control so beamline staff can drive SPEC again.
#
# atexit ALONE WAS NOT ENOUGH, and the gap was the worst kind (audit R1):
# atexit handlers do not run on SIGTERM, and SIGTERM is exactly how the hub
# stops every app (src/proc_lifecycle.kill_tree — SIGTERM, then SIGKILL after
# 5 s). So pressing Stop on the reactor card in the hub left every pump holding
# its last commanded flow with NOTHING supervising it — the control loop was
# gone, so the over-temperature, over-pressure, volume and flow-fault checks
# were gone with it — and left SPEC locked to a process that no longer existed.
#
# Both paths are wired, and _shutdown_once makes them idempotent so the signal
# handler running first does not mean atexit repeats the work on the way out.
import atexit as _atexit                                             # noqa: E402
import signal as _signal                                             # noqa: E402

_shutdown_done = threading.Event()


def _shutdown_once(why: str = "exit") -> None:
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    try:
        _emit(f"⏻ shutting down ({why}) — idling pumps, closing the shutter, "
              f"releasing SPEC control", "warn")
    except Exception:
        pass
    try:
        _ctrl.shutdown()
    except Exception:
        pass


def _on_signal(signum, _frame):
    _shutdown_once(_signal.Signals(signum).name)
    # Restore the default disposition and re-raise, so the process still dies
    # the way the sender asked and the hub sees the exit it expects.
    try:
        _signal.signal(signum, _signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except Exception:
        os._exit(0)


_atexit.register(_shutdown_once, "atexit")
for _sig in (_signal.SIGTERM, _signal.SIGINT):
    try:
        _signal.signal(_sig, _on_signal)
    except (ValueError, OSError):
        # Not the main thread (a test importing this module, say) — atexit
        # still covers the polite path.
        pass
# Point the SPEC save folder at the hub's project folder at startup (translated
# Windows→Linux via spec.hub_path_map). The user can still override in the app.
# ── restart recovery (platform audit O1) ─────────────────────────────────────
# DELIBERATELY DIFFERENT from the data-processing apps: those just read and write
# files, so they resume automatically. Auto-run moves PUMPS. A power blip must not
# cause reagents to flow into a hot reactor with nobody in the hutch, so we only
# REPORT that auto-run was on and wait for a human to press Start.
# Set run.resume_auto_run: true once you trust it for a long unattended campaign.
def _persist_auto_run(on: bool) -> None:
    try:
        from src.runstate import save_state
        save_state(_project_root, "reactor_auto", {"auto_run": bool(on)})
    except Exception:
        pass


def _restore_auto_run() -> None:
    try:
        from src.runstate import load_state
        st = load_state(_project_root, "reactor_auto", max_age_s=24 * 3600)
        if not st or not st.get("auto_run"):
            return
        if bool((_CFG.get("run", {}) or {}).get("resume_auto_run", False)):
            _ctrl.set_auto_run(True)
            _emit("♻ auto-run RESUMED after restart (run.resume_auto_run: true) — "
                  "pumps may start as soon as a condition is queued", "warn")
        else:
            _emit("⚠ auto-run was ON before this restart. It is NOT resumed "
                  "automatically because it moves pumps — check the rig, then "
                  "press Start (or set run.resume_auto_run: true).", "warn")
    except Exception:
        pass


#: The live run settings (arming mode/wait, synthesis duration, flush rate and
#: duration) were IN-MEMORY ONLY. So an operator who set 60 s synthesis + 60 s
#: flush in the app got those values until the next restart — after which the
#: reactor silently fell back to reactor/config.yml (600 s and 1200 s), and a
#: resumed autonomous run used the long defaults with nothing to say so.
_RUN_SETTINGS_STATE = "reactor_run_settings"


def _save_run_settings(d: dict) -> None:
    try:
        from src.runstate import save_state
        save_state(_project_root, _RUN_SETTINGS_STATE, dict(d or {}))
    except Exception:
        pass


#: The beamline data-collection settings (exposure, frames, trigger-before-end,
#: the sample/background keywords, the SPEC save folder) were IN-MEMORY ONLY,
#: exactly as the run settings once were: an operator who set exposure 20 s × 5
#: got those until the next restart, after which the reactor silently reverted
#: to reactor/config.yml. Persist them the same way.
_SPEC_SETTINGS_STATE = "reactor_spec_settings"


def _save_spec_settings(d: dict) -> None:
    try:
        from src.runstate import save_state
        # keep only the fields set_spec_settings understands; a blank value is
        # dropped so it never overwrites a good stored one with nothing.
        keep = ("exposure_s", "frames", "spec_lead_s",
                "sample_tag", "bkg_tag", "data_dir")
        clean = {k: d[k] for k in keep
                 if k in (d or {}) and str(d.get(k)).strip() != ""}
        if clean:
            save_state(_project_root, _SPEC_SETTINGS_STATE, clean)
    except Exception:
        pass


def _restore_spec_settings() -> None:
    """Re-apply the beamline data-collection settings after a restart.

    Same contract as _restore_run_settings: read unconditionally
    (honour_no_resume=False) so the displayed and executed acquisition matches
    what the operator last chose, rather than reverting to config defaults with
    the UI still showing the old values. set_spec_settings validates each field
    (e.g. exposure_s > 0), so a corrupt stored value is refused, not trusted."""
    try:
        from src.runstate import load_state
        st = load_state(_project_root, _SPEC_SETTINGS_STATE, max_age_s=48 * 3600,
                        honour_no_resume=False)
        if st:
            st = {k: v for k, v in st.items()
                  if not k.startswith("_") and v is not None}
        if st:
            ok, msg = _ctrl.set_spec_settings(st)
            if ok:
                _emit("♻  data-collection settings restored: " + msg, "ok")
                # Fold the restored beamline params into the SAME restart banner
                # the run settings raised, so the operator sees one list to
                # review — exposure/frames/lead/tags/dir alongside arm mode and
                # flush — instead of the beamline half being silently restored.
                global _RESTART_NOTICE
                if _RESTART_NOTICE.get("level") == "restored":
                    merged = sorted(set(_RESTART_NOTICE.get("params", [])) | set(st))
                    _RESTART_NOTICE = {
                        **_RESTART_NOTICE,
                        "message": "Run and data-collection settings were restored "
                                   "from your last session. Review arm mode, run "
                                   "duration, flush and the beamline exposure / "
                                   "frames / trigger before starting.",
                        "params": merged}
                else:
                    # run settings weren't restored (fresh, or lost) but the
                    # beamline ones were — still tell the operator.
                    _RESTART_NOTICE = {
                        "level": "restored",
                        "message": "Data-collection settings were restored from "
                                   "your last session. Review exposure, frames and "
                                   "trigger-before-end before starting.",
                        "params": sorted(st.keys())}
    except Exception as exc:
        _emit(f"⚠ could not restore the data-collection settings: {exc}", "warn")


#: Restart notice for the UI banner. Two tiers, deliberately distinct:
#:   "restored" — last session's values came back; a calm "review before running".
#:   "lost"     — a saved file existed but could NOT be restored (too old/unreadable),
#:                so a run would use CONFIG DEFAULTS; a loud warning naming what reset.
#: "none" = genuine fresh start (nothing saved) — no banner, so the banner stays
#: meaningful instead of firing on every restart.
_RESTART_NOTICE: dict = {"level": "none", "message": "", "params": []}


def _restore_run_settings() -> None:
    """Re-apply the operator's own run settings after a restart.

    RESTORING VALUES is decoupled from RESUMING the loop: settings are read
    unconditionally (``honour_no_resume=False``) so the displayed/executed values
    match after any restart, while AUTO-RUN stays behind the resume policy and the
    human Start (see ``_restore_auto_run``). Without this the restore code ran
    only under SWAXS_RESUME=1 and was inert on a normal restart — the reactor
    silently reverted to config defaults while the UI showed the old values."""
    global _RESTART_NOTICE
    try:
        from src.runstate import load_state, state_path
        p = state_path(_project_root, _RUN_SETTINGS_STATE)
        existed = bool(p and p.is_file())
        st = load_state(_project_root, _RUN_SETTINGS_STATE, max_age_s=48 * 3600,
                        honour_no_resume=False)
        # drop runstate's own bookkeeping keys (_saved_at) before replaying
        if st:
            st = {k: v for k, v in st.items()
                  if not k.startswith("_") and v is not None}
        if st:
            _ctrl.set_run_settings(st)
            _RESTART_NOTICE = {
                "level": "restored",
                "message": "Run settings were restored from your last session. "
                           "Review arm mode, run duration and flush before starting.",
                "params": sorted(st.keys())}
            _emit("♻  run settings restored: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(st.items())), "ok")
        elif existed:
            # A saved file was there but load_state refused it (stale >48 h or
            # unreadable). Do NOT silently run on defaults — shout, and name it.
            _RESTART_NOTICE = {
                "level": "lost",
                "message": "Saved run settings could NOT be restored (too old or "
                           "unreadable). The reactor is on CONFIG DEFAULTS — set "
                           "arm mode, run duration and flush before starting.",
                "params": ["arm_mode", "run_duration", "flush_rate", "flush_duration"]}
            _emit("⚠ saved run settings could not be restored — running on config "
                  "defaults; set them before starting", "warn")
        # else: nothing was ever saved — a genuine fresh start, no banner.
    except Exception as exc:
        _emit(f"⚠ could not restore the run settings: {exc}", "warn")



# ── persisted per-project settings (pump limits, conditions folder) ─────────
# Defined HERE, above the startup block, because that block now calls them
# (audit R7). They used to live below the routes, which is why the only
# caller could be /api/set_project.
def _limits_path() -> Path | None:
    return Path(_project_root) / "reactor_limits.json" if _project_root else None


def _save_limits(limits: dict) -> None:
    p = _limits_path()
    if p is None:
        return
    try:
        p.write_text(json.dumps({"limits": limits}, indent=2), encoding="utf-8")
    except Exception as exc:
        _emit(f"⚠ could not save reactor_limits.json: {exc}", "warn")


def _load_limits() -> None:
    p = _limits_path()
    if p is None or not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}").get("limits", {})
        if data:
            _ctrl.set_pump_limits(data)
            _emit(f"loaded saved pump flow limits for {len(data)} pump(s)", "info")
    except Exception as exc:
        _emit(f"⚠ could not load reactor_limits.json: {exc}", "warn")


def _settings_path() -> Path | None:
    return Path(_project_root) / "reactor_settings.json" if _project_root else None


def _save_recipes_folder(folder: str) -> None:
    p = _settings_path()
    if p is None:
        return
    try:
        cur = json.loads(p.read_text(encoding="utf-8") or "{}") if p.is_file() else {}
        cur["recipes_folder"] = folder
        p.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except Exception as exc:
        _emit(f"⚠ could not save reactor_settings.json: {exc}", "warn")


def _clear_stale_conditions() -> int:
    """Start each session with an empty queue — set aside anything left over.

    Condition files now stay in the watched folder until the reactor has
    finished with them (R26), which is what makes a queue survive a crash. The
    operator asked for the opposite on a DELIBERATE restart: stop the app from
    the hub, start it again, and begin from a clean slate rather than
    inheriting whatever the optimizer had proposed before.

    Both are right, for different reasons, and the difference is intent — but
    a process cannot tell a crash from a hub Stop after the fact, so this takes
    the operator's instruction literally: EVERY start clears. Clearing the
    in-memory queue alone would achieve nothing, because the watcher would
    re-read the same files within one poll; the files have to be set aside too.

    NOTHING IS DELETED. Files are moved to the processed folder with a line
    saying why, so a condition can be put back by moving it out again, and the
    count is logged loudly rather than slipping past in a quiet start-up.

    Turn it off with ``run.clear_queue_on_restart: false`` to get the
    crash-resumes-where-it-left-off behaviour instead.
    """
    if not bool((_CFG.get("run", {}) or {}).get("clear_queue_on_restart", True)):
        return 0
    try:
        rdir = _resolve("recipes")
        if not rdir.is_dir():
            return 0
        stale = sorted(list(rdir.glob("*.dat")) + list(rdir.glob("*.txt"))
                       + list(rdir.glob("*.json")))
        if not stale:
            return 0
        done = _resolve("processed")
        done.mkdir(parents=True, exist_ok=True)
        moved = []
        for f in stale:
            try:
                dest = done / f.name
                f.replace(dest)
                with dest.open("a", encoding="utf-8") as fh:
                    fh.write(f"\n# ── NOT RUN — cleared when the reactor app started "
                             f"at {datetime.datetime.now().isoformat(timespec='seconds')}\n"
                             f"# Every app start begins with an empty queue "
                             f"(run.clear_queue_on_restart). Move this file back "
                             f"into the conditions folder to run it.\n")
                moved.append(f.name)
            except Exception as exc:
                _emit(f"⚠ could not set aside {f.name}: {exc}", "warn")
        _watch_handled.clear()
        _watch_lastsig.clear()
        if moved:
            _emit(f"🧹 started with an empty queue — set aside "
                  f"{len(moved)} leftover condition(s) from the previous session "
                  f"({', '.join(moved[:6])}{' …' if len(moved) > 6 else ''}). "
                  f"They are in {done} and were NOT run; move one back to run it.",
                  "warn")
        return len(moved)
    except Exception as exc:
        _emit(f"⚠ could not clear leftover conditions: {exc}", "warn")
        return 0


def _load_recipes_folder() -> None:
    p = _settings_path()
    if p is None or not p.is_file():
        return
    try:
        f = json.loads(p.read_text(encoding="utf-8") or "{}").get("recipes_folder")
        if f:
            _CFG.setdefault("folders", {})["recipes"] = f
            _CFG["folders"]["processed"] = str(Path(f) / "done")
            _emit(f"conditions folder set to: {f}", "info")
    except Exception as exc:
        _emit(f"⚠ could not load reactor_settings.json: {exc}", "warn")


if _project_root:
    # The project root holds config.yml (poni_files / detector_shapes). The 2D
    # simulator needs it to reuse the SAME geometry the reduction app uses —
    # without it, frames were generated with a synthetic fallback geometry.
    _ctrl.set_project_root(_project_root)
    _sync_data_dir_from_hub(_project_root)
    # PERSISTED SETTINGS, BEFORE ANYTHING CAN RUN (audit R7).
    #
    # These two were reachable only from /api/set_project, which the hub POSTs
    # only when the folder CHANGES while apps are already running — on launch
    # it just puts SWAXS_PROJECT in the child's environment. So on every normal
    # start they never ran, and:
    #
    #   * the conditions-folder override silently reverted to config.yml,
    #     making reactor/knowledge.md's "reloaded on the next start" false;
    #   * worse, PUMP FLOW LIMITS reverted too — and those are the hard limits
    #     that reject an unsafe recipe at intake. An operator who narrowed a
    #     limit after a bad batch got it back only if they happened to re-pick
    #     the project folder in the hub.
    #
    # Ordered before the run settings and auto-run restore for the same reason
    # those are ordered before each other: nothing may start a recipe until
    # every persisted safety value is in force.
    _load_limits()
    _load_recipes_folder()
    _clear_stale_conditions()
    # Restore the operator's run settings BEFORE auto-run may start a recipe, so a
    # resumed campaign uses the durations they actually chose.
    _restore_run_settings()
    _restore_spec_settings()
    _restore_auto_run()


# ── hub bus: end the run when SAXS produces a new averaged file ───────────────
def _on_bus_event(event: dict) -> None:
    etype = event.get("type") or event.get("event_type") or ""
    data = event.get("data", event)
    if etype == "file.averaged":
        # Correlate the averaged file to the running recipe so a late/duplicate
        # event from a previous (pipelined) recipe can't truncate the current run.
        # The averaging keyword is "{recipe_id}_{role}"; fall back to the filename.
        fp = str(data.get("file_path", ""))
        key = str(data.get("keyword") or "") or Path(fp).name
        rid, _role = split_role(key)
        # A background/blank average never ends a synthesis run — only the sample does.
        if is_background(key):
            return
        _ctrl.signal_measurement_complete(fp, recipe_id=rid or "")


if _bus is not None:
    try:
        _bus.on_event(_on_bus_event)
    except Exception:
        pass


# ── recipes-folder watcher (backstop to the API) ─────────────────────────────
# A file is ingested only once it is STABLE (size+mtime unchanged across two
# polls), so a recipe still being written by the ML pipeline is never parsed
# mid-write and lost. Handled files are remembered by signature, so a corrected
# re-write of the same filename is picked up again.


def _folder_watcher() -> None:
    interval = float(_CFG.get("poll_interval", 3.0))
    while True:
        try:
            rdir = _resolve("recipes")
            try:
                rdir.mkdir(parents=True, exist_ok=True)   # create the folder if missing
            except Exception:
                pass
            if rdir.is_dir():
                # .dat/.txt (ML pipeline params) and .json (app format), oldest first
                # Oldest first, with the FILENAME as the tie-break. Sorting on
                # mtime alone left the order to the filesystem whenever two
                # conditions shared a timestamp — three files written in the
                # same instant came back r001, r003, r002 — and a campaign is
                # supposed to run the optimizer's proposals in the order it
                # proposed them.
                files = sorted(list(rdir.glob("*.dat")) + list(rdir.glob("*.txt"))
                               + list(rdir.glob("*.json")),
                               key=lambda p: (p.stat().st_mtime, p.name))
                present = set()
                for f in files:
                    key = str(f)
                    present.add(key)
                    try:
                        st = f.stat()
                        sig = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        continue
                    action = decide_intake(key, sig, _watch_handled, _watch_lastsig)
                    if action == "skip":
                        continue
                    if action == "wait":
                        _watch_lastsig[key] = sig   # (new or still changing) re-check next poll
                        continue
                    # action == "go": file is stable and not yet handled
                    try:
                        text = f.read_text(encoding="utf-8")
                        if f.suffix.lower() == ".json":
                            data = json.loads(text or "{}")
                        else:
                            data = parse_param_file(text)
                        data.setdefault("recipe_id", f.stem)
                        _ctrl.submit(data, source=f"folder:{f.name}")
                        # DO NOT move the file here. It is retired only once the
                        # reactor is finished with the condition — see
                        # _retire_condition_file. Marking it handled is what
                        # stops it being re-ingested on every poll while it
                        # waits in the queue; the file staying put is what makes
                        # the queue survive a restart, and what stops an
                        # auto-run-off reactor from quietly emptying the folder.
                        _watch_handled[key] = sig
                        _watch_lastsig.pop(key, None)
                    except RecipeError as e:
                        _emit(f"✗ rejected {f.name}: {e}", "error")
                        _watch_handled[key] = sig       # genuinely bad — don't retry this version
                        _watch_lastsig.pop(key, None)
                    except Exception as e:
                        _emit(f"⚠ {f.name}: {e}", "warn")
                        _watch_handled[key] = sig       # stable but unreadable — don't loop
                        _watch_lastsig.pop(key, None)
                # forget state for files that have vanished (moved/deleted)
                for k in [k for k in _watch_lastsig if k not in present]:
                    _watch_lastsig.pop(k, None)
                for k in [k for k in _watch_handled if k not in present]:
                    _watch_handled.pop(k, None)
        except Exception:
            pass
        time.sleep(interval)


threading.Thread(target=_folder_watcher, daemon=True).start()


# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    s = _ctrl.status()
    return jsonify({"status": "ok", "app": "reactor",
                    "state": s["state"], "queue": s["queue_len"],
                    "runs": s["runs_completed"]})


@app.route("/api/recipes_folder", methods=["GET", "POST"])
def api_recipes_folder():
    """GET the watched conditions folder; POST {folder} to change it live."""
    if request.method == "POST":
        b = request.get_json(force=True) or {}
        folder = str(b.get("folder", "")).strip()
        if not folder:
            return jsonify({"ok": False,
                            "error": "give a folder path",
                            "folder": _CFG.get("folders", {}).get("recipes", ""),
                            "resolved": str(_resolve("recipes"))}), 400
        # Refuse a path that is not already there (audit R20). The watcher
        # CREATES its folder if missing, so a typo used to be accepted in
        # silence: an empty directory appeared, the campaign stopped receiving
        # conditions, and nothing anywhere said so. A relative path is resolved
        # against the project root first, exactly as the watcher will.
        probe = Path(folder)
        if not probe.is_absolute():
            probe = (Path(_project_root) if _project_root else Path.cwd()) / folder
        if not probe.is_dir():
            _emit(f"⚠ conditions folder NOT changed — {probe} does not exist. "
                  f"Create it first, or check the spelling.", "warn")
            return jsonify({"ok": False,
                            "error": f"no such folder: {probe}",
                            "folder": _CFG.get("folders", {}).get("recipes", ""),
                            "resolved": str(_resolve("recipes"))}), 400
        if True:
            _CFG.setdefault("folders", {})["recipes"] = folder
            _CFG["folders"]["processed"] = str(Path(folder) / "done")
            # re-scan the new folder from scratch (these are the real watcher
            # caches; _watch_seen no longer exists and raised NameError here)
            _watch_handled.clear()
            _watch_lastsig.clear()
            _save_recipes_folder(folder)
            _emit(f"📁 conditions folder → {folder}", "info")
    return jsonify({"ok": True,
                    "folder": _CFG.get("folders", {}).get("recipes", ""),
                    "resolved": str(_resolve("recipes"))})


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    body = request.get_json(force=True)
    p = (body.get("path", "") or "").strip()
    if p and not Path(p).is_dir():
        # The hub checks this before propagating, but the route is reachable
        # directly and a bad root silently misdirects the 2D save folder and
        # every persisted settings file (audit R25).
        _emit(f"⚠ refused project folder that does not exist: {p}", "warn")
        return jsonify({"ok": False, "error": f"not a folder: {p}"}), 400
    if p:
        os.environ["SWAXS_PROJECT"] = p
        _project_root = p
        _ctrl.set_project_root(p)    # geometry source for the 2D simulator
        _sync_data_dir_from_hub(p)   # follow the hub folder into SPEC data_dir
        _load_limits()          # pick up saved per-pump flow limits
        _load_recipes_folder()  # pick up saved conditions-folder override
    return jsonify({"ok": True})


@app.route("/api/pumps", methods=["GET", "POST"])
def api_pumps():
    """GET current per-pump flow limits; POST {limits:{pump:{sensor_min,max_flow}}}."""
    if request.method == "POST":
        body = request.get_json(force=True)
        try:
            out = _ctrl.set_pump_limits(body.get("limits", {}))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        _save_limits(out)
        return jsonify({"ok": True, "limits": out})
    return jsonify({"limits": _ctrl.pump_limits()})


@app.route("/api/project")
def api_project():
    return jsonify({"project_root": _project_root})


@app.route("/api/config")
def api_config():
    """Expose bounds / pump names / flush defaults for the UI form."""
    from src.reactor.config import PUMP_NAMES
    return jsonify({"pumps": PUMP_NAMES, "bounds": _CFG.get("bounds", {}),
                    "flush": _CFG.get("flush", {}), "backend": _BACKEND})


@app.route("/api/recipe", methods=["POST"])
def api_recipe():
    """Submit a recipe as JSON (BO/SAXS push) or form fields."""
    data = request.get_json(silent=True) or request.form.to_dict()
    src = "form" if request.form else "api"
    try:
        out = _ctrl.submit(data, source=src)
        return jsonify({"ok": True, **out})
    except RecipeError as e:
        _emit(f"✗ rejected recipe: {e}", "error")
        return jsonify({"ok": False, "error": str(e)}), 400


def _simple(fn, refused: str = "the reactor refused — check its state"):
    """Run a controller action that answers True/False.

    ``refused`` matters: a bare ``{"ok": false}`` with no message is invisible
    in the UI, because post() only renders r.error. That is how Flush now came
    to be a silent no-op in four of six states (audit R16)."""
    try:
        ok = bool(fn())
        return jsonify({"ok": ok} if ok else {"ok": False, "error": refused})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def _acted(result):
    """Render an (acted, reason) pair from the controller. `acted` False is not
    an error — the button was pressed in a state where it does nothing — but it
    must still reach the operator rather than reporting a green tick."""
    ok, why = result
    return jsonify({"ok": bool(ok), "error": None if ok else why,
                    "note": why if ok else None})


@app.route("/api/start", methods=["POST"])
def api_start():
    return _simple(_ctrl.start,
                   "nothing to start — the queue is empty, or a run is already "
                   "in progress")


@app.route("/api/abort", methods=["POST"])
def api_abort():   return _acted(_ctrl.abort())


@app.route("/api/estop", methods=["POST"])
def api_estop():
    # NEVER report a bare success here: if a pump could not be idled the operator
    # must see it, not a green tick.
    failed = _ctrl.estop() or []
    return jsonify({"ok": not failed, "failed_to_idle": failed,
                    "error": (f"could not idle: {', '.join(failed)} — CHECK THESE "
                              f"PUMPS IMMEDIATELY") if failed else None})


@app.route("/api/reset", methods=["POST"])
def api_reset():   return _acted(_ctrl.reset())


@app.route("/api/vent", methods=["POST"])
def api_vent():
    # Reports which pumps (if any) refused to idle, for the same reason the
    # E-stop route does — venting is the other path where "done" must not be
    # printed over a pump that is still delivering.
    failed = _ctrl.vent_all() or []
    return jsonify({"ok": not failed, "failed_to_idle": failed,
                    "error": (f"vented, but could NOT idle: {', '.join(failed)} "
                              f"— check these pumps") if failed else None})


@app.route("/api/backend", methods=["POST"])
def api_backend():
    mode = str((request.get_json(silent=True) or {}).get("backend", "")).lower()
    ok, msg = _ctrl.switch_backend(mode)
    if ok and _project_root:
        # mock ⇄ real changes where data should be written (local hub folder vs
        # the translated beamline path), so re-resolve the save folder.
        _sync_data_dir_from_hub(_project_root)
    return jsonify({"ok": ok, "backend": _ctrl.backend, "error": None if ok else msg})


@app.route("/api/start_now", methods=["POST"])
def api_start_now():
    ok = _ctrl.start_now()
    return jsonify({"ok": bool(ok),
                    "error": None if ok else "not arming — nothing to skip"})


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    removed = _ctrl.clear_queue()
    # Retire their files too. A folder-sourced condition keeps its file in the
    # watched folder until the reactor is finished with it, so without this the
    # cleared conditions would simply be re-ingested on the next restart and
    # Clear queue would not have cleared anything durable.
    for d in removed:
        _retire_condition_file(d.get("source", ""), "cleared from the queue")
    return jsonify({"ok": True, "cleared": len(removed),
                    "recipe_ids": [d.get("recipe_id") for d in removed]})


@app.route("/api/flush", methods=["POST"])
def api_flush():
    b = request.get_json(silent=True) or {}
    rate = b.get("rate"); dur = b.get("duration")
    ok = _ctrl.flush_now(float(rate) if rate else None, float(dur) if dur else None)
    # A refusal used to be a bare {"ok": false} with no message, and the UI only
    # renders r.error — so in the four states where flush_now returns False the
    # button was a silent no-op that read as success (audit R16).
    return jsonify({"ok": ok, "error": None if ok else
                    (f"can't flush while {_ctrl.status()['state']} — a flush runs "
                     f"only from idle or ready")})


@app.route("/api/auto_run", methods=["POST"])
def api_auto_run():
    b = request.get_json(force=True)
    _ctrl.set_auto_run(bool(b.get("on", False)))
    _persist_auto_run(_ctrl.auto_run)      # so a restart can REPORT it (not resume it)
    return jsonify({"ok": True, "auto_run": _ctrl.auto_run})


@app.route("/api/spec_settings", methods=["POST"])
def api_spec_settings():
    body = request.get_json(silent=True) or {}
    ok, msg = _ctrl.set_spec_settings(body)
    # Persist only what the controller accepted, so a restart keeps the
    # beamline settings instead of reverting to config.yml. Nothing is saved on
    # a refusal (409) — a rejected value must not become the stored one.
    if ok:
        _save_spec_settings(body)
    # Refused while a run or campaign is in flight. 409, not 400: the request
    # is well-formed, it just conflicts with the current state.
    return jsonify({"ok": ok} if ok else {"ok": False, "error": msg}), \
        (200 if ok else 409)


@app.route("/api/collect_now", methods=["POST"])
def api_collect_now():
    role = str((request.get_json(silent=True) or {}).get("role", "sample"))
    ok, msg = _ctrl.collect_now(role)
    return jsonify({"ok": ok, "error": None if ok else msg})


@app.route("/api/run_settings", methods=["POST"])
def api_run_settings():
    b = request.get_json(force=True) or {}
    _ctrl.set_run_settings(b)
    # persist, so a restart does not quietly revert to the config defaults
    _save_run_settings(b)
    return jsonify({"ok": True})


@app.route("/api/tare", methods=["POST"])
def api_tare():
    b = request.get_json(force=True) or {}
    ok, msg = _ctrl.tare_pump(str(b.get("pump", "")), kind=str(b.get("kind", "pressure")))
    # "error" as well as "msg": the UI's generic post() handler only looks at
    # r.error, and tare() threw the whole reply away, so a refusal ("can't tare
    # while running") never reached the operator at all (audit R16).
    return jsonify({"ok": ok, "msg": msg, "error": None if ok else msg})


#: status() takes the controller lock and calls beamline.is_collecting(), and
#: /api/stream rebuilt it twice a second FOR EVERY CONNECTED BROWSER (audit
#: R22) — five tabs left open overnight is ten of those a second competing with
#: the control loop for the same lock. One shared snapshot, refreshed at most
#: this often, serves every client and every poll.
_STATUS_TTL_S = 0.4
_status_cache: tuple = (0.0, None)
_status_lock = threading.Lock()


def _status_cached() -> dict:
    global _status_cache
    now = time.time()
    ts, snap = _status_cache
    if snap is not None and (now - ts) < _STATUS_TTL_S:
        return snap
    with _status_lock:
        ts, snap = _status_cache            # re-check: another thread may have
        if snap is not None and (time.time() - ts) < _STATUS_TTL_S:
            return snap                     # refreshed it while we waited
        snap = _ctrl.status()
        _status_cache = (time.time(), snap)
        return snap


@app.route("/api/status")
def api_status():
    return jsonify(_status_cached())


@app.route("/api/restart_notice")
def api_restart_notice():
    """Whether run settings were restored, lost, or this is a fresh start — the
    UI renders a two-tier banner from this so displayed never silently != executed."""
    return jsonify(_RESTART_NOTICE)


@app.route("/api/stream")
def api_stream():
    """SSE: pushes {status, logs[]} ~2×/s."""
    def gen():
        last = 0
        while True:
            with _log_lock:
                new = [ln for (s, ln) in _log if s > last]
                if _log:
                    last = _log[-1][0]
            yield "data: " + json.dumps({"status": _status_cached(), "logs": new}) + "\n\n"
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _project_root = os.environ.get("SWAXS_PROJECT", "")
    print("━" * 52)
    print("  Autonomous Synthesis (reactor)  ·  http://localhost:5108")
    print(f"  backend = {_BACKEND}   (set SWAXS_REACTOR_BACKEND=real for hardware)")
    print("━" * 52)
    app.run(host="127.0.0.1", port=5108, debug=False, threaded=True)
