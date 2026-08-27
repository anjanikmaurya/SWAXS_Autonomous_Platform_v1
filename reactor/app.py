"""
reactor/app.py — Flow Synthesis (port 5007)
============================================
Pump-control / execution layer for the 5-pump continuous-flow nanoparticle
reactor (Fong et al., J. Chem. Phys. 154, 224201, 2021).  Receives an
already-predicted recipe (folder / JSON API / form) and drives the pumps; the
BO/SAXS optimization itself lives elsewhere.

All hardware + run logic is in src/reactor/.  This file is a thin Flask shell:
routes, SSE, the recipes-folder watcher, and the hub event-bus wiring.

Run:  uv run reactor/app.py    Open: http://localhost:5007
"""

from __future__ import annotations

import collections
import datetime
import json
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

from src.reactor import load_config, ReactorController, RecipeError   # noqa: E402
from src.reactor.config import hub_to_spec_dir                        # noqa: E402
from src.reactor.recipe import parse_param_file                       # noqa: E402
from src.reactor.intake import decide_intake                          # noqa: E402
from src.manifest import update_manifest, add_reactor_run            # noqa: E402

# ── Event bus (graceful degradation) ─────────────────────────────────────────
try:
    from src.events import EventBusClient as _EventBusClient
    _bus = _EventBusClient("reactor").connect(retry=True)
except Exception:
    _bus = None

app = Flask(__name__)

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


# ── Slack notifications (unattended operation) ────────────────────────────────
# Credentials come from the environment, never config.yml (which is in git):
#   SWAXS_SLACK_BOT_TOKEN  — threaded updates + plot uploads
#   SWAXS_SLACK_WEBHOOK    — flat messages, zero setup
try:
    from src.notify import MultiNotifier                            # noqa: E402
    # One object, both transports (Slack + email). Email needs no workspace
    # approval, so it works when a Slack app install is still pending.
    _slack = MultiNotifier(_CFG.get("notify", {}) or {}, log=_emit)
except Exception as _exc:                                            # pragma: no cover
    _slack = None
    print(f"[Flow Synthesis] notifier unavailable: {_exc}", file=sys.stderr)


def _slack_event(etype: str, data: dict) -> None:
    """Translate reactor events into Slack messages. Wrapped by the caller so a
    failure here can never reach the control loop."""
    if _slack is None or not _slack.enabled:
        return
    rid = str(data.get("recipe_id") or "")
    if etype == "reactor.run_start":
        _slack.recipe_applied(rid, data.get("recipe") or {},
                              float(data.get("duration_s") or 0.0))
    elif etype == "reactor.run_complete":
        _slack.run_complete(rid, str(data.get("reason") or "?"),
                            float(data.get("duration_s") or 0.0),
                            result=data.get("analysis"))
    elif etype == "reactor.estop":
        failed = data.get("failed_to_idle") or []
        _slack.fault("EMERGENCY STOP",
                     (f"pumps that did NOT idle: {', '.join(failed)} — check them "
                      f"immediately") if failed else "all pumps idle",
                     recipe_id=rid)
    elif etype == "reactor.safety":
        _slack.fault(f"SAFETY: {data.get('check', 'fault')}",
                     str(data.get("detail") or ""), recipe_id=rid)
    elif etype == "reactor.backend":
        _slack.notify(f":gear: backend switched to *{data.get('backend')}*",
                      tier="session")


# ── controller callbacks ──────────────────────────────────────────────────────
def _event_cb(etype: str, data: dict) -> None:
    if _bus is not None:
        try:
            _bus.publish(etype, data)
        except Exception:
            pass
    try:
        _slack_event(etype, data)
    except Exception:
        pass          # notifications must never disturb the reactor


def _feedback_cb(recipe_id: str, payload: dict) -> None:
    """Write <recipe_id>.done.json so the BO/SAXS side knows the run finished."""
    try:
        fb = _resolve("feedback")
        fb.mkdir(parents=True, exist_ok=True)
        (fb / f"{recipe_id}.done.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        # append the measured flow-sensor readings as a footer to the consumed
        # condition file (in the processed/done folder) — commanded vs delivered.
        rec = payload.get("recipe") or {}
        src = str(rec.get("source", ""))
        if src.startswith("folder:"):
            done_file = _resolve("processed") / src.split("folder:", 1)[1]
            if done_file.is_file():
                sp = payload.get("setpoints", {})
                meas = payload.get("measured_flows", {})
                foot = ["", "# ── RESULT (measured, appended by reactor) ──────────────",
                        f"# ended:       {datetime.datetime.now().isoformat(timespec='seconds')}",
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
                              manifest_cb=_manifest_cb)
except Exception as exc:
    print("\n[Flow Synthesis] Startup failed:\n  " + str(exc) +
          "\n\nIn real mode, close the Dolomite GUI and any other program using "
          "the pump COM ports, then restart.\n", file=sys.stderr)
    sys.exit(1)
_emit(f"Flow Synthesis ready — backend={_BACKEND}", "ok")
# On exit, hand the rig back: idle pumps, close shutter, release SPEC control.
import atexit as _atexit                                             # noqa: E402
_atexit.register(lambda: _ctrl.shutdown())
if _slack is not None and _slack.enabled:
    _emit(f"🔔 notifications ON ({_slack.mode})", "ok")
    _slack.session_start(_BACKEND, _project_root)
    _atexit.register(lambda: (_slack.session_end(len(_ctrl.history)), _slack.close()))
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


def _restore_run_settings() -> None:
    """Re-apply the operator's own run settings after a restart."""
    try:
        from src.runstate import load_state
        st = load_state(_project_root, _RUN_SETTINGS_STATE, max_age_s=48 * 3600)
        if not st:
            return
        # drop runstate's own bookkeeping keys (_saved_at) before replaying
        st = {k: v for k, v in st.items() if not k.startswith("_") and v is not None}
        if not st:
            return
        _ctrl.set_run_settings(st)
        _emit("♻  run settings restored: "
              + ", ".join(f"{k}={v}" for k, v in sorted(st.items())), "ok")
    except Exception as exc:
        _emit(f"⚠ could not restore the run settings: {exc}", "warn")



if _project_root:
    # The project root holds config.yml (poni_files / detector_shapes). The 2D
    # simulator needs it to reuse the SAME geometry the reduction app uses —
    # without it, frames were generated with a synthetic fallback geometry.
    _ctrl.set_project_root(_project_root)
    _sync_data_dir_from_hub(_project_root)
    # Restore the operator's run settings BEFORE auto-run may start a recipe, so a
    # resumed campaign uses the durations they actually chose.
    _restore_run_settings()
    _restore_auto_run()


# ── hub bus: end the run when SAXS produces a new averaged file ───────────────
def _on_bus_event(event: dict) -> None:
    etype = event.get("type") or event.get("event_type") or ""
    data = event.get("data", event)
    if etype == "file.averaged":
        _ctrl.signal_measurement_complete(str(data.get("file_path", "")))
    elif etype == "analysis.complete":
        # The analyzer's answer — the message actually worth reading at 3 a.m.
        # Posted into this recipe's Slack thread, with the QC plot attached when
        # the fit is suspect.
        try:
            _slack_analysis(data)
        except Exception:
            pass


def _slack_analysis(data: dict) -> None:
    if _slack is None or not _slack.enabled:
        return
    rid = str(data.get("recipe_id") or "")
    size, pdi = data.get("size"), data.get("pdi")
    conf = data.get("confidence")
    suspect = bool(data.get("suspect"))
    fields = {}
    for label, key in (("size (nm)", "size"), ("PDI", "pdi"),
                       ("confidence", "confidence"), ("loss", "loss"),
                       ("distribution", "distribution"), ("phase", "phase")):
        if data.get(key) is not None:
            fields[label] = data[key]
    fields["file"] = data.get("file", "")
    head = (":mag: *Fit LOW CONFIDENCE*" if suspect else ":bar_chart: *Fit result*")
    _slack.notify(f"{head} — `{rid or data.get('file', '?')}`",
                  tier=("alert" if suspect else "progress"),
                  recipe_id=rid, fields=fields)
    png = str(data.get("plot_png") or "")
    if suspect and png:
        _slack.upload_png(png, title=f"{rid} I(q) + fit", recipe_id=rid,
                          comment=(f"low-confidence fit ({conf}) — R={size} nm, "
                                   f"PDI={pdi}"))


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
_watch_handled: dict = {}    # path -> signature of the version already ingested/rejected
_watch_lastsig: dict = {}    # path -> signature seen on the previous poll


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
                files = sorted(list(rdir.glob("*.dat")) + list(rdir.glob("*.txt"))
                               + list(rdir.glob("*.json")),
                               key=lambda p: p.stat().st_mtime)
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
                        done = _resolve("processed"); done.mkdir(parents=True, exist_ok=True)
                        # replace() (not rename()) overwrites an existing dest —
                        # rename() raises on Windows if done/<name> already exists.
                        f.replace(done / f.name)
                        _watch_lastsig.pop(key, None)
                        _watch_handled.pop(key, None)   # moved away; a re-drop is new
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


@app.route("/api/recipes_folder", methods=["GET", "POST"])
def api_recipes_folder():
    """GET the watched conditions folder; POST {folder} to change it live."""
    if request.method == "POST":
        b = request.get_json(force=True) or {}
        folder = str(b.get("folder", "")).strip()
        if folder:
            _CFG.setdefault("folders", {})["recipes"] = folder
            _CFG["folders"]["processed"] = str(Path(folder) / "done")
            # re-scan the new folder from scratch (these are the real watcher
            # caches; _watch_seen no longer exists and raised NameError here)
            _watch_handled.clear()
            _watch_lastsig.clear()
            _save_recipes_folder(folder)
            _emit(f"📁 conditions folder → {folder}", "info")
    return jsonify({"folder": _CFG.get("folders", {}).get("recipes", ""),
                    "resolved": str(_resolve("recipes"))})


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    body = request.get_json(force=True)
    p = (body.get("path", "") or "").strip()
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


def _simple(fn):
    try:
        return jsonify({"ok": bool(fn())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/start", methods=["POST"])
def api_start():   return _simple(_ctrl.start)


@app.route("/api/abort", methods=["POST"])
def api_abort():   _ctrl.abort();  return jsonify({"ok": True})


@app.route("/api/estop", methods=["POST"])
def api_estop():
    # NEVER report a bare success here: if a pump could not be idled the operator
    # must see it, not a green tick.
    failed = _ctrl.estop() or []
    return jsonify({"ok": not failed, "failed_to_idle": failed,
                    "error": (f"could not idle: {', '.join(failed)} — CHECK THESE "
                              f"PUMPS IMMEDIATELY") if failed else None})


@app.route("/api/reset", methods=["POST"])
def api_reset():   _ctrl.reset();  return jsonify({"ok": True})


@app.route("/api/vent", methods=["POST"])
def api_vent():    _ctrl.vent_all(); return jsonify({"ok": True})


# ── Slack notifications: arm on the way out of the hutch ──────────────────────
def _slack_status() -> dict:
    if _slack is None:
        return {"enabled": False, "configured": False, "mode": "",
                "error": "notifier unavailable"}
    return _slack.status()


@app.route("/api/slack")
def api_slack():
    return jsonify(_slack_status())


@app.route("/api/slack", methods=["POST"])
def api_slack_set():
    """Toggle notifications while the platform is running — the point is to arm
    them AFTER the measurement is started and you're about to walk away."""
    if _slack is None:
        return jsonify({"ok": False, "error": "notifier unavailable"}), 400
    body = request.get_json(silent=True) or {}
    want = body.get("enabled")
    which = str(body.get("channel", "all") or "all")
    want = (not _slack.enabled) if want is None else bool(want)   # None = toggle
    ok, msg = _slack.enable(which) if want else _slack.disable(which)
    if ok:
        _emit(f"🔔 {msg}", "ok" if want else "info")
        if want:
            # Confirm in the channel itself, so you know it works before leaving.
            _slack.notify(
                f":bell: *Notifications armed* — the reactor will report from here "
                f"(backend `{_ctrl.backend}`, state `{_ctrl.state}`)",
                tier="session")
    else:
        _emit(f"⚠ Slack: {msg}", "warn")
    return jsonify({"ok": ok, "error": None if ok else msg, **_slack_status()})


@app.route("/api/slack/test", methods=["POST"])
def api_slack_test():
    """Send one message now, so the channel can be verified before walking away."""
    if _slack is None or not _slack.configured:
        return jsonify({"ok": False,
                        "error": "no notification channel configured — set a Slack "
                                 "credential, or SWAXS_SMTP_HOST + "
                                 "SWAXS_NOTIFY_EMAIL in .env"}), 400
    was = _slack.enabled
    for ch in _slack.channels.values():        # a test must not be silenced
        if ch.configured:
            ch.enabled = True
            ch._ensure_worker()
    _slack.notify(":wave: *Test from the reactor app* — notifications are working.",
                  tier="session")
    if not was:
        # leave it as we found it; the queued messages still go out
        def _restore():
            for ch in _slack.channels.values():
                ch.enabled = False
        threading.Timer(3.0, _restore).start()
    _emit(f"🔔 test message queued ({_slack.mode or 'no channel'})", "info")
    return jsonify({"ok": True, **_slack_status()})


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
    return jsonify({"ok": True, "cleared": _ctrl.clear_queue()})


@app.route("/api/flush", methods=["POST"])
def api_flush():
    b = request.get_json(silent=True) or {}
    rate = b.get("rate"); dur = b.get("duration")
    ok = _ctrl.flush_now(float(rate) if rate else None, float(dur) if dur else None)
    return jsonify({"ok": ok})


@app.route("/api/auto_run", methods=["POST"])
def api_auto_run():
    b = request.get_json(force=True)
    _ctrl.set_auto_run(bool(b.get("on", False)))
    _persist_auto_run(_ctrl.auto_run)      # so a restart can REPORT it (not resume it)
    return jsonify({"ok": True, "auto_run": _ctrl.auto_run})


@app.route("/api/spec_settings", methods=["POST"])
def api_spec_settings():
    _ctrl.set_spec_settings(request.get_json(silent=True) or {})
    return jsonify({"ok": True})


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
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/status")
def api_status():
    return jsonify(_ctrl.status())


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
            yield "data: " + json.dumps({"status": _ctrl.status(), "logs": new}) + "\n\n"
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _project_root = os.environ.get("SWAXS_PROJECT", "")
    print("━" * 52)
    print("  Flow Synthesis (reactor)  ·  http://localhost:5007")
    print(f"  backend = {_BACKEND}   (set SWAXS_REACTOR_BACKEND=real for hardware)")
    print("━" * 52)
    app.run(host="127.0.0.1", port=5007, debug=False, threaded=True)
