"""
tests/test_quick_audit_bg_analysis_quality.py

Platform quick audit (2026-10), Background / Analysis / Quality items:
Q7 (q unit), Q8 (per-detector truncation), Q11 (Stop/Start runs one loop),
Q14 (no rewrite of the source .dat), Q15 (Guinier auto range converges),
Q16 (stale LLM verdict cleared), Q30 (quality flags replaced), Q31 (resume keeps
the saved threshold), Q32 (auto scale warning), plus the analysis detector guess.
"""
from __future__ import annotations

import importlib.util as u
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_app(app: str, tag: str, tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")
    spec = u.spec_from_file_location(tag, str(ROOT / app / "app.py"))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


def _sphere(q, R=5.0):
    x = q * R
    return (3 * (np.sin(x) - x * np.cos(x)) / x ** 3) ** 2 * 1000.0 + 1e-3


def _write(path: Path, label: str, q, I, sig=None):
    sig = 0.01 * I if sig is None else sig
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# test profile", f"# Columns: {label}  I  sigma", f"# {label}  I  sigma"]
    lines += [f"{a:.8e}  {b:.8e}  {c:.8e}" for a, b, c in zip(q, I, sig)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── Q7 q unit ─────────────────────────────────────────────────────────────────
def test_q_unit_helpers():
    from src.utils.read_dat_metadata import q_unit_from_header, q_to_nm
    assert q_unit_from_header(["# Columns: q_A-1  I  sigma", "# q_A-1  I  sigma"]) == "A^-1"
    assert q_unit_from_header(["# q_nm-1  I  sigma"]) == "nm^-1"
    assert q_unit_from_header(["# nothing here"]) is None
    q, unit = q_to_nm([0.01, 0.1], ["# q_A-1 I sigma"])
    assert unit == "A^-1" and np.allclose(q, [0.1, 1.0])
    q, unit = q_to_nm([0.01, 0.1], ["# no label"], default="A^-1")
    assert np.allclose(q, [0.1, 1.0])


def test_analysis_load_converts_angstrom_and_sasview_not_twice(tmp_path, monkeypatch):
    m = _load_app("analysis", "qa_an_q7", tmp_path, monkeypatch)
    q_nm = np.linspace(0.1, 3.0, 200)
    I = _sphere(q_nm)
    fa = _write(tmp_path / "a_sub.dat", "q_A-1", q_nm / 10.0, I)
    fn = _write(tmp_path / "n_sub.dat", "q_nm-1", q_nm, I)
    qa, Ia, _ = m._load(fa)
    qn, In, _ = m._load(fn)
    assert np.allclose(qa, qn) and np.allclose(Ia, In)
    # Guinier Rg identical whatever unit the file was written in
    ga = m.guinier_fit(*m._load(fa)); gn = m.guinier_fit(*m._load(fn))
    assert ga["Rg"] == pytest.approx(gn["Rg"], rel=1e-6)

    seen = {}

    def fake_fit(q, I, s, model, params, free=None, bounds=None, q_unit="nm^-1"):
        seen["q"] = np.asarray(q); seen["unit"] = q_unit
        return {"model": model, "params": {}, "chi2": 1.0}

    monkeypatch.setattr(m, "sasmodels_fit", fake_fit)
    m._run_model(fa, {"model_name": "sphere", "q_unit": "A^-1"})   # UI said Å⁻¹
    assert seen["unit"] == "nm^-1"
    assert seen["q"].max() == pytest.approx(3.0, rel=1e-6)        # nm⁻¹, converted once


def test_fit_dat_header_names_columns(tmp_path):
    from src.analysis import io
    src = _write(tmp_path / "1D" / "SAXS" / "Subtracted" / "s_sub.dat", "q_A-1",
                 np.linspace(0.01, 0.3, 50), np.ones(50))
    out = io.save_analysis(None, src, "saxs", "atsas", {}, {"Dmax": 10.0},
                           fit_curve=([1, 2, 3], [0.1, 0.2, 0.1]), register=False)
    assert "# r_A  P(r)" in Path(out["dat"]).read_text()
    out = io.save_analysis(None, src, "saxs", "guinier", {}, {"Rg": 1.0},
                           fit_curve=([0.1, 0.2], [1, 2]), register=False)
    assert "# q_nm-1  I" in Path(out["dat"]).read_text()


def test_analysis_detector_of_windows_paths(tmp_path, monkeypatch):
    m = _load_app("analysis", "qa_an_det", tmp_path, monkeypatch)
    assert m._detector_of("C:\\data\\run\\1D\\WAXS\\Subtracted\\a_sub.dat") == "waxs"
    assert m._detector_of("C:\\data\\SWAXS_run\\1D\\SAXS\\Subtracted\\a_sub.dat") == "saxs"
    assert m._detector_of("/x/1D/WAXS/Subtracted/a_sub.dat") == "waxs"


# ── Q8 per-detector truncation ───────────────────────────────────────────────
def test_truncation_is_per_detector_and_waxs_off_by_default(tmp_path, monkeypatch):
    bg = _load_app("background", "qa_bg_q8", tmp_path, monkeypatch)
    assert bg._TRUNC["enabled"] is True and bg._TRUNC_WAXS["enabled"] is False
    q = np.linspace(2.0, 40.0, 1500); I = 10.0 / q; s = 0.01 * I
    qs, *_ = bg._write_dat(tmp_path / "s.dat", q, I, s, det="saxs")
    qw, *_ = bg._write_dat(tmp_path / "w.dat", q, I, s, det="waxs")
    assert len(qs) == 549 and qs.max() <= 6.0 + 1e-9
    assert len(qw) == len(q) and qw.max() == pytest.approx(40.0)
    assert "Truncated" not in (tmp_path / "w.dat").read_text()

    c = bg.app.test_client()
    r = c.post("/api/truncation", json={"detector": "waxs", "enabled": True,
                                        "q_min": 10, "q_max": 30, "n_points": 100})
    j = r.get_json()
    assert j["detector"] == "waxs" and j["enabled"] is True and j["n_points"] == 100
    assert bg._TRUNC["n_points"] == 549, "a WAXS change leaked into the SAXS setting"
    assert c.get("/api/truncation").get_json()["detector"] == "saxs"
    assert c.get("/api/truncation?detector=waxs").get_json()["q_min"] == 10
    qw2, *_ = bg._write_dat(tmp_path / "w2.dat", q, I, s, det="waxs")
    assert len(qw2) == 100 and qw2.min() == pytest.approx(10.0)


def test_auto_scale_reports_too_few_points():
    from src.background.core import _auto_scale
    q = np.linspace(0.1, 1.0, 50); I = np.ones_like(q)
    r = _auto_scale(q, I, I * 0.1, q, I, I * 0.1, qmin=5.0, qmax=6.0)
    assert r["scale"] == 1.0 and r["n_points"] < 3 and "warning" in r


# ── Q11 Stop then quick Start runs one loop ──────────────────────────────────
def test_stop_then_quick_start_never_runs_two_loops(tmp_path, monkeypatch):
    bg = _load_app("background", "qa_bg_q11", tmp_path, monkeypatch)
    avg = tmp_path / "1D" / "SAXS" / "Averaged"; avg.mkdir(parents=True)
    c = bg.app.test_client()
    body = {"interval": 30, "saxs_avg_folder": str(avg)}
    try:
        assert c.post("/api/monitor/start", json=body).get_json()["ok"]
        t1 = bg._sub_monitor_thread
        c.post("/api/monitor/stop")
        r = c.post("/api/monitor/start", json=body).get_json()   # within the 30 s sleep
        assert r["ok"], r
        t2 = bg._sub_monitor_thread
        assert t2 is not t1
        assert not t1.is_alive(), "the old loop is still running next to the new one"
        assert t2.is_alive() and bg._sub_monitoring
    finally:
        c.post("/api/monitor/stop")
        if bg._sub_monitor_thread is not None:
            bg._sub_monitor_thread.join(timeout=5)
    assert not bg._sub_monitor_thread.is_alive()


# ── Q14 no rewrite of the source .dat ────────────────────────────────────────
def test_save_analysis_leaves_source_dat_untouched(tmp_path):
    from src.analysis import io
    src = _write(tmp_path / "1D" / "SAXS" / "Subtracted" / "s_sub.dat", "q_nm-1",
                 np.linspace(0.1, 3, 60), np.linspace(10, 1, 60))
    before = (src.read_bytes(), src.stat().st_mtime_ns)
    out = io.save_analysis(None, src, "saxs", "guinier", {}, {"Rg": 2.5}, register=False)
    assert (src.read_bytes(), src.stat().st_mtime_ns) == before
    side = Path(out["annotation"])
    assert side.parent == tmp_path / "1D" / "SAXS" / "Analysed"
    assert not side.name.endswith(".dat")
    assert io.read_annotations(src) == {"guinier.Rg": "2.5"}


# ── Q15 Guinier auto range ───────────────────────────────────────────────────
def test_guinier_auto_range_converges_to_limit():
    from src.analysis.core import guinier_fit
    q = np.linspace(0.02, 3.0, 600)
    I = _sphere(q, R=5.0)                       # Rg = sqrt(3/5)·R ≈ 3.873 nm
    res = guinier_fit(q, I, 0.01 * I, auto_range=True)
    assert res["converged"] is True
    assert res["qRg_max"] <= 1.3 + 1e-6
    assert res["Rg"] == pytest.approx(np.sqrt(3 / 5) * 5.0, rel=0.03)


def test_guinier_not_converged_is_flagged_and_not_batch_saved(tmp_path, monkeypatch):
    from src.analysis.core import guinier_fit, guinier_quality
    q = np.linspace(0.5, 3.0, 40)               # q·Rg > 1.3 everywhere: no Guinier region
    I = np.exp(-(4.0 ** 2) * q ** 2 / 3) + 1e-6 * q ** -4
    res = guinier_fit(q, I, 0.01 * I, auto_range=True)
    if "error" not in res:
        assert res["converged"] is False
        assert guinier_quality(res)["verdict"] == "FAIL"

    m = _load_app("analysis", "qa_an_q15", tmp_path, monkeypatch)
    m._project_root = str(tmp_path)
    sub = tmp_path / "1D" / "SAXS" / "Subtracted"
    _write(sub / "bad_sub.dat", "q_nm-1", q, I)
    monkeypatch.setattr(m, "guinier_fit", lambda *a, **k: {
        "Rg": 9.0, "I0": 1.0, "R2": 0.99, "q_range": [0.5, 3.0], "qRg_max": 27.0,
        "converged": False})
    j = m.app.test_client().post("/api/classical/batch",
                                 json={"analysis": "guinier", "detector": "SAXS"}).get_json()
    assert j["n_ok"] == 0 and j["errors"] and j["errors"][0]["converged"] is False
    assert not (tmp_path / "1D" / "SAXS" / "Analysed" / "Guinier" /
                "bad_sub_guinier.json").exists()


# ── Q16 stale LLM verdict ────────────────────────────────────────────────────
def test_llm_verdict_cleared_when_out_of_band(tmp_path, monkeypatch):
    m = _load_app("quality", "qa_q_q16", tmp_path, monkeypatch)
    m._params.clear(); m._params.update({"score_pass": 60.0, "borderline": 10.0})
    p = str(tmp_path / "x_sub.dat")
    rec = {"path": p, "name": "x_sub.dat", "score": 55.0, "llm_verdict": "good",
           "llm_note": "fine", "verdict": "good", "metrics": {}, "flags": [], "reasons": []}
    m._results.clear(); m._results[p] = rec
    m._recolor()
    assert rec["verdict"] == "good" and rec["llm_verdict"] == "good"   # still borderline
    m._params["score_pass"] = 80.0                                      # 55 is now far below
    m._recolor()
    assert "llm_verdict" not in rec and rec["llm_note"] is None
    assert rec["verdict"] == "bad"


def test_resume_keeps_saved_threshold(tmp_path, monkeypatch):
    m = _load_app("quality", "qa_q_q31", tmp_path, monkeypatch)
    sub = tmp_path / "1D" / "SAXS" / "Subtracted"; sub.mkdir(parents=True)
    m._params["score_pass"] = 72.0
    c = m.app.test_client()
    try:
        c.post("/api/monitor/start", json={"interval": 30, "saxs_folder": str(sub),
                                           "threshold": 40, "resume": True})
        assert m._pass_threshold() == 72.0
    finally:
        c.post("/api/monitor/stop")


# ── Q30 quality flags replaced on re-grade ───────────────────────────────────
def test_quality_flags_replaced_not_merged(tmp_path):
    from src.manifest import add_quality_entry
    p = tmp_path / "x_sub.dat"; p.write_text("1 1 1\n")
    key = str(p.resolve())
    m = {"files": {key: {"quality_flags": []}}}
    add_quality_entry(m, path=p, score=30, verdict="bad", flags=["over_subtracted", "low_snr"])
    m["files"][key]["quality_flags"].append("possible_aggregation")      # e.g. Assistant
    m["ai_memory"]["quality_flags"][key].append("possible_aggregation")
    add_quality_entry(m, path=p, score=80, verdict="good", flags=["low_snr"])
    assert m["files"][key]["quality_flags"] == ["possible_aggregation", "low_snr"]
    assert m["ai_memory"]["quality_flags"][key] == ["possible_aggregation", "low_snr"]
    add_quality_entry(m, path=p, score=95, verdict="good", flags=[])
    assert m["files"][key]["quality_flags"] == ["possible_aggregation"]
