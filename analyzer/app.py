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
from src.optimizer import ParameterSpace, CampaignController             # noqa: E402
from src.optimizer.io import (to_param_file, match_recipe_id,           # noqa: E402
                              recipe_id_from_filename)
from src.runstate import save_state, load_state, save_monitor, load_monitor  # noqa: E402

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


def _new_rid() -> str:
    return "auto_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]


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
    elif st == "exhausted":
        _emit(f"⏹ campaign budget exhausted ({_campaign.status()['n_evaluations']} runs) — "
              f"best size {(_campaign.best or {}).get('size')}", "warn")
        _oc = {"outcome": "exhausted", "best": _campaign.best,
               "n_evaluations": _campaign.status().get("n_evaluations")}
        _write_campaign_record(_oc); _write_campaign_results(_oc)
    elif st == "aborted":
        _emit("⏹ campaign aborted", "warn")
        _oc = {"outcome": "aborted", "best": _campaign.best}
        _write_campaign_record(_oc); _write_campaign_results(_oc)


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
                if recipe_id in str(rec.get("recipe_id", "")) or \
                        recipe_id in str(rec.get("params", {}).get("recipe_id", "")):
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


def _campaign_record() -> dict:
    """Everything needed to answer "what was this run aiming for?" later."""
    rec = {"campaign_id": _campaign_id, **(_campaign_cfg or {}), **(_campaign_meta or {})}
    if _campaign is not None:
        rec["status"] = _campaign.status_str
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


def _record_campaign_in_manifest() -> None:
    """Put the target in the permanent cross-app record, so the assistant and
    anyone reading the experiment afterwards can see it. Best-effort."""
    if not _project_root or not _campaign_id:
        return
    try:
        update_manifest(_project_root,
                        lambda mf: set_project_meta(mf, campaign=_campaign_record()))
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
            "handled": {k: list(v) for k, v in _handled.items()},
        })
    except Exception as exc:
        _emit(f"⚠ could not save the campaign state: {exc}", "warn")


def _restore_campaign() -> None:
    """Rebuild the campaign from disk by replaying its history through tell().

    Replay must NOT re-emit condition files or notifications, so the proposal
    step is skipped while replaying — the pending set is restored verbatim
    instead.
    """
    global _campaign, _campaign_cfg, _campaign_id, _campaign_meta
    # NEVER clobber a live campaign. _boot_resume runs on a timer ~1 s after
    # import, so an operator (or a test) who starts a campaign inside that window
    # would have their running campaign silently replaced by the rebuilt one from
    # disk — losing its history and resetting its status to "running", including
    # after it had already converged.
    if _campaign is not None:
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
    try:
        space = ParameterSpace.from_config(load_config())
        camp = CampaignController(space, **cfg)
        camp.start()
        for rec in hist:                       # rebuild the GP from real results
            camp.tell(rec.get("params") or {}, rec.get("size"),
                      rec.get("pdi"), float(rec.get("confidence") or 0.0))
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
            for k, v in (st.get("handled") or {}).items():
                try:
                    _handled[k] = tuple(v)
                except Exception:
                    pass
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
                _campaign.tell(params, None, None, 0.0)
            except Exception as exc:
                _emit(f"⚠ could not record the failed measurement: {exc}", "warn")
        if stale:
            _advance_campaign()
    if stale:
        _save_campaign()


def _feed_campaign(name: str, res: dict) -> None:
    """Match a measured profile to a pending proposed condition and drive the loop."""
    with _campaign_lock:
        if _campaign is None or _campaign.status_str != "running":
            return
        rid = match_recipe_id(name, _pending.keys())
        if not rid:
            return
        params = _pending.pop(rid)
        _pending_at.pop(rid, None)
        sz = res.get("size") or {}
        size = sz.get("radius")
        pdi = res.get("pdi")
        conf = res.get("confidence", 0.0)
        _campaign.tell(params, size, pdi, conf)
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
            update_manifest(_project_root, lambda mf: add_analysis_entry(
                mf, analysis_type="nanoparticle", file_path=path,
                params=_params,
                results=summary, quality_score=conf))
        except Exception as exc:
            _emit(f"⚠ manifest write failed: {exc}", "warn")

    # ── publish the result so the reactor can report it against its recipe ────
    # Every fit gets a durable record (Results/Fit/ — PNG + .dat, see
    # _write_fit_record) so it can be checked or replotted after the beamtime.
    # A low-confidence fit additionally gets its PNG attached to notifications.
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

    _feed_campaign(path.name, res)          # drive the closed loop, if a campaign is running


# ── folder watcher ─────────────────────────────────────────────────────────────
_handled: dict = {}
_lastsig: dict = {}


def _watcher() -> None:
    while True:
        try:
            d = _resolve_sub()
            if d.is_dir():
                # non-recursive: analyze only the flat Subtracted/*.dat, NOT the
                # Good/ & NeedsReview/ copies the Quality app makes (avoids re-analysis)
                files = sorted(d.glob("*.dat"), key=lambda p: p.stat().st_mtime)
                present = set()
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
                    _analyze_file(f)
                    _handled[key] = sig; _lastsig.pop(key, None)
                for k in [k for k in _lastsig if k not in present]:
                    _lastsig.pop(k, None)
                # `_handled` used to grow forever. Drop entries whose file is no
                # longer in the folder, then hard-cap it — an overnight campaign
                # otherwise accumulates thousands of dead keys.
                for k in [k for k in _handled if k not in present]:
                    _handled.pop(k, None)
                if len(_handled) > _MAX_RESULTS * 2:
                    for k in list(_handled)[:len(_handled) - _MAX_RESULTS]:
                        _handled.pop(k, None)
            _expire_pending()      # self-heal a proposal whose data never arrived
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


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    p = (request.get_json(silent=True) or {}).get("path", "").strip()
    if p:
        os.environ["SWAXS_PROJECT"] = p
        _project_root = p
        threading.Thread(target=_boot_resume, daemon=True).start()
        _handled.clear(); _lastsig.clear()      # rescan under the new project
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
            _handled.clear(); _lastsig.clear()
            _gate_note_shown = False
            _emit(f"📁 watching → {f}", "info")
        g = str(body.get("gate", "") or "").strip().lower()
        if g in ("auto", "good", "off"):
            _gate_mode = g
            _handled.clear(); _lastsig.clear()
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
    st = _campaign.status()
    st["pending"] = list(_pending.keys())
    st["conditions_folder"] = str(_resolve_cond())
    return st


@app.route("/api/campaign", methods=["GET"])
def api_campaign():
    return jsonify(_campaign_status())


@app.route("/api/campaign/start", methods=["POST"])
def api_campaign_start():
    global _campaign, _campaign_cfg, _campaign_id, _campaign_meta
    b = request.get_json(silent=True) or {}
    try:
        space = ParameterSpace.from_config(load_config())
        with _campaign_lock:
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
    with _campaign_lock:
        if _campaign is not None:
            _campaign.abort()
            _emit("⏹ campaign aborted by operator", "warn")
            # An operator abort does NOT pass through _advance_campaign, so save
            # the results here too — an aborted run is still a run worth keeping.
            _oc = {"outcome": "aborted", "best": _campaign.best}
            _write_campaign_record(_oc)
            _write_campaign_results(_oc)
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
