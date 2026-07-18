"""
analyzer/app.py — Nanoparticle Analyzer (port 5008)
===================================================
Watches the SAXS Subtracted folder and, as each new profile appears, fits a
polydisperse-sphere model to extract size, PDI, the (relative) Porod invariant,
and a 0-1 confidence — the measurement half of the closed synthesis loop.

Thin Flask shell: all science is in src/analysis/nanoparticle.py. Routes, the
folder watcher, SSE, and manifest writing live here.

Run:  uv run analyzer/app.py    Open: http://localhost:5008
"""

from __future__ import annotations

import collections
import json
import os
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

from src.analysis.nanoparticle import analyze_profile, model_intensity   # noqa: E402
from src.utils.read_dat_metadata import read_dat_data_metadata           # noqa: E402
from src.reactor.intake import decide_intake                             # noqa: E402
from src.manifest import update_manifest, add_analysis_entry             # noqa: E402
from src.ai.loop_advice import narrate_fit                               # noqa: E402
from src.reactor import load_config                                      # noqa: E402
from src.optimizer import ParameterSpace, CampaignController             # noqa: E402
from src.optimizer.io import to_param_file, match_recipe_id             # noqa: E402
import datetime, uuid                                                    # noqa: E402,E401

app = Flask(__name__)

_project_root: str = os.environ.get("SWAXS_PROJECT", "")
_sub_folder: str = "1D/SAXS/Subtracted"     # relative to project (or absolute)
_cond_folder: str = "1D/SAXS/Conditions"    # where proposed conditions are written (reactor watches this)

# ── closed-loop campaign state ─────────────────────────────────────────────────
_campaign: CampaignController | None = None
_pending: dict = {}          # recipe_id -> proposed params awaiting a measurement
_campaign_lock = threading.Lock()

_results: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_results_lock = threading.Lock()
_log: collections.deque = collections.deque(maxlen=300)
_seq = 0
_log_lock = threading.Lock()


def _emit(msg: str, tag: str = "info") -> None:
    global _seq
    with _log_lock:
        _seq += 1
        _log.append((_seq, {"ts": time.strftime("%H:%M:%S"), "msg": msg, "tag": tag}))


def _resolve_sub() -> Path:
    p = Path(_sub_folder)
    if not p.is_absolute():
        p = (Path(_project_root) if _project_root else Path.cwd()) / _sub_folder
    return p


def _resolve_cond() -> Path:
    p = Path(_cond_folder)
    if not p.is_absolute():
        p = (Path(_project_root) if _project_root else Path.cwd()) / _cond_folder
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
            _write_condition(rid, p)
            return
    st = _campaign.status_str
    if st == "converged":
        cc = _campaign.converged_condition or {}
        _emit(f"🎯 campaign CONVERGED — size {cc.get('size')} at "
              f"{ {k: round(v,1) for k,v in (cc.get('params') or {}).items()} }", "ok")
    elif st == "exhausted":
        _emit(f"⏹ campaign budget exhausted ({_campaign.status()['n_evaluations']} runs) — "
              f"best size {(_campaign.best or {}).get('size')}", "warn")
    elif st == "aborted":
        _emit("⏹ campaign aborted", "warn")


def _feed_campaign(name: str, res: dict) -> None:
    """Match a measured profile to a pending proposed condition and drive the loop."""
    with _campaign_lock:
        if _campaign is None or _campaign.status_str != "running":
            return
        rid = match_recipe_id(name, _pending.keys())
        if not rid:
            return
        params = _pending.pop(rid)
        sz = res.get("size") or {}
        size = sz.get("radius")
        pdi = res.get("pdi")
        conf = res.get("confidence", 0.0)
        _campaign.tell(params, size, pdi, conf)
        _emit(f"📊 told campaign {rid}: R={size} PDI={pdi} conf={conf} "
              f"(loss={_campaign.history[-1]['loss']:.3f})", "info")
        _advance_campaign()


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
                      "model": _downsample(model) if model is not None else None}}
    with _results_lock:
        _results[path.name] = entry
    conf = summary["confidence"]
    tag = "ok" if conf >= 0.6 else ("warn" if conf >= 0.3 else "info")
    r = summary["radius"]
    _emit(f"✓ {path.name}: R={r} PDI={summary['pdi']} conf={conf} ({summary['distribution']})", tag)
    # record in the manifest (best-effort)
    if _project_root:
        try:
            update_manifest(_project_root, lambda mf: add_analysis_entry(
                mf, analysis_type="nanoparticle", file_path=path,
                params={"model": "polydisperse_sphere", "distribution": summary["distribution"]},
                results=summary, quality_score=conf))
        except Exception as exc:
            _emit(f"⚠ manifest write failed: {exc}", "warn")
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
        except Exception:
            pass
        time.sleep(3.0)


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
        _handled.clear(); _lastsig.clear()      # rescan under the new project
        _emit(f"📁 project → {p}", "info")
    return jsonify({"ok": True, "watching": str(_resolve_sub())})


@app.route("/api/folder", methods=["GET", "POST"])
def api_folder():
    global _sub_folder
    if request.method == "POST":
        f = (request.get_json(silent=True) or {}).get("folder", "").strip()
        if f:
            _sub_folder = f
            _handled.clear(); _lastsig.clear()
            _emit(f"📁 watching → {f}", "info")
    return jsonify({"folder": _sub_folder, "resolved": str(_resolve_sub())})


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
    global _campaign
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
            _campaign.start()
            _emit(f"🚀 campaign started — target R={_campaign.target_size}±{_campaign.tolerance} nm, "
                  f"PDI<{_campaign.pdi_cap}, budget {_campaign.budget}", "ok")
            _advance_campaign()             # emit the first condition
        return jsonify({"ok": True, "campaign": _campaign_status()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/campaign/abort", methods=["POST"])
def api_campaign_abort():
    with _campaign_lock:
        if _campaign is not None:
            _campaign.abort()
            _emit("⏹ campaign aborted by operator", "warn")
    return jsonify({"ok": True})


@app.route("/api/campaign/folder", methods=["GET", "POST"])
def api_campaign_folder():
    global _cond_folder
    if request.method == "POST":
        f = (request.get_json(silent=True) or {}).get("folder", "").strip()
        if f:
            _cond_folder = f
    return jsonify({"folder": _cond_folder, "resolved": str(_resolve_cond())})


@app.route("/api/stream")
def api_stream():
    def gen():
        last = 0
        while True:
            with _log_lock:
                new = [ln for (s, ln) in _log if s > last]
                if _log:
                    last = _log[-1][0]
            with _results_lock:
                summ = [e["summary"] for e in _results.values()]
            yield "data: " + json.dumps({"results": summ, "logs": new,
                                         "campaign": _campaign_status()}) + "\n\n"
            time.sleep(1.0)
    return Response(gen(), mimetype="text/event-stream")


if __name__ == "__main__":
    _project_root = os.environ.get("SWAXS_PROJECT", _project_root)
    print("━" * 52)
    print("  Nanoparticle Analyzer  →  http://localhost:5008")
    print(f"  watching: {_resolve_sub()}")
    print("━" * 52)
    app.run(host="127.0.0.1", port=5008, debug=False, threaded=True)
