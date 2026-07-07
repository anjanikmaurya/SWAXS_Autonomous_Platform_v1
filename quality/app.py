"""
quality/app.py — SWAXS Quality Gate (port 5006)
================================================
AI-assisted quality grading of background-subtracted scattering profiles.

Sits between Background Subtraction (5003) and Analysis (5004).  Watches the
Subtracted/ folder(s), scores each profile 0–100 (rule-based, with optional LLM
adjudication of borderline cases), assigns a good/bad verdict, sorts profiles
into Good/ and NeedsReview/ subfolders, and records everything in the manifest
so the Analysis app and Assistant can consume it.

All science/scoring logic lives in src/quality/.  This file is a thin Flask
shell: routing, the monitor thread, file-sorting, manifest/event wiring.

Run:  uv run quality/app.py
Open: http://localhost:5006
"""

from __future__ import annotations

import collections
import datetime
import gc
import json
import os
import shutil
import sys
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

from src.quality import (                                       # noqa: E402
    grade_profile, score_metrics, DEFAULT_THRESHOLDS, thresholds_for, sample_key,
)
from src.manifest import (                                      # noqa: E402
    update_manifest, add_quality_entry, make_provenance,
)
from src.utils.read_dat_metadata import read_dat_data_metadata  # noqa: E402

# ── Event bus (graceful degradation) ─────────────────────────────────────────
try:
    from src.events import EventBusClient as _EventBusClient
    _bus = _EventBusClient("quality").connect(retry=True)
except Exception:
    _bus = None

app = Flask(__name__)

_project_root: str = ""

# ── Monitor / results state ───────────────────────────────────────────────────
_grading: bool = False
_grader_thread: threading.Thread | None = None
_lock = threading.Lock()
_log: collections.deque = collections.deque(maxlen=500)   # (seq, line)
_seq: int = 0

# path -> graded record (latest).  Records carry score/verdict/flags/metrics/
# reasons/spark/detector + any user override.
_results: dict = {}
_overrides: dict = {}                 # path -> {"verdict": str, "note": str}
# Sparse user overrides for ANY scoring parameter (weights, thresholds, score_pass,
# borderline).  Empty = use the per-detector defaults.  Persisted to a config file.
_params: dict = {}
_llm_enabled: bool = True              # use LLM to adjudicate borderline scores
_llm_model: str = os.environ.get("SWAXS_LLM_MODEL", "claude-sonnet-4-6")
_watch: list = []                     # [(detector, folder)] currently watched
_status: dict = {"monitoring": False, "graded": 0, "good": 0, "bad": 0,
                 "interval": None, "threshold": DEFAULT_THRESHOLDS["score_pass"]}
# threshold adaptation memory: scores the user judged good / bad
_adapt = {"good_scores": [], "bad_scores": []}

# Human labels for the editable scoring parameters (shown in the UI editor).
_PARAM_LABELS = {
    "score_pass":      "Pass threshold (good ≥)",
    "borderline":      "LLM borderline band (±)",
    "neg_warn_pct":    "Over-sub: warn at % negative",
    "neg_fail_pct":    "Over-sub: fail at % negative",
    "snr_good":        "SNR: full marks at",
    "snr_floor":       "SNR: zero marks at",
    "min_decades":     "Coverage: min q-decades",
    "min_points":      "Coverage: min points",
    "spike_fail_frac": "Spikes: fail fraction",
    "dyn_range_min":   "Featureless: low dyn-range",
    "dyn_range_hard":  "Featureless: hard-flat dyn-range",
    "aggr_slope":      "Aggregation: low-q slope",
    "w_neg":           "Weight — over-subtraction",
    "w_snr":           "Weight — low SNR",
    "w_cov":           "Weight — coverage",
    "w_spike":         "Weight — spikes",
    "w_featureless":   "Weight — featureless",
    "w_aggr":          "Weight — aggregation",
}


def _effective_params() -> dict:
    """Defaults with the user's overrides applied (full param set)."""
    p = dict(DEFAULT_THRESHOLDS)
    p.update(_params)
    return p


def _active_thresholds(detector: str | None) -> dict:
    """Per-detector defaults with the user's parameter overrides applied."""
    t = thresholds_for(detector)
    t.update(_params)
    return t


def _pass_threshold() -> float:
    return float(_params.get("score_pass", DEFAULT_THRESHOLDS["score_pass"]))


def _band() -> float:
    return float(_params.get("borderline", DEFAULT_THRESHOLDS["borderline"]))


def _config_path() -> Path | None:
    return Path(_project_root) / "quality_config.json" if _project_root else None


def _save_params() -> None:
    p = _config_path()
    if p is None:
        return
    try:
        p.write_text(json.dumps({"params": _params}, indent=2))
    except Exception as exc:
        _emit(f"⚠  could not save quality_config.json: {exc}", "warn")


def _load_params() -> None:
    p = _config_path()
    if p is None or not p.is_file():
        return
    try:
        data = json.loads(p.read_text() or "{}")
        saved = data.get("params", {})
        if isinstance(saved, dict):
            _params.clear()
            _params.update({k: float(v) for k, v in saved.items()
                            if k in DEFAULT_THRESHOLDS})
            _emit(f"loaded {len(_params)} saved scoring parameter override(s)", "info")
    except Exception as exc:
        _emit(f"⚠  could not load quality_config.json: {exc}", "warn")


def _emit(msg: str, tag: str = "info") -> None:
    global _seq
    line = {"ts": datetime.datetime.now().strftime("%H:%M:%S"), "msg": msg, "tag": tag}
    with _lock:
        _seq += 1
        _log.append((_seq, line))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_output_dir(p: Path) -> bool:
    """True for our own sort folders, so we never grade what we sorted."""
    return p.name in ("Good", "NeedsReview")


def _list_profiles(folder: Path) -> list[Path]:
    """Subtracted .dat files in *folder* (skips Good/NeedsReview and dotfiles)."""
    if not folder.is_dir():
        return []
    return sorted(
        f for f in folder.glob("*.dat")
        if f.is_file() and not f.name.startswith("._")
    )


def _downsample(q, I, n=64):
    q = np.asarray(q, float); I = np.asarray(I, float)
    m = np.isfinite(q) & np.isfinite(I) & (q > 0)
    q, I = q[m], I[m]
    if q.size == 0:
        return [], []
    if q.size > n:
        idx = np.round(np.linspace(0, q.size - 1, n)).astype(int)
        q, I = q[idx], I[idx]
    return q.tolist(), I.tolist()


def _spark(path: Path):
    """Tiny downsampled curve for gallery thumbnails."""
    try:
        _, q, I, _s, _m = read_dat_data_metadata(path)
        qs, Is = _downsample(q, I, 64)
        return {"q": qs, "I": Is}
    except Exception:
        return {"q": [], "I": []}


def _effective_verdict(rec: dict) -> str:
    """Verdict honoring (a) a user override, else (b) the live threshold."""
    ov = _overrides.get(rec["path"])
    if ov:
        return ov["verdict"]
    return "good" if rec["score"] >= _pass_threshold() else "bad"


def _sort_into_folder(src: Path, verdict: str) -> Path | None:
    """Copy *src* into Good/ or NeedsReview/ beside it; remove stale copy in the
    other folder.  Idempotent.  Returns the destination path (or None)."""
    good_dir = src.parent / "Good"
    bad_dir  = src.parent / "NeedsReview"
    dst_dir, other = (good_dir, bad_dir) if verdict == "good" else (bad_dir, good_dir)
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        if (not dst.exists()) or dst.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(src, dst)
        stale = other / src.name
        if stale.exists():
            stale.unlink()
        return dst
    except Exception as exc:
        _emit(f"⚠  could not sort {src.name}: {exc}", "warn")
        return None


def _llm_client():
    """Return an anthropic client or None (no key / library)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic  # noqa: PLC0415
        return anthropic.Anthropic(api_key=key)
    except Exception:
        return None


def _llm_judge(rec: dict, force: bool = False) -> dict | None:
    """LLM judgment of one profile.  When ``force`` is False this only fires for
    borderline scores and respects the enable toggle (used during grading); when
    True it always runs (used by the 'Re-grade with AI' button).  Returns
    {"verdict","note"} or None — always degrades gracefully."""
    if not force:
        if not _llm_enabled:
            return None
        if abs(rec["score"] - _pass_threshold()) > _band():
            return None
    client = _llm_client()
    if client is None:
        return None
    try:
        m = rec.get("metrics", {})
        prompt = (
            "You are grading a background-subtracted SAXS/WAXS profile as 'good' "
            "or 'bad'. A bad profile is over-subtracted (many negatives), very "
            "noisy (low SNR), or featureless (resembles background, no structure).\n"
            f"Metrics: score={rec['score']}, SNR={m.get('snr')}, "
            f"%negative={m.get('pct_negative')}, q_decades={m.get('q_decades')}, "
            f"dynamic_range={m.get('dyn_range')}, spikes={m.get('spike_frac')}.\n"
            f"Rule reasons: {rec.get('reasons')}.\n"
            "Reply with a single line: VERDICT=<good|bad>; <≤15-word reason>."
        )
        msg = client.messages.create(
            model=_llm_model, max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        verdict = "good" if "good" in text.lower().split(";")[0] else "bad"
        note = text.split(";", 1)[1].strip() if ";" in text else text
        return {"verdict": verdict, "note": note}
    except Exception:
        return None


def _llm_suggest_params() -> dict | None:
    """Ask the LLM to suggest scoring-weight adjustments from the user's
    overrides (labeled examples).  Returns {"params": {...}, "note": str} with
    only valid numeric keys, or None when unavailable."""
    client = _llm_client()
    if client is None:
        return None
    examples = []
    for path, ov in _overrides.items():
        rec = _results.get(path)
        if rec:
            examples.append({"user_verdict": ov["verdict"], "score": rec["score"],
                             "metrics": rec.get("metrics", {})})
    # need both classes to learn a boundary
    verdicts = {e["user_verdict"] for e in examples}
    if len({"good", "bad"} & verdicts) < 2 or len(examples) < 3:
        return None
    try:
        cur = _effective_params()
        prompt = (
            "You tune the weights/thresholds of a rule-based SAXS quality scorer "
            "(score 0-100, good>=score_pass). Below are profiles a scientist "
            "RE-LABELLED, with their metrics, plus the current parameters. Suggest "
            "small adjustments to better match the scientist's labels. Only return "
            "parameters that should change.\n"
            f"Current parameters: {json.dumps(cur)}\n"
            f"Labeled examples: {json.dumps(examples)[:6000]}\n"
            "Respond with ONLY a JSON object: "
            '{"params": {"<key>": <number>, ...}, "note": "<one sentence>"}'
        )
        msg = client.messages.create(
            model=_llm_model, max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1]) if start >= 0 else {}
        params = {k: float(v) for k, v in (data.get("params", {}) or {}).items()
                  if k in DEFAULT_THRESHOLDS and isinstance(v, (int, float))}
        return {"params": params, "note": data.get("note", "")}
    except Exception:
        return None


def _grade_and_record(path: Path, det: str) -> dict | None:
    """Grade one profile, write manifest + sort + event.  Returns the record."""
    # Per-detector signal thresholds, with the user's parameter overrides on top.
    t = _active_thresholds(det)
    rec = grade_profile(path, thresholds=t, detector=det)
    rec["detector"] = det
    rec["sample"] = sample_key(rec["name"])
    rec["spark"] = _spark(path)

    # Borderline LLM adjudication (optional).
    rec["llm_note"] = None
    adj = _llm_judge(rec)
    if adj:
        rec["llm_note"] = adj["note"]
        rec["llm_verdict"] = adj["verdict"]
        rec["verdict"] = adj["verdict"]   # LLM has final say on borderline calls

    verdict = _effective_verdict(rec)     # honor any user override
    rec["verdict"] = verdict

    prev = _results.get(rec["path"])
    _results[rec["path"]] = rec

    dst = _sort_into_folder(path, verdict)
    rec["sorted_to"] = str(dst) if dst else None

    if _project_root:
        try:
            prov = make_provenance("quality", input_files=[path],
                                   config={"threshold": _pass_threshold(),
                                           "detector": det})
            ov = _overrides.get(rec["path"])
            def _add(m, rec=rec, prov=prov, ov=ov):
                add_quality_entry(
                    m, path=rec["path"], score=rec["score"], verdict=rec["verdict"],
                    flags=rec["flags"], metrics=rec["metrics"], reasons=rec["reasons"],
                    detector=det, sample=rec["sample"],
                    source="user" if ov else "ai",
                    llm_note=rec.get("llm_note"),
                    overridden=bool(ov), override_note=(ov or {}).get("note"),
                    provenance=prov)
            update_manifest(_project_root, _add)
        except Exception as exc:
            _emit(f"⚠  manifest update failed for {path.name}: {exc}", "warn")

    if _bus is not None:
        try:
            _bus.publish("file.classified", {
                "file_path": str(path), "score": rec["score"],
                "verdict": verdict, "detector": det, "flags": rec["flags"],
            })
        except Exception:
            pass

    # Log only on first sighting or verdict change (keeps the log readable).
    if prev is None or prev.get("verdict") != verdict:
        tag = "ok" if verdict == "good" else "warn"
        mark = "✓" if verdict == "good" else "✗"
        why = ("; ".join(rec["reasons"][:2])) if verdict == "bad" else "clean"
        _emit(f"{mark}  {rec['name']}  ·  {rec['score']:.0f}/100  ·  {verdict.upper()}  ·  {why}", tag)
    return rec


def _recount() -> None:
    good = sum(1 for r in _results.values() if r["verdict"] == "good")
    bad  = len(_results) - good
    _status.update({"graded": len(_results), "good": good, "bad": bad,
                    "threshold": _pass_threshold()})


def _recolor() -> None:
    """Re-derive verdicts from the current threshold without re-reading files."""
    for rec in _results.values():
        rec["verdict"] = _effective_verdict(rec)
        try:
            _sort_into_folder(Path(rec["path"]), rec["verdict"])
        except Exception:
            pass
    _recount()


def _rescore_all() -> None:
    """Recompute score/flags/reasons for every cached profile from its stored
    metrics (no file re-read) after a scoring-parameter change, then re-derive
    verdicts and re-sort."""
    for rec in _results.values():
        t = _active_thresholds(rec.get("detector"))
        score, flags, reasons = score_metrics(rec.get("metrics", {}), t)
        rec["score"] = score
        rec["flags"] = flags
        if score >= t["score_pass"] and not reasons:
            reasons = ["clean profile — good SNR, structured, no over-subtraction"]
        rec["reasons"] = reasons
        rec["verdict"] = _effective_verdict(rec)
        try:
            _sort_into_folder(Path(rec["path"]), rec["verdict"])
        except Exception:
            pass
    _recount()


def _adapt_threshold() -> None:
    """Gently move the pass threshold to agree with the user's overrides:
    place it midway between the highest score the user called 'bad' and the
    lowest score they called 'good' (damped, clamped 20–90)."""
    gs, bs = _adapt["good_scores"], _adapt["bad_scores"]
    if not gs or not bs:
        return
    lo_good = min(gs); hi_bad = max(bs)
    if lo_good <= hi_bad:           # overlapping judgments — leave threshold alone
        return
    target = 0.5 * (lo_good + hi_bad)
    cur = _pass_threshold()
    new = cur + 0.5 * (target - cur)            # damping factor 0.5
    _params["score_pass"] = round(float(min(max(new, 20.0), 90.0)), 1)
    _save_params()


# ── Monitor loop ───────────────────────────────────────────────────────────────

def _on_bus_event(event: dict) -> None:
    """Grade immediately when the background app reports a new subtraction."""
    if not _grading:
        return
    etype = event.get("type") or event.get("event_type") or ""
    if etype != "file.subtracted":
        return
    data = event.get("data", event)
    fp = data.get("file_path")
    if not fp:
        return
    p = Path(fp)
    det = "waxs" if "waxs" in str(p).lower() else "saxs"
    try:
        _grade_and_record(p, det)
        _recount()
    except Exception as exc:
        _emit(f"⚠  event grade failed: {exc}", "warn")


def _grader_loop(dets, interval):
    global _grading
    _emit(f"▶  Quality grading started — every {interval}s  ·  "
          f"threshold {_pass_threshold():.0f}/100", "ok")
    while _grading:
        for det, folder in dets:
            fp = Path(folder)
            if not fp.is_dir() or _is_output_dir(fp):
                continue
            for prof in _list_profiles(fp):
                if not _grading:
                    break
                try:
                    _grade_and_record(prof, det)     # always reprocess
                except Exception as exc:
                    _emit(f"✗  {prof.name}: {exc}", "error")
        _recount()
        gc.collect()
        time.sleep(interval)
    _grading = False
    _status["monitoring"] = False
    _emit("⏹  Quality grading stopped", "warn")


# ── Routes ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "quality",
                    "good": _status["good"], "bad": _status["bad"],
                    "graded": _status["graded"]})


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    body = request.get_json(force=True)
    project = (body.get("path", "") or "").strip()
    if project:
        os.environ["SWAXS_PROJECT"] = project
        _project_root = project
        _load_params()          # pick up saved scoring-parameter overrides
    return jsonify({"ok": True})


@app.route("/api/project")
def api_project():
    return jsonify({"project_root": _project_root})


@app.route("/api/browse")
def api_browse():
    raw = (request.args.get("path", "") or "").strip()
    p = Path(raw) if raw else Path.home()
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.is_dir():
        p = Path.home()
    try:
        dirs = sorted(d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith("."))
    except PermissionError:
        dirs = []
    return jsonify({"current": str(p), "parent": str(p.parent) if p != p.parent else None,
                    "dirs": dirs})


def _public(rec: dict) -> dict:
    """Trim a record for the gallery payload."""
    return {"name": rec["name"], "path": rec["path"], "detector": rec.get("detector"),
            "sample": rec.get("sample"), "score": rec["score"], "verdict": rec["verdict"],
            "flags": rec["flags"], "reasons": rec["reasons"],
            "llm_note": rec.get("llm_note"), "spark": rec.get("spark", {"q": [], "I": []}),
            "overridden": rec["path"] in _overrides,
            "metrics": rec.get("metrics", {})}


@app.route("/api/grade", methods=["POST"])
def api_grade():
    """One-shot grade of the given folder(s) (no monitoring)."""
    global _llm_enabled, _llm_model
    body = request.get_json(force=True)
    saxs = (body.get("saxs_folder", "") or "").strip()
    waxs = (body.get("waxs_folder", "") or "").strip()
    if "threshold" in body:
        try:
            _params["score_pass"] = float(body["threshold"])
        except (TypeError, ValueError):
            pass
    if "llm_enabled" in body:
        _llm_enabled = bool(body["llm_enabled"])
    if (body.get("llm_model") or "").strip():
        _llm_model = body["llm_model"].strip()
    dets = [("saxs", saxs)] * bool(saxs) + [("waxs", waxs)] * bool(waxs)
    if not dets:
        return jsonify({"error": "No Subtracted folder provided"}), 400
    n = 0
    for det, folder in dets:
        for prof in _list_profiles(Path(folder)):
            try:
                _grade_and_record(prof, det); n += 1
            except Exception as exc:
                _emit(f"✗  {prof.name}: {exc}", "error")
    _recount()
    return jsonify({"ok": True, "graded": n,
                    "results": [_public(r) for r in _results.values()],
                    "status": _status})


@app.route("/api/results")
def api_results():
    return jsonify({"results": [_public(r) for r in _results.values()],
                    "status": _status, "threshold": _pass_threshold()})


@app.route("/api/profile")
def api_profile():
    path = (request.args.get("path", "") or "").strip()
    p = Path(path)
    if not p.is_file():
        return jsonify({"error": "not found"}), 404
    try:
        _, q, I, sigma, meta = read_dat_data_metadata(p)
        qs, Is = _downsample(q, I, 400)
        _, ss = _downsample(q, sigma, 400)
        rec = _results.get(str(p)) or _results.get(str(p.resolve()))
        return jsonify({"name": p.name, "q": qs, "I": Is, "sigma": ss,
                        "record": _public(rec) if rec else None})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/threshold", methods=["POST"])
def api_threshold():
    body = request.get_json(force=True)
    try:
        _params["score_pass"] = float(min(max(float(body.get("threshold", 60)), 0), 100))
    except (TypeError, ValueError):
        return jsonify({"error": "bad threshold"}), 400
    _save_params()
    _recolor()
    return jsonify({"ok": True, "threshold": _pass_threshold(),
                    "results": [_public(r) for r in _results.values()], "status": _status})


@app.route("/api/override", methods=["POST"])
def api_override():
    body = request.get_json(force=True)
    path = (body.get("path", "") or "").strip()
    verdict = (body.get("verdict", "") or "").strip().lower()
    note = (body.get("note", "") or "").strip()
    rec = _results.get(path) or _results.get(str(Path(path).resolve()))
    if rec is None or verdict not in ("good", "bad"):
        return jsonify({"error": "unknown profile or verdict"}), 400
    _overrides[rec["path"]] = {"verdict": verdict, "note": note}
    # remember for threshold adaptation
    (_adapt["good_scores"] if verdict == "good" else _adapt["bad_scores"]).append(rec["score"])
    _adapt_threshold()
    rec["verdict"] = verdict
    _sort_into_folder(Path(rec["path"]), verdict)
    if _project_root:
        try:
            prov = make_provenance("quality", input_files=[Path(rec["path"])],
                                   config={"override": True})
            def _add(m, rec=rec, note=note, prov=prov):
                add_quality_entry(m, path=rec["path"], score=rec["score"], verdict=rec["verdict"],
                                  flags=rec["flags"], metrics=rec["metrics"], reasons=rec["reasons"],
                                  detector=rec.get("detector"), sample=rec.get("sample"),
                                  source="user", llm_note=rec.get("llm_note"),
                                  overridden=True, override_note=note, provenance=prov)
            update_manifest(_project_root, _add)
        except Exception:
            pass
    _emit(f"✎  override: {rec['name']} → {verdict.upper()}"
          f"{(' — ' + note) if note else ''}  (threshold now {_pass_threshold():.0f})", "info")
    _recount()
    return jsonify({"ok": True, "threshold": _pass_threshold(),
                    "results": [_public(r) for r in _results.values()], "status": _status})


@app.route("/api/params", methods=["GET", "POST"])
def api_params():
    """GET current scoring parameters; POST a dict of overrides to change them."""
    if request.method == "POST":
        body = request.get_json(force=True)
        incoming = body.get("params", {}) or {}
        changed = 0
        for k, v in incoming.items():
            if k not in DEFAULT_THRESHOLDS:
                continue
            try:
                _params[k] = float(v)
                changed += 1
            except (TypeError, ValueError):
                pass
        _save_params()
        _rescore_all()
        _emit(f"⚙  scoring parameters updated ({changed} field(s))", "info")
        return jsonify({"ok": True, "changed": changed,
                        "params": _effective_params(),
                        "results": [_public(r) for r in _results.values()],
                        "status": _status, "threshold": _pass_threshold()})
    return jsonify({"params": _effective_params(),
                    "defaults": dict(DEFAULT_THRESHOLDS),
                    "overridden": sorted(_params.keys()),
                    "labels": _PARAM_LABELS})


@app.route("/api/params/reset", methods=["POST"])
def api_params_reset():
    _params.clear()
    _save_params()
    _rescore_all()
    _emit("⚙  scoring parameters reset to defaults", "info")
    return jsonify({"ok": True, "params": _effective_params(),
                    "results": [_public(r) for r in _results.values()],
                    "status": _status, "threshold": _pass_threshold()})


@app.route("/api/llm_grade", methods=["POST"])
def api_llm_grade():
    """Force a full LLM judgment on one profile (ignores the borderline band)."""
    body = request.get_json(force=True)
    path = (body.get("path", "") or "").strip()
    rec = _results.get(path) or _results.get(str(Path(path).resolve()))
    if rec is None:
        return jsonify({"error": "unknown profile"}), 400
    adj = _llm_judge(rec, force=True)
    if adj is None:
        return jsonify({"ok": False,
                        "error": "LLM unavailable (no ANTHROPIC_API_KEY or library)"}), 200
    rec["llm_note"] = adj["note"]
    rec["verdict"] = adj["verdict"]
    _overrides[rec["path"]] = {"verdict": adj["verdict"], "note": "AI: " + adj["note"]}
    _sort_into_folder(Path(rec["path"]), rec["verdict"])
    _recount()
    _emit(f"🧠  AI re-grade: {rec['name']} → {adj['verdict'].upper()} — {adj['note']}", "ok")
    return jsonify({"ok": True, "results": [_public(r) for r in _results.values()],
                    "status": _status})


@app.route("/api/refine_ai", methods=["POST"])
def api_refine_ai():
    """Ask the LLM to suggest adjusted scoring weights/thresholds that better
    match the user's overrides.  Returns suggestions (NOT applied)."""
    suggestion = _llm_suggest_params()
    if suggestion is None:
        return jsonify({"ok": False,
                        "error": "Need an ANTHROPIC_API_KEY and at least a few "
                                 "overrides (good and bad) to suggest weights."}), 200
    return jsonify({"ok": True, "suggested": suggestion.get("params", {}),
                    "note": suggestion.get("note", ""),
                    "defaults": dict(DEFAULT_THRESHOLDS),
                    "current": _effective_params()})


@app.route("/api/report")
def api_report():
    """Write a QC summary report (CSV + accepted-list) under the project and
    return its contents."""
    rows = [_public(r) for r in _results.values()]
    rows.sort(key=lambda r: (r["detector"] or "", -r["score"]))
    hdr = ["name", "detector", "sample", "score", "verdict", "flags", "overridden", "reasons"]
    lines = [",".join(hdr)]
    for r in rows:
        lines.append(",".join([
            r["name"], r["detector"] or "", r["sample"] or "", f"{r['score']:.1f}",
            r["verdict"], "|".join(r["flags"]), "yes" if r["overridden"] else "no",
            (" ".join(r["reasons"])).replace(",", ";"),
        ]))
    csv = "\n".join(lines) + "\n"
    accepted = [r["name"] for r in rows if r["verdict"] == "good"]
    saved = None
    if _project_root:
        try:
            rep_dir = Path(_project_root) / "1D" / "QualityReports"
            rep_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            (rep_dir / f"quality_report_{stamp}.csv").write_text(csv)
            (rep_dir / f"accepted_{stamp}.txt").write_text("\n".join(accepted) + "\n")
            saved = str(rep_dir)
        except Exception:
            pass
    return jsonify({"csv": csv, "accepted": accepted, "saved_to": saved,
                    "counts": {"good": _status["good"], "bad": _status["bad"],
                               "total": _status["graded"]}})


@app.route("/api/monitor/start", methods=["POST"])
def monitor_start():
    global _grading, _grader_thread, _watch, _llm_enabled, _llm_model
    if _grading:
        return jsonify({"ok": False, "error": "Already monitoring"})
    body = request.get_json(force=True)
    interval = max(int(body.get("interval", 10) or 10), 1)
    saxs = (body.get("saxs_folder", "") or "").strip()
    waxs = (body.get("waxs_folder", "") or "").strip()
    if "threshold" in body:
        try:
            _params["score_pass"] = float(body["threshold"])
        except (TypeError, ValueError):
            pass
    if "llm_enabled" in body:
        _llm_enabled = bool(body["llm_enabled"])
    if (body.get("llm_model") or "").strip():
        _llm_model = body["llm_model"].strip()
    dets = [("saxs", saxs)] * bool(saxs) + [("waxs", waxs)] * bool(waxs)
    if not dets:
        return jsonify({"ok": False, "error": "No Subtracted folder provided"}), 400
    _watch = dets
    _status.update({"monitoring": True, "interval": interval,
                    "threshold": _pass_threshold()})
    _grading = True
    _grader_thread = threading.Thread(target=_grader_loop, args=(dets, interval), daemon=True)
    _grader_thread.start()
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
def monitor_stop():
    global _grading
    _grading = False
    return jsonify({"ok": True})


@app.route("/api/monitor/status")
def monitor_status():
    return jsonify(_status)


@app.route("/api/monitor/stream")
def monitor_stream():
    def _generate():
        last = 0
        while True:
            with _lock:
                new = [(s, ln) for (s, ln) in _log if s > last]
            for s, ln in new:
                last = s
                yield f"data: {json.dumps(ln)}\n\n"
            yield ": keepalive\n\n"
            time.sleep(0.8)
    return Response(_generate(), mimetype="text/event-stream")


# Subscribe to the bus so a new subtraction is graded immediately (poll is the
# backstop).  Safe no-op if the bus is unavailable.
if _bus is not None:
    try:
        _bus.on_event(_on_bus_event)
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _project_root = os.environ.get("SWAXS_PROJECT", "")
    print("━" * 52)
    print("  SWAXS Quality Gate  ·  http://localhost:5006")
    print("━" * 52)
    app.run(host="127.0.0.1", port=5006, debug=False, threaded=True)
