"""Regression tests for the Quality Gate audit fixes.

1. Detector inference: "waxs" in str(path).lower() matched any path containing
   "SWAXS", so SAXS profiles in a SWAXS_data project were graded with WAXS
   thresholds (and then memoised, so never re-graded).
2. _recount() iterated _results while the event-bus thread inserted into it, which
   could raise "dictionary changed size during iteration" and kill the grader.
3. Quality reports were always written under 1D/SAXS/, even for WAXS profiles.
"""
from __future__ import annotations

import importlib.util as u
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quality import detector_of  # noqa: E402


def _load(tag: str, tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")
    spec = u.spec_from_file_location(tag, str(ROOT / "quality" / "app.py"))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


# ── 1. detector inference ────────────────────────────────────────────────────
@pytest.mark.parametrize("path,expected", [
    ("/x/SWAXS_data/1D/SAXS/Subtracted/Run1_r001_sample_sub.dat", "saxs"),
    ("/x/SWAXS_data/1D/WAXS/Subtracted/Run1_r001_sample_sub.dat", "waxs"),
    ("/x/SWAXS_data/1D/SAXS/Subtracted/Good/Run1_sample_sub.dat", "saxs"),
    ("/x/swaxs/Run1_sample_WAXS.dat", "waxs"),
    ("/x/swaxs/Run1_sample_waxs_sub.dat", "waxs"),
    ("/x/1D/WAXS/Subtracted/Run1_sample_SAXS.dat", "saxs"),   # filename wins
    ("/x/SWAXS/Run1_SWAXS_sample.dat", "saxs"),               # no exact token
    ("C:\\data\\SWAXS_run\\1D\\WAXS\\Subtracted\\a_sub.dat", "waxs"),
    ("relative/a.dat", "saxs"),
])
def test_detector_of(path, expected):
    assert detector_of(path) == expected


def test_app_no_longer_uses_substring_detector_guess():
    src = (ROOT / "quality" / "app.py").read_text(encoding="utf-8")
    assert '"waxs" in str(' not in src
    assert "detector_of(p)" in src


def test_bus_event_grades_swaxs_project_saxs_file_as_saxs(tmp_path, monkeypatch):
    q = _load("ql_fix_bus", tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(q, "_grade_and_record", lambda p, det: seen.append(det))
    monkeypatch.setattr(q, "_grading", True)
    q._on_bus_event({"type": "file.subtracted", "data": {
        "file_path": "/x/SWAXS_data/1D/SAXS/Subtracted/Run1_r001_sample_sub.dat"}})
    assert seen == ["saxs"]


# ── 2. _results locking ──────────────────────────────────────────────────────
def test_recount_survives_concurrent_insertion(tmp_path, monkeypatch):
    q = _load("ql_fix_lock", tmp_path, monkeypatch)
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            with q._results_lock:
                q._results[f"/p/{i}.dat"] = {"verdict": "good" if i % 2 else "bad"}
            i += 1
            if i % 500 == 0:
                with q._results_lock:
                    q._results.clear()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        end = time.time() + 1.0
        while time.time() < end:
            try:
                q._recount()
                q._results_snapshot()
            except RuntimeError as exc:      # "dictionary changed size ..."
                errors.append(exc)
                break
    finally:
        stop.set()
        t.join(2)
    assert not errors, errors


def test_results_insertion_and_iteration_are_locked():
    src = (ROOT / "quality" / "app.py").read_text(encoding="utf-8")
    assert "_results.values()" not in src.replace(
        "return list(_results.values())", ""), "unlocked iteration of _results"
    gr = src[src.index("def _grade_and_record"):]
    gr = gr[:gr.index("\ndef ", 5)]
    assert "with _results_lock:" in gr


def test_grader_loop_survives_a_failing_cycle(tmp_path, monkeypatch):
    q = _load("ql_fix_loop", tmp_path, monkeypatch)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] >= 3:
            q._grading = False
        raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(q, "_recount", boom)
    q._grading = True
    th = threading.Thread(target=q._grader_loop, args=([], 0.01), daemon=True)
    th.start()
    th.join(5)
    assert not th.is_alive()
    assert calls["n"] >= 3, "the loop died on the first exception"
    msgs = [e["msg"] for _, e in list(q._log)]
    assert any("grading cycle failed" in m for m in msgs)


# ── 3. report routing ────────────────────────────────────────────────────────
def _rec(path, det, verdict="good"):
    return {"name": Path(path).name, "path": path, "detector": det, "sample": "s",
            "score": 80.0 if verdict == "good" else 20.0, "verdict": verdict,
            "flags": [], "reasons": [], "metrics": {}}


def test_reports_are_routed_by_detector(tmp_path, monkeypatch):
    q = _load("ql_fix_report", tmp_path, monkeypatch)
    c = q.app.test_client()
    c.post("/api/set_project", json={"path": str(tmp_path)})
    q._results["/a/1D/SAXS/Subtracted/s1_sub.dat"] = _rec("/a/1D/SAXS/Subtracted/s1_sub.dat", "saxs")
    q._results["/a/1D/WAXS/Subtracted/w1_sub.dat"] = _rec("/a/1D/WAXS/Subtracted/w1_sub.dat", "waxs")
    body = c.get("/api/report").get_json()
    saxs_dir = tmp_path / "1D" / "SAXS" / "Results" / "QualityReports"
    waxs_dir = tmp_path / "1D" / "WAXS" / "Results" / "QualityReports"
    s_csv = next(saxs_dir.glob("quality_report_*.csv")).read_text()
    w_csv = next(waxs_dir.glob("quality_report_*.csv")).read_text()
    assert "s1_sub.dat" in s_csv and "w1_sub.dat" not in s_csv
    assert "w1_sub.dat" in w_csv and "s1_sub.dat" not in w_csv
    assert set(body["saved_dirs"]) == {"saxs", "waxs"}


def test_waxs_only_report_does_not_touch_saxs_tree(tmp_path, monkeypatch):
    q = _load("ql_fix_report_w", tmp_path, monkeypatch)
    c = q.app.test_client()
    c.post("/api/set_project", json={"path": str(tmp_path)})
    q._results["/a/1D/WAXS/Subtracted/w1_sub.dat"] = _rec("/a/1D/WAXS/Subtracted/w1_sub.dat", "waxs")
    c.get("/api/report")
    assert list((tmp_path / "1D" / "WAXS" / "Results" / "QualityReports").glob("*.csv"))
    assert not (tmp_path / "1D" / "SAXS" / "Results" / "QualityReports").exists()
