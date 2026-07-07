"""
analysis/app.py — SWAXS Data Analysis App (port 5004)
======================================================
Tabs:
  • Guinier   — Rg, I₀ from ln(I) vs q² fit (q·Rg ≤ 1.3)
  • Porod     — power-law slope (log-log), Porod constant
  • Kratky    — I·q² vs q (globular vs disordered)
  • Peak      — Gaussian + linear background; d-spacing, Scherrer width
  • Model     — sasmodels library model fitting

Run:  uv run analysis/app.py
Open: http://localhost:5004
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template, request

# ── sys.path ──────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.read_dat_metadata import read_dat_data_metadata   # noqa: E402
from src.manifest import (                                        # noqa: E402
    update_manifest, add_analysis_entry, make_provenance,
)
from src.analysis.core import (                                   # noqa: E402
    guinier_fit, porod_fit, kratky_plot, peak_fit, sasmodels_fit,
    pair_distance_ift, dimensionless_kratky, classical_invariants,
    guinier_quality, sasmodels_params,
)
from src.analysis import io as analysis_io                        # noqa: E402
from src.analysis import atsas as atsas_mod                       # noqa: E402

# ── Event bus (graceful degradation) ─────────────────────────────────────────
try:
    from src.events import EventBusClient as _EventBusClient
    _bus = _EventBusClient("analysis").connect(retry=True)
except Exception:
    _bus = None

app = Flask(__name__)
_project_root: str = ""


# ── Data loading ──────────────────────────────────────────────────────────────
# Analysis functions (guinier_fit, porod_fit, kratky_plot, peak_fit,
# sasmodels_fit) live in src/analysis/core.py and are imported above.

def _load(path: Path):
    """Return (q, I, sigma) arrays, positive only."""
    _, q, I, sigma, _ = read_dat_data_metadata(path)
    mask = (q > 0) & (I > 0) & (sigma > 0)
    return q[mask], I[mask], sigma[mask]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "analysis"})


@app.route("/api/set_project", methods=["POST"])
def set_project():
    global _project_root
    body = request.get_json(force=True)
    path = body.get("path", "").strip()
    if path and Path(path).is_dir():
        _project_root = path
    return jsonify({"ok": True})


@app.route("/api/project")
def api_project():
    """Current project root (set by the hub) — used by the UI to auto-fill paths."""
    return jsonify({"project_root": _project_root})


@app.route("/api/browse")
def api_browse():
    raw = request.args.get("path", "").strip()
    p   = Path(raw) if raw else Path.home()
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.is_dir():
        p = Path.home()
    try:
        dirs = sorted(d.name for d in p.iterdir()
                      if d.is_dir() and not d.name.startswith("."))
    except PermissionError:
        dirs = []
    return jsonify({"current": str(p), "parent": str(p.parent) if p != p.parent else None, "dirs": dirs})


@app.route("/api/load_dat", methods=["POST"])
def api_load_dat():
    """Load a .dat file and return (q, I, sigma) for display."""
    body = request.get_json(force=True)
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        q, I, sigma = _load(path)
        n = 600
        if len(q) > n:
            idx = np.round(np.linspace(0, len(q)-1, n)).astype(int)
            q, I, sigma = q[idx], I[idx], sigma[idx]
        return jsonify({"q": q.tolist(), "I": I.tolist(), "sigma": sigma.tolist(),
                        "filename": path.name})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/guinier", methods=["POST"])
def api_guinier():
    body = request.get_json(force=True)
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        q, I, sigma = _load(path)
        result = guinier_fit(
            q, I, sigma,
            q_min=body.get("q_min") or None,
            q_max=body.get("q_max") or None,
            auto_range=bool(body.get("auto_range", True)),
        )
        if "error" not in result:
            if _project_root:
                prov = make_provenance("analysis", input_files=[path])
                update_manifest(_project_root, lambda m: add_analysis_entry(
                    m, analysis_type="guinier", file_path=path,
                    params=body, results=result,
                    fit_range=result.get("q_range", []),
                    provenance=prov))
            if _bus is not None:
                try:
                    _bus.emit_analysis_complete("guinier", str(path), result)
                except Exception:
                    pass
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/porod", methods=["POST"])
def api_porod():
    body = request.get_json(force=True)
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        q, I, sigma = _load(path)
        result = porod_fit(
            q, I, sigma,
            q_min=body.get("q_min") or None,
            q_max=body.get("q_max") or None,
        )
        if "error" not in result:
            if _project_root:
                prov = make_provenance("analysis", input_files=[path])
                update_manifest(_project_root, lambda m: add_analysis_entry(
                    m, analysis_type="porod", file_path=path,
                    params=body, results=result,
                    fit_range=result.get("q_range", []),
                    provenance=prov))
            if _bus is not None:
                try:
                    _bus.emit_analysis_complete("porod", str(path), result)
                except Exception:
                    pass
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kratky", methods=["POST"])
def api_kratky():
    body = request.get_json(force=True)
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        q, I, sigma = _load(path)
        result = kratky_plot(
            q, I,
            q_min=body.get("q_min") or None,
            q_max=body.get("q_max") or None,
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/peak", methods=["POST"])
def api_peak():
    body = request.get_json(force=True)
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        q, I, sigma = _load(path)
        result = peak_fit(
            q, I, sigma,
            q_min=body.get("q_min") or None,
            q_max=body.get("q_max") or None,
            n_peaks=max(1, int(body.get("n_peaks", 1))),
        )
        if "error" not in result:
            if _project_root:
                prov = make_provenance("analysis", input_files=[path])
                update_manifest(_project_root, lambda m: add_analysis_entry(
                    m, analysis_type="peak", file_path=path,
                    params=body, results=result,
                    fit_range=result.get("q_range", []),
                    provenance=prov))
            if _bus is not None:
                try:
                    _bus.emit_analysis_complete("peak", str(path), result)
                except Exception:
                    pass
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/model", methods=["POST"])
def api_model():
    body = request.get_json(force=True)
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    model_name = body.get("model_name", "sphere")
    params     = body.get("params", {})
    try:
        q, I, sigma = _load(path)
        # Apply q-range
        q_min = body.get("q_min") or None
        q_max = body.get("q_max") or None
        mask  = np.ones(len(q), bool)
        if q_min: mask &= q >= float(q_min)
        if q_max: mask &= q <= float(q_max)
        q, I, sigma = q[mask], I[mask], sigma[mask]
        result = sasmodels_fit(q, I, sigma, model_name, params)
        if "error" not in result:
            if _project_root:
                prov = make_provenance("analysis", input_files=[path])
                update_manifest(_project_root, lambda m: add_analysis_entry(
                    m, analysis_type="model", file_path=path,
                    params=body, results=result,
                    provenance=prov))
            if _bus is not None:
                try:
                    _bus.emit_analysis_complete("model", str(path), result)
                except Exception:
                    pass
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Classical analysis (unified individual + batch) ───────────────────────────

_CLASSICAL = ("guinier", "kratky", "porod", "pair_distance",
              "dimensionless_kratky", "invariant")


def _detector_of(path: Path) -> str:
    s = str(path).upper()
    return "waxs" if "/WAXS/" in s or "_WAXS" in s else "saxs"


def _stage_dir(detector: str, stage: str = "Subtracted") -> Path | None:
    if not _project_root:
        return None
    d = Path(_project_root) / "1D" / detector.upper() / stage.capitalize()
    return d if d.is_dir() else None


def _subtracted_dir(detector: str) -> Path | None:
    return _stage_dir(detector, "Subtracted")


def _run_classical(path: Path, analysis: str, body: dict) -> dict:
    """Run one classical analysis; return {results, qc, fit_curve?}. No saving."""
    q, I, sigma = _load(path)
    qmin = body.get("q_min") or None
    qmax = body.get("q_max") or None
    auto = bool(body.get("auto_range", True))

    if analysis == "guinier":
        res = guinier_fit(q, I, sigma, q_min=qmin, q_max=qmax, auto_range=auto)
        qc = guinier_quality(res, body.get("shape", "globular")) if "error" not in res else None
        fit = None
        if "error" not in res:
            Rg, I0 = res["Rg"], float(res["I0"])
            qf = np.linspace(q.min(), q.max(), 300)
            fit = (qf.tolist(), (I0 * np.exp(-(Rg ** 2) * qf ** 2 / 3.0)).tolist())
        return {"results": res, "qc": qc, "fit_curve": fit}

    if analysis == "porod":
        res = porod_fit(q, I, sigma, q_min=qmin, q_max=qmax)
        return {"results": res, "qc": None, "fit_curve": None}

    if analysis == "kratky":
        return {"results": kratky_plot(q, I, q_min=qmin, q_max=qmax),
                "qc": None, "fit_curve": None}

    if analysis == "pair_distance":
        res = pair_distance_ift(q, I, sigma, dmax=body.get("dmax"))
        fit = (res.get("q_fit"), res.get("I_fit")) if "error" not in res else None
        return {"results": res, "qc": None, "fit_curve": fit}

    # dimensionless_kratky and invariant need Rg, I0 — auto-run Guinier if absent
    Rg = body.get("Rg"); I0 = body.get("I0")
    if Rg is None or I0 is None:
        g = guinier_fit(q, I, sigma, auto_range=True)
        if "error" in g:
            return {"results": {"error": f"Need Rg/I0 (Guinier failed: {g['error']})."}}
        Rg, I0 = g["Rg"], float(g["I0"])

    if analysis == "dimensionless_kratky":
        return {"results": dimensionless_kratky(q, I, float(Rg), float(I0)),
                "qc": None, "fit_curve": None}
    if analysis == "invariant":
        return {"results": classical_invariants(q, I, float(Rg), float(I0)),
                "qc": None, "fit_curve": None}
    return {"results": {"error": f"Unknown analysis '{analysis}'."}}


@app.route("/api/classical", methods=["POST"])
def api_classical():
    """Run ONE classical analysis on one file; optionally save to Analysed/."""
    body = request.get_json(force=True) or {}
    path = Path(body.get("path", "").strip())
    analysis = (body.get("analysis") or "guinier").lower()
    if analysis not in _CLASSICAL:
        return jsonify({"error": f"analysis must be one of {_CLASSICAL}"}), 400
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        out = _run_classical(path, analysis, body)
        res = out["results"]
        if "error" in res:
            return jsonify(res), 200
        saved = None
        if body.get("save", False):
            saved = analysis_io.save_analysis(
                _project_root or None, path, _detector_of(path), analysis,
                params={k: body.get(k) for k in ("q_min", "q_max", "auto_range", "dmax")},
                results=res, fit_curve=out.get("fit_curve"),
                user=body.get("user", ""))
        if _bus is not None:
            try:
                _bus.emit_analysis_complete(analysis, str(path), res)
            except Exception:
                pass
        return jsonify({"results": res, "qc": out.get("qc"),
                        "fit_curve": out.get("fit_curve"), "saved": saved})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/classical/batch", methods=["POST"])
def api_classical_batch():
    """Run a classical analysis over all Subtracted files matching a keyword,
    save each, and write a combined summary table."""
    body = request.get_json(force=True) or {}
    analysis = (body.get("analysis") or "guinier").lower()
    keyword  = (body.get("keyword") or "").lower()
    detector = (body.get("detector") or "SAXS").lower()
    if analysis not in _CLASSICAL:
        return jsonify({"error": f"analysis must be one of {_CLASSICAL}"}), 400
    sub = _subtracted_dir(detector)
    if sub is None:
        return jsonify({"error": "No Subtracted folder for this detector/project."}), 400

    files = [p for p in sorted(sub.glob("*.dat"))
             if not keyword or keyword in p.name.lower()]
    if not files:
        return jsonify({"error": f"No subtracted {detector.upper()} files match '{keyword}'."}), 200

    rows, errors = [], []
    for p in files:
        try:
            out = _run_classical(p, analysis, body)
            res = out["results"]
            if "error" in res:
                errors.append({"file": p.name, "error": res["error"]})
                continue
            analysis_io.save_analysis(_project_root, p, detector, analysis,
                                      params=body, results=res,
                                      fit_curve=out.get("fit_curve"),
                                      user=body.get("user", ""))
            row = {"file": p.name}
            row.update({k: v for k, v in res.items()
                        if isinstance(v, (int, float, str)) and k != "plot"})
            rows.append(row)
        except Exception as exc:
            errors.append({"file": p.name, "error": str(exc)})

    summary = None
    if rows:
        out_dir = Path(_project_root) / "1D" / detector.upper() / "Analysed" / \
            analysis_io._TYPE_DIR.get(analysis, analysis.capitalize())
        summary = analysis_io.write_batch_summary(out_dir, analysis, rows)
    return jsonify({"analysis": analysis, "detector": detector.upper(),
                    "n_ok": len(rows), "n_error": len(errors),
                    "rows": rows, "errors": errors, "summary": summary})


@app.route("/api/list_subtracted")
def api_list_subtracted():
    """List .dat files. Either from an explicit ``dir`` (independent use) or from
    the project's 1D/<detector>/<stage>/ folder. Optional keyword filter."""
    keyword  = (request.args.get("keyword") or "").lower()
    explicit = (request.args.get("dir") or "").strip()
    if explicit:
        d = Path(explicit)
    else:
        detector = (request.args.get("detector") or "SAXS").lower()
        stage    = (request.args.get("stage") or "Subtracted")
        d = _stage_dir(detector, stage)
    if d is None or not d.is_dir():
        return jsonify({"files": [], "dir": (str(d) if d else None)})
    files = [{"name": p.name, "path": str(p)} for p in sorted(d.glob("*.dat"))
             if not keyword or keyword in p.name.lower()]
    return jsonify({"files": files, "dir": str(d)})


@app.route("/api/save", methods=["POST"])
def api_save():
    """Persist a previously-run result to Analysed/ (fit and save are separate —
    the UI calls this only when the user is happy with the fit). Works without a
    project (saves next to the source .dat; manifest registration is skipped)."""
    body = request.get_json(force=True) or {}
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    atype = body.get("analysis_type") or "analysis"
    detector = body.get("detector") or _detector_of(path)
    fc = body.get("fit_curve")
    fit = (fc[0], fc[1]) if (isinstance(fc, list) and len(fc) == 2 and fc[0]) else None
    try:
        saved = analysis_io.save_analysis(
            _project_root, path, detector, atype,
            params=body.get("params", {}), results=body.get("results", {}),
            fit_curve=fit, user=body.get("user", ""))
        return jsonify({"saved": saved})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── WAXS peak fitting ─────────────────────────────────────────────────────────

def _peak_qc(res: dict) -> dict:
    warns = []
    if isinstance(res.get("chi2"), (int, float)) and res["chi2"] > 5:
        warns.append(f"High reduced χ² = {res['chi2']} — check peak count/shape.")
    if not res.get("peaks"):
        warns.append("No peaks fitted.")
    for pk in res.get("peaks", []):
        if pk.get("fwhm", 0) >= (res.get("q_range", [0, 1])[1] - res.get("q_range", [0, 1])[0]):
            warns.append(f"Peak at q={pk['q0']} is very broad — may be background.")
    return {"verdict": "PASS" if not warns else "WARN", "warnings": warns}


@app.route("/api/waxs_peaks", methods=["POST"])
def api_waxs_peaks():
    """Auto-detect + fit WAXS peaks (gaussian/lorentzian/voigt); save to Analysed/Peaks/."""
    body = request.get_json(force=True) or {}
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        q, I, sigma = _load(path)
        res = peak_fit(q, I, sigma, q_min=body.get("q_min") or None,
                       q_max=body.get("q_max") or None,
                       n_peaks=(int(body["n_peaks"]) if body.get("n_peaks") else None),
                       shape=body.get("shape", "gaussian"))
        if "error" in res:
            return jsonify(res), 200
        qc = _peak_qc(res)
        p = res.get("plot", {})
        saved = None
        if body.get("save", False):
            flat = {"shape": res["shape"], "n_peaks": res["n_peaks"], "chi2": res["chi2"]}
            for i, pk in enumerate(res["peaks"], 1):
                for k in ("q0", "fwhm", "area", "d_nm", "d_A"):
                    flat[f"peak{i}_{k}"] = pk.get(k)
            saved = analysis_io.save_analysis(
                _project_root or None, path, _detector_of(path), "peaks",
                params={"shape": res["shape"], "n_peaks": res["n_peaks"]},
                results=flat, fit_curve=(p.get("q_fit"), p.get("I_fit")),
                user=body.get("user", ""))
        if _bus is not None:
            try:
                _bus.emit_analysis_complete("peaks", str(path), res)
            except Exception:
                pass
        return jsonify({"results": res, "qc": qc, "saved": saved})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/waxs_peaks/batch", methods=["POST"])
def api_waxs_peaks_batch():
    """Fit peaks across files matching a keyword (WAXS); per-file + summary."""
    body = request.get_json(force=True) or {}
    keyword  = (body.get("keyword") or "").lower()
    detector = (body.get("detector") or "WAXS").lower()
    stage    = body.get("stage") or "Subtracted"
    d = _stage_dir(detector, stage)
    if d is None:
        return jsonify({"error": f"No {stage} folder for {detector.upper()}."}), 400
    files = [p for p in sorted(d.glob("*.dat")) if not keyword or keyword in p.name.lower()]
    if not files:
        return jsonify({"error": f"No {detector.upper()} {stage} files match '{keyword}'."}), 200
    rows, errors = [], []
    for p in files:
        try:
            q, I, sigma = _load(p)
            res = peak_fit(q, I, sigma, q_min=body.get("q_min") or None,
                           q_max=body.get("q_max") or None,
                           n_peaks=(int(body["n_peaks"]) if body.get("n_peaks") else None),
                           shape=body.get("shape", "gaussian"))
            if "error" in res:
                errors.append({"file": p.name, "error": res["error"]}); continue
            pl = res.get("plot", {})
            flat = {"shape": res["shape"], "n_peaks": res["n_peaks"], "chi2": res["chi2"]}
            for i, pk in enumerate(res["peaks"], 1):
                for k in ("q0", "fwhm", "area", "d_nm", "d_A"):
                    flat[f"peak{i}_{k}"] = pk.get(k)
            analysis_io.save_analysis(_project_root, p, detector, "peaks",
                                      params={"shape": res["shape"]}, results=flat,
                                      fit_curve=(pl.get("q_fit"), pl.get("I_fit")),
                                      user=body.get("user", ""))
            rows.append({"file": p.name, **flat})
        except Exception as exc:
            errors.append({"file": p.name, "error": str(exc)})
    summary = None
    if rows:
        out_dir = Path(_project_root) / "1D" / detector.upper() / "Analysed" / "Peaks"
        summary = analysis_io.write_batch_summary(out_dir, "peaks", rows)
    return jsonify({"detector": detector.upper(), "n_ok": len(rows),
                    "n_error": len(errors), "rows": rows, "errors": errors,
                    "summary": summary})


# ── SASView (sasmodels) model fitting ─────────────────────────────────────────

@app.route("/api/sasmodels/params")
def api_sasmodels_params():
    """Parameters (name, default, units, limits) of a model — to build the UI."""
    model = request.args.get("model", "").strip()
    if not model:
        return jsonify({"error": "model required"}), 400
    return jsonify(sasmodels_params(model))


def _trim_q(q, I, sigma, body):
    qmin = body.get("q_min") or None
    qmax = body.get("q_max") or None
    m = np.ones(len(q), bool)
    if qmin:
        m &= q >= float(qmin)
    if qmax:
        m &= q <= float(qmax)
    return q[m], I[m], sigma[m]


def _model_qc(res: dict) -> dict:
    warns = []
    if not res.get("converged", True):
        warns.append("Optimiser did not converge — adjust guesses/bounds.")
    if res.get("at_bounds"):
        warns.append("Parameter(s) pinned at a bound: " + ", ".join(res["at_bounds"]))
    chi2 = res.get("chi2")
    if isinstance(chi2, (int, float)) and chi2 > 5:
        warns.append(f"High reduced χ² = {chi2} — fit may be poor.")
    return {"verdict": "PASS" if not warns else "WARN", "warnings": warns}


def _run_model(path: Path, body: dict) -> dict:
    q, I, sigma = _load(path)
    q, I, sigma = _trim_q(q, I, sigma, body)
    model = body.get("model_name", "sphere")
    sf    = (body.get("structure_factor") or "").strip()
    if sf and "@" not in model:
        model = f"{model}@{sf}"
    res = sasmodels_fit(q, I, sigma, model, body.get("params", {}),
                        free=body.get("free"), bounds=body.get("bounds"),
                        q_unit=body.get("q_unit", "nm^-1"))
    return res


@app.route("/api/sasview", methods=["POST"])
def api_sasview():
    """Fit one curve with a sasmodels model; save to Analysed/Model/."""
    body = request.get_json(force=True) or {}
    path = Path(body.get("path", "").strip())
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        res = _run_model(path, body)
        if "error" in res:
            return jsonify(res), 200
        qc = _model_qc(res)
        p = res.get("plot", {})
        fit_curve = (p.get("q_fit"), p.get("I_fit"))
        saved = None
        if body.get("save", False):
            saved = analysis_io.save_analysis(
                _project_root or None, path, _detector_of(path), "model",
                params={"model": res.get("model"), "free": body.get("free"),
                        "bounds": body.get("bounds")},
                results={**{k: v for k, v in res.items() if k != "plot"},
                         **{f"p_{k}": v for k, v in res.get("params", {}).items()}},
                fit_curve=fit_curve, user=body.get("user", ""))
        if _bus is not None:
            try:
                _bus.emit_analysis_complete("model", str(path), res)
            except Exception:
                pass
        return jsonify({"results": res, "qc": qc, "saved": saved})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/sasview/batch", methods=["POST"])
def api_sasview_batch():
    """Fit the same model over all Subtracted files matching a keyword."""
    body = request.get_json(force=True) or {}
    keyword  = (body.get("keyword") or "").lower()
    detector = (body.get("detector") or "SAXS").lower()
    sub = _subtracted_dir(detector)
    if sub is None:
        return jsonify({"error": "No Subtracted folder for this detector/project."}), 400
    files = [p for p in sorted(sub.glob("*.dat"))
             if not keyword or keyword in p.name.lower()]
    if not files:
        return jsonify({"error": f"No subtracted {detector.upper()} files match '{keyword}'."}), 200
    rows, errors = [], []
    for p in files:
        try:
            res = _run_model(p, body)
            if "error" in res:
                errors.append({"file": p.name, "error": res["error"]}); continue
            pl = res.get("plot", {})
            analysis_io.save_analysis(
                _project_root, p, detector, "model",
                params={"model": res.get("model")},
                results={**{k: v for k, v in res.items() if k != "plot"},
                         **{f"p_{k}": v for k, v in res.get("params", {}).items()}},
                fit_curve=(pl.get("q_fit"), pl.get("I_fit")), user=body.get("user", ""))
            row = {"file": p.name, "model": res.get("model"), "chi2": res.get("chi2")}
            row.update({f"p_{k}": v for k, v in res.get("params", {}).items()})
            rows.append(row)
        except Exception as exc:
            errors.append({"file": p.name, "error": str(exc)})
    summary = None
    if rows:
        out_dir = Path(_project_root) / "1D" / detector.upper() / "Analysed" / "Model"
        summary = analysis_io.write_batch_summary(out_dir, "model", rows)
    return jsonify({"detector": detector.upper(), "n_ok": len(rows),
                    "n_error": len(errors), "rows": rows, "errors": errors,
                    "summary": summary})


@app.route("/api/sasview/compare", methods=["POST"])
def api_sasview_compare():
    """Fit several candidate models to one curve; rank by reduced χ²."""
    body = request.get_json(force=True) or {}
    path = Path(body.get("path", "").strip())
    models = body.get("models") or []
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    if not models:
        return jsonify({"error": "Provide a list of models to compare."}), 400
    try:
        q, I, sigma = _load(path)
        q, I, sigma = _trim_q(q, I, sigma, body)
        out = []
        for mn in models:
            r = sasmodels_fit(q, I, sigma, mn, body.get("params", {}).get(mn, {}),
                              free=body.get("free", {}).get(mn),
                              q_unit=body.get("q_unit", "nm^-1"))
            out.append({"model": mn, "chi2": r.get("chi2"),
                        "error": r.get("error"), "params": r.get("params")})
        out.sort(key=lambda x: (x["chi2"] is None, x["chi2"] if x["chi2"] is not None else 1e30))
        return jsonify({"ranking": out})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── ATSAS ─────────────────────────────────────────────────────────────────────

@app.route("/api/atsas/available")
def api_atsas_available():
    """Which ATSAS binaries are on PATH (to enable/disable UI tools)."""
    av = atsas_mod.available()
    return jsonify({"tools": {k: bool(v) for k, v in av.items()},
                    "any": any(av.values())})


def _run_atsas(path: Path, tool: str, body: dict) -> dict:
    """Dispatch one ATSAS tool (chaining datgnom where needed)."""
    if tool == "autorg":
        return atsas_mod.run_autorg(path)
    if tool == "datvc":
        return atsas_mod.run_datvc(path)
    if tool == "datmw":
        return atsas_mod.run_datmw(path, method=body.get("method", "vc"))
    if tool in ("gnom", "datgnom"):
        return atsas_mod.run_datgnom(path, rg=body.get("rg"))
    if tool == "datporod":
        g = atsas_mod.run_datgnom(path, rg=body.get("rg"))
        if "error" in g:
            return g
        r = atsas_mod.run_datporod(g["out_file"])
        if "error" not in r:
            r.update({"Dmax": g.get("Dmax"), "Rg_real": g.get("Rg_real")})
        return r
    if tool == "dammif":
        g = atsas_mod.run_datgnom(path, rg=body.get("rg"))
        if "error" in g:
            return g
        out_dir = Path(_project_root or tempfile_gettmp()) / "1D" / \
            _detector_of(path).upper() / "Analysed" / "ATSAS" / (Path(path).stem + "_dammif")
        return atsas_mod.run_dammif(g["out_file"], out_dir, mode=body.get("mode", "fast"))
    return {"error": f"Unknown ATSAS tool '{tool}'."}


def tempfile_gettmp():
    import tempfile
    return tempfile.gettempdir()


@app.route("/api/atsas", methods=["POST"])
def api_atsas():
    """Run an ATSAS tool on one curve; save results to Analysed/ATSAS/."""
    body = request.get_json(force=True) or {}
    path = Path(body.get("path", "").strip())
    tool = (body.get("tool") or "autorg").lower()
    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    try:
        res = _run_atsas(path, tool, body)
        if "error" in res:
            return jsonify(res), 200
        # build a p(r) plot for GNOM-type results
        plot = None
        if res.get("r") and res.get("pr"):
            plot = {"r": res["r"], "pr": res["pr"], "Dmax": res.get("Dmax")}
        saved = None
        if body.get("save", False) and tool != "dammif":
            scal = {k: v for k, v in res.items()
                    if isinstance(v, (int, float, str)) and k not in ("raw",)}
            fit = (res.get("r"), res.get("pr")) if res.get("r") else None
            saved = analysis_io.save_analysis(
                _project_root, path, _detector_of(path), "atsas",
                params={"tool": tool}, results={**scal, "atsas_tool": tool},
                fit_curve=fit, user=body.get("user", ""))
        return jsonify({"results": res, "plot": plot, "saved": saved})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/atsas/batch", methods=["POST"])
def api_atsas_batch():
    """Run an ATSAS tool over all Subtracted files matching a keyword."""
    body = request.get_json(force=True) or {}
    tool     = (body.get("tool") or "autorg").lower()
    keyword  = (body.get("keyword") or "").lower()
    detector = (body.get("detector") or "SAXS").lower()
    if tool == "dammif":
        return jsonify({"error": "dammif is too slow for batch — run it individually."}), 400
    sub = _subtracted_dir(detector)
    if sub is None:
        return jsonify({"error": "No Subtracted folder for this detector/project."}), 400
    files = [p for p in sorted(sub.glob("*.dat")) if not keyword or keyword in p.name.lower()]
    if not files:
        return jsonify({"error": f"No subtracted {detector.upper()} files match '{keyword}'."}), 200
    rows, errors = [], []
    for p in files:
        try:
            res = _run_atsas(p, tool, body)
            if "error" in res:
                errors.append({"file": p.name, "error": res["error"]}); continue
            scal = {k: v for k, v in res.items()
                    if isinstance(v, (int, float)) and k != "raw"}
            analysis_io.save_analysis(_project_root, p, detector, "atsas",
                                      params={"tool": tool},
                                      results={**scal, "atsas_tool": tool},
                                      fit_curve=((res.get("r"), res.get("pr")) if res.get("r") else None),
                                      user=body.get("user", ""))
            rows.append({"file": p.name, "tool": tool, **scal})
        except Exception as exc:
            errors.append({"file": p.name, "error": str(exc)})
    summary = None
    if rows:
        out_dir = Path(_project_root) / "1D" / detector.upper() / "Analysed" / "ATSAS"
        summary = analysis_io.write_batch_summary(out_dir, "atsas_" + tool, rows)
    return jsonify({"detector": detector.upper(), "n_ok": len(rows),
                    "n_error": len(errors), "rows": rows, "errors": errors,
                    "summary": summary})


@app.route("/api/sasmodels/list")
def api_sasmodels_list():
    """Return list of available sasmodels model names."""
    try:
        import sasmodels.core as sm_core
        models = sorted(sm_core.list_models())
        return jsonify({"models": models})
    except ImportError:
        return jsonify({"models": [], "warning": "sasmodels not installed"})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    _project_root = os.environ.get("SWAXS_PROJECT", "")
    print("━" * 48)
    print("  SWAXS Data Analysis App")
    print("  → http://localhost:5004")
    print("━" * 48)
    app.run(debug=False, port=5004, threaded=True)
