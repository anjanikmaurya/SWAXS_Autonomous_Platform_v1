"""
analyzer/app.py — Autonomous Analyser (port 5107)
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

from src.favicon import register_favicon               # noqa: E402
from src.plot_lock import MPL_LOCK                      # noqa: E402  (process-wide pyplot lock)
from src.analysis.nanoparticle import analyze_profile, model_intensity   # noqa: E402
from src.utils.read_dat_metadata import read_dat_data_metadata           # noqa: E402
from src.reactor.intake import decide_intake                             # noqa: E402
from src.manifest import (update_manifest, add_analysis_entry,          # noqa: E402
                          set_project_meta)
from src.ai.loop_advice import narrate_fit                               # noqa: E402
from src.reactor import load_config                                      # noqa: E402
from src.reactor.recipe import parse_param_file                          # noqa: E402
from src.optimizer import ParameterSpace, CampaignController, NAMES      # noqa: E402
from src.optimizer import methods as optimisers                         # noqa: E402  (pluggable optimiser registry)
from src.optimizer.intake_rules import (held_reason, check_seeds,        # noqa: E402
                                        EXPECTED_SHAPES, DEFAULT_SHAPE)
from src.optimizer import guide as _guide                               # noqa: E402
from src.optimizer import ai_design as _aid                             # noqa: E402
from src.design_knowledge import store as kstore                       # noqa: E402
from src.design_knowledge import cards as kcards                       # noqa: E402
from src.design_knowledge import literature as klit                    # noqa: E402
from src.design_knowledge.campaigns import entry_from_campaign          # noqa: E402
from src.design_knowledge.freeze import for_campaign as _knowledge_for  # noqa: E402
from src.design_knowledge import replay as kreplay                     # noqa: E402
import src.fitting as fitting                                           # noqa: E402  (pluggable fitting registry)
from src.fitting import fit_knowledge as fk                            # noqa: E402  (AI fitter knowledge)
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

# Per-app browser-tab icon, from apps.yml — ten apps on ten ports
# otherwise give ten identical tabs. See src/favicon.py.
register_favicon(app, "analyzer")

_project_root: str = os.environ.get("SWAXS_PROJECT", "")

from src.folder_browse import register_browse   # noqa: E402
register_browse(app, lambda: _project_root)    # GET /api/browse for the Browse… button
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

# ── selected methods (Setup tab) ───────────────────────────────────────────────
# Defaults reproduce today's behaviour exactly: least-squares sphere fitter and
# the GP+EI optimiser. See docs/AUTOFIT_REDESIGN.md.
from src.operator_id import current_operator as _current_operator   # noqa: E402
_active_fitter: str = fitting.DEFAULT_ID
_active_optimiser: str = optimisers.DEFAULT_ID


#: Goals that MAP a set rather than find one recipe: they run to the budget
#: instead of stopping at the first in-spec hit (see CampaignController.stop_on_hit).
_MAPPING_GOALS = ("level-set", "pareto")


def _build_optimiser(space, goal, size_range, base_cfg, method_params=None,
                     chosen=None, ai_meta=None, knowledge=None):
    """Instantiate the optimiser a campaign should use.

    The campaign GOAL, not just the operator's dropdown, picks the acquisition:
    level-set → BAX (band-membership entropy), Pareto → EHVI (size-vs-PDI
    hypervolume). target-hit / benchmark keep the manually selected optimiser.

    ``chosen`` is the strategy to honour; it defaults to the live dropdown, but a
    RESUME passes the run's recorded optimiser (and its recorded method_params)
    so a restarted TuRBO/UCB run comes back as itself with its κ / τ / trust size,
    not silently as the dropdown default. Returns (controller, resolved_id).
    """
    opt_id = optimisers.for_goal(goal, chosen or _active_optimiser)
    extra = {"stop_on_hit": (goal or "target-hit") not in _MAPPING_GOALS}
    # method-specific inputs (UCB kappa, TuRBO size, BAX tau) for the RESOLVED
    # optimiser only — never a foreign kwarg from a different method's dropdown.
    mp = method_params or {}
    for p in optimisers.params_for(opt_id):
        if p["key"] in mp:
            try:
                extra[p["key"]] = float(mp[p["key"]])
            except (TypeError, ValueError):
                pass
    # Pareto EHVI maps a diameter range; convert it to the radius the loop uses.
    if opt_id == "pareto_ehvi" and isinstance(size_range, (list, tuple)) \
            and len(size_range) == 2:
        try:
            lo, hi = sorted((float(size_range[0]), float(size_range[1])))
            extra["size_lo"], extra["size_hi"] = lo / 2.0, hi / 2.0
        except (TypeError, ValueError):
            pass
    ctl = optimisers.make(opt_id, space, **base_cfg, **extra)
    ai = (_campaign_meta or {}).get("ai") if ai_meta is None else ai_meta
    if ai and ai.get("level") in _aid.LEVELS:
        # An AI level drives the optimiser it was set up with (its shortlist and
        # its fallback); the AI never replaces the optimiser's safety checks.
        _kh = (((_campaign_meta or {}).get("knowledge") if knowledge is None else knowledge) or {}).get("harness") or {}
        ctl = _aid.AIDesign(ctl, ai["level"], review_every=int(ai.get("review_every") or 0),
                            apply=ai.get("apply") or "auto",
                            k=int(_kh.get("shortlist_k", 5)), trust=float(_kh.get("trust", 0.25)),
                            model=str(ai.get("model") or ""), thinking=str(ai.get("thinking") or "off"),
                            knowledge=(_campaign_meta or {}).get("knowledge") if knowledge is None else knowledge,
                            every_pick=bool(ai.get("every_pick")), chemistry=_chemistry(),
                            caps=(_space_cfg().get("safety") or {}))
    return ctl, opt_id


def _is_ai(c) -> bool:
    return c is not None and hasattr(c, "prepare_pick")


#: one AI job of each kind at a time; the AI call itself runs WITHOUT the lock
_ai_busy = {"pick": False, "review": False}


def _ai_pick_worker(camp) -> None:
    """Prepare under the lock, call the AI without it, commit under it again.
    The SSE stream and every other route stay responsive while the AI thinks."""
    try:
        with _campaign_lock:
            if _campaign is not camp or _paused or camp.status_str != "running":
                return
            st = camp.prepare_pick()
        if st is None:
            return
        res = camp.decide(st)
        with _campaign_lock:
            if _campaign is not camp or _paused or camp.status_str != "running":
                return
            p = camp.commit_pick(st, res)
            if p is None:
                return
            rid = _new_rid()
            _pending[rid] = p
            _pending_at[rid] = time.time()
            _write_condition(rid, p)
            camp.label_last(rid)
            d = camp.decisions[-1]
            _emit((f"🤖 {d['by']} chose shortlist #{d.get('choice')} for {rid}: {d.get('reason', '')}"
                   + (f" (based on {', '.join(d.get('cited') or [])})" if d.get("cited") else ""))
                  if not d.get("fallback") else
                  f"🤖 {_aid.LABELS[camp.level]}: {d['fallback']}; the optimiser's own pick is used for {rid}",
                  "info" if not d.get("fallback") else "warn")
    except Exception as exc:                                  # noqa: BLE001
        _emit(f"⚠ the AI pick failed ({type(exc).__name__}: {exc}); falling back to the optimiser", "warn")
        try:
            with _campaign_lock:
                if _campaign is camp and camp.status_str == "running" and not _pending and not _paused:
                    _advance_sync()
        except Exception:                                     # noqa: BLE001
            pass
    finally:
        _ai_busy["pick"] = False
        _save_campaign()


def _ai_review_worker(camp) -> None:
    try:
        with _campaign_lock:
            if _campaign is not camp:
                return
            st = camp.prepare_review()
        if st is None:
            return
        res = camp.decide(st)
        with _campaign_lock:
            if _campaign is not camp:
                return
            d = camp.commit_review(st, res)
        if d:
            _emit(f"🤖 {d['by']} review after run {d['n']}: "
                  + (d.get("note") or d.get("fallback") or "")
                  + (f" [range {d['action']}]" if d.get("bounds") else ""), "info")
    except Exception as exc:                                  # noqa: BLE001
        _emit(f"⚠ the AI review failed ({type(exc).__name__}: {exc})", "warn")
    finally:
        _ai_busy["review"] = False
        _save_campaign()


def _kick_ai(kind: str) -> bool:
    """Start an AI job in the background. Lock held by caller."""
    if _ai_busy[kind]:
        return True
    _ai_busy[kind] = True
    target = _ai_pick_worker if kind == "pick" else _ai_review_worker
    threading.Thread(target=target, args=(_campaign,), daemon=True,
                     name=f"ai-{kind}").start()
    return True


def _ai_wait(timeout: float = 30.0) -> None:
    """Tests and tools: wait for the background AI jobs to finish."""
    t_end = time.time() + timeout
    while (_ai_busy["pick"] or _ai_busy["review"]) and time.time() < t_end:
        time.sleep(0.02)


# ── closed-loop campaign state ─────────────────────────────────────────────────
_campaign: CampaignController | None = None
_pending: dict = {}          # recipe_id -> proposed params awaiting a measurement
#: Operator Pause: no NEW condition is issued while set. Results for conditions
#: already issued are still recorded, so nothing measured is lost; Resume issues
#: the next condition if none is outstanding. Persisted with the campaign.
_paused: bool = False
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
    the data path at all. So we prefer Good/ — but ONLY when it actually holds
    data.

    The trap this avoids: a bare, empty Good/ (left over from a past run, or
    present because the Quality Gate app simply isn't running this session)
    used to divert the analyzer to it the moment the *directory* existed. With
    the gate not populating it, the analyzer then watched an empty folder,
    never saw the profiles sitting in the flat Subtracted/, fitted nothing, and
    the optimizer starved — the loop silently stalled. Auto mode now keys on
    Good/ HAVING .dat data, not merely existing. `mode="good"` stays strict
    (require Good/ even if empty) for operators who opt into hard gating;
    `mode="off"` always reads the flat folder.
    """
    global _gate_note_shown
    base = _resolve_sub_base()
    mode = str(_gate_mode or "auto").lower()
    if mode == "off":
        return base
    good = base / "Good"
    if mode == "good":
        if not _gate_note_shown:
            _gate_note_shown = True
            _emit(f"🔒 quality gate REQUIRED — analysing {good} only "
                  f"(rejected/unclassified profiles are never fitted)", "ok")
        return good
    # auto: honour the gate only when it is actually producing Good/ profiles.
    # Re-evaluated every poll, so if the gate starts later the analyzer switches
    # to Good/ on the next poll with no restart.
    if good.is_dir() and any(good.glob("*.dat")):
        if not _gate_note_shown:
            _gate_note_shown = True
            _emit(f"🔒 quality gate honoured — analysing {good} only "
                  f"(rejected profiles are never fitted)", "ok")
        return good
    if not _gate_note_shown:
        _gate_note_shown = True
        if good.is_dir():
            _emit(f"⚠ Good/ exists but holds no profiles — the Quality Gate "
                  f"isn't populating it, so analysing the flat {base} instead "
                  f"(otherwise the optimizer would starve and the loop stall). "
                  f"Run the Quality Gate to gate, or set gate mode 'off' to "
                  f"silence this.", "warn")
        else:
            _emit(f"⚠ no Good/ folder — analysing every subtracted profile in "
                  f"{base}. Start the Quality Gate so bad profiles can't reach "
                  f"the optimizer.", "warn")
    return base


def _space_cfg() -> dict:
    """The reactor config the search space is built from, with the reactor's
    LIVE pump limits (<project>/reactor_limits.json, saved by the reactor app's
    pump-limit card) laid over config.yml, so ParameterSpace.valid() rejects
    the same per-pump rates the reactor would (audit Q33)."""
    cfg = load_config()
    try:
        if _project_root:
            lp = Path(_project_root) / "reactor_limits.json"
            if lp.is_file():
                lim = json.loads(lp.read_text(encoding="utf-8") or "{}").get("limits") or {}
                pumps = cfg.setdefault("pumps", {})
                for name, v in lim.items():
                    if name in pumps and isinstance(v, dict):
                        for k in ("sensor_min", "max_flow"):
                            if v.get(k) not in (None, ""):
                                pumps[name][k] = float(v[k])
    except Exception as exc:
        _emit(f"⚠ could not read reactor_limits.json for the search space: {exc}",
              "warn")
    return cfg


_DEFAULT_COND_FOLDER = "1D/SAXS/Conditions"
#: True once the operator sets the Conditions folder HERE (POST
#: /api/campaign/folder). Until then the analyzer follows the reactor's folder.
_cond_folder_explicit: bool = False
_reactor_cond_cache: tuple = (0.0, "", "")      # (time, project_root, folder)
_cond_mismatch_warned: str = ""


def _reactor_cond_folder() -> str:
    """The Conditions folder the REACTOR watches, resolved the way reactor/app.py
    does: the operator's override in <project>/reactor_settings.json
    ("recipes_folder"), else folders.recipes from the reactor config. Cached for
    a few seconds (the status stream asks every second). "" when unknown."""
    global _reactor_cond_cache
    now = time.time()
    t, root, folder = _reactor_cond_cache
    if root == _project_root and now - t < 5.0:
        return folder
    folder = ""
    try:
        if _project_root:
            sp = Path(_project_root) / "reactor_settings.json"
            if sp.is_file():
                folder = str(json.loads(sp.read_text(encoding="utf-8") or "{}")
                             .get("recipes_folder") or "")
    except Exception:
        folder = ""
    if not folder:
        try:
            folder = str((load_config().get("folders") or {}).get("recipes") or "")
        except Exception:
            folder = ""
    _reactor_cond_cache = (now, _project_root, folder)
    return folder


def _abs_folder(f: str) -> Path:
    p = Path(f)
    if not p.is_absolute():
        p = (Path(_project_root) if _project_root else Path.cwd()) / f
    return p


def _resolve_cond() -> Path:
    """Where conditions are written. Follows the reactor's folder (audit Q20)
    unless the operator chose one here explicitly; a mismatch is then reported
    by _cond_folder_check()."""
    f = _cond_folder
    if not _cond_folder_explicit and _cond_folder == _DEFAULT_COND_FOLDER:
        f = _reactor_cond_folder() or _cond_folder
    return _abs_folder(f)


def _cond_folder_check() -> dict:
    """{reactor, mismatch}: is the analyzer writing where the reactor reads?
    Warns loudly (once per differing pair) when not, since a mismatch means
    the reactor never sees a single proposed condition."""
    global _cond_mismatch_warned
    rf = _reactor_cond_folder()
    if not rf:
        return {"reactor": "", "mismatch": False}
    mine, theirs = _resolve_cond(), _abs_folder(rf)
    try:
        same = mine.resolve() == theirs.resolve()
    except Exception:
        same = str(mine) == str(theirs)
    if not same:
        key = f"{mine}|{theirs}"
        if key != _cond_mismatch_warned:
            _cond_mismatch_warned = key
            _emit(f"⚠ CONDITIONS FOLDER MISMATCH: the analyzer writes to {mine} "
                  f"but the reactor watches {theirs}. The reactor will not see "
                  f"any proposed condition. Set the same folder in both apps.",
                  "error")
            print(f"[analyzer] WARNING conditions folder mismatch: analyzer={mine} "
                  f"reactor={theirs}", file=sys.stderr, flush=True)
    else:
        _cond_mismatch_warned = ""
    return {"reactor": str(theirs), "mismatch": not same}


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


def _run_no(s: str) -> int | None:
    """The integer run number in a run tag or recipe_id ("Run12" or
    "Run12_r003" → 12), or None. Used to attribute a fit record to its run."""
    if not s:
        return None
    m = _RUN_RE.search(s if s.rstrip().endswith("_") else s + "_")
    return int(m.group(1)) if m else None


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


def _latest_run_record() -> dict | None:
    """The record of the LATEST Target Run on disk, complete or not.

    Ordered by run_no, then by started_at as a tie-break (two records can
    share a run_no if one was written before the number was derived).
    """
    best = None
    try:
        for rec_path in _resolve_results().glob("campaign_*.json"):
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            key = (int(rec.get("run_no") or 0), str(rec.get("started_at") or ""))
            if best is None or key > best["_key"]:
                best = {**rec, "_path": str(rec_path),
                        "run_no": key[0], "_key": key}
    except Exception:
        pass
    return best


def _latest_incomplete_run() -> dict | None:
    """The LATEST Target Run, but only if it is still incomplete.

    Was: the highest-numbered run with no "outcome" key, anywhere in the
    folder. That meant one run that ended without its record being finalised —
    a crash, a kill -9, a power cut — became the Continue candidate FOREVER.
    Run9 was offered for continuation after Run10, Run11, Run12 had been and
    gone, because they had outcomes and it did not. Continuing it would rebuild
    a campaign from weeks-old records and then propose conditions tagged Run9
    alongside whatever is current.

    There are only two sensible things to do with a stale run: continue the one
    you just interrupted, or start fresh. So Continue is now offered for the
    latest run and nothing else — if that run finished, there is nothing to
    continue and the answer is Start. An older unfinished run stays on disk with
    all its records; it simply is not something this button will resume.
    """
    rec = _latest_run_record()
    if rec is None or "outcome" in rec:
        return None
    return rec


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
    _cond_folder_check()            # warn loudly if the reactor reads elsewhere
    d = _resolve_cond(); d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.txt").write_text(to_param_file(rid, params), encoding="utf-8")
    sp = " ".join(f"{k}={float(v):g}" for k, v in params.items())
    _emit(f"➡ proposed {rid}: {sp}", "ok")


def _clear_conditions_for_new_campaign(why: str = "a new campaign started") -> int:
    """Set aside any leftover condition files before a NEW campaign proposes its
    first one.

    The reactor is a pure consumer and no longer clears its queue on boot (that
    is what makes startup order-free), so the responsibility for staleness sits
    here, with the producer: when the operator starts a NEW campaign, whatever
    conditions are still queued belong to an old/aborted run and must not be
    dosed against the new target. This is called ONLY from campaign *start* —
    never from resume/continue, which deliberately re-issues its own in-flight
    conditions.

    NOTHING IS DELETED. Leftovers are moved into Conditions/done/ with a note,
    mirroring how the reactor retires a consumed condition, so one can be put
    back by moving it out again.
    """
    try:
        cond = _resolve_cond()
        if not cond.is_dir():
            return 0
        done = cond / "done"
        leftovers = sorted(list(cond.glob("*.dat")) + list(cond.glob("*.txt"))
                           + list(cond.glob("*.json")))
        if not leftovers:
            return 0
        done.mkdir(parents=True, exist_ok=True)
        moved = []
        for f in leftovers:
            if f.is_dir():
                continue
            try:
                dest = done / f.name
                f.replace(dest)
                with dest.open("a", encoding="utf-8") as fh:
                    fh.write(f"\n# ── NOT RUN — set aside because {why} at "
                             f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
                             f"# These conditions belong to a run that no longer "
                             f"exists. Move this file back into the Conditions "
                             f"folder to run it.\n")
                moved.append(f.name)
            except Exception as exc:
                _emit(f"⚠ could not set aside {f.name}: {exc}", "warn")
        if moved:
            _emit(f"🧹 {why}: set aside {len(moved)} queued "
                  f"condition(s) "
                  f"({', '.join(moved[:6])}{' …' if len(moved) > 6 else ''}); "
                  f"they are in {done} and were NOT run.", "info")
        return len(moved)
    except Exception as exc:
        _emit(f"⚠ could not clear leftover conditions: {exc}", "warn")
        return 0


def _withdraw_condition_file(rid: str, why: str) -> bool:
    """Move this recipe's condition file(s) out of Conditions/ into done/ with a
    NOT RUN note, the same way _clear_conditions_for_new_campaign does. Never
    raises; returns True if a file was moved."""
    moved = False
    try:
        cond = _resolve_cond()
        done = cond / "done"
        for ext in (".txt", ".dat", ".json"):
            f = cond / f"{rid}{ext}"
            if not f.is_file():
                continue
            done.mkdir(parents=True, exist_ok=True)
            dest = done / f.name
            f.replace(dest)
            with dest.open("a", encoding="utf-8") as fh:
                fh.write(f"\n# ── NOT RUN: withdrawn because {why} at "
                         f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
                         f"# Move this file back into the Conditions folder to "
                         f"run it.\n")
            moved = True
        if moved:
            _emit(f"↩ withdrew {rid} from the reactor queue ({why})", "info")
    except Exception as exc:
        _emit(f"⚠ could not withdraw {rid}: {exc}", "warn")
    return moved


def _advance_campaign() -> None:
    """Emit the next condition, or report the campaign has stopped. Lock held by caller."""
    if _campaign is None:
        return
    if _campaign.status_str == "running":
        if _paused:
            return                      # Pause: hold back the next condition
        if _is_ai(_campaign) and _campaign.wants_ai_pick():
            _kick_ai("pick")            # the AI chooses in the background
            return
        _advance_sync()
        return
    _report_end()


def _advance_sync() -> None:
    """The optimiser's own next proposal (cold start, Advisor, or a fallback). Lock held."""
    p = (_campaign.inner.ask() if _is_ai(_campaign) else _campaign.ask())
    if getattr(_campaign, "last_error", None):
        _emit(f"⚠ search strategy failed ({_campaign.last_error}); proposed a "
              f"space-filling recipe instead so the loop keeps moving "
              f"(fallback #{_campaign.n_fallbacks})", "warn")
    if p is not None:
        rid = _new_rid()
        _pending[rid] = p
        _pending_at[rid] = time.time()
        _write_condition(rid, p)
        return
    _report_end()


def _report_end() -> None:
    """Report a campaign that has stopped. Lock held by caller."""
    st = _campaign.status_str
    if st == "converged":
        cc = _campaign.converged_condition or {}
        _emit(f"🎯 campaign CONVERGED — size {cc.get('size')} at "
              f"{ {k: round(v,1) for k,v in (cc.get('params') or {}).items()} }", "ok")
        _oc = {"outcome": "converged", "converged_condition": cc,
               "n_evaluations": _campaign.status().get("n_evaluations")}
        _write_campaign_record(_oc); _write_campaign_results(_oc)
        _record_campaign_in_manifest(_oc)
    elif st == "exhausted" and not getattr(_campaign, "stop_on_hit", True):
        # A mapping goal (level-set / Pareto) is SUPPOSED to use its whole budget.
        _emit(f"✓ mapping complete: {_campaign.status()['n_evaluations']} recipes "
              f"measured across the requested space", "ok")
        _oc = {"outcome": "mapped", "best": _campaign.best,
               "converged_condition": _campaign.converged_condition,
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
        if _is_ai(_campaign):
            body["ai_decisions"] = _campaign.decisions
        (d / "campaign.json").write_text(
            json.dumps(body, indent=2, default=str), encoding="utf-8")

        hist = _campaign.history or []
        if hist:
            keys = ["recipe_id", "size", "pdi", "confidence", "loss", "model", "held"]
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
        _join_knowledge_set(body)
        return str(d)
    except Exception as exc:
        _emit(f"⚠ could not save the campaign results: {exc}", "warn")
        return ""


def _join_knowledge_set(record: dict) -> None:
    """A finished campaign joins the knowledge set it used, with its measured
    runs, so the next campaign on this chemistry can warm-start from it.
    Never raises: losing this must not stop a campaign from ending cleanly."""
    try:
        sid = ((_campaign_meta or {}).get("knowledge") or {}).get("set_id")
        if not sid or _campaign is None:
            return
        entry = entry_from_campaign(record, _campaign.history, _project_root or "")
        if kstore.add_campaign(sid, entry, _current_operator(_project_root)):
            _emit(f"📚 {entry.get('run_tag') or entry['campaign_id']} joined the knowledge set "
                  f"{sid} ({len(entry['runs'])} measured run(s))", "ok")
    except Exception as exc:
        _emit(f"⚠ could not add this campaign to its knowledge set: {exc}", "warn")


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
            "paused": bool(_paused),
            "ai_state": _campaign.to_state() if _is_ai(_campaign) else None,
            "history": _campaign.history,
            "pending": _pending,
            "pending_at": _pending_at,
            "handled": _snapshot_handled(),
        })
    except Exception as exc:
        _emit(f"⚠ could not save the campaign state: {exc}", "warn")


def _restore_fitter(meta: dict) -> str | None:
    """The fitter belongs to the campaign (audit AN1): a resumed or continued run
    must fit with the method it started with, not the app's default. Returns an
    error text when that fitter is not available here (the caller pauses)."""
    global _active_fitter
    fid = str((meta or {}).get("fitter") or "")
    if not fid:
        return None                       # an older record: keep the current fitter
    ready = {m["id"] for m in fitting.available() if m.get("ready", True)}
    if fid not in ready:
        return (f"this campaign was fitted with '{fid}', which is not available here; "
                "it is paused so the scoring does not change. Make it available and Resume, "
                "or Abort")
    if fid != _active_fitter:
        _emit(f"🧩 fitter restored for this campaign: {fid} (was {_active_fitter})", "info")
    _active_fitter = fid
    return None


def _restore_campaign() -> None:
    """Rebuild the campaign from disk by replaying its history through tell().

    Replay must NOT re-emit condition files or notifications, so the proposal
    step is skipped while replaying — the pending set is restored verbatim
    instead.
    """
    global _campaign, _campaign_cfg, _campaign_id, _campaign_meta, _run_tag, _run_seq
    global _CAMPAIGN_NOTICE, _paused
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
    _fit_problem = _restore_fitter(_campaign_meta)
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
        space = ParameterSpace.from_config(_space_cfg())
        # Rebuild the SAME acquisition the run used: for level-set/pareto the goal
        # pins it (BAX / EHVI), recorded in meta["optimiser"]; _build_optimiser
        # re-derives it from the goal so a resume never silently swaps methods.
        camp, _ = _build_optimiser(space, _campaign_meta.get("goal"),
                                   _campaign_meta.get("size_range"), cfg,
                                   method_params=_campaign_meta.get("method_params"),
                                   chosen=_campaign_meta.get("optimiser"),
                                   ai_meta=_campaign_meta.get("ai") or {},
                                   knowledge=_campaign_meta.get("knowledge") or {})
        # the operator's own starting recipes come first again, so the cold
        # start replays in the same order it was issued
        camp.start(seeds=_campaign_meta.get("seeds"), prior=_campaign_meta.get("prior"))
        for rec in hist:                       # rebuild the GP from real results
            camp.tell(rec.get("params") or {}, rec.get("size"),
                      rec.get("pdi"), float(rec.get("confidence") or 0.0),
                      recipe_id=rec.get("recipe_id", ""),
                      held=rec.get("held"), model=rec.get("model"))
            if rec.get("accepted"):
                camp.history[-1]["accepted"] = True
        if _is_ai(camp):
            camp.from_state(st.get("ai_state"))
        # Next ask() must skip every proposal already issued: the replayed results
        # AND the conditions still in flight. Counting only history re-proposed an
        # in-flight Sobol seed when a restart landed during seeding.
        camp._n_asked = len(camp.history) + len(st.get("pending") or {})
        if _run_tag:
            # the r-number counts every ask() this run made (see _continue_run)
            camp._n_asked = max(camp._n_asked, _run_seq)
        with _campaign_lock:
            _campaign = camp
            _campaign_cfg = cfg
            _paused = bool(st.get("paused")) or bool(_fit_problem)
            _pending.clear(); _pending.update(st.get("pending") or {})
            # Restart the expiry clock NOW for every pending condition (audit
            # Q19). The saved proposal times include the downtime, so a restart
            # after an hour expired every in-flight condition on the first poll,
            # even one the reactor was measuring at that moment.
            _now = time.time()
            _pending_at.clear()
            _pending_at.update({k: _now for k in _pending})
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
        if _fit_problem:
            _CAMPAIGN_NOTICE = {"level": "lost", "message": "Campaign resumed PAUSED: " + _fit_problem,
                                "params": []}
            _emit("⏸ campaign resumed PAUSED: " + _fit_problem, "error")
        # If nothing is outstanding the loop would sit idle forever, so kick it.
        with _campaign_lock:
            if not _pending and _campaign.status_str == "running" and not _paused:
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
                rec = _campaign.tell(params, None, None, 0.0, recipe_id=rid)
                # Marked so a LATE result for it can replace the failure
                # (CampaignController.replace_failed), see _feed_campaign.
                rec["expired"] = True
            except Exception as exc:
                _emit(f"⚠ could not record the failed measurement: {exc}", "warn")
            # Withdraw the file too (audit Q9). Left in Conditions/ the reactor
            # still ran it later (oldest first), spending reagent and beam on a
            # recipe already scored as failed, and delaying the replacement.
            # Moving it to done/ is the existing withdraw contract: the reactor
            # drops a QUEUED recipe whose file went there; one already RUNNING
            # finishes safely and its late result is accepted in _feed_campaign.
            _withdraw_condition_file(rid, f"it expired after "
                                          f"{_PENDING_TIMEOUT_S / 60:.0f} min")
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
def _load_fit_records_for_campaign(campaign_id: str, run_tag: str = "") -> list:
    """Every Results/Fit/*.dat header belonging to this campaign, oldest first
    (by the "Written" timestamp), as {recipe_id, size, pdi, confidence}.

    recipe_id comes from the ORIGINAL subtracted filename (the fit record's
    own stem, after stripping the "fit_" prefix _write_fit_record adds) via
    recipe_id_from_filename — the header itself carries no recipe_id field.

    ``run_tag`` (e.g. "Run12") is the authoritative per-run identity and, when
    given, is REQUIRED: a record counts only if its recipe_id belongs to this run
    (``Run12_r...``). The header's "Campaign ID" alone is not trustworthy — it is
    stamped with whatever campaign was active when the fit RAN, so a profile from
    an earlier run that got (re-)fit during this campaign carries this
    campaign_id even though it is not part of this run. Filtering by run_tag
    excludes those, which is the "37 of 25" over-count fix. (This matters for
    historical records already on disk; new records avoid the cross-stamp because
    the watcher no longer re-fits the back-catalogue.)"""
    out = []
    want_run = _run_no(run_tag) if run_tag else None
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
            # Trust the run tag embedded in the recipe_id over the stamped
            # Campaign ID (see docstring): drop records from other runs.
            if want_run is not None and _run_no(recipe_id) != want_run:
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
                "model": hdr.get("Model") or None,
                "written": hdr.get("Written", ""),
            })
    except Exception:
        pass
    out.sort(key=lambda r: r.get("written") or "")
    return out


def _fit_recs_by_recipe(fit_recs: list) -> list:
    """Collapse fit records to ONE per recipe_id, keeping the first (oldest).

    The run budget counts optimizer evaluations — i.e. distinct recipes — not Fit
    files. A single condition yields several batch/scan profiles (and a past
    re-fit storm can leave duplicate Fit records for the same profile), so a raw
    len(fit_recs) over-counts badly (the "343 of 25 used" button). The live loop
    feeds the campaign once per recipe (first matching profile wins, then the
    pending entry is cleared); keeping the first record per recipe_id here makes
    the count and the resume replay match that. Records with no recipe_id are
    dropped — the live loop never fed those either."""
    seen: dict = {}
    for fr in fit_recs:
        rid = fr.get("recipe_id") or ""
        if rid and rid not in seen:
            seen[rid] = fr
    return list(seen.values())


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
    global _paused
    if _campaign is not None:
        raise RuntimeError("a campaign is already running — abort it first")
    rec = _latest_incomplete_run()
    if rec is None:
        raise RuntimeError("no incomplete Target Run found")

    cfg_keys = ("target_size", "tolerance", "pdi_cap", "budget", "n_init",
                "confidence_min")
    meta_keys = ("objective", "started_at", "operator", "run_no", "run_tag",
                 "goal", "spec_units", "optimiser", "optimiser_requested",
                 "size_range", "fitter", "method_params", "expected_shape", "seeds",
                 "seed_kinds", "knowledge", "prior", "ai", "fit_knowledge")
    cfg = {k: rec[k] for k in cfg_keys if k in rec}
    meta = {k: rec[k] for k in meta_keys if k in rec}
    campaign_id = str(rec.get("campaign_id") or "")
    run_tag = str(meta.get("run_tag") or f"Run{rec['run_no']}")

    fit_recs = _load_fit_records_for_campaign(campaign_id, run_tag)
    # Replay ONE evaluation per recipe (as the live loop did), not one per Fit
    # file — otherwise a condition's several batch profiles, or duplicate records
    # left by a past re-fit storm, inflate the history far past the budget. The
    # run_tag scope above also keeps out other runs' profiles cross-stamped with
    # this campaign_id.
    recipe_recs = _fit_recs_by_recipe(fit_recs)
    space = ParameterSpace.from_config(_space_cfg())
    camp, _ = _build_optimiser(space, meta.get("goal"),
                               meta.get("size_range"), cfg,
                               method_params=meta.get("method_params"),
                               chosen=meta.get("optimiser"), ai_meta=meta.get("ai") or {},
                               knowledge=meta.get("knowledge") or {})
    camp.start(seeds=meta.get("seeds"), prior=meta.get("prior"))

    replayed, skipped = 0, 0
    for fr in recipe_recs:
        params = _load_params_for_recipe(fr["recipe_id"])
        if params is None:
            skipped += 1
            _emit(f"⚠ resume: no recoverable params for {fr['recipe_id']} — "
                  f"this measurement is not counted toward the budget", "warn")
            continue
        camp.tell(params, fr["size"], fr["pdi"], fr["confidence"],
                  recipe_id=fr["recipe_id"],
                  held=held_reason(fr.get("model"), meta.get("expected_shape") or "any"),
                  model=fr.get("model"))
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
    # Every proposal already made consumed one ask(), and the r-number counts
    # them: r001 was the first ask, rNNN the NNN-th. History length undercounts
    # (expired, withdrawn and unrecoverable conditions are not in it), which
    # made Continue hand out a Sobol seed already issued (audit Q18). So take
    # the highest r-number issued anywhere this run: fit records, re-issued
    # files, Conditions/done/ (consumed or withdrawn) and the reactor feedback.
    issued = [fr["recipe_id"] for fr in fit_recs] + reissued
    for folder, pat in ((done_dir, f"{run_tag}_r*.*"),
                        (Path(_project_root or Path.cwd()) / "reactor" / "feedback",
                         f"{run_tag}_r*.done.json")):
        try:
            if folder.is_dir():
                issued += [f.name for f in folder.glob(pat)]
        except Exception:
            pass
    hi_seq = _max_run_seq(run_tag, issued)
    camp._n_asked = max(hi_seq, len(camp.history) + len(reissued))

    if _restore_fitter(meta):
        raise RuntimeError(f"this run was fitted with '{meta.get('fitter')}', which is not "
                           "available here; Continue would change how results are scored")
    _run_tag = run_tag
    _run_seq = hi_seq
    with _campaign_lock:
        _campaign = camp
        _campaign_cfg = cfg
        _campaign_meta = meta
        _campaign_id = campaign_id
        _paused = False
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


def _accept_late_result(name: str, res: dict) -> bool:
    """A profile for a condition that EXPIRED (scored as failed) arrived after
    all: record it in place of the failure instead of discarding it (audit Q9).
    Lock held by caller. Returns True when it was recorded."""
    expired = [str(h.get("recipe_id") or "") for h in _campaign.history
               if h.get("expired") and h.get("size") is None and h.get("recipe_id")]
    rid = match_recipe_id(name, expired) if expired else None
    if not rid:
        return False
    sz = res.get("size") or {}
    size, pdi, conf = sz.get("radius"), res.get("pdi"), res.get("confidence", 0.0)
    model = res.get("model") or "polydisperse_sphere"
    held = held_reason(model, _expected_shape())
    try:
        rec = _campaign.replace_failed(rid, size, pdi, conf, held=held, model=model)
    except Exception as exc:
        _emit(f"⚠ could not record the late result for {rid}: {exc}", "warn")
        return False
    if rec is None:
        return False
    _emit(f"📊 late result for expired {rid}: R={size} PDI={pdi} conf={conf} "
          f"recorded in place of the failure (loss={rec['loss']:.3f})", "info")
    if _campaign.status_str != "running":
        # The late result ended the run (e.g. a confident in-spec hit): report
        # it and withdraw whatever is still queued for this campaign.
        _clear_conditions_for_new_campaign(
            why=f"the campaign {_campaign.status_str} on a late result")
        _pending.clear(); _pending_at.clear()
        _advance_campaign()
    _save_campaign()      # safe under _campaign_lock: it takes only _intake_lock
    return True


def _feed_campaign(name: str, res: dict) -> None:
    """Match a measured profile to a pending proposed condition and drive the loop."""
    with _campaign_lock:
        if _campaign is None:
            return
        if _campaign.status_str != "running":
            # A late result for an expired condition still counts once the
            # budget is spent; it can only improve the record.
            if _campaign.status_str == "exhausted":
                _accept_late_result(name, res)
            return
        rid = match_recipe_id(name, _pending.keys())
        if not rid and _accept_late_result(name, res):
            return
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
        t_issued = _pending_at.pop(rid, None)
        sz = res.get("size") or {}
        size = sz.get("radius")
        pdi = res.get("pdi")
        conf = res.get("confidence", 0.0)
        model = res.get("model") or "polydisperse_sphere"
        # A fit of another structure than the campaign expects is HELD: it uses
        # its budget slot but does not steer the search until the operator
        # accepts it (src/optimizer/intake_rules.py).
        held = held_reason(model, _expected_shape())
        try:
            _campaign.tell(params, size, pdi, conf, recipe_id=rid, held=held, model=model)
        except Exception as exc:
            # Put the condition back so expiry can still account for it, rather
            # than losing both the measurement and the pending slot.
            _pending[rid] = params
            _pending_at[rid] = t_issued or time.time()
            _emit(f"❌ could not record {rid} in the campaign ({type(exc).__name__}: "
                  f"{exc}); left pending", "error")
            return
        _emit(f"📊 told campaign {rid}: R={size} PDI={pdi} conf={conf} "
              f"(loss={_campaign.history[-1]['loss']:.3f})", "info")
        if _is_ai(_campaign) and _campaign.review_due():
            _kick_ai("review")
        if held:
            _emit(f"⏸ {rid} fitted as {model}, not the expected shape: HELD (counts "
                  f"against the budget, does not steer the search). Accept it in "
                  f"Fitting if it should count.", "warn")
        try:
            _advance_campaign()
        except Exception as exc:
            # ask() already falls back internally; this catches failures in
            # writing the condition file etc. Say so loudly instead of stalling
            # silently (nothing pending + no proposal = a dead loop).
            _emit(f"❌ could not issue the next condition after {rid} "
                  f"({type(exc).__name__}: {exc}); the loop is stalled until "
                  f"this is fixed and the campaign is continued", "error")
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


#: Keep at most this many fit-record pairs (PNG + .dat) in Results/Fit/. Durable
#: records are the whole point, but "never pruned" grows the project volume without
#: bound over weeks of a live loop and eventually fills the disk (which then breaks
#: every savefig / state write). 0 disables pruning. 5000 pairs is generous — weeks
#: of a 25-run loop — while still bounding disk. The newest are always kept.
_FIT_RETENTION = int(os.environ.get("SWAXS_ANALYZER_FIT_RETENTION", 5000))
#: Pruning globs the folder (O(N)), so it is not run on every write — only once
#: per this many records, which bounds the amortised cost.
_FIT_PRUNE_EVERY = 200
_fit_writes_since_prune = 0


def _prune_fit_dir(out_dir: Path) -> None:
    """Delete the oldest fit-record pairs beyond _FIT_RETENTION. Best-effort:
    a pruning failure must never disturb the analysis."""
    if _FIT_RETENTION <= 0:
        return
    try:
        dats = sorted(out_dir.glob("fit_*.dat"), key=lambda p: p.stat().st_mtime)
        excess = len(dats) - _FIT_RETENTION
        if excess <= 0:
            return
        for d in dats[:excess]:
            for p in (d, d.with_suffix(".png")):
                try:
                    p.unlink()
                except OSError:
                    pass
        _emit(f"🧹 Results/Fit: pruned {excess} old record(s), keeping the newest "
              f"{_FIT_RETENTION}", "info")
    except Exception:
        pass


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

        # pyplot has global state: this runs in the WATCHER thread while the
        # campaign-plot route renders figures in request threads. Serialise on the
        # one process-wide lock, and ALWAYS close the figure (try/finally) so an
        # error between subplots() and close() cannot leak a Figure — a slow memory
        # and file-descriptor leak over days of running.
        with MPL_LOCK:
            fig, ax = plt.subplots(figsize=(5.2, 3.8), dpi=120)
            try:
                ax.loglog(q, np.maximum(I, 1e-12), lw=1.0, color="#5b8fc9",
                          marker="o", ms=2.2, alpha=0.75, mew=0, label="subtracted I(q)")
                if model is not None:
                    ax.loglog(q, np.maximum(model, 1e-12), lw=1.8, color="#e2402c", label="fit")
                ax.set_xlabel("q (nm$^{-1}$)"); ax.set_ylabel("I (a.u.)")
                ax.set_title(path.name, fontsize=9)
                ax.legend(fontsize=7, loc="lower left")

                _m = summary.get("model") or "polydisperse_sphere"
                value_lines = [
                    f"model = {_m}" if _m != "polydisperse_sphere" else None,
                    (f"R = {summary.get('radius')} nm" if _m == "polydisperse_sphere"
                     else f"R_eq = {summary.get('radius')} nm") if summary.get("radius") is not None else None,
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
            finally:
                plt.close(fig)

        sig_col = np.asarray(sigma, float) if sigma is not None else np.full_like(q, np.nan)
        fit_col = np.asarray(model, float) if model is not None else np.full_like(q, np.nan)
        header = [
            "# Nanoparticle fit record -- Autonomous Analyser (analyzer)",
            f"# Source file     : {path}",
            f"# Written         : {datetime.datetime.now().isoformat(timespec='seconds')}",
            f"# Radius (nm)     : {summary.get('radius')}",
            f"# Diameter (nm)   : {summary.get('diameter')}",
            f"# PDI             : {summary.get('pdi')}",
            f"# Distribution    : {summary.get('distribution')}",
            f"# Model           : {summary.get('model') or 'polydisperse_sphere'}",
            f"# Phase           : {summary.get('phase')}",
            f"# Confidence      : {summary.get('confidence')}",
            f"# Invariant (rel) : {summary.get('invariant_rel')}",
            f"# Guinier Rg (nm) : {summary.get('guinier_rg')}",
        ]
        for _k, _v in (res.get("model_params") or {}).items():
            _e = (res.get("model_errors") or {}).get(_k)
            header.append(f"# Param {_k:<17s}: {_v:.5g}" + (f" +/- {_e:.2g}" if _e else ""))
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
        global _fit_writes_since_prune
        _fit_writes_since_prune += 1
        if _fit_writes_since_prune >= _FIT_PRUNE_EVERY:
            _fit_writes_since_prune = 0
            _prune_fit_dir(out_dir)
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


def _analyze_file(path: Path, drive_loop: bool = True) -> None:
    """Fit one subtracted profile, store the result, write the durable Fit
    record and manifest entry.

    ``drive_loop`` (default True — the live watcher's behaviour) also feeds the
    running campaign and publishes ``fit.complete`` on the bus. The operator
    backfill (``_analyze_existing_worker``) passes ``drive_loop=False``: a
    historical profile must be fit, recorded and shown, but must NEVER drive the
    optimizer or report to the reactor as if it were a live measurement."""
    try:
        hdr, q, I, sigma, _meta = read_dat_data_metadata(path)
        q = np.asarray(q, float)
        # The nanoparticle fit + optimizer target work in nm⁻¹ (radius in nm). If the
        # subtracted file was truncated to Å⁻¹ for the ML model, convert first so sizes
        # aren't 10× off and the campaign optimizes toward the right target.
        if _q_is_angstrom(hdr):
            q = q * 10.0                       # Å⁻¹ → nm⁻¹
        # path + project root let a context-aware fitter (LLM-assisted) gather
        # what the pipeline knows about this curve; the others ignore them
        _fo = {"fit_knowledge": _fk_snapshot()} if _active_fitter == "llm_assisted" else {}
        res = fitting.get(_active_fitter).fit(q, I, sigma, dist="auto", path=str(path),
                                              project_root=_project_root, **_fo)
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
    _fc = res.get("fit_curve") or {}
    if _fc.get("q") and _fc.get("I"):
        # any catalogue model (catalogue_bic, llm_assisted): the fitter returns
        # its own fit curve, so the overlay is not limited to a sphere
        try:
            model = np.interp(q, np.asarray(_fc["q"], float), np.asarray(_fc["I"], float),
                              left=np.nan, right=np.nan)
        except Exception:
            model = None
    elif res.get("size") and res["size"].get("source") == "form_factor" and res.get("fit"):
        model = model_intensity(q, res["size"]["radius"], res["pdi"],
                                res["fit"]["scale"], res["fit"]["background"],
                                res.get("distribution", "schulz"))
    # advisory LLM QC note (empty + instant if no AI credentials configured)
    # The LLM-assisted fitter already made this call; never pay for it twice.
    if isinstance(res.get("llm_assist"), dict):
        res["llm"] = res["llm_assist"]
    else:
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
        "model": res.get("model") or "polydisperse_sphere",
        "phase": ph.get("phase"),
        "invariant_rel": (round(res["invariant"]["Q_rel"], 4)
                          if res.get("invariant") else None),
        "guinier_rg": (round(res["guinier"]["Rg"], 3)
                       if res.get("guinier") and res["guinier"].get("Rg") else None),
        "ts": time.strftime("%H:%M:%S"),
    }
    entry = {"summary": summary, "full": res, "path": str(path),
             "plot": {"q": _downsample(q), "I": _downsample(I),
                      "model": _downsample(model) if model is not None else None,
                      "sigma": _downsample(sig) if sig is not None else None}}
    _store_result(path.name, entry)
    conf = summary["confidence"]
    tag = "ok" if conf >= 0.6 else ("warn" if conf >= 0.3 else "info")
    r = summary["radius"]
    _mdl = summary["model"]
    _emit(f"✓ {path.name}: R={r} PDI={summary['pdi']} conf={conf} "
          f"({summary['distribution'] if _mdl == 'polydisperse_sphere' else _mdl + ', equivalent R'})", tag)
    # record in the manifest (best-effort)
    if _project_root:
        try:
            # The TARGET goes in per entry, not just once per campaign: an
            # operator can abort and restart with a different target mid-run, and
            # only a per-fit copy stays true about what this fit was judged against.
            _params = {"model": summary["model"],
                       "distribution": summary["distribution"]}
            if res.get("model_params"):
                _params["model_params"] = res["model_params"]
                _params["model_errors"] = res.get("model_errors") or {}
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
    if drive_loop:
        _feed_campaign(path.name, res)      # drive the closed loop, if a campaign is running

    try:
        rid = recipe_id_from_filename(path.name)
        suspect = (conf or 0.0) <= QC_CONF_THRESHOLD
        png = _write_fit_record(path, q, I, sig, model, summary, res)
        if drive_loop and _bus is not None:
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
#: "seen-once" memo for the pending-recipe flat-folder pass (_watch_pending_in_flat).
#: Separate from _lastsig because the main watch prunes _lastsig to the Good/ files.
_flat_lastsig: dict = {}
#: Paths that were ALREADY in the watched folder when the analyzer started (or when
#: the folder/project last changed). These are pre-existing profiles, not live
#: arrivals, so the watcher must NEVER auto-fit them — that is the re-fit-on-restart
#: the operator saw (an upstream re-subtract storm churns their mtime and makes them
#: look new). The "Analyse existing profiles" button and a running campaign still fit
#: them on demand; once fit, the durable Results/Fit/ record keeps them skipped.
_startup_present: set = set()
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
        # non-recursive glob of the WATCHED dir (from _resolve_sub: Good/ when
        # the gate is populating it, else the flat Subtracted/). Non-recursive so
        # the Good/ & NeedsReview/ copies never get double-analysed alongside the
        # flat originals.
        # Enumerate names (glob does not stat). `present` must list EVERY current
        # file so the cleanup below can drop memos for vanished ones.
        all_dats = list(d.glob("*.dat"))
        present = set(str(p) for p in all_dats)
        fit_dir = _resolve_fit()
        # Only STAT the files we might act on. Over weeks the folder holds
        # thousands of already-handled / startup-frozen profiles; stat-ing all of
        # them every 3 s was O(N) per poll and climbing. Files still "waiting"
        # (in _lastsig, mid-write) are not in _handled, so they stay candidates.
        # A profile for a PENDING condition is never "pre-existing history": it
        # is the measurement the running campaign is waiting for, which landed
        # while this process was down (audit Q19). Exempt it from the startup
        # freeze, or the loop waits for it until the condition expires.
        with _campaign_lock:
            pend = list(_pending.keys()) if (
                _campaign is not None and _campaign.status_str == "running") else []

        def _frozen(p) -> bool:
            return (str(p) in _startup_present
                    and not (pend and match_recipe_id(p.name, pend)))

        candidates = [p for p in all_dats
                      if str(p) not in _handled and not _frozen(p)]
        files = sorted(candidates, key=lambda p: p.stat().st_mtime)
        # Decide for every file FIRST, fit second: the fit-me list has to be
        # known in full before any fitting starts, or _triage_backlog can't
        # tell a two-file poll from a two-hundred-file one.
        go: list = []
        for f in files:
            key = str(f)
            try:
                st = f.stat(); sig = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
            # IDEMPOTENT ON THE DURABLE FIT RECORD. decide_intake keys on the
            # file SIGNATURE (size+mtime), so anything that re-touches a
            # subtracted profile — an upstream re-subtract/re-classify storm on
            # restart, a folder resync, a reseed with a stale signature — makes
            # the profile look new and the analyzer re-fits it. That is the
            # reported "autofit keeps loading the old runs": the whole
            # back-catalogue re-fit while a live run waited behind it. A profile
            # that already has Results/Fit/fit_<stem>.dat has been fit; skip it
            # for good, whatever its signature. New profiles (no record yet) are
            # untouched, and rebuilding a past campaign reads these records
            # directly (_load_fit_records_for_campaign), not through this watcher.
            if (fit_dir / f"fit_{f.stem}.dat").is_file():
                _handled[key] = sig; _lastsig.pop(key, None)
                continue
            # PRE-EXISTING at startup → never auto-fit (even if an upstream
            # re-subtract just churned its mtime). The operator backfill button
            # and a running campaign still fit these; the record check above then
            # skips it for good. Live arrivals (paths not present at startup) fall
            # through and are fit normally.
            if _frozen(f):
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
    _watch_pending_in_flat()   # never stall the loop on a pending recipe
    _expire_pending()          # self-heal a proposal whose data never arrived


def _watch_pending_in_flat() -> None:
    """Pick up profiles for PENDING proposed recipes from the flat Subtracted/
    folder, even when the gate routes the main watch to Good/.

    When a prior run has left a populated Good/, `_resolve_sub` watches Good/ and
    the analyzer only sees profiles the Quality Gate has promoted there. A running
    campaign then stalls after proposing one condition: the profile it is waiting
    for sits in the flat Subtracted/ and is never fit (the reported
    continue-then-no-fit bug). The loop must not depend on the gate for the very
    recipes it asked for, so here we additionally fit flat-folder files whose
    recipe_id matches a pending condition. Targeted by pending id, so ungraded or
    rejected NON-loop profiles are still excluded; the durable Fit-record check
    keeps it from double-fitting a profile the gate later promotes to Good/."""
    with _campaign_lock:
        pend = list(_pending.keys()) if (
            _campaign is not None and _campaign.status_str == "running") else []
    if not pend:
        return
    d = _resolve_sub()
    base = _resolve_sub_base()
    if base == d or not base.is_dir():      # already watching the flat folder
        return
    fit_dir = _resolve_fit()
    for f in sorted(base.glob("*.dat"), key=lambda p: p.stat().st_mtime):
        key = str(f)
        if key in _handled or not match_recipe_id(f.name, pend):
            continue
        try:
            st = f.stat(); sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
        if (fit_dir / f"fit_{f.stem}.dat").is_file():
            with _intake_lock:
                _handled[key] = sig
            continue
        # Use a SEPARATE "seen-once" memo: the main pass prunes _lastsig to the
        # files in the Good/ watch, which would delete this flat file's entry
        # between polls and trap it at "wait" forever.
        action = decide_intake(key, sig, _handled, _flat_lastsig)
        if action == "wait":
            _flat_lastsig[key] = sig
            continue
        if action == "skip":
            continue
        _analyze_file(f)
        with _intake_lock:
            _handled[key] = sig
        _flat_lastsig.pop(key, None)


#: Throttle for watcher-loop error reporting — a persistent fault (a malformed
#: file it chokes on every poll, a permissions/disk error) must not spam the log
#: every 3 s, but it must NOT be invisible either: a silent `except: pass` let a
#: stuck watcher read as "running" with nothing happening for a whole night.
_WATCH_ERR_THROTTLE_S = 60.0
_last_watch_err = (0.0, "")


def _watcher() -> None:
    global _last_watch_err
    while True:
        try:
            _watch_once()
        except Exception as exc:
            now = time.time()
            msg = f"{type(exc).__name__}: {exc}"
            last_t, last_msg = _last_watch_err
            if msg != last_msg or (now - last_t) > _WATCH_ERR_THROTTLE_S:
                _emit(f"⚠ watcher poll failed ({msg}) — retrying", "warn")
                _last_watch_err = (now, msg)
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
    _startup_present.clear()
    if not d.is_dir():
        return 0
    fit_dir = _resolve_fit()
    n = 0
    for f in d.glob("*.dat"):
        try:
            st = f.stat()
        except OSError:
            continue
        # Everything already on disk now is PRE-EXISTING: freeze it from auto-fit
        # (keyed on path, so an upstream re-subtract that churns the mtime cannot
        # retrigger a fit). The operator's "Analyse existing profiles" button and a
        # running campaign still fit these on demand. This replaces the old
        # mtime-based crash-gap exemption, which let anything younger than
        # _CRASH_GAP_WINDOW_S through and re-fit the back-catalogue on every restart.
        _startup_present.add(str(f))
        if (fit_dir / f"fit_{f.stem}.dat").is_file():
            _handled[str(f)] = (st.st_size, st.st_mtime_ns)
            n += 1
    return n


def _seed_handled_at_boot() -> None:
    """Record every profile already on disk as PRE-EXISTING and mark the
    already-fit ones as handled, so a restart re-analyses NOTHING that was
    already there — the FRESH default: instant startup.

    Via _startup_present (set in _seed_handled_locked) every file present at
    startup is frozen from auto-fitting, whatever its mtime. This is stricter
    than the previous mtime crash-gap rule, which let anything younger than
    _CRASH_GAP_WINDOW_S through and so re-fit the back-catalogue whenever an
    upstream re-subtract churned the files' timestamps on restart. A
    pre-existing profile that was never fit (including one written just before a
    crash) is recovered on demand via the "Analyse existing profiles" button or
    by a running campaign — never silently re-fit on boot. Only profiles that
    ARRIVE after startup are auto-fit by the watcher."""
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


# ── operator-driven backfill of previously-collected profiles ──────────────────
# The live watcher (and _seed_handled_at_boot) deliberately IGNORE the historical
# back-catalogue: on startup every old profile with no Results/Fit/ record is
# marked "handled" so a restart can't re-fit a whole prior campaign and starve a
# live run. That leaves the plain standard-use case — "I collected data earlier,
# analyse it now" — with no path. This backfill is that path: an explicit, operator-
# triggered pass that fits the old, never-fit profiles oldest-first, in the
# background, yielding between files so a live campaign keeps priority. It never
# drives the optimizer or publishes fit.complete (drive_loop=False), so running it
# mid-campaign cannot corrupt the loop.
_backfill_lock = threading.Lock()
_backfill_running = False
#: Pause between backfill fits, so the 3 s live watcher always interleaves ahead
#: of the historical pass. Overridable for tests.
_BACKFILL_GAP_S = float(os.environ.get("SWAXS_ANALYZER_BACKFILL_GAP_S", "0.5"))


def _backfill_candidates() -> list:
    """Pre-existing subtracted profiles with no durable Fit record yet, oldest
    first. These are exactly the files frozen from auto-fit at startup
    (``_startup_present``); a live arrival that appeared after startup is left to
    the watcher, not the button."""
    d = _resolve_sub()
    if not d.is_dir():
        return []
    fit_dir = _resolve_fit()
    out = []
    for f in d.glob("*.dat"):
        key = str(f)
        if key not in _startup_present:
            continue                                  # live arrival — the watcher handles it
        if (fit_dir / f"fit_{f.stem}.dat").is_file():
            continue                                  # already fit
        out.append(f)
    try:
        out.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        pass
    return out


def _analyze_existing_worker() -> None:
    global _backfill_running
    n = 0
    try:
        cands = _backfill_candidates()
        total = len(cands)
        if total:
            _emit(f"↻ backfill: analysing {total} previously-collected profile(s) "
                  f"(oldest first; live run keeps priority)", "info")
        fit_dir = _resolve_fit()
        for f in cands:
            key = str(f)
            try:
                st = f.stat(); sig = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
            if (fit_dir / f"fit_{f.stem}.dat").is_file():   # fit by the watcher meanwhile
                with _intake_lock:
                    _handled[key] = sig
                continue
            with _intake_lock:          # claim it so the live watcher won't also grab it
                _handled[key] = sig; _lastsig.pop(key, None)
            _analyze_file(f, drive_loop=False)
            n += 1
            time.sleep(_BACKFILL_GAP_S)
        _emit(f"↻ backfill complete — {n} profile(s) analysed", "ok" if n else "info")
    except Exception as exc:
        _emit(f"⚠ backfill failed: {exc}", "warn")
    finally:
        with _backfill_lock:
            _backfill_running = False


def _start_backfill() -> tuple[bool, str, int]:
    """Launch the backfill thread if one isn't already running. Returns
    (started, message, candidate_count)."""
    global _backfill_running
    pending = len(_backfill_candidates())
    with _backfill_lock:
        if _backfill_running:
            return False, "a backfill is already running", pending
        if pending == 0:
            return False, "no previously-collected profiles need analysing", 0
        _backfill_running = True
    threading.Thread(target=_analyze_existing_worker, daemon=True).start()
    return True, f"analysing {pending} profile(s)", pending


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


@app.route("/api/methods", methods=["GET", "POST"])
def api_methods():
    """List the available fitting + optimising methods and the active ones, and
    (POST) set the active ones. Changing the optimiser while a campaign is running
    is refused — it would swap the strategy mid-loop; abort first."""
    global _active_fitter, _active_optimiser
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        fit_id = (body.get("fitter") or "").strip()
        opt_id = (body.get("optimiser") or "").strip()
        ready_fit = {m["id"] for m in fitting.available() if m["ready"]}
        ready_opt = {m["id"] for m in optimisers.available() if m["ready"]}
        if fit_id:
            if fit_id not in ready_fit:
                return jsonify({"ok": False, "error": f"fitter '{fit_id}' is not available"}), 400
            # The fitter belongs to the campaign (audit AN5): changing it mid-run
            # would change how every later result is scored.
            if (fit_id != _active_fitter and _campaign is not None
                    and _campaign.status_str == "running"):
                return jsonify({"ok": False, "error": "a campaign is running: abort it before changing the fitter"}), 409
            _active_fitter = fit_id
        if opt_id:
            if _campaign is not None:
                return jsonify({"ok": False, "error": "a campaign is running — abort it before changing the optimiser"}), 409
            if opt_id not in ready_opt:
                return jsonify({"ok": False, "error": f"optimiser '{opt_id}' is not available"}), 400
            _active_optimiser = opt_id
    return jsonify({
        "fitters": fitting.available(),
        "optimisers": optimisers.available(),
        "goals": optimisers.goals_payload(),      # goal → {methods, default}
        "active": {"fitter": _active_fitter, "optimiser": _active_optimiser},
    })


_study_lock = threading.Lock()
_study_result: dict | None = None     # last run_study() output, for the Report figure


@app.route("/api/study", methods=["POST"])
def api_study():
    """Run an in-silico method-comparison: drive the chosen optimisers through a
    campaign against the hidden ground-truth simulator and keep the per-method
    best-loss curves for /api/study/plot.png. Pure computation — never touches the
    live campaign, the reactor, or real data."""
    global _study_result
    body = request.get_json(silent=True) or {}
    ready_opt = {m["id"] for m in optimisers.available() if m["ready"]} | {"ai_copilot", "ai_pilot"}
    ids = [m for m in (body.get("methods") or []) if m in ready_opt]
    if not ids:
        return jsonify({"ok": False, "error": "select at least one available optimiser"}), 400
    try:
        from src.optimizer import study as _study                 # noqa: PLC0415
        cfg = _study.default_cfg()
        for k in ("target_size", "tolerance", "pdi_cap", "budget", "n_init"):
            if k in body:
                cfg[k] = (int if k in ("budget", "n_init") else float)(body[k])
        repeats = max(1, min(int(body.get("repeats", 3)), 10))
        space = ParameterSpace.from_config(_space_cfg())
        res = _study.run_study(space, ids, cfg, repeats=repeats)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    with _study_lock:
        _study_result = res
    summary = {mid: {"label": r["label"],
                     "runs": [{"status": x["status"], "n": x["n"], "best": x["best"]}
                              for x in r["repeats"]]}
               for mid, r in res.items()}
    return jsonify({"ok": True, "cfg": cfg, "repeats": repeats, "summary": summary})


@app.route("/api/study/plot.png")
def api_study_plot():
    from src.optimizer import plots as opl                         # noqa: PLC0415
    with _study_lock:
        res = _study_result
    png = opl.study_figure(res or {})
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


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


@app.route("/api/analyze_existing", methods=["POST"])
def api_analyze_existing():
    """Operator-driven backfill: fit previously-collected subtracted profiles
    that were never analysed (no Results/Fit/ record). Runs in the background,
    oldest-first, yielding to the live watcher; does not drive the optimizer.
    GET-like callers can check `pending` via the returned count."""
    started, msg, pending = _start_backfill()
    with _backfill_lock:
        running = _backfill_running
    return jsonify({"ok": started, "message": msg, "pending": pending,
                    "running": running})


@app.route("/api/result/<name>")
def api_result(name):
    with _results_lock:
        e = _results.get(name)
    if not e:
        return jsonify({"error": "not found"}), 404
    return jsonify({"summary": e["summary"], "full": e["full"], "plot": e["plot"],
                    "fit_verdict": _fk_verdict_for(name)})


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
    _cc = _cond_folder_check()
    st["conditions_folder_reactor"] = _cc["reactor"]
    st["conditions_folder_mismatch"] = _cc["mismatch"]
    # Goal + size range so the Live Progress panel can read the run the way the
    # operator set it up: a single target for target-hit / level-set, a diameter
    # RANGE for Pareto (where "distance to one target" is meaningless).
    st["goal"] = str((_campaign_meta or {}).get("goal") or "target-hit")
    st["paused"] = bool(_paused)
    st["ai_busy"] = dict(_ai_busy)
    st["expected_shape"] = _expected_shape()
    _k = (_campaign_meta or {}).get("knowledge") or {}
    if _k:
        st["knowledge"] = {"set_id": _k.get("set_id"), "name": _k.get("name"),
                           "version": _k.get("version"), "n_cards": len(_k.get("cards") or []),
                           "n_prior": _k.get("n_prior", 0)}
    with _campaign_lock:
        st["held"] = _campaign.held() if _campaign is not None else []
        st["runs"] = _runs_summary() if _campaign is not None else []
    if (_campaign_meta or {}).get("size_range"):
        st["size_range"] = _campaign_meta["size_range"]
    if (_campaign_meta or {}).get("fit_knowledge"):
        st["fit_knowledge"] = _campaign_meta["fit_knowledge"]
    return st


@app.route("/api/campaign", methods=["GET"])
def api_campaign():
    return jsonify(_campaign_status())


@app.route("/api/campaign/incomplete")
def api_campaign_incomplete():
    """{run_tag, used, budget, best_size} for the "Continue RunN — X of Y
    used" button, or {"reason": ...} when there is nothing to continue.

    Only ever describes the LATEST run (see _latest_incomplete_run). `reason`
    exists so the UI can say *why* Continue is unavailable — an empty response
    reads as "the button is broken", which is how a stale Run9 offer went
    unquestioned for so long.
    """
    if _campaign is not None:
        return jsonify({"reason": "a campaign is already running"})
    rec = _latest_incomplete_run()
    if rec is None:
        latest = _latest_run_record()
        if latest is None:
            return jsonify({"reason": "no previous run in this project — Start a new one"})
        tag = latest.get("run_tag") or f"Run{latest.get('run_no')}"
        return jsonify({"reason": f"the latest run ({tag}) finished "
                                  f"({latest.get('outcome')}) — Start a new one",
                        "latest_run_tag": tag,
                        "latest_outcome": latest.get("outcome")})
    campaign_id = str(rec.get("campaign_id") or "")
    run_tag = str(rec.get("run_tag") or (f"Run{rec.get('run_no')}" if rec.get("run_no") else ""))
    # Count by distinct recipe (optimizer evaluations), not by Fit-file count —
    # one condition yields several batch profiles, so len(fit_recs) over-counts
    # (the "343 of 25 used" bug). Scope to this run_tag so records from other runs
    # that were (re-)fit while this campaign was active and thus carry its
    # Campaign ID are excluded (the "37 of 25" bug).
    recs = _fit_recs_by_recipe(_load_fit_records_for_campaign(campaign_id, run_tag))
    sized = [r for r in recs if r.get("size") is not None]
    best = (min(sized, key=lambda r: abs(r["size"] - float(rec.get("target_size", 0))))
            if sized else None)
    return jsonify({
        "run_tag": rec.get("run_tag") or f"Run{rec.get('run_no')}",
        "used": len(recs), "budget": int(rec.get("budget") or 0),
        "best_size": (best or {}).get("size"),
    })


@app.route("/api/campaign/continue", methods=["POST"])
def api_campaign_continue():
    try:
        summary = _continue_run()
        return jsonify({"ok": True, **summary})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def _runs_summary(n: int = 40) -> list[dict]:
    """The last runs of the campaign: what was made, who chose it, the result,
    and the operator's verdict. Lock held by caller."""
    kinds = list((_campaign_meta or {}).get("seed_kinds") or [])
    out = []
    hist = _campaign.history
    for j, h in enumerate(hist):
        if j < len(hist) - n:
            continue
        p = h.get("params") or {}
        if h.get("decided_by"):
            by = h["decided_by"]
        elif j < (_campaign.n_init or 0):
            by = {"own": "your own", "knowledge": "from knowledge"}.get(
                kinds[j] if j < len(kinds) else "", "cold start (spread)")
        else:
            by = "optimiser"
        out.append({"recipe_id": h.get("recipe_id", ""), "n": j + 1, "by": by,
                    "params": {k: round(float(v), 4) for k, v in p.items()},
                    "diameter_nm": None if h.get("size") is None else round(2 * float(h["size"]), 3),
                    "pdi": None if h.get("pdi") is None else round(float(h["pdi"]), 3),
                    "held": h.get("held"), "verdict": h.get("verdict")})
    return out


def _expected_shape() -> str:
    """The running campaign's expected shape. A campaign started before this
    setting existed has none recorded and keeps its old behaviour: any shape."""
    return str((_campaign_meta or {}).get("expected_shape") or "any")


def _cold_start_request(b: dict, space, cfg: dict) -> tuple[int, list, list, str, dict]:
    """(n_init, seeds, errors, expected_shape, knowledge) from a Start/Preview body.

    The cold start is ``n_init`` recipes: the operator's own (``seeds``, checked
    against the search bounds and the reactor's hard caps), then recipes from
    knowledge (inside the range the approved cards allow), then Sobol points
    spread over the whole search range for the rest. ``knowledge`` is the
    frozen snapshot of the chosen set (src/design_knowledge/freeze.py)."""
    errors = []
    try:
        budget = int(b.get("budget", 25))
    except (TypeError, ValueError):
        budget = 25
    # An explicit cold start longer than the budget is refused below; an API
    # caller that leaves it out gets the usual 10, capped at the budget.
    try:
        n_init = int(b["n_init"]) if "n_init" in b else min(10, max(budget, 1))
    except (TypeError, ValueError):
        n_init, errors = 10, ["the cold start must be a whole number of recipes"]
    if n_init < 1:
        errors.append("the cold start needs at least 1 recipe")
    if n_init > budget:
        errors.append(f"the cold start ({n_init}) is longer than the whole budget ({budget})")
    seeds, seed_errs = check_seeds(b.get("seeds") or [], space, (cfg or {}).get("safety") or {})
    errors += seed_errs
    # what the design loop takes from the base is ticked in the Knowledge tab and
    # saved with it; Setup only names the base
    kreq = b.get("knowledge")
    if isinstance(kreq, dict) and kreq.get("set_id") and not isinstance(kreq.get("use"), dict):
        try:
            kreq["use"] = kstore.design_use(kstore.load_set(str(kreq["set_id"])))
        except (ValueError, OSError):
            pass
    kn = _knowledge_for(kreq, space)
    errors += kn["errors"]
    kn["n_own"] = len(seeds)
    seeds = seeds + kn["seeds"]
    if len(seeds) > n_init:
        errors.append(f"{len(seeds)} recipes of your own and from knowledge do not fit in a "
                      f"cold start of {n_init}")
    shape = str(b.get("expected_shape") or DEFAULT_SHAPE)
    if shape not in EXPECTED_SHAPES:
        errors.append(f"unknown expected shape '{shape}'")
    return n_init, seeds, errors, shape, kn


@app.route("/api/campaign/start", methods=["POST"])
def api_campaign_start():
    global _campaign, _campaign_cfg, _campaign_id, _campaign_meta, _run_tag, _run_seq
    global _paused
    b = request.get_json(silent=True) or {}
    try:
        _cfg_live = _space_cfg()
        space = ParameterSpace.from_config(_cfg_live)
        # Refuse BEFORE touching anything: a bad starting recipe or cold start
        # must never clear the queue or half-start a run.
        _n_init, _seeds, _errs, _shape, _kn = _cold_start_request(b, space, _cfg_live)
        try:
            _conf_min = float(b.get("confidence_min", 0.5))
        except (TypeError, ValueError):
            _conf_min = -1.0
        if not (0.0 <= _conf_min <= 1.0):
            _errs.append("the minimum fit confidence must be between 0 and 1")
        _ai = b.get("ai") or None
        # the AI fitter's knowledge: the Fitter part of the chosen base (frozen below)
        _fk_set = str((b.get("knowledge") or {}).get("set_id") or "") or None
        if _ai:
            lvl = str(_ai.get("level") or "")
            try:
                rev = int(_ai.get("review_every") or 0)
            except (TypeError, ValueError):
                rev = -1
            if lvl not in _aid.LEVELS:
                _errs.append(f"unknown AI level '{lvl}'")
            elif str(b.get("goal") or "target-hit") == "benchmark":
                _errs.append("AI levels race in Compare methods, not as a live campaign")
            if rev not in (0, 5, 10):
                _errs.append("the progress review must be every 5 or 10 runs, or off")
            if lvl == "ai_pilot" and rev not in (5, 10):
                _errs.append("AI Pilot needs a progress review every 5 or 10 runs")
            if lvl == "ai_pilot" and _ai.get("confirmed") is not True:
                _errs.append("AI Pilot writes the recipes itself: confirm the limits first")
            _model_id = str(_ai.get("model") or "")
            if _model_id and _model_id not in {i for i, _ in _aid.MODELS}:
                _errs.append(f"unknown AI model '{_model_id}'")
            _think = str(_ai.get("thinking") or "off")
            if _think not in _aid.THINKING:
                _errs.append(f"unknown thinking level '{_think}'")
            _ai = {"level": lvl, "review_every": rev, "model": _model_id, "thinking": _think,
                   "apply": "ask" if _ai.get("apply") == "ask" else "auto",
                   "every_pick": bool(((b.get("knowledge") or {}).get("use") or {}).get("every_pick"))}
        if _errs:
            return jsonify({"ok": False, "error": "; ".join(_errs), "errors": _errs}), 400
        with _campaign_lock:
            # Refuse to start over a live run: a second start would drop the running
            # campaign's history, orphan its pending conditions, and re-derive the
            # SAME RunN tag (its data isn't finalized on disk yet) → overwrite risk.
            # Abort it explicitly first (which clears the slate).
            if _campaign is not None and _campaign.status_str == "running":
                return jsonify({"ok": False, "error": "a campaign is already "
                                "running — abort it before starting a new one"}), 409
            _pending.clear()
            # Producer owns staleness: a new campaign invalidates any conditions
            # left queued from a previous/aborted run, so set them aside before we
            # propose the first one. (The reactor no longer clears on boot — this
            # is what keeps the pipeline clean while startup stays order-free.)
            _clear_conditions_for_new_campaign()
            # Campaign goal (proposal Aim 1): target-hit | level-set | pareto |
            # benchmark. It is resolved BEFORE building the optimiser because the
            # goal itself picks the acquisition for two of the four (level-set →
            # BAX, pareto → EHVI); target-hit / benchmark keep the dropdown choice.
            _goal = str(b.get("goal") or "target-hit")
            if _goal not in ("target-hit", "level-set", "pareto", "benchmark"):
                _goal = "target-hit"
            _size_range = None
            if isinstance(b.get("size_range"), (list, tuple)) and len(b["size_range"]) == 2:
                try:
                    _size_range = [float(b["size_range"][0]), float(b["size_range"][1])]
                except (TypeError, ValueError):
                    _size_range = None
            _base_cfg = dict(
                target_size=float(b.get("target_size", 5.0)),
                tolerance=float(b.get("tolerance", 0.3)),
                pdi_cap=float(b.get("pdi_cap", 0.15)),
                budget=int(b.get("budget", 25)),
                n_init=_n_init,
                confidence_min=_conf_min)
            _campaign, _opt_id = _build_optimiser(
                space, _goal, _size_range, _base_cfg, b.get("method_params"),
                ai_meta=_ai or {}, knowledge=_kn.get("frozen") or {})
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
                "n_init": _n_init,
                "confidence_min": _conf_min,
            }
            # Recorded with the run so the Report tab and the durable record know
            # what the campaign pursued. `optimiser` is the RESOLVED acquisition
            # (goal-driven for level-set/pareto), so resume rebuilds the same one.
            _campaign_meta = {
                "objective": "min ((size - target_size)/tolerance)^2 + w*(PDI/pdi_cap)",
                "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "operator": _current_operator(_project_root),   # set once in the hub
                "run_no": _run_no,          # Target-Run number (also read back by _next_run_no)
                "run_tag": _run_tag,
                "goal": _goal,
                "spec_units": str(b.get("spec_units") or "diameter"),
                "optimiser": _opt_id,
                "optimiser_requested": _active_optimiser,
                "fitter": _active_fitter,
                # the strategy's own knobs (κ, τ, trust size) so a resume rebuilds
                # the same search, not the defaults
                "method_params": {p["key"]: (b.get("method_params") or {})[p["key"]]
                                  for p in optimisers.params_for(_opt_id)
                                  if p["key"] in (b.get("method_params") or {})},
            }
            # Pareto goal carries the diameter size-range it maps (the EHVI
            # acquisition maps the best PDI across it; recorded for the Report).
            if _size_range is not None:
                _campaign_meta["size_range"] = _size_range
            # what steers the search, recorded so resume and Continue keep it
            _campaign_meta["expected_shape"] = _shape
            if _ai:
                _campaign_meta["ai"] = _ai
            if _active_fitter == "llm_assisted":
                _fz = fk.freeze(_fk_set, save_root=_project_root or None)
                _campaign_meta["fit_knowledge"] = {
                    "hash": _fz["hash"], "set_id": _fz.get("set_id"), "name": _fz.get("name"),
                    "sources": _fz["sources"], "base_version": _fz["project_version"],
                    "n_notes": len(_fz["notes"]), "n_fits": len(_fz["fits"]),
                    "settings": _fz["settings"]}
            if _seeds:
                _campaign_meta["seeds"] = _seeds
                _campaign_meta["seed_kinds"] = (["own"] * _kn["n_own"]
                                                + ["knowledge"] * (len(_seeds) - _kn["n_own"]))
            # the knowledge set as it was at Start: version, the cards and campaigns used
            if _kn.get("frozen"):
                _campaign_meta["knowledge"] = _kn["frozen"]
                _campaign_meta["prior"] = _kn["prior"]
            _paused = False
            _campaign.start(seeds=_seeds, prior=_kn.get("prior"))
            _emit(f"🚀 campaign started — target R={_campaign.target_size}±{_campaign.tolerance} nm, "
                  f"PDI<{_campaign.pdi_cap}, budget {_campaign.budget}, cold start "
                  f"{_n_init} ({_kn['n_own']} of your own, {len(_seeds) - _kn['n_own']} from "
                  f"knowledge, {_n_init - len(_seeds)} spread out), "
                  f"expected shape: {EXPECTED_SHAPES[_shape]['label']}", "ok")
            if _campaign_meta.get("fit_knowledge"):
                _fkm = _campaign_meta["fit_knowledge"]
                _emit(f"🧩 AI fitter knowledge {_fkm['hash']} ({_fkm.get('name') or 'no base'}): "
                      + ", ".join(_fkm["sources"])
                      + f"; {_fkm['n_notes']} note(s), {_fkm['n_fits']} judged fit(s)", "info")
            if _kn.get("frozen"):
                _fz = _kn["frozen"]
                _emit(f"📚 knowledge: {_fz['name']} v{_fz['version']}, {len(_fz['cards'])} "
                      f"card(s), {_fz['n_prior']} warm-start point(s) from past campaigns"
                      + ("".join(f"; {w}" for w in _kn["warnings"])), "info")
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
    global _paused
    with _campaign_lock:
        _paused = False
        if _campaign is not None:
            _campaign.abort()
            _emit("⏹ campaign aborted by operator", "warn")
            # Tell the reactor, via the existing file contract: withdraw every
            # condition this run left queued (moved to Conditions/done/ with a
            # NOT RUN note). The reactor drops queued recipes whose file was
            # withdrawn, so it stops dosing for a campaign that no longer exists.
            # A condition already RUNNING is not interrupted; it finishes safely.
            _clear_conditions_for_new_campaign(why="the campaign was aborted")
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


@app.route("/api/campaign/pause", methods=["POST"])
def api_campaign_pause():
    """Pause or resume a running campaign. Paused: no NEW condition is written;
    anything already issued still runs and its result is still recorded."""
    global _paused
    want = bool((request.get_json(silent=True) or {}).get("paused", True))
    with _campaign_lock:
        if _campaign is None or _campaign.status_str != "running":
            return jsonify({"ok": False, "error": "no running campaign"}), 409
        if want == _paused:
            return jsonify({"ok": True, "paused": _paused})
        _paused = want
        if want:
            _emit("⏸ campaign paused: no new condition will be issued until Resume "
                  "(anything already queued still runs)", "warn")
        else:
            _emit("▶ campaign resumed", "ok")
            if not _pending:
                _advance_campaign()
    _save_campaign()
    return jsonify({"ok": True, "paused": _paused})


@app.route("/api/campaign/accept", methods=["POST"])
def api_campaign_accept():
    """Accept a HELD result (fitted as another shape than expected) so it counts."""
    b = request.get_json(silent=True) or {}
    with _campaign_lock:
        if _campaign is None:
            return jsonify({"ok": False, "error": "no campaign"}), 409
        held_ids = [h["recipe_id"] for h in _campaign.held() if h.get("recipe_id")]
        rid = str(b.get("recipe_id") or "")
        if not rid and b.get("name"):
            rid = match_recipe_id(str(b["name"]), held_ids) or ""
        if rid not in held_ids:
            return jsonify({"ok": False, "error": "nothing held for that recipe"}), 404
        rec = _campaign.accept(rid)
        _emit(f"✓ {rid} accepted by the operator ({rec.get('model')}): it now counts "
              f"in the search", "ok")
        if _campaign.status_str != "running":
            # the accepted result ended the run (a confident in-spec hit)
            _clear_conditions_for_new_campaign(
                why=f"the campaign {_campaign.status_str} on an accepted result")
            _pending.clear(); _pending_at.clear()
            _advance_campaign()
    _save_campaign()
    return jsonify({"ok": True, "campaign": _campaign_status()})


@app.route("/api/ai/status")
def api_ai_status():
    """Is an AI login configured (no call made), the default model, the choices."""
    return jsonify({"available": _aid.ai_available(), "default_model": _aid._model(),
                    "models": [{"id": i, "label": lab} for i, lab in _aid.MODELS],
                    "thinking": list(_aid.THINKING)})


@app.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    """One tiny real call with the chosen model: checks the login, the gateway and
    the model name. Shows the error type, never a credential."""
    b = request.get_json(silent=True) or {}
    model = str(b.get("model") or "")
    if model and model not in {i for i, _ in _aid.MODELS}:
        return jsonify({"ok": False, "error": "unknown model"}), 400
    thinking = str(b.get("thinking") or "off")
    if thinking not in _aid.THINKING:
        return jsonify({"ok": False, "error": "unknown thinking level"}), 400
    r = _aid.test_connection(model, thinking)
    _emit(f"🤖 AI connection test ({r['model']}, thinking {thinking}): "
          + (f"OK in {r['ms']} ms" if r["ok"] else f"failed: {r['error']}"), "ok" if r["ok"] else "warn")
    return jsonify(r)


@app.route("/api/campaign/ai/suggestion", methods=["POST"])
def api_campaign_ai_suggestion():
    """Apply or reject the AI Advisor's waiting range ("ask me first")."""
    accept = bool((request.get_json(silent=True) or {}).get("accept"))
    with _campaign_lock:
        if not _is_ai(_campaign) or not _campaign.resolve_pending(accept):
            return jsonify({"ok": False, "error": "no suggestion is waiting"}), 409
    _emit(f"🤖 Advisor suggestion {'applied' if accept else 'rejected'} by the operator", "info")
    _save_campaign()
    return jsonify({"ok": True, "campaign": _campaign_status()})


@app.route("/api/campaign/ai/undo", methods=["POST"])
def api_campaign_ai_undo():
    with _campaign_lock:
        if not _is_ai(_campaign) or not _campaign.undo():
            return jsonify({"ok": False, "error": "nothing to undo"}), 409
    _emit("↩ the last applied working range was undone", "info")
    _save_campaign()
    return jsonify({"ok": True, "campaign": _campaign_status()})


@app.route("/api/campaign/preview", methods=["POST"])
def api_campaign_preview():
    """The cold-start recipes a Start with this body would issue, in order,
    without starting anything: your own recipes first, then the Sobol spread
    (the same sequence CampaignController.start() draws)."""
    b = request.get_json(silent=True) or {}
    try:
        cfg = _space_cfg()
        space = ParameterSpace.from_config(cfg)
        n_init, seeds, errors, _shape, kn = _cold_start_request(b, space, cfg)
        if errors:
            return jsonify({"ok": False, "error": "; ".join(errors), "errors": errors}), 400
        spread = space.sobol(max(0, n_init - len(seeds)), seed=0)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    no = kn["n_own"]
    recipes = ([{"kind": "own", "params": s} for s in seeds[:no]]
               + [{"kind": "knowledge", "params": s} for s in seeds[no:]]
               + [{"kind": "spread", "params": s} for s in spread])
    return jsonify({"ok": True, "n_init": n_init, "recipes": recipes,
                    "knowledge": {"cited": kn.get("cited") or {}, "warnings": kn["warnings"],
                                  "n_prior": len(kn.get("prior") or []),
                                  "version": (kn.get("frozen") or {}).get("version")}})


# ── design knowledge (Knowledge tab) ─────────────────────────────────────────
# Thin routes over src/design_knowledge. Everything drafted here is a DRAFT
# until the operator approves it; nothing here touches a running campaign
# (a campaign froze its set's version at Start) or the reactor.

def _who() -> str:
    try:
        return _current_operator(_project_root)
    except Exception:                                         # noqa: BLE001
        return ""


def _chemistry() -> dict:
    try:
        from src.analysis.fit_context import _read_yaml          # noqa: PLC0415
        return _read_yaml(Path(_project_root) / "chemistry.yml") if _project_root else {}
    except Exception:                                         # noqa: BLE001
        return {}


def _kerr(exc: Exception, code: int = 400):
    return jsonify({"ok": False, "error": str(exc)}), code


@app.route("/api/guide")
def api_guide():
    return jsonify({"pages": _guide.pages(fitting.available(), optimisers.available())})


@app.route("/api/knowledge/sets", methods=["GET", "POST"])
def api_knowledge_sets():
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        try:
            s = kstore.create_set(b.get("name"), b.get("keywords") or [], _who())
        except ValueError as exc:
            return _kerr(exc)
        return jsonify({"ok": True, "set": s})
    chem = _chemistry()
    sets = kstore.list_sets()
    for s in sets:
        s["matches_chemistry"] = kstore.matches(s, chem)
    return jsonify({"ok": True, "sets": sets, "chemistry": chem})


@app.route("/api/knowledge/set/<sid>")
def api_knowledge_set(sid):
    try:
        s = kstore.load_set(sid)
        space = ParameterSpace.from_config(_space_cfg())
    except ValueError as exc:
        return _kerr(exc, 404)
    for c in s.get("cards") or []:
        c["check"] = kcards.check_card(c, space)
    s["matches_chemistry"] = kstore.matches(s, _chemistry())
    return jsonify({"ok": True, "set": s})


def _drafted(sid: str, drafts: list, info: dict | None = None, what: str = "drafts"):
    try:
        added = kstore.add_drafts(sid, drafts, _who(), what=what)
    except ValueError as exc:
        return _kerr(exc, 404)
    _emit(f"📚 {len(added)} draft card(s) added to {sid} ({what}); approve them in Knowledge, "
          f"Drafts", "info")
    return jsonify({"ok": True, "added": added, "info": info or {}})


@app.route("/api/knowledge/set/<sid>/notes", methods=["POST"])
def api_knowledge_notes(sid):
    b = request.get_json(silent=True) or {}
    drafts, skipped = kcards.notes_to_drafts(b.get("text") or "", added_by=_who())
    if not drafts:
        return jsonify({"ok": False, "error": "no line names a recipe setting "
                                              "(temperature, flow, ODE, TOP, oleylamine)",
                        "skipped": skipped}), 400
    return _drafted(sid, drafts, {"skipped": skipped}, "from your notes")


@app.route("/api/knowledge/set/<sid>/text", methods=["POST"])
def api_knowledge_text(sid):
    b = request.get_json(silent=True) or {}
    drafts, info = klit.draft_cards(b.get("text") or "", b.get("source") or "pasted text")
    for d in drafts:
        if b.get("doi"):
            d["doi"] = str(b["doi"])[:120]
    if not drafts:
        return jsonify({"ok": False, "error": "no statement about a recipe setting was found",
                        "info": info}), 400
    return _drafted(sid, drafts, info, f"from {str(b.get('source') or 'pasted text')[:60]}")


@app.route("/api/knowledge/set/<sid>/doi", methods=["POST"])
def api_knowledge_doi(sid):
    b = request.get_json(silent=True) or {}
    try:
        w = klit.by_doi(b.get("doi") or "")
    except ValueError as exc:
        return _kerr(exc)
    except Exception as exc:                                  # noqa: BLE001
        return _kerr(RuntimeError(f"could not reach OpenAlex: {type(exc).__name__}"), 502)
    if not w.get("abstract"):
        return jsonify({"ok": False, "error": "no abstract available for that DOI: paste the "
                                              "text or add the PDF", "work": w}), 400
    src = f"{w['title'][:120]} ({w.get('year') or 'n.d.'})"
    drafts, info = klit.draft_cards(w["abstract"], src)
    for d in drafts:
        d["doi"] = w.get("doi") or ""
    if not drafts:
        return jsonify({"ok": False, "error": "the abstract states nothing about a recipe setting",
                        "work": w}), 400
    return _drafted(sid, drafts, info, f"from DOI {w.get('doi')}")


@app.route("/api/knowledge/set/<sid>/pdf", methods=["POST"])
def api_knowledge_pdf(sid):
    f = request.files.get("file")
    if f is None:
        return _kerr(ValueError("no file"))
    data = f.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        return _kerr(ValueError("the PDF is larger than 25 MB"))
    try:
        text = klit.pdf_text(data)
    except ValueError as exc:
        return _kerr(exc)
    except Exception as exc:                                  # noqa: BLE001
        return _kerr(RuntimeError(f"could not read the PDF ({type(exc).__name__})"))
    drafts, info = klit.draft_cards(text, request.form.get("source") or (f.filename or "PDF"))
    if not drafts:
        return jsonify({"ok": False, "error": "no statement about a recipe setting was found",
                        "info": info}), 400
    return _drafted(sid, drafts, info, f"from {str(f.filename or 'PDF')[:60]}")


@app.route("/api/knowledge/search", methods=["POST"])
def api_knowledge_search():
    b = request.get_json(silent=True) or {}
    try:
        res = klit.search(b.get("query") or "", n=int(b.get("n") or 8))
    except ValueError as exc:
        return _kerr(exc)
    except Exception as exc:                                  # noqa: BLE001
        return _kerr(RuntimeError(f"could not reach OpenAlex: {type(exc).__name__}"), 502)
    return jsonify({"ok": True, "results": res})


@app.route("/api/knowledge/set/<sid>/from_search", methods=["POST"])
def api_knowledge_from_search(sid):
    """Draft cards from the abstracts of the works the operator ticked."""
    b = request.get_json(silent=True) or {}
    drafts, methods = [], set()
    for w in (b.get("works") or [])[:10]:
        if not isinstance(w, dict) or not w.get("abstract"):
            continue
        src = f"{str(w.get('title') or '')[:120]} ({w.get('year') or 'n.d.'})"
        ds, info = klit.draft_cards(str(w["abstract"]), src)
        methods.add(info.get("method"))
        for d in ds:
            d["doi"] = str(w.get("doi") or "")[:120]
        drafts += ds
    if not drafts:
        return jsonify({"ok": False, "error": "none of the ticked abstracts states anything "
                                              "about a recipe setting"}), 400
    return _drafted(sid, drafts, {"method": ",".join(sorted(m for m in methods if m))},
                    "from web search")


@app.route("/api/knowledge/set/<sid>/review", methods=["POST"])
def api_knowledge_review(sid):
    b = request.get_json(silent=True) or {}
    try:
        c = kstore.review(sid, str(b.get("card_id") or ""), str(b.get("action") or ""),
                          b.get("edit"), _who())
    except ValueError as exc:
        return _kerr(exc)
    return jsonify({"ok": True, "card": c})


@app.route("/api/campaign/verdict", methods=["POST"])
def api_campaign_verdict():
    """Good call / Bad call on a run, with one line why. Kept with the run (in the
    history and the campaign records) and, when the campaign used a knowledge
    set, in that set straight away, where an AI level reads it next time."""
    b = request.get_json(silent=True) or {}
    call = str(b.get("call") or "")
    if call not in ("good", "bad"):
        return jsonify({"ok": False, "error": "the call must be good or bad"}), 400
    rid = str(b.get("recipe_id") or "")
    why = " ".join(str(b.get("why") or "").split())[:240]
    with _campaign_lock:
        if _campaign is None:
            return jsonify({"ok": False, "error": "no campaign"}), 409
        rec = next((h for h in _campaign.history if rid and h.get("recipe_id") == rid), None)
        if rec is None:
            return jsonify({"ok": False, "error": "no measured run with that recipe id"}), 404
        dec = None
        if _is_ai(_campaign):
            dec = next((d for d in reversed(_campaign.decisions)
                        if d.get("kind") == "pick" and d.get("recipe_id") == rid), None)
        sid = ((_campaign_meta or {}).get("knowledge") or {}).get("set_id")
        payload = {"call": call, "why": why, "recipe_id": rid, "campaign_id": _campaign_id,
                   "run_tag": _run_tag, "chosen_by": rec.get("decided_by") or "optimiser",
                   "params": rec.get("params") or {},
                   "result": {"diameter_nm": None if rec.get("size") is None else round(2 * float(rec["size"]), 3),
                              "pdi": rec.get("pdi"), "held": rec.get("held")},
                   "reason": (dec or {}).get("reason", "")}
        rec["verdict"] = {"call": call, "why": why}
        if dec is not None:
            dec["verdict"] = {"call": call, "why": why}
    saved = None
    if sid:
        try:
            saved = kstore.add_verdict(sid, payload, _who())
        except ValueError as exc:
            return _kerr(exc)
    _emit(f"🗳 {rid}: {call} call" + (f" ({why})" if why else "")
          + (f"; kept in the knowledge set {sid} as {saved['id']}" if saved else
             "; no knowledge set on this campaign, kept with the run only"), "info")
    _save_campaign()
    return jsonify({"ok": True, "verdict": saved, "kept_in_set": bool(saved)})


@app.route("/api/knowledge/set/<sid>/verdict/<vid>/retire", methods=["POST"])
def api_knowledge_verdict_retire(sid, vid):
    try:
        ok = kstore.retire_verdict(sid, vid, _who())
    except ValueError as exc:
        return _kerr(exc, 404)
    return jsonify({"ok": ok}) if ok else (jsonify({"ok": False, "error": "no such verdict"}), 404)


@app.route("/api/knowledge/set/<sid>/harness", methods=["POST"])
def api_knowledge_harness(sid):
    try:
        h = kstore.set_harness(sid, request.get_json(silent=True) or {}, _who(), "set by the operator")
    except ValueError as exc:
        return _kerr(exc, 404)
    return jsonify({"ok": True, "harness": h})


# ── did the knowledge help? (replay of past campaigns, background jobs) ─────
_eval_jobs: dict = {}              # set_id -> {state, kind, started, result | error}
_eval_lock = threading.Lock()


def _eval_worker(sid: str, kind: str, opts: dict) -> None:
    try:
        s = kstore.load_set(sid)
        space = ParameterSpace.from_config(_space_cfg())
        if kind == "evaluate":
            arms = kreplay.ARMS + (kreplay.AI_ARMS if opts.get("include_ai") else ())
            res = kreplay.evaluate(s, space, arms=arms, repeats=opts["repeats"])
            kstore.add_evaluation(sid, res)
        else:
            res = kreplay.propose_improvement(s, space, repeats=opts["repeats"],
                                              include_ai=bool(opts.get("include_ai")))
            if res.get("proposal"):
                kstore.set_proposal(sid, res)
        with _eval_lock:
            _eval_jobs[sid] = {"state": "done", "kind": kind, "result": res,
                               "finished": time.strftime("%H:%M:%S")}
        _emit(f"📚 {kind} of {sid} finished"
              + (f": a change is proposed ({res['proposal']}), approve it in Knowledge, Evaluation"
                 if kind == "propose" and res.get("proposal") else ""), "ok")
    except Exception as exc:                                  # noqa: BLE001
        with _eval_lock:
            _eval_jobs[sid] = {"state": "error", "kind": kind, "error": f"{type(exc).__name__}: {exc}"}
        _emit(f"⚠ {kind} of {sid} failed: {exc}", "warn")


def _eval_start(sid: str, kind: str):
    b = request.get_json(silent=True) or {}
    try:
        kstore.load_set(sid)
    except ValueError as exc:
        return _kerr(exc, 404)
    try:
        repeats = max(1, min(int(b.get("repeats") or 3), 10))
    except (TypeError, ValueError):
        repeats = 3
    with _eval_lock:
        if (_eval_jobs.get(sid) or {}).get("state") == "running":
            return jsonify({"ok": False, "error": "a test is already running for this set"}), 409
        _eval_jobs[sid] = {"state": "running", "kind": kind, "started": time.strftime("%H:%M:%S")}
    threading.Thread(target=_eval_worker, daemon=True, name=f"eval-{sid}",
                     args=(sid, kind, {"repeats": repeats, "include_ai": bool(b.get("include_ai"))})).start()
    return jsonify({"ok": True, "state": "running"})


def _eval_wait(timeout: float = 120.0) -> None:
    """Tests and tools: wait for every replay job to finish."""
    t_end = time.time() + timeout
    while time.time() < t_end:
        with _eval_lock:
            if not any(j.get("state") == "running" for j in _eval_jobs.values()):
                return
        time.sleep(0.05)


@app.route("/api/knowledge/set/<sid>/evaluate", methods=["POST"])
def api_knowledge_evaluate(sid):
    """Replay the set's past campaigns with and without each source of knowledge."""
    return _eval_start(sid, "evaluate")


@app.route("/api/knowledge/set/<sid>/propose", methods=["POST"])
def api_knowledge_propose(sid):
    """Try other harness settings on the practice campaigns; propose one only if it
    also wins on the held-out campaign. Nothing changes until it is approved."""
    return _eval_start(sid, "propose")


@app.route("/api/knowledge/set/<sid>/evaluation")
def api_knowledge_evaluation(sid):
    with _eval_lock:
        job = dict(_eval_jobs.get(sid) or {})
    if job:
        return jsonify(job)
    try:
        evs = kstore.load_set(sid).get("evaluations") or []
    except ValueError as exc:
        return _kerr(exc, 404)
    return jsonify({"state": "done" if evs else "none", "kind": "evaluate",
                    "result": evs[-1] if evs else None})


@app.route("/api/knowledge/set/<sid>/proposal", methods=["POST"])
def api_knowledge_proposal(sid):
    approve = bool((request.get_json(silent=True) or {}).get("approve"))
    try:
        s = kstore.load_set(sid)
    except ValueError as exc:
        return _kerr(exc, 404)
    prop = (s.get("proposal") or {}).get("proposal")
    if not prop:
        return jsonify({"ok": False, "error": "no proposal is waiting"}), 409
    h = kstore.harness(s)
    if approve:
        h = kstore.set_harness(sid, prop, _who(), "approved improvement: " + str(s["proposal"].get("why", ""))[:80])
    kstore.set_proposal(sid, None)
    _emit(f"📚 improvement for {sid} {'approved' if approve else 'discarded'}", "info")
    return jsonify({"ok": True, "harness": h})


@app.route("/api/knowledge/set/<sid>/export")
def api_knowledge_export(sid):
    try:
        body = kstore.export_set(sid)
    except ValueError as exc:
        return _kerr(exc, 404)
    return Response(json.dumps(body, indent=2, ensure_ascii=False), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="knowledge_{sid}.json"'})


@app.route("/api/knowledge/import", methods=["POST"])
def api_knowledge_import():
    try:
        s = kstore.import_set(request.get_json(silent=True) or {}, _who())
    except ValueError as exc:
        return _kerr(exc)
    return jsonify({"ok": True, "set": {"id": s["id"], "name": s["name"],
                                        "n_drafts": len(s.get("cards") or [])}})


@app.route("/api/search_range")
def api_search_range():
    """The search bounds the optimiser may use and the reactor's hard caps,
    read-only (they are changed in the Reactor app)."""
    try:
        cfg = _space_cfg()
        space = ParameterSpace.from_config(cfg)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    saf = cfg.get("safety") or {}
    return jsonify({
        "ok": True,
        "bounds": {k: list(v) for k, v in space.bounds.items()},
        "x_sum_max": space.x_sum_max,
        "safety": {k: saf.get(k) for k in ("T_max", "F_tot_max", "per_pump_max")
                   if saf.get(k) is not None},
        "source": str(cfg.get("_path") or ""),
        "shapes": {k: v["label"] for k, v in EXPECTED_SHAPES.items()},
        "default_shape": DEFAULT_SHAPE,
    })


@app.route("/api/campaign/folder", methods=["GET", "POST"])
def api_campaign_folder():
    global _cond_folder, _cond_folder_explicit
    if request.method == "POST":
        f = (request.get_json(silent=True) or {}).get("folder", "").strip()
        if f:
            _cond_folder = f
            _cond_folder_explicit = True
    cc = _cond_folder_check()
    return jsonify({"folder": _cond_folder, "resolved": str(_resolve_cond()),
                    "reactor": cc["reactor"], "mismatch": cc["mismatch"]})


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


# ── knowledge for the AI fitter: the Fitter part of a knowledge base ────────
# (src/fitting/fit_knowledge.py). Setup picks the base by name (POST
# /api/knowledge/active); what the fitter uses is ticked in the Knowledge tab and
# saved with the base. A campaign freezes its snapshot at Start; outside a
# campaign every AI fit uses the active base as it is now.
_active_set: str = ""
_fk_cache: dict = {}
_fk_jobs: dict = {}               # set_id -> {state, kind, started, result | error}
_fk_lock = threading.Lock()


def _campaign_running() -> bool:
    return _campaign is not None and getattr(_campaign, "status_str", "") == "running"


def _fk_snapshot() -> dict | None:
    fm = (_campaign_meta or {}).get("fit_knowledge") or {}
    if _campaign_running() and fm.get("hash"):
        snap = _fk_cache.get(fm["hash"]) or fk.load_frozen(_project_root, fm["hash"])
        if snap:
            _fk_cache[fm["hash"]] = snap
            return snap
    try:
        return fk.freeze(_active_set or None, save_root=_project_root or None)
    except Exception:                                         # noqa: BLE001
        return fk.freeze(None)


def _verdict_set() -> str:
    """Where a call on a fit is saved: the running campaign's base, else the active one."""
    if _campaign_running():
        sid = (((_campaign_meta or {}).get("knowledge") or {}).get("set_id")
               or ((_campaign_meta or {}).get("fit_knowledge") or {}).get("set_id"))
        if sid:
            return sid
    return _active_set


def _fk_verdict_for(name: str) -> dict | None:
    sid = _verdict_set()
    if not sid:
        return None
    try:
        fits = fk.load(sid).get("fits") or []
    except ValueError:
        return None
    for f in reversed(fits):
        if f.get("file") == name and f.get("status") != "retired":
            return {k: f.get(k) for k in ("id", "verdict", "label", "note", "fitted_model", "at")}
    return None


def _fk_loader(path):
    hdr, q, I, sigma, _m = read_dat_data_metadata(Path(path))
    q = np.asarray(q, float)
    if _q_is_angstrom(hdr):
        q = q * 10.0
    return q, np.asarray(I, float), (np.asarray(sigma, float) if sigma is not None else None)


@app.route("/api/knowledge/active", methods=["GET", "POST"])
def api_knowledge_active():
    """The base chosen in Setup: used by AI fits outside a campaign and for calls on fits."""
    global _active_set
    if request.method == "POST":
        sid = str((request.get_json(silent=True) or {}).get("set_id") or "")
        if sid:
            try:
                kstore.load_set(sid)
            except ValueError as exc:
                return _kerr(exc, 404)
        _active_set = sid
    return jsonify({"ok": True, "set_id": _active_set, "verdicts_go_to": _verdict_set()})


@app.route("/api/knowledge/set/<sid>/use", methods=["POST"])
def api_knowledge_design_use(sid):
    """What the design loop takes from this base (ticked in the Knowledge tab)."""
    try:
        u = kstore.set_design_use(sid, request.get_json(silent=True) or {}, _who())
    except ValueError as exc:
        return _kerr(exc, 404)
    return jsonify({"ok": True, "design_use": u})


@app.route("/api/fitknowledge")
def api_fitknowledge():
    """Guinier's sources with what is there now, the catalogue's models, and the
    running campaign's frozen fitter knowledge."""
    models, sfs = fk.catalogue_names()
    return jsonify({"shared": fk.shared_status(), "own_sources": list(fk.PROJECT),
                    "models": sorted(models), "structure_factors": sorted(sfs),
                    "active_set": _active_set, "verdicts_go_to": _verdict_set(),
                    "frozen": (_campaign_meta or {}).get("fit_knowledge"),
                    "legacy": fk.has_legacy(_project_root), "active_fitter": _active_fitter})


def _fk_part(sid):
    try:
        return fk.load(sid), None
    except ValueError as exc:
        return None, _kerr(exc, 404)


@app.route("/api/knowledge/set/<sid>/fitter")
def api_fitter_part(sid):
    d, e = _fk_part(sid)
    if e:
        return e
    with _fk_lock:
        job = dict(_fk_jobs.get(sid) or {})
    return jsonify({
        "ok": True, "set_id": sid, "name": d["name"], "version": d["version"],
        "sources": d["sources"], "summary": fk.summary(sid), "notes": d["notes"],
        "fits": [{k: f.get(k) for k in ("id", "file", "fitter", "fitted_model", "verdict", "label",
                                         "note", "status", "who", "at")} for f in d["fits"]],
        "settings": d["settings"], "limits": fk.SETTINGS_LIMITS,
        "evaluation": (d.get("evaluations") or [None])[-1], "proposal": d.get("proposal"),
        "job": job})


@app.route("/api/knowledge/set/<sid>/fitter/sources", methods=["POST"])
def api_fitter_sources(sid):
    src = (request.get_json(silent=True) or {}).get("sources")
    if not isinstance(src, list):
        return jsonify({"ok": False, "error": "sources must be a list"}), 400
    try:
        return jsonify({"ok": True, "sources": fk.set_sources(sid, [str(x) for x in src], _who())})
    except ValueError as exc:
        return _kerr(exc, 404)


@app.route("/api/knowledge/set/<sid>/fitter/note", methods=["POST"])
def api_fitter_note(sid):
    _d, e = _fk_part(sid)
    if e:
        return e
    added, errs = [], []
    for line in str((request.get_json(silent=True) or {}).get("text") or "").splitlines():
        if line.strip():
            try:
                added.append(fk.add_note(sid, line, _who()))
            except ValueError as exc:
                errs.append(f"{line.strip()[:40]}: {exc}")
    if not added and not errs:
        return jsonify({"ok": False, "error": "write the note first"}), 400
    return jsonify({"ok": bool(added), "added": added, "errors": errs})


@app.route("/api/knowledge/set/<sid>/fitter/note/<nid>", methods=["POST"])
def api_fitter_note_review(sid, nid):
    try:
        n = fk.review_note(sid, nid, str((request.get_json(silent=True) or {}).get("action") or ""), _who())
    except ValueError as exc:
        return _kerr(exc)
    return jsonify({"ok": True, "note": n})


@app.route("/api/knowledge/set/<sid>/fitter/fit/<fid>/retire", methods=["POST"])
def api_fitter_fit_retire(sid, fid):
    try:
        ok = fk.retire_fit(sid, fid, _who())
    except ValueError as exc:
        return _kerr(exc, 404)
    return (jsonify({"ok": True}) if ok else (jsonify({"ok": False, "error": "unknown judged fit"}), 404))


@app.route("/api/knowledge/set/<sid>/fitter/settings", methods=["POST"])
def api_fitter_settings(sid):
    b = request.get_json(silent=True) or {}
    try:
        new = {k: int(b[k]) for k in fk.SETTINGS_LIMITS if k in b}
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "settings must be whole numbers"}), 400
    try:
        return jsonify({"ok": True, "settings": fk.set_settings(sid, new, _who())})
    except ValueError as exc:
        return _kerr(exc, 404)


@app.route("/api/knowledge/set/<sid>/fitter/import_project", methods=["POST"])
def api_fitter_import_project(sid):
    """Copy this project's earlier fit_knowledge.json (before bases had a Fitter part) in."""
    try:
        return jsonify({"ok": True, **fk.import_legacy(sid, _project_root, _who())})
    except ValueError as exc:
        return _kerr(exc)


@app.route("/api/fitknowledge/verdict", methods=["POST"])
def api_fitknowledge_verdict():
    """The operator's Right / Wrong-model call on one fit in the Fitting tab,
    saved in the Fitter part of the running campaign's base, else the active one."""
    b = request.get_json(silent=True) or {}
    sid = _verdict_set()
    if not sid:
        return jsonify({"ok": False, "error": "choose a knowledge base in Setup first (or make one in Knowledge)"}), 400
    name = str(b.get("name") or "")
    with _results_lock:
        ent = _results.get(name)
        ent = dict(ent) if ent else None
    if not ent:
        return jsonify({"ok": False, "error": "no fit with that name"}), 404
    path = ent.get("path")
    try:
        q, I, _s = _fk_loader(path) if path and Path(path).is_file() else (
            ent["plot"]["q"], ent["plot"]["I"], None)
    except Exception:                                         # noqa: BLE001
        q, I = ent["plot"]["q"], ent["plot"]["I"]
    full, summ = ent.get("full") or {}, ent.get("summary") or {}
    feats = {"rg": summ.get("guinier_rg"),
             "porod_n": ((full.get("features") or {}).get("porod") or {}).get("n")}
    try:
        f = fk.add_fit_verdict(sid, file=name, path=path, q=q, I=I,
                               fitted_model=summ.get("model") or "polydisperse_sphere",
                               verdict=str(b.get("verdict") or ""),
                               correct_model=b.get("correct_model") or None,
                               note=str(b.get("note") or ""),
                               fitter=str((full.get("diagnostics") or {}).get("method") or _active_fitter),
                               features=feats, who=_who())
    except ValueError as exc:
        return _kerr(exc)
    drafts = fk.draft_notes_from_fits(sid, _who())
    _emit(f"🧩 {name}: fit judged {f['verdict']}"
          + (f", right model {f['label']}" if f["verdict"] == "wrong" and f.get("label") else "")
          + f" (saved in {sid})"
          + (f"; {len(drafts)} note(s) drafted for approval (Knowledge, Fitter notes)" if drafts else ""), "info")
    return jsonify({"ok": True, "set_id": sid,
                    "verdict": {k: f.get(k) for k in ("id", "verdict", "label", "note", "fitted_model", "at")},
                    "drafted": drafts})


def _fk_worker(sid: str, kind: str) -> None:
    try:
        fn = fk.evaluate if kind == "evaluate" else fk.propose_improvement
        res = fn(sid, loader=_fk_loader, cache_root=_project_root or None)
        with _fk_lock:
            _fk_jobs[sid] = {"state": "done", "kind": kind, "result": res,
                             "finished": time.strftime("%H:%M:%S")}
        _emit(f"🧩 fitter knowledge {kind} ({sid}): {res.get('note', '')}", "ok")
    except Exception as exc:                                  # noqa: BLE001
        with _fk_lock:
            _fk_jobs[sid] = {"state": "error", "kind": kind, "error": f"{type(exc).__name__}: {exc}"}
        _emit(f"⚠ fitter knowledge {kind} failed: {exc}", "warn")


def _fk_start(sid: str, kind: str):
    _d, e = _fk_part(sid)
    if e:
        return e
    with _fk_lock:
        if (_fk_jobs.get(sid) or {}).get("state") == "running":
            return jsonify({"ok": False, "error": "a fitter test is already running for this base"}), 409
        _fk_jobs[sid] = {"state": "running", "kind": kind, "started": time.strftime("%H:%M:%S")}
    threading.Thread(target=_fk_worker, args=(sid, kind), daemon=True, name=f"fk-{kind}").start()
    return jsonify({"ok": True, "state": "running"})


def _fk_wait(timeout: float = 120.0) -> None:
    """Tests and tools: wait for every fitter-knowledge job to finish."""
    t_end = time.time() + timeout
    while time.time() < t_end:
        with _fk_lock:
            if not any(j.get("state") == "running" for j in _fk_jobs.values()):
                return
        time.sleep(0.05)


@app.route("/api/knowledge/set/<sid>/fitter/evaluate", methods=["POST"])
def api_fitter_evaluate(sid):
    """Refit the base's judged curves with and without each kind of knowledge."""
    return _fk_start(sid, "evaluate")


@app.route("/api/knowledge/set/<sid>/fitter/propose", methods=["POST"])
def api_fitter_propose(sid):
    """Tune the settings on the practice half; propose only if the held-out half improves."""
    return _fk_start(sid, "propose")


@app.route("/api/knowledge/set/<sid>/fitter/proposal", methods=["POST"])
def api_fitter_proposal(sid):
    try:
        r = fk.decide_proposal(sid, bool((request.get_json(silent=True) or {}).get("approve")), _who())
    except ValueError as exc:
        return _kerr(exc, 409)
    return jsonify({"ok": True, **r})

if __name__ == "__main__":
    _project_root = os.environ.get("SWAXS_PROJECT", _project_root)
    print("━" * 52)
    print("  Autonomous Analyser  →  http://localhost:5107")
    print(f"  watching: {_resolve_sub()}")
    print("━" * 52)
    app.run(host="127.0.0.1", port=5107, debug=False, threaded=True)
