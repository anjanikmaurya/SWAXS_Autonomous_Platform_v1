"""
analyzer/app.py — Auto-Fit & Optimiser (port 5107)
===================================================
Watches the SAXS Subtracted folder and, as each new profile appears, fits a
polydisperse-sphere model to extract size, PDI, the (relative) Porod invariant,
and a 0-1 confidence — the measurement half of the closed synthesis loop.

Thin Flask shell: all science is in src/analysis/nanoparticle.py. Routes, the
folder watcher, SSE, and manifest writing live here.

Run:  python analyzer/app.py    Open: http://localhost:5107
      (from the activated venv — see CLAUDE.md)
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template, request, Response

# ── sys.path ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analysis.nanoparticle import analyze_profile, model_intensity   # noqa: E402
from src.utils.read_dat_metadata import read_dat_data_metadata           # noqa: E402
from src.reactor.intake import decide_intake                             # noqa: E402
from src.manifest import (update_manifest, add_analysis_entry,          # noqa: E402
                          set_project_meta)
from src.ai.loop_advice import narrate_fit                               # noqa: E402
from src.reactor import load_config                                      # noqa: E402
from src.reactor.recipe import parse_param_file                          # noqa: E402
from src.optimizer import ParameterSpace, CampaignController, NAMES      # noqa: E402
from src.optimizer.io import (to_param_file, match_recipe_id,           # noqa: E402
                              recipe_id_from_filename)
from src.runstate import (save_state, load_state, clear_state,             # noqa: E402
                          save_monitor, load_monitor, resume_disabled)

# ── Event bus (graceful degradation) ─────────────────────────────────────────
# Used to publish `fit.complete` so downstream apps (e.g. the reactor's
# Slack notifier) can report the fitted result against its recipe.
#
# Deliberately NOT named `analysis.complete`: the Data Analysis app already
# owns that name for a different payload shape (analysis_type/file_path/
# results, via EventBusClient.emit_analysis_complete). Reusing it here made
# the hub's event log render this app's events as "undefined on ?" (it read
# data.analysis_type/data.file_path, which this payload doesn't have — it has
# `file`, `size`, `pdi`, etc.), and would have made the reactor's Slack
# notifier (which listens for this exact name) fire garbage messages for
# every Guinier/Porod/Kratky/peak/model result the Data Analysis app produces.
# Caught live from a user seeing the "undefined on ?" hub log line.
try:
    from src.events import EventBusClient as _EventBusClient               # noqa: E402
    _bus = _EventBusClient("analyzer").connect(retry=True)
except Exception:
    _bus = None
import datetime, uuid                                                    # noqa: E402,E401

app = Flask(__name__)

_project_root: str = os.environ.get("SWAXS_PROJECT", "")
_sub_folder: str = "1D/SAXS/Subtracted"     # relative to project (or absolute)
_cond_folder: str = "1D/SAXS/Conditions"    # where proposed conditions are written (reactor watches this)
#: Sibling of Conditions/. One subfolder per campaign, written when the
#: campaign ENDS: the figures, the record and the history. Until this
#: existed, a real run's convergence/trajectory plots were rendered on
#: demand for the browser and never persisted — once the app restarted or
#: the next campaign started, the only view of the finished run was gone.
#: (docs/figures/*.png are NOT from a real run: tools/campaign_plots.py
#: generates those in silico against the simulator, for documentation.)
_results_folder: str = "1D/SAXS/Results"

# ── closed-loop campaign state ─────────────────────────────────────────────────
_campaign: CampaignController | None = None
_pending: dict = {}          # recipe_id -> proposed params awaiting a measurement
_campaign_lock = threading.Lock()

_results: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_results_lock = threading.Lock()
#: Monotonic id per stored result, so the SSE stream can send only what CHANGED.
#: Previously every frame re-sent EVERY summary — 0.8 MB of JSON per second at
#: 3000 profiles, plus a full <tbody> rebuild in the browser. That is what made
#: the app crawl once a campaign had run for a few hours.
_result_seq = 0
#: Hard cap on retained results. An overnight campaign produces thousands; the
#: table only ever shows the most recent, and the full record lives in the
#: manifest, so retaining every fit in RAM bought nothing but slowdown.
_MAX_RESULTS = int(os.environ.get("SWAXS_ANALYZER_MAX_RESULTS", 600))
#: rows sent in the first SSE frame (the table shows newest-first anyway)
_SNAPSHOT = 200
_log: collections.deque = collections.deque(maxlen=300)
_seq = 0
_log_lock = threading.Lock()


def _emit(msg: str, tag: str = "info") -> None:
    global _seq
    with _log_lock:
        _seq += 1
        _log.append((_seq, {"ts": time.strftime("%H:%M:%S"), "msg": msg, "tag": tag}))


#: How the Quality Gate is honoured.
#:   "auto" (default) — if a Good/ subfolder exists under the Subtracted folder,
#:                      analyse THAT, so a profile the gate rejected can never
#:                      reach the fit or the optimizer.
#:   "good"           — always require Good/
#:   "off"            — legacy: analyse the flat folder, gate advisory only
_gate_mode: str = "auto"
_gate_note_shown = False


def _resolve_sub_base() -> Path:
    p = Path(_sub_folder)
    if not p.is_absolute():
        p = (Path(_project_root) if _project_root else Path.cwd()) / _sub_folder
    return p


def _resolve_sub() -> Path:
    """The folder actually analysed.

    The Quality Gate COPIES profiles into Good/ and NeedsReview/ and leaves the
    original in place, so watching the flat folder meant every rejected profile
    was still fitted and fed to the Bayesian campaign — the gate had no effect on
    the data path at all. Prefer Good/ whenever it exists.
    """
    global _gate_note_shown
    base = _resolve_sub_base()
    mode = str(_gate_mode or "auto").lower()
    if mode == "off":
        return base
    good = base / "Good"
    if mode == "good" or good.is_dir():
        if not _gate_note_shown:
            _gate_note_shown = True
            _emit(f"🔒 quality gate honoured — analysing {good} only "
                  f"(rejected profiles are never fitted)", "ok")
        return good
    if not _gate_note_shown:
        _gate_note_shown = True
        _emit(f"⚠ no Good/ folder yet — analysing every subtracted profile in "
              f"{base}. Start the Quality Gate so bad profiles can't reach the "
              f"optimizer.", "warn")
    return base


def _resolve_cond() -> Path:
    p = Path(_cond_folder)
    if not p.is_absolute():
        p = (Path(_project_root) if _project_root else Path.cwd()) / _cond_folder
    return p


def _resolve_results() -> Path:
    p = Path(_results_folder)
    if not p.is_absolute():
        p = (Path(_project_root) if _project_root else Path.cwd()) / _results_folder
    return p


# ── Target-Run tagging ────────────────────────────────────────────────────────
# Each optimization campaign is a "Target Run", tagged RunN. The tag is prepended
# to every recipe_id, so it propagates automatically into the condition file, the
# reactor recipe, and the 2D/SAXS filenames ({recipe_id}_{role}_..._SAXS.raw) — you
# can tell at a glance which data belongs to which target-run conditions. N is
# DERIVED FROM DISK at campaign start (max existing Run<n> + 1), never stored as a
# setting, so it survives a restart without carrying anything over. RunN must stay
# digits-only: split_role() truncates a recipe_id at the first role token, so a
# word tag that aliased sample/background/bkg/blank/... would corrupt parsing.
_run_tag: str = ""          # e.g. "Run3"; empty when no campaign is active
_run_seq: int = 0           # per-campaign proposal counter (in-memory, resets each run)
_RUN_RE = re.compile(r"(?:^|[^A-Za-z])Run(\d+)_", re.IGNORECASE)


def _next_run_no() -> int:
    """The next Target-Run number = 1 + the highest Run<n> ALREADY ON DISK.

    Overwrite safety is the whole point: after a restart, RunN must never reuse a
    number whose data still exists, or a new run would overwrite it (same filenames).
    So we take the max over EVERY durable place a RunN can appear, not just the
    campaign bookkeeping (which could be cleared or moved):

      * the run_no recorded in each Results/campaign_*.json (explicit + survives);
      * any RunN_-prefixed file anywhere under the project's 2D/ and 1D/ trees —
        the raw frames, the reduced/averaged/subtracted/analysed outputs, and the
        Conditions files (incl. those the reactor moved into a processed/ subfolder).

    Derived from disk, so it needs no stored counter and cannot reset to 1 while
    RunN data is present. Returns 1 only on a genuinely empty project."""
    hi = 0
    # 1) explicit run_no in the durable campaign records
    try:
        for rec in _resolve_results().glob("campaign_*.json"):
            try:
                hi = max(hi, int(json.loads(rec.read_text(encoding="utf-8")).get("run_no") or 0))
            except Exception:
                continue
    except Exception:
        pass
    # 2) any RunN_-tagged artifact in the actual data trees — this is what
    #    guarantees we never step on data that exists but lost its record.
    root = Path(_project_root) if _project_root else Path.cwd()
    for base in (root / "2D", root / "1D"):
        try:
            if base.is_dir():
                for p in base.rglob("Run*"):
                    m = _RUN_RE.search(p.name)
                    if m:
                        hi = max(hi, int(m.group(1)))
        except Exception:
            continue
    return hi + 1


def _latest_incomplete_run() -> dict | None:
    """The highest-run_no Results/campaign_<id>.json with no "outcome" key —
    i.e. a Target Run that was started but never converged/exhausted/aborted.
    Detected from the record, never from the files on disk."""
    best = None
    try:
        for rec_path in _resolve_results().glob("campaign_*.json"):
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if "outcome" in rec:
                continue
            run_no = int(rec.get("run_no") or 0)
            if best is None or run_no > best["run_no"]:
                best = {**rec, "_path": str(rec_path), "run_no": run_no}
    except Exception:
        pass
    return best


def _new_rid() -> str:
    """The id for the next proposed condition. When a Target Run is active it is
    Run{N}_r{seq} (e.g. Run3_r001), which flows into the SAXS filenames; otherwise
    a timestamp+uuid fallback for manual/non-campaign use."""
    global _run_seq
    if _run_tag:
        _run_seq += 1
        return f"{_run_tag}_r{_run_seq:03d}"
    return "auto_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]


def _max_run_seq(tag: str, ids) -> int:
    """Highest ``r{NNN}`` sequence already issued under ``tag`` across recipe ids.

    On resume the counter must continue PAST what's already on disk — restarting
    at r001 would reuse ids and overwrite this run's frames (same filenames)."""
    if not tag:
        return 0
    rx = re.compile(rf"^{re.escape(tag)}_r(\d+)")
    hi = 0
    for rid in ids:
        m = rx.match(str(rid or ""))
        if m:
            hi = max(hi, int(m.group(1)))
    return hi


def _write_condition(rid: str, params: dict) -> None:
    d = _resolve_cond(); d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.txt").write_text(to_param_file(rid, params), encoding="utf-8")
    sp = " ".join(f"{k}={float(v):g}" for k, v in params.items())
    _emit(f"➡ proposed {rid}: {sp}", "ok")


def _advance_campaign() -> None:
    """Emit the next condition, or report the campaign has stopped. Lock held by caller."""
    if _campaign is None:
        return
    if _campaign.status_str == "running":
        p = _campaign.ask()
        if p is not None:
            rid = _new_rid()
            _pending[rid] = p
            _pending_at[rid] = time.time()
            _write_condition(rid, p)
            return
    st = _campaign.status_str
    if st == "converged":
        cc = _campaign.converged_condition or {}
        _emit(f"🎯 campaign CONVERGED — size {cc.get('size')} at "
              f"{ {k: round(v,1) for k,v in (cc.get('params') or {}).items()} }", "ok")
        _oc = {"outcome": "converged", "converged_condition": cc,
               "n_evaluations": _campaign.status().get("n_evaluations")}
        _write_campaign_record(_oc); _write_campaign_results(_oc)
        _record_campaign_in_manifest(_oc)
    elif st == "exhausted":
        _emit(f"⏹ campaign budget exhausted ({_campaign.status()['n_evaluations']} runs) — "
              f"best size {(_campaign.best or {}).get('size')}", "warn")
        _oc = {"outcome": "exhausted", "best": _campaign.best,
               "n_evaluations": _campaign.status().get("n_evaluations")}
        _write_campaign_record(_oc); _write_campaign_results(_oc)
        _record_campaign_in_manifest(_oc)
    elif st == "aborted":
        _emit("⏹ campaign aborted", "warn")
        _oc = {"outcome": "aborted", "best": _campaign.best}
        _write_campaign_record(_oc); _write_campaign_results(_oc)
        _record_campaign_in_manifest(_oc)


def _last_loss_for(recipe_id: str):
    """The campaign's loss for this recipe, if a campaign is running. Reported in
    the notification so the objective — not just the size — is visible."""
    if not recipe_id:
        return None
    try:
        with _campaign_lock:
            if _campaign is None:
                return None
            for rec in reversed(_campaign.history):
                if str(rec.get("recipe_id", "")) == recipe_id:
                    return round(float(rec["loss"]), 4)
    except Exception:
        pass
    return None


# ── campaign persistence (platform audit O2) ─────────────────────────────────
# The campaign used to live ONLY in this process's memory, so an analyzer restart
# silently ended the closed loop: fits still ran and still reached the manifest,
# but _feed_campaign returned early, no new condition was written, and the
# reactor idled until somebody noticed in the morning. Persist enough to rebuild
# the controller and keep going.
_CAMPAIGN_STATE = "campaign"
#: Two-tier restart notice (see reactor/background/average/quality). Unlike the data
#: apps, an interrupted campaign is NOT auto-resumed — it drives the reactor, so like
#: reactor auto-run it stays opt-in. "lost" (loud) tells the operator a running
#: campaign was interrupted and needs a manual Start; "restored" (calm) is the opt-in
#: SWAXS_RESUME path; "none" = nothing running / a campaign that had already ended.
_CAMPAIGN_NOTICE: dict = {"level": "none", "message": "", "params": []}
_campaign_cfg: dict = {}        # the hyperparameters the campaign was created with
#: Identifies THIS campaign in the durable records below. The optimisation
#: target used to exist only in _campaign_cfg (overwritten by the next
#: campaign) and in one log line, so months later nothing said what a run had
#: been aiming for. It is now written to three durable places on start:
#: manifest project_meta.campaign, every analyses entry, and a record file.
_campaign_id: str = ""
#: Descriptive campaign metadata for the records. Kept SEPARATE from
#: _campaign_cfg because that dict is splatted into CampaignController(**cfg) on
#: resume — any extra key there is a TypeError that silently kills the restore.
_campaign_meta: dict = {}


def _campaign_record(outcome: dict | None = None) -> dict:
    """Everything needed to answer "what was this run aiming for?" later."""
    rec = {"campaign_id": _campaign_id, **(_campaign_cfg or {}), **(_campaign_meta or {})}
    if _campaign is not None:
        rec["status"] = _campaign.status_str
    if outcome:                       # final outcome/best/converged_condition
        rec["outcome"] = outcome
    return rec


def _write_campaign_record(outcome: dict | None = None) -> None:
    """One JSON per campaign, in Results/ — a sibling of the folder this same
    campaign gets at the end (`Results/campaign_<id>/`), not inside it.

    Deliberately NOT .swaxs_state/: that file is resume state, is overwritten by
    the next campaign and expires after 7 days, so it can never answer what an
    old run targeted. This one is written on start and rewritten on finish.

    Deliberately NOT Conditions/ either — that is a HIGH-severity fix, not a
    style choice. The reactor globs *.dat/*.txt/*.json in Conditions/ every
    poll looking for the next recipe (src/reactor/intake.py via reactor/app.py);
    a record file living there gets rejected as an invalid recipe (missing
    T_reac) on every single poll, forever, spamming the reactor log. Caught
    live in a mock run — see test_the_campaign_record_never_lands_in_conditions.
    Never raises — a record-keeping failure must not stop a campaign.
    """
    if not _campaign_id:
        return
    try:
        d = _resolve_results(); d.mkdir(parents=True, exist_ok=True)
        path = d / f"campaign_{_campaign_id}.json"
        body = _campaign_record()
        if path.is_file():                      # keep started_at from the first write
            try:
                body = {**json.loads(path.read_text(encoding="utf-8")), **body}
            except Exception:
                pass
        if outcome:
            body.update(outcome)
            body["ended_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        tmp = path.with_suffix(".json.part")    # atomic: a torn record is useless
        tmp.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        _emit(f"⚠ could not write the campaign record: {exc}", "warn")


def _write_campaign_results(outcome: dict) -> str:
    """On campaign end, persist everything worth keeping into one folder.

    `1D/SAXS/Results/campaign_<id>/` — a sibling of Conditions/, one folder per
    campaign (a new one every time a target is set):

        campaign.json     the target, the outcome, the full history
        history.csv       one row per evaluation, for a spreadsheet or a paper
        convergence.png   loss/size vs evaluation
        trajectory.png    the path the optimiser took through the space
        slice.png         the model's surface through the best point

    The figures are the SAME renderer the live UI uses (src/optimizer/plots.py),
    which previously drew only on demand and saved nothing — so a finished run
    left no plot behind at all.

    Returns the folder path, or "" on failure. Never raises: losing the figures
    must not stop a campaign from ending cleanly.
    """
    if _campaign is None or not _campaign_id:
        return ""
    try:
        d = _resolve_results() / f"campaign_{_campaign_id}"
        d.mkdir(parents=True, exist_ok=True)

        body = {**_campaign_record(), **outcome,
                "ended_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "history": _campaign.history}
        (d / "campaign.json").write_text(
            json.dumps(body, indent=2, default=str), encoding="utf-8")

        hist = _campaign.history or []
        if hist:
            keys = ["recipe_id", "size", "pdi", "confidence", "loss"]
            pkeys = sorted({k for h in hist for k in (h.get("params") or {})})
            lines = [",".join(keys + pkeys)]
            for h in hist:
                pr = h.get("params") or {}
                lines.append(",".join(
                    [str(h.get(k, "")) for k in keys] +
                    [str(pr.get(k, "")) for k in pkeys]))
            (d / "history.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

        from src.optimizer import plots as opl                # noqa: PLC0415
        truth = _truth_for_plots()
        for view, name in (("convergence", "convergence.png"),
                           ("trajectory",  "trajectory.png"),
                           ("slice",       "slice.png")):
            try:
                (d / name).write_bytes(opl.figure(view, _campaign, truth=truth))
            except Exception as exc:
                _emit(f"⚠ could not save {name}: {exc}", "warn")

        _emit(f"💾 campaign results saved to {d}", "ok")
        return str(d)
    except Exception as exc:
        _emit(f"⚠ could not save the campaign results: {exc}", "warn")
        return ""


def _record_campaign_in_manifest(outcome: dict | None = None) -> None:
    """Put the target in the permanent cross-app record, so the assistant and
    anyone reading the experiment afterwards can see it. Called on start AND on
    end — otherwise the manifest's campaign stays "running" forever and the
    assistant reports a finished/aborted run as still live. Best-effort."""
    if not _project_root or not _campaign_id:
        return
    try:
        update_manifest(_project_root,
                        lambda mf: set_project_meta(mf, campaign=_campaign_record(outcome)))
    except Exception as exc:
        _emit(f"⚠ could not record the campaign in the manifest: {exc}", "warn")


def _save_campaign() -> None:
    """Snapshot the campaign. Cheap, frequent, and deliberately NOT the manifest
    (which is a full locked read-modify-write). Never raises."""
    try:
        if _campaign is None or not _project_root:
            return
        save_state(_project_root, _CAMPAIGN_STATE, {
            "campaign_id": _campaign_id,
            "cfg": _campaign_cfg,
            "meta": _campaign_meta,
            "status": _campaign.status_str,
            "history": _campaign.history,
            "pending": _pending,
            "pending_at": _pending_at,
            "handled": _snapshot_handled(),
        })
    except Exception as exc:
        _emit(f"⚠ could not save the campaign state: {exc}", "warn")


def _restore_campaign() -> None:
    """Rebuild the campaign from disk by replaying its history through tell().

    Replay must NOT re-emit condition files or notifications, so the proposal
    step is skipped while replaying — the pending set is restored verbatim
    instead.
    """
    global _campaign, _campaign_cfg, _campaign_id, _campaign_meta, _run_tag, _run_seq
    global _CAMPAIGN_NOTICE
    # NEVER clobber a live campaign. _boot_resume runs on a timer ~1 s after
    # import, so an operator (or a test) who starts a campaign inside that window
    # would have their running campaign silently replaced by the rebuilt one from
    # disk — losing its history and resetting its status to "running", including
    # after it had already converged.
    if _campaign is not None:
        return
    # Peek unconditionally (independent of the resume gate) so we can WARN when a
    # running campaign will NOT be auto-resumed. The campaign drives the reactor via
    # condition files, so — exactly like reactor auto-run — it stays opt-in: a power
    # blip must not restart reagent flow with nobody in the hutch. But it must not
    # silently look idle either.
    peek = load_state(_project_root, _CAMPAIGN_STATE, max_age_s=7 * 24 * 3600,
                      honour_no_resume=False)
    if (peek and peek.get("cfg") and str(peek.get("status")) == "running"
            and resume_disabled()):
        _tgt = (peek.get("cfg") or {}).get("target_size")
        _CAMPAIGN_NOTICE = {
            "level": "lost",
            "message": (f"A campaign toward {_tgt} nm was interrupted by a restart "
                        "and is NOT auto-resumed (it drives the reactor). Press "
                        "Start to continue it, or start a new campaign."),
            "params": ["target_size"]}
        _emit("⚠ a running campaign was interrupted and is NOT auto-resumed "
              "(it moves the reactor) — press Start to continue it", "warn")
        return
    st = load_state(_project_root, _CAMPAIGN_STATE, max_age_s=7 * 24 * 3600)
    if not st or not st.get("cfg"):
        return
    if str(st.get("status")) != "running":
        _emit(f"ℹ previous campaign ended ({st.get('status')}) — not resuming", "info")
        return
    cfg = dict(st["cfg"])
    # Keep the SAME campaign id across a restart, so the resumed run keeps
    # writing to its existing record instead of orphaning it.
    _campaign_id = str(st.get("campaign_id") or "")
    _campaign_meta = dict(st.get("meta") or {})
    hist = st.get("history") or []
    # Rehydrate the Target-Run tag AND advance the sequence counter past every id
    # already issued this run. Without this, a resumed campaign either falls back to
    # timestamp ids (empty _run_tag → provenance breaks) or, worse, restarts the
    # counter at r001 and OVERWRITES the frames already collected this run.
    _run_tag = str(_campaign_meta.get("run_tag") or "")
    if _run_tag:
        _issued = (list((st.get("pending") or {}).keys())
                   + [str(h.get("recipe_id") or "") for h in hist]
                   + list((st.get("handled") or {}).keys()))
        _run_seq = _max_run_seq(_run_tag, _issued)
    try:
        space = ParameterSpace.from_config(load_config())
        camp = CampaignController(space, **cfg)
        camp.start()
        for rec in hist:                       # rebuild the GP from real results
            camp.tell(rec.get("params") or {}, rec.get("size"),
                      rec.get("pdi"), float(rec.get("confidence") or 0.0),
                      recipe_id=rec.get("recipe_id", ""))
        camp._n_asked = len(camp.history)      # next ask() proposes a NEW point, not an already-replayed seed
        with _campaign_lock:
            _campaign = camp
            _campaign_cfg = cfg
            _pending.clear(); _pending.update(st.get("pending") or {})
            _pending_at.clear()
            _pending_at.update({k: float(v) for k, v in
                                (st.get("pending_at") or {}).items()})
            # Restoring `handled` is what stops a restart re-analysing every
            # existing profile — which would append a duplicate manifest entry
            # (new uuid each time) and fire a duplicate notification per file.
            with _intake_lock:
                for k, v in (st.get("handled") or {}).items():
                    try:
                        _handled[k] = tuple(v)
                    except Exception:
                        pass
        _CAMPAIGN_NOTICE = {
            "level": "restored",
            "message": (f"Campaign toward {cfg.get('target_size')} nm was resumed "
                        f"({len(hist)} run(s) replayed). Review the target before it "
                        "proposes the next condition."),
            "params": ["target_size"]}
        _emit(f"♻ campaign RESUMED from disk — {len(hist)} result(s) replayed, "
              f"{len(_pending)} condition(s) still pending, target "
              f"R={cfg.get('target_size')}±{cfg.get('tolerance')} nm", "ok")
        # If nothing is outstanding the loop would sit idle forever, so kick it.
        with _campaign_lock:
            if not _pending and _campaign.status_str == "running":
                _emit("♻ no pending condition after the restart — proposing the "
                      "next one", "info")
                _advance_campaign()
    except Exception as exc:
        _emit(f"⚠ could not resume the campaign: {exc}", "warn")


#: A proposed condition whose measurement never arrives would otherwise stall
#: the loop forever: _pending never expired and the campaign only proposes after
#: a tell(). After this long, record it as a FAILED measurement — which is the
#: documented path (tell(params, None, None, 0.0)) — and move on.
_PENDING_TIMEOUT_S = float(os.environ.get("SWAXS_PENDING_TIMEOUT_S", 3600.0))
_pending_at: dict = {}          # recipe_id -> time the condition was proposed


def _expire_pending() -> None:
    """Time out proposals whose measurement never appeared, so the autonomous
    loop self-heals instead of idling until somebody notices in the morning."""
    if not _pending:
        return
    now = time.time()
    stale: list = []
    with _campaign_lock:
        if _campaign is None or _campaign.status_str != "running":
            return
        stale = [rid for rid, t in list(_pending_at.items())
                 if rid in _pending and (now - t) > _PENDING_TIMEOUT_S]
        for rid in stale:
            params = _pending.pop(rid, None)
            _pending_at.pop(rid, None)
            if params is None:
                continue
            _emit(f"⏱ no measurement for {rid} after "
                  f"{_PENDING_TIMEOUT_S / 60:.0f} min — recording it as a FAILED "
                  f"measurement and proposing the next condition", "warn")
            try:
                _campaign.tell(params, None, None, 0.0, recipe_id=rid)
            except Exception as exc:
                _emit(f"⚠ could not record the failed measurement: {exc}", "warn")
        if stale:
            _advance_campaign()
    if stale:
        _save_campaign()


# ── Continue a stopped Target Run (operator-triggered, durable records only) ──
# Distinct from _restore_campaign above: that one is automatic, silent, only for
# a still-RUNNING campaign surviving a power blip, sourced from the 7-day-
# expiring .swaxs_state/ snapshot. This one is an explicit operator action, no
# time limit, and reads ONLY permanent records — Results/campaign_<id>.json,
# Results/Fit/*.dat, and the reactor's <recipe_id>.done.json feedback files —
# so it works no matter how long ago the process died.
def _load_fit_records_for_campaign(campaign_id: str) -> list:
    """Every Results/Fit/*.dat header belonging to this campaign_id, oldest
    first (by the "Written" timestamp), as {recipe_id, size, pdi, confidence}.

    recipe_id comes from the ORIGINAL subtracted filename (the fit record's
    own stem, after stripping the "fit_" prefix _write_fit_record adds) via
    recipe_id_from_filename — the header itself carries no recipe_id field."""
    out = []
    try:
        fit_dir = _resolve_fit()
        if not fit_dir.is_dir():
            return out
        for dat_path in fit_dir.glob("fit_*.dat"):
            try:
                lines = dat_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            hdr = {}
            for line in lines:
                if not line.startswith("#"):
                    break
                body = line[1:].strip()
                if ":" not in body:
                    continue
                k, v = body.split(":", 1)
                hdr[k.strip()] = v.strip()
            if hdr.get("Campaign ID") != campaign_id:
                continue
            stem = dat_path.stem
            if stem.startswith("fit_"):
                stem = stem[len("fit_"):]
            recipe_id = recipe_id_from_filename(stem)
            if not recipe_id:
                continue

            def _num(key):
                v = hdr.get(key)
                if v in (None, "", "None"):
                    return None
                try:
                    return float(v)
                except ValueError:
                    return None

            out.append({
                "recipe_id": recipe_id,
                "size": _num("Radius (nm)"),
                "pdi": _num("PDI"),
                "confidence": _num("Confidence") or 0.0,
                "written": hdr.get("Written", ""),
            })
    except Exception:
        pass
    out.sort(key=lambda r: r.get("written") or "")
    return out


def _load_params_for_recipe(recipe_id: str) -> dict | None:
    """params dict for tell() replay, recovered from the reactor's
    <recipe_id>.done.json feedback file (written once a synthesis run
    finishes — see reactor/app.py::_feedback_cb; never pruned). Returns None
    if the file is missing or doesn't carry all five ParameterSpace.NAMES —
    an unrecoverable observation, skipped by the caller, never fabricated."""
    root = Path(_project_root) if _project_root else Path.cwd()
    p = root / "reactor" / "feedback" / f"{recipe_id}.done.json"
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        recipe = payload.get("recipe") or {}
        if all(k in recipe for k in NAMES):
            return {k: float(recipe[k]) for k in NAMES}
    except Exception:
        pass
    return None


def _continue_run() -> dict:
    """Rebuild _campaign from durable records only (no .swaxs_state, no
    re-fitting). Returns a summary dict for the confirmation banner, or
    raises with a message on failure. Caller (the route) holds no lock;
    this acquires _campaign_lock/_intake_lock itself."""
    global _campaign, _campaign_cfg, _campaign_id, _campaign_meta, _run_tag, _run_seq
    if _campaign is not None:
        raise RuntimeError("a campaign is already running — abort it first")
    rec = _latest_incomplete_run()
    if rec is None:
        raise RuntimeError("no incomplete Target Run found")

    cfg_keys = ("target_size", "tolerance", "pdi_cap", "budget", "n_init")
    meta_keys = ("objective", "started_at", "operator", "run_no", "run_tag")
    cfg = {k: rec[k] for k in cfg_keys if k in rec}
    meta = {k: rec[k] for k in meta_keys if k in rec}
    campaign_id = str(rec.get("campaign_id") or "")
    run_tag = str(meta.get("run_tag") or f"Run{rec['run_no']}")

    fit_recs = _load_fit_records_for_campaign(campaign_id)
    space = ParameterSpace.from_config(load_config())
    camp = CampaignController(space, **cfg)
    camp.start()

    replayed, skipped = 0, 0
    for fr in fit_recs:
        params = _load_params_for_recipe(fr["recipe_id"])
        if params is None:
            skipped += 1
            _emit(f"⚠ resume: no recoverable params for {fr['recipe_id']} — "
                  f"this measurement is not counted toward the budget", "warn")
            continue
        camp.tell(params, fr["size"], fr["pdi"], fr["confidence"],
                  recipe_id=fr["recipe_id"])
        replayed += 1
    camp._n_asked = len(camp.history)     # next ask() proposes a NEW point, not an already-replayed seed

    # In-flight condition: files still sitting in Conditions/ (not yet moved to
    # Conditions/done/) under this run's tag were proposed but never even
    # started by the reactor — re-issue them (fresh timeout, no budget spent).
    cond_dir = _resolve_cond()
    done_dir = cond_dir / "done"
    reissued = []
    if cond_dir.is_dir():
        for f in cond_dir.glob(f"{run_tag}_r*.*"):
            if f.is_dir() or (done_dir / f.name).exists():
                continue
            try:
                text = f.read_text(encoding="utf-8")
                data = json.loads(text) if f.suffix == ".json" else parse_param_file(text)
            except Exception:
                continue
            rid = f.stem
            with _campaign_lock:
                _pending[rid] = {k: float(data[k]) for k in NAMES if k in data}
                _pending_at[rid] = time.time()
            reissued.append(rid)

    _run_tag = run_tag
    _run_seq = _max_run_seq(run_tag, [fr["recipe_id"] for fr in fit_recs] + reissued)
    with _campaign_lock:
        _campaign = camp
        _campaign_cfg = cfg
        _campaign_meta = meta
        _campaign_id = campaign_id
        if not _pending and _campaign.status_str == "running":
            _advance_campaign()
    _save_campaign()
    _record_campaign_in_manifest()

    summary = {
        "run_tag": run_tag, "replayed": replayed, "skipped": skipped,
        "reissued": reissued, "best": camp.best,
        "used": len(camp.history), "budget": camp.budget,
    }
    _emit(
        f"♻ Restored {replayed} measurement(s) from {run_tag}"
        + (f" (best {camp.best['size']} nm)" if camp.best and camp.best.get("size") else "")
        + f", {camp.budget - len(camp.history)} of {camp.budget} remaining."
        + (f" {len(reissued)} in-flight condition(s) re-issued." if reissued else "")
        + (f" {skipped} measurement(s) could not be restored (missing feedback record)." if skipped else ""),
        "ok",
    )
    return summary


def _feed_campaign(name: str, res: dict) -> None:
    """Match a measured profile to a pending proposed condition and drive the loop."""
    with _campaign_lock:
        if _campaign is None or _campaign.status_str != "running":
            return
        rid = match_recipe_id(name, _pending.keys())
        if not rid:
            # A measurement that carries a recipe_id but matches nothing pending is
            # an ORPHAN — a late frame from a timed-out/expired condition, or a
            # naming mismatch. Silently dropping it meant a fed-but-lost result the
            # operator never saw; surface it (once) so the loop's blind spot shows.
            carried = recipe_id_from_filename(name)
            if carried:
                _emit(f"⚠ measured profile {name} carries recipe_id "
                      f"'{carried}' but no matching pending condition "
                      f"(expired or already fed) — not driving the loop", "warn")
            return
        params = _pending.pop(rid)
        _pending_at.pop(rid, None)
        sz = res.get("size") or {}
        size = sz.get("radius")
        pdi = res.get("pdi")
        conf = res.get("confidence", 0.0)
        _campaign.tell(params, size, pdi, conf, recipe_id=rid)
        _emit(f"📊 told campaign {rid}: R={size} PDI={pdi} conf={conf} "
              f"(loss={_campaign.history[-1]['loss']:.3f})", "info")
        _advance_campaign()
    _save_campaign()


#: fits at or below this confidence get flagged as "suspect" downstream
#: (attached to notifications) — the fit RECORD itself (see _write_fit_record)
#: is now written for every fit, suspect or not.
QC_CONF_THRESHOLD = 0.5


def _resolve_fit() -> Path:
    """Results/Fit/ — a sibling of Results/campaign_<id>/ and
    Results/QualityReports/. One per-fit record lives here for every analyzed
    profile, so a fit can be checked or replotted after the beamtime without
    redoing it."""
    return _resolve_results() / "Fit"


def _write_fit_record(path: Path, q, I, sigma, model, summary: dict, res: dict) -> str:
    """Save a self-contained record of this fit — a PNG with the fit values
    annotated on the plot, and a .dat with q/I/sigma/fit columns plus the fit
    parameters in the header.

    Written for EVERY analyzed profile, not just low-confidence ones: the
    point is a durable cross-check trail for the whole campaign, available
    after the beamtime ends, not just a flag for suspect fits. Returns the
    PNG path (still used to attach suspect fits to notifications), or "" on
    any failure — a record-keeping failure must never stop the analysis.
    """
    try:
        out_dir = _resolve_fit()
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = path.stem
        png_path = out_dir / f"fit_{stem}.png"
        dat_path = out_dir / f"fit_{stem}.dat"

        import matplotlib                                     # noqa: PLC0415
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt                       # noqa: PLC0415

        fig, ax = plt.subplots(figsize=(5.2, 3.8), dpi=120)
        ax.loglog(q, np.maximum(I, 1e-12), lw=1.0, color="#5b8fc9",
                  marker="o", ms=2.2, alpha=0.75, mew=0, label="subtracted I(q)")
        if model is not None:
            ax.loglog(q, np.maximum(model, 1e-12), lw=1.8, color="#e2402c", label="fit")
        ax.set_xlabel("q (nm$^{-1}$)"); ax.set_ylabel("I (a.u.)")
        ax.set_title(path.name, fontsize=9)
        ax.legend(fontsize=7, loc="lower left")

        value_lines = [
            f"R = {summary.get('radius')} nm" if summary.get("radius") is not None else None,
            f"D = {summary.get('diameter')} nm" if summary.get("diameter") is not None else None,
            f"PDI = {summary.get('pdi')}" if summary.get("pdi") is not None else None,
            f"phase = {summary.get('phase')}" if summary.get("phase") else None,
            f"dist = {summary.get('distribution')}" if summary.get("distribution") else None,
            f"conf = {summary.get('confidence')}" if summary.get("confidence") is not None else None,
        ]
        text = "\n".join(v for v in value_lines if v)
        if text:
            ax.text(0.98, 0.98, text, transform=ax.transAxes, ha="right", va="top",
                    fontsize=8, family="monospace", linespacing=1.5,
                    bbox=dict(boxstyle="round", fc="white", ec="#999", alpha=0.88))
        fig.tight_layout()
        fig.savefig(png_path)
        plt.close(fig)

        sig_col = np.asarray(sigma, float) if sigma is not None else np.full_like(q, np.nan)
        fit_col = np.asarray(model, float) if model is not None else np.full_like(q, np.nan)
        header = [
            "# Nanoparticle fit record -- Auto-Fit & Optimiser (analyzer)",
            f"# Source file     : {path}",
            f"# Written         : {datetime.datetime.now().isoformat(timespec='seconds')}",
            f"# Radius (nm)     : {summary.get('radius')}",
            f"# Diameter (nm)   : {summary.get('diameter')}",
            f"# PDI             : {summary.get('pdi')}",
            f"# Distribution    : {summary.get('distribution')}",
            f"# Phase           : {summary.get('phase')}",
            f"# Confidence      : {summary.get('confidence')}",
            f"# Invariant (rel) : {summary.get('invariant_rel')}",
            f"# Guinier Rg (nm) : {summary.get('guinier_rg')}",
        ]
        if res.get("fit"):
            header.append(f"# Fit scale       : {res['fit'].get('scale')}")
            header.append(f"# Fit background  : {res['fit'].get('background')}")
        if _campaign_cfg:
            header += [
                f"# Campaign ID     : {_campaign_id}",
                f"# Target size (nm): {_campaign_cfg.get('target_size')}",
                f"# Tolerance       : {_campaign_cfg.get('tolerance')}",
                f"# PDI cap         : {_campaign_cfg.get('pdi_cap')}",
            ]
        header.append("# Columns: q_nm-1  I_data  sigma  I_fit  (I_fit is NaN if no form-factor fit)")
        body = "\n".join(f"{qi:.6e}  {Ii:.6e}  {si:.6e}  {fi:.6e}"
                         for qi, Ii, si, fi in zip(q, I, sig_col, fit_col))
        dat_path.write_text("\n".join(header) + "\n" + body + "\n", encoding="utf-8")
        return str(png_path)
    except Exception as exc:
        _emit(f"⚠ could not write the fit record: {exc}", "warn")
        return ""


def _store_result(name: str, entry: dict) -> int:
    """Single entry point into the result store.

    Stamps a monotonic sequence number (the client streams rows by `seq`, so this
    is what keeps SSE frames small) and enforces the cap. Every insertion must go
    through here — an unbounded store is both a memory leak over a long campaign
    and, because the snapshot frame is built from it, a slow page load.
    """
    global _result_seq
    with _results_lock:
        _result_seq += 1
        entry["summary"]["seq"] = _result_seq
        _results[name] = entry
        _results.move_to_end(name)
        while len(_results) > _MAX_RESULTS:
            _results.popitem(last=False)
        return _result_seq


def _downsample(x, n=260):
    x = np.asarray(x, float)
    if x.size <= n:
        return x.tolist()
    idx = np.linspace(0, x.size - 1, n).round().astype(int)
    return x[idx].tolist()


def _q_is_angstrom(header_lines) -> bool:
    """True if the .dat q column is in Å⁻¹ (e.g. background's ML-truncated files,
    labelled 'q_A-1'). Otherwise nm⁻¹ (the platform default)."""
    txt = " ".join(header_lines or []).lower()
    return ("q_a-1" in txt) or ("a^-1" in txt) or ("å" in txt)


def _is_simulated(meta, header_lines) -> bool:
    """True when the profile came from the mock simulator, not the beamline. The
    simulator stamps ``simulated=1`` into the frame metadata (writer.py); it flows
    into the .dat footer, so mock and real fits stay distinguishable in provenance."""
    try:
        if isinstance(meta, dict):
            for k, v in meta.items():
                if str(k).strip().lower() == "simulated" and str(v).strip() not in ("", "0", "false", "none"):
                    return True
    except Exception:
        pass
    return "simulated" in " ".join(header_lines or []).lower()


def _analyze_file(path: Path) -> None:
    try:
        hdr, q, I, sigma, _meta = read_dat_data_metadata(path)
        q = np.asarray(q, float)
        # The nanoparticle fit + optimizer target work in nm⁻¹ (radius in nm). If the
        # subtracted file was truncated to Å⁻¹ for the ML model, convert first so sizes
        # aren't 10× off and the campaign optimizes toward the right target.
        if _q_is_angstrom(hdr):
            q = q * 10.0                       # Å⁻¹ → nm⁻¹
        res = analyze_profile(q, I, sigma, dist="auto")
    except Exception as exc:
        _emit(f"✗ {path.name}: {exc}", "error")
        return
    # model overlay for the plot (only when a real form-factor fit succeeded)
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0) & (I > 0)
    # sigma travels with the plot so the UI can draw error bars and turn the
    # residual strip into a proper (data-model)/sigma plot instead of a % plot.
    try:
        sig = np.asarray(sigma, float)
        sig = sig[m] if sig.shape == m.shape else None
    except Exception:
        sig = None
    q, I = q[m], I[m]
    model = None
    if res.get("size") and res["size"].get("source") == "form_factor" and res.get("fit"):
        model = model_intensity(q, res["size"]["radius"], res["pdi"],
                                res["fit"]["scale"], res["fit"]["background"],
                                res.get("distribution", "schulz"))
    # advisory LLM QC note (empty + instant if no AI credentials configured)
    try:
        res["llm"] = narrate_fit(res.get("diagnostics", {}))
    except Exception:
        res["llm"] = {"summary": "", "flags": []}
    sz = res.get("size") or {}
    ph = res.get("phase") or {}
    summary = {
        "name": path.name,
        "radius": round(sz["radius"], 3) if sz.get("radius") is not None else None,
        "diameter": round(sz["diameter"], 3) if sz.get("diameter") is not None else None,
        "pdi": round(res["pdi"], 3) if res.get("pdi") is not None else None,
        "confidence": res.get("confidence", 0.0),
        "distribution": res.get("distribution"),
        "phase": ph.get("phase"),
        "invariant_rel": (round(res["invariant"]["Q_rel"], 4)
                          if res.get("invariant") else None),
        "guinier_rg": (round(res["guinier"]["Rg"], 3)
                       if res.get("guinier") and res["guinier"].get("Rg") else None),
        "ts": time.strftime("%H:%M:%S"),
    }
    entry = {"summary": summary, "full": res,
             "plot": {"q": _downsample(q), "I": _downsample(I),
                      "model": _downsample(model) if model is not None else None,
                      "sigma": _downsample(sig) if sig is not None else None}}
    _store_result(path.name, entry)
    conf = summary["confidence"]
    tag = "ok" if conf >= 0.6 else ("warn" if conf >= 0.3 else "info")
    r = summary["radius"]
    _emit(f"✓ {path.name}: R={r} PDI={summary['pdi']} conf={conf} ({summary['distribution']})", tag)
    # record in the manifest (best-effort)
    if _project_root:
        try:
            # The TARGET goes in per entry, not just once per campaign: an
            # operator can abort and restart with a different target mid-run, and
            # only a per-fit copy stays true about what this fit was judged against.
            _params = {"model": "polydisperse_sphere",
                       "distribution": summary["distribution"]}
            if _campaign_cfg:
                _params.update({
                    "campaign_id":  _campaign_id,
                    "target_size":  _campaign_cfg.get("target_size"),
                    "tolerance":    _campaign_cfg.get("tolerance"),
                    "pdi_cap":      _campaign_cfg.get("pdi_cap"),
                })
            # Provenance is derived from the FILE, not the current campaign state,
            # so a re-analysis after a restart/target-change still records what
            # this fit actually pertains to. `simulated` keeps mock and real fits
            # distinguishable in the manifest (the simulator stamps simulated=1,
            # which flows into the .dat metadata footer).
            _m = _RUN_RE.search(path.name)
            _prov = {
                "recipe_id": recipe_id_from_filename(path.name),
                "run_tag":   (f"Run{_m.group(1)}" if _m else ""),
                "q_unit":    ("A^-1->nm^-1" if _q_is_angstrom(hdr) else "nm^-1"),
                "source":    ("simulated" if _is_simulated(_meta, hdr) else "measured"),
            }
            update_manifest(_project_root, lambda mf: add_analysis_entry(
                mf, analysis_type="nanoparticle", file_path=path,
                params=_params, provenance=_prov,
                results=summary, quality_score=conf))
        except Exception as exc:
            _emit(f"⚠ manifest write failed: {exc}", "warn")

    # ── publish the result so the reactor can report it against its recipe ────
    # Every fit gets a durable record (Results/Fit/ — PNG + .dat, see
    # _write_fit_record) so it can be checked or replotted after the beamtime.
    # A low-confidence fit additionally gets its PNG attached to notifications.
    # Drive the closed loop FIRST, so the loss for THIS recipe is in the campaign
    # history before fit.complete is published — otherwise _last_loss_for(rid)
    # below sees only the previous recipe's history and always reported None.
    _feed_campaign(path.name, res)          # drive the closed loop, if a campaign is running

    try:
        rid = recipe_id_from_filename(path.name)
        suspect = (conf or 0.0) <= QC_CONF_THRESHOLD
        png = _write_fit_record(path, q, I, sig, model, summary, res)
        if _bus is not None:
            _bus.publish("fit.complete", {
                "recipe_id": rid, "file": path.name,
                "size": summary["radius"], "pdi": summary["pdi"],
                "confidence": conf, "distribution": summary["distribution"],
                "phase": summary["phase"], "guinier_rg": summary["guinier_rg"],
                "suspect": suspect, "plot_png": png,
                "loss": _last_loss_for(rid),
            })
    except Exception as exc:
        _emit(f"⚠ could not publish fit.complete: {exc}", "warn")


# ── folder watcher ─────────────────────────────────────────────────────────────
_handled: dict = {}
_lastsig: dict = {}
# Guards the intake memos above. They are mutated by the watcher thread and
# CLEARED by request threads (set_project / api_folder), and snapshotted by
# _save_campaign — so an unguarded iteration (the cleanup/cap loops, or the save
# snapshot) could raise "dictionary changed size during iteration". A dedicated
# lock, NOT _campaign_lock: the watcher calls _feed_campaign (which takes
# _campaign_lock), so reusing it here would deadlock.
_intake_lock = threading.Lock()


def _snapshot_handled() -> dict:
    """A consistent copy of _handled for persistence, taken under _intake_lock so
    it can't race the watcher's cleanup/cap loops (RuntimeError: dict changed
    size during iteration)."""
    with _intake_lock:
        return {k: list(v) for k, v in _handled.items()}


#: More than this many profiles wanting a fit in ONE 3 s poll is not a live
#: pipeline — the closed loop produces a handful per poll at most. It is a
#: historical backlog, and fitting one serially (a curve_fit plus a matplotlib
#: Fit record each, ~1 s a file) starves the live frame for minutes. That is
#: the reported stall: a fresh run whose data waited behind 163 old profiles.
_BACKLOG_TRIAGE_N = 12


def _triage_backlog(go: list) -> list:
    """Given this poll's fit-me list (oldest first), take any HISTORICAL
    backlog out of it and seed those files as handled instead.

    Belt to _reseed_intake's braces. Seeding at boot, on set_project, and on
    every intake reset should mean a backlog never forms — but if one ever
    does (a deleted Results/Fit/ folder, a clock jump, some future path that
    empties _handled without reseeding), this bounds the cost to one poll
    instead of stalling the live run until the whole back-catalogue is re-fit.

    Kept: anything written within _CRASH_GAP_WINDOW_S — live data, plus what
    landed just before a crash — and the newest _BACKLOG_TRIAGE_N regardless,
    so a genuine burst is never throttled. Dropped files are seeded as handled
    (not merely skipped) so they don't come back on the next poll. Order is
    preserved: the campaign is still fed oldest-first.
    """
    if len(go) <= _BACKLOG_TRIAGE_N:
        return go
    now = time.time()
    cutoff = len(go) - _BACKLOG_TRIAGE_N
    keep, drop = [], []
    for i, (f, sig) in enumerate(go):
        recent = (now - sig[1] / 1e9) < _CRASH_GAP_WINDOW_S
        (keep if (recent or i >= cutoff) else drop).append((f, sig))
    if drop:
        with _intake_lock:
            for f, sig in drop:
                _handled[str(f)] = sig
        _emit(f"⏭ {len(drop)} older profile(s) skipped — no Fit record and written "
              f"over {int(_CRASH_GAP_WINDOW_S // 60)} min ago, so they are history, "
              f"not this run. Fitting them would stall the live loop; use "
              f"“Continue a stopped Target Run” to rebuild a previous campaign.",
              "warn")
    return keep


def _watch_once() -> None:
    """One poll of the watched folder. Extracted from _watcher's loop so the
    tests can drive a single poll directly, with no thread and no Flask app."""
    d = _resolve_sub()
    if d.is_dir():
        # non-recursive: analyze only the flat Subtracted/*.dat, NOT the
        # Good/ & NeedsReview/ copies the Quality app makes (avoids re-analysis)
        files = sorted(d.glob("*.dat"), key=lambda p: p.stat().st_mtime)
        present = set()
        # Decide for every file FIRST, fit second: the fit-me list has to be
        # known in full before any fitting starts, or _triage_backlog can't
        # tell a two-file poll from a two-hundred-file one.
        go: list = []
        for f in files:
            key = str(f); present.add(key)
            try:
                st = f.stat(); sig = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
            action = decide_intake(key, sig, _handled, _lastsig)
            if action == "skip":
                continue
            if action == "wait":
                _lastsig[key] = sig; continue
            go.append((f, sig))

        for f, sig in _triage_backlog(go):
            _analyze_file(f)
            with _intake_lock:
                _handled[str(f)] = sig; _lastsig.pop(str(f), None)
        with _intake_lock:
            for k in [k for k in _lastsig if k not in present]:
                _lastsig.pop(k, None)
            # `_handled` used to grow forever. Drop entries whose file is
            # no longer in the folder, then hard-cap it — an overnight
            # campaign otherwise accumulates thousands of dead keys.
            for k in [k for k in _handled if k not in present]:
                _handled.pop(k, None)
            if len(_handled) > _MAX_RESULTS * 2:
                for k in list(_handled)[:len(_handled) - _MAX_RESULTS]:
                    _handled.pop(k, None)
    _expire_pending()      # self-heal a proposal whose data never arrived


def _watcher() -> None:
    while True:
        try:
            _watch_once()
        except Exception:
            pass
        time.sleep(3.0)


# Resume a campaign that was running before this process restarted. Without this
# the closed loop ended silently on any restart: fits kept running, but no new
# condition was ever proposed and the reactor idled until morning.
def _boot_resume() -> None:
    time.sleep(1.0)          # let the project root arrive from the hub first
    try:
        _restore_campaign()
    except Exception as exc:
        _emit(f"⚠ campaign resume failed: {exc}", "warn")


#: How recent a Fit-record-less file must be to count as a possible crash-gap
#: profile (see _seed_handled_at_boot). The watcher polls every 3 s, so a few
#: minutes comfortably covers "written just before the process died" while
#: still treating anything genuinely old as historical, not in-flight.
_CRASH_GAP_WINDOW_S = 600.0


def _seed_handled_locked() -> int:
    """The body of _seed_handled_at_boot, with _intake_lock ALREADY held.
    Returns how many profiles were seeded. Separate from the public wrapper so
    _reseed_intake() can clear and reseed in one atomic critical section — the
    watcher must never observe an empty _handled, which is what makes it re-fit
    the whole back-catalogue."""
    d = _resolve_sub()
    if not d.is_dir():
        return 0
    fit_dir = _resolve_fit()
    now = time.time()
    n = 0
    for f in d.glob("*.dat"):
        try:
            st = f.stat()
        except OSError:
            continue
        has_record = (fit_dir / f"fit_{f.stem}.dat").is_file()
        if not has_record and (now - st.st_mtime) < _CRASH_GAP_WINDOW_S:
            continue              # recently written, never fit — let the watcher handle it
        _handled[str(f)] = (st.st_size, st.st_mtime_ns)
        n += 1
    return n


def _seed_handled_at_boot() -> None:
    """Mark every already-fit profile as handled WITHOUT fitting it, so a
    restart doesn't re-fit an entire prior campaign's history — this is the
    FRESH default: instant startup, nothing re-analysed.

    A RECENTLY-WRITTEN file with no matching Results/Fit/ record is left
    alone — it will be fit normally on the watcher's next poll. This closes
    the crash-gap: a profile that landed on disk but was never fit before the
    process died must not be silently marked "already seen". "Recent" is
    bounded to _CRASH_GAP_WINDOW_S: older files missing a Fit record are
    historical (most commonly, fit before "every fit gets a durable record"
    existed) and must still be seeded — otherwise every restart re-fits the
    project's entire pre-that-feature history, which is the exact re-fit
    storm this function exists to prevent."""
    try:
        with _intake_lock:
            _seed_handled_locked()
    except Exception:
        pass


def _reseed_intake(reason: str) -> None:
    """Drop the intake memos and IMMEDIATELY reseed them from the durable
    Results/Fit/ records, in one critical section.

    Every caller that wants a clean slate must come through here. A bare
    ``_handled.clear()`` leaves the watcher believing that every profile in
    Subtracted/ is new: on its next poll it re-fits the entire back-catalogue,
    oldest first, one file at a time — and because that is the same single
    watcher thread that fits live data, the frames from the run the operator
    just started sit behind the whole backlog. That is the reported stall
    (163 historical profiles re-fit while a fresh run starved), and the reason
    it survived _seed_handled_at_boot: seeding only ran at boot and on
    set_project, not on abort or on a folder/gate change."""
    with _intake_lock:
        _handled.clear(); _lastsig.clear()
        n = _seed_handled_locked()
    if n:
        _emit(f"↺ intake reset ({reason}) — {n} already-fit profile(s) seeded, "
              f"not re-analysed", "info")


if os.environ.get("SWAXS_NO_WATCH", "").strip().lower() not in ("1", "true", "yes"):
    _seed_handled_at_boot()
    threading.Thread(target=_boot_resume, daemon=True).start()
    threading.Thread(target=_watcher, daemon=True).start()


# ── routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    with _results_lock:
        return jsonify({"status": "ok", "app": "analyzer", "analyzed": len(_results)})


@app.route("/api/project")
def api_project():
    return jsonify({"project_root": _project_root, "watching": str(_resolve_sub())})


@app.route("/api/restart_notice")
def api_restart_notice():
    """Whether an interrupted campaign is awaiting a manual Start (loud), was resumed
    under the opt-in flag (calm), or there is nothing to report — so a stopped
    campaign never silently looks idle after a restart."""
    return jsonify(_CAMPAIGN_NOTICE)


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    p = (request.get_json(silent=True) or {}).get("path", "").strip()
    if p:
        os.environ["SWAXS_PROJECT"] = p
        _project_root = p
        threading.Thread(target=_boot_resume, daemon=True).start()
        _reseed_intake("project changed")   # drop the old seed, reseed from the new project
        _emit(f"📁 project → {p}", "info")
    return jsonify({"ok": True, "watching": str(_resolve_sub())})


@app.route("/api/folder", methods=["GET", "POST"])
def api_folder():
    global _sub_folder, _gate_mode, _gate_note_shown
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        f = (body.get("folder", "") or "").strip()
        if f:
            _sub_folder = f
            _reseed_intake("watched folder changed")
            _gate_note_shown = False
            _emit(f"📁 watching → {f}", "info")
        g = str(body.get("gate", "") or "").strip().lower()
        if g in ("auto", "good", "off"):
            _gate_mode = g
            _reseed_intake("quality-gate mode changed")
            _gate_note_shown = False
            _emit(f"🔒 quality gate mode → {g}"
                  + (" (rejected profiles WILL be analysed)" if g == "off" else ""),
                  "warn" if g == "off" else "ok")
    return jsonify({"folder": _sub_folder, "resolved": str(_resolve_sub()),
                    "gate": _gate_mode})


@app.route("/api/results")
def api_results():
    with _results_lock:
        return jsonify({"results": [e["summary"] for e in _results.values()]})


@app.route("/api/result/<name>")
def api_result(name):
    with _results_lock:
        e = _results.get(name)
    if not e:
        return jsonify({"error": "not found"}), 404
    return jsonify({"summary": e["summary"], "full": e["full"], "plot": e["plot"]})


def _campaign_status() -> dict:
    if _campaign is None:
        return {"status": "idle"}
    # Snapshot under the lock: the watcher thread pops _pending inside
    # _campaign_lock, so an unguarded list(_pending.keys()) here (called from the
    # 1 Hz SSE stream and GET /api/campaign) could raise "dictionary changed size
    # during iteration" and kill the client's event stream.
    with _campaign_lock:
        if _campaign is None:
            return {"status": "idle"}
        st = _campaign.status()
        st["pending"] = list(_pending.keys())
    st["conditions_folder"] = str(_resolve_cond())
    return st


@app.route("/api/campaign", methods=["GET"])
def api_campaign():
    return jsonify(_campaign_status())


@app.route("/api/campaign/incomplete")
def api_campaign_incomplete():
    """{} if there's nothing to continue, else {run_tag, used, budget,
    best_size} for the "Continue RunN — X of Y used" button label."""
    if _campaign is not None:
        return jsonify({})
    rec = _latest_incomplete_run()
    if rec is None:
        return jsonify({})
    campaign_id = str(rec.get("campaign_id") or "")
    fit_recs = _load_fit_records_for_campaign(campaign_id)
    sized = [r for r in fit_recs if r.get("size") is not None]
    best = (min(sized, key=lambda r: abs(r["size"] - float(rec.get("target_size", 0))))
            if sized else None)
    return jsonify({
        "run_tag": rec.get("run_tag") or f"Run{rec.get('run_no')}",
        "used": len(fit_recs), "budget": int(rec.get("budget") or 0),
        "best_size": (best or {}).get("size"),
    })


@app.route("/api/campaign/continue", methods=["POST"])
def api_campaign_continue():
    try:
        summary = _continue_run()
        return jsonify({"ok": True, **summary})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/campaign/start", methods=["POST"])
def api_campaign_start():
    global _campaign, _campaign_cfg, _campaign_id, _campaign_meta, _run_tag, _run_seq
    b = request.get_json(silent=True) or {}
    try:
        space = ParameterSpace.from_config(load_config())
        with _campaign_lock:
            # Refuse to start over a live run: a second start would drop the running
            # campaign's history, orphan its pending conditions, and re-derive the
            # SAME RunN tag (its data isn't finalized on disk yet) → overwrite risk.
            # Abort it explicitly first (which clears the slate).
            if _campaign is not None and _campaign.status_str == "running":
                return jsonify({"ok": False, "error": "a campaign is already "
                                "running — abort it before starting a new one"}), 409
            _pending.clear()
            _campaign = CampaignController(
                space,
                target_size=float(b.get("target_size", 5.0)),
                tolerance=float(b.get("tolerance", 0.3)),
                pdi_cap=float(b.get("pdi_cap", 0.15)),
                budget=int(b.get("budget", 25)),
                n_init=int(b.get("n_init", 10)))
            _campaign_id = (datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                            + "_" + uuid.uuid4().hex[:4])
            # New Target Run: derive N from disk (max existing + 1) and reset the
            # per-run proposal counter. Must be set BEFORE _advance_campaign(),
            # which mints the first recipe_id via _new_rid().
            _run_no = _next_run_no()
            _run_tag = f"Run{_run_no}"
            _run_seq = 0
            _campaign_cfg = {
                "target_size": _campaign.target_size,
                "tolerance": _campaign.tolerance,
                "pdi_cap": _campaign.pdi_cap,
                "budget": _campaign.budget,
                "n_init": int(b.get("n_init", 10)),
            }
            _campaign_meta = {
                "objective": "min ((size - target_size)/tolerance)^2 + w*(PDI/pdi_cap)",
                "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "operator": os.environ.get("SWAXS_USER_ID", "") or "",
                "run_no": _run_no,          # Target-Run number (also read back by _next_run_no)
                "run_tag": _run_tag,
            }
            _campaign.start()
            _emit(f"🚀 campaign started — target R={_campaign.target_size}±{_campaign.tolerance} nm, "
                  f"PDI<{_campaign.pdi_cap}, budget {_campaign.budget}", "ok")
            _advance_campaign()             # emit the first condition
        _save_campaign()
        # Durable record of the TARGET, in the two places that outlive the run.
        _record_campaign_in_manifest()
        _write_campaign_record()
        return jsonify({"ok": True, "campaign": _campaign_status()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/campaign/abort", methods=["POST"])
def api_campaign_abort():
    global _campaign, _campaign_id, _campaign_cfg, _campaign_meta, _run_tag, _run_seq
    with _campaign_lock:
        if _campaign is not None:
            _campaign.abort()
            _emit("⏹ campaign aborted by operator", "warn")
            # An operator abort does NOT pass through _advance_campaign, so save
            # the results here too — an aborted run is still a run worth keeping.
            _oc = {"outcome": "aborted", "best": _campaign.best}
            _write_campaign_record(_oc)
            _write_campaign_results(_oc)
            # Refresh the manifest to "aborted" BEFORE clearing _campaign_id (the
            # recorder no-ops without it) — otherwise the manifest campaign stays
            # "running" forever after an operator abort.
            _record_campaign_in_manifest(_oc)
            # Reset to a CLEAN SLATE so the next start is a brand-new Target Run,
            # never a continuation. The durable records above are already written;
            # everything below is transient run state.
            _campaign = None
            _campaign_id = ""; _campaign_cfg = {}; _campaign_meta = {}
            _run_tag = ""; _run_seq = 0
            _pending.clear(); _pending_at.clear()
            # RESEED, never just clear: the next Start is a fresh Target Run, and
            # a bare clear would have the watcher re-fit every profile this run
            # (and every earlier one) left in Subtracted/, starving the new run's
            # live frames behind the backlog.
            _reseed_intake("campaign aborted")
            clear_state(_project_root, _CAMPAIGN_STATE)   # remove the resume file too
    return jsonify({"ok": True})


@app.route("/api/campaign/folder", methods=["GET", "POST"])
def api_campaign_folder():
    global _cond_folder
    if request.method == "POST":
        f = (request.get_json(silent=True) or {}).get("folder", "").strip()
        if f:
            _cond_folder = f
    return jsonify({"folder": _cond_folder, "resolved": str(_resolve_cond())})


# ── parameter-space diagnostics ──────────────────────────────────────────────
# Read-only views of what the optimizer currently believes. These endpoints must
# never advance the campaign: they use peek(), not ask(). An operator opening the
# panel mid-run must not change which recipe the reactor is told to make next.
# See docs/PARAMETER_SPACE_AND_CONVERGENCE.md for how to read them.

def _truth_for_plots() -> dict | None:
    """The simulator's hidden optimum — MOCK ONLY.

    With real beam nobody knows where the optimum is, and drawing a marker there
    would be self-deception. Gated on the reactor backend being mock, exactly like
    the simulator itself.
    """
    try:
        cfg = load_config()
        backend = str((cfg.get("spec") or {}).get("backend", "mock")).strip().lower()
        if backend == "real":
            return None
        if not (cfg.get("simulator") or {}).get("enabled", False):
            return None
        from src.simulator.ground_truth import DEFAULTS
        t = dict(DEFAULTS)
        t.update({k: v for k, v in ((cfg.get("simulator") or {}).get("truth") or {}).items()
                  if k in DEFAULTS})
        return t
    except Exception:
        return None


@app.route("/api/campaign/diagnostics")
def api_campaign_diagnostics():
    """JSON summary: is the loop still learning, and is it still roaming?"""
    with _campaign_lock:
        if _campaign is None:
            return jsonify({"ok": False, "error": "no campaign"}), 404
        try:
            from src.optimizer import diagnostics as dg
            return jsonify({"ok": True, "summary": dg.summary(_campaign),
                            "convergence": dg.convergence(_campaign),
                            "names": _campaign.space.names(),
                            "has_truth": _truth_for_plots() is not None})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/campaign/plot/<view>.png")
def api_campaign_plot(view: str):
    """Server-rendered figure — the SAME renderer that made the report figures."""
    xn = request.args.get("x") or None
    yn = request.args.get("y") or None
    anchor = request.args.get("anchor", "best")
    with _campaign_lock:
        if _campaign is None:
            from src.optimizer.plots import _empty
            png = _empty("No campaign running — start one to see the recipe space.")
        else:
            from src.optimizer import plots as opl
            kw = {"truth": _truth_for_plots()}
            if view == "slice":
                kw.update({"xname": xn, "yname": yn, "anchor_mode": anchor})
            png = opl.figure(view, _campaign, **kw)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/stream")
def api_stream():
    """Incremental stream: only summaries NEWER than what this client already has.

    The first frame carries a bounded snapshot (newest first) so the table fills
    immediately; after that each frame is normally empty or a single row. This is
    the difference between ~0.8 MB/s and a few hundred bytes per second once a
    campaign has produced thousands of profiles.
    """
    def gen():
        last_log = 0
        sent_seq = 0
        first = True
        while True:
            with _log_lock:
                new_logs = [ln for (s, ln) in _log if s > last_log]
                if _log:
                    last_log = _log[-1][0]
            with _results_lock:
                total = len(_results)
                if first:
                    rows = [e["summary"] for e in list(_results.values())[-_SNAPSHOT:]]
                else:
                    rows = [e["summary"] for e in _results.values()
                            if int(e["summary"].get("seq", 0)) > sent_seq]
                if rows:
                    sent_seq = max(sent_seq,
                                   max(int(r.get("seq", 0)) for r in rows))
            payload = {"results": rows, "logs": new_logs,
                       "campaign": _campaign_status(),
                       "total": total, "seq": sent_seq,
                       "reset": first}
            first = False
            yield "data: " + json.dumps(payload) + "\n\n"
            time.sleep(1.0)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    _project_root = os.environ.get("SWAXS_PROJECT", _project_root)
    print("━" * 52)
    print("  Auto-Fit & Optimiser  →  http://localhost:5107")
    print(f"  watching: {_resolve_sub()}")
    print("━" * 52)
    app.run(host="127.0.0.1", port=5107, debug=False, threaded=True)
