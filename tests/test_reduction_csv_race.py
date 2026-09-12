"""
tests/test_reduction_csv_race.py — reduction/app.py CSV-arrival race fix.

The metadata CSV for an acquisition is written only when the acquisition
COMPLETES, but .raw frames land throughout it. Before this fix, a frame with
no matching CSV yet burned its whole 3-strike retry budget in the first
30 s of a run that can legitimately take minutes, permanently discarding
early frames (see the bug report for the observed Run9_r003_sample /
Run9_r004_bkg losses).

These tests drive reduction/app.py::_process_one_raw directly — the unit of
work _loop() calls per file — with a stub Experiment, so no Flask app, no
thread, and no real acquisition needs to run.
"""
from __future__ import annotations

import importlib.util as u
import os
import sys
from pathlib import Path

import pytest

from src.reduction.csv_wait import QUIET_POLLS

ROOT = Path(__file__).resolve().parents[1]


def _load(tag, tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")
    spec = u.spec_from_file_location(tag, str(ROOT / "reduction" / "app.py"))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


class _FakeExperiment:
    """Stub standing in for src.reduction.core.Experiment: no PyFAI, no PONI,
    just enough surface for _process_one_raw / _register_reduced to run."""

    def __init__(self, tmp_path, fail_with=None):
        self.output_dir_1d = tmp_path / "1D"
        self.data_directory = tmp_path / "2D"
        self._fail_with = fail_with
        self.calls: list[Path] = []

    def _result_for(self, f: Path) -> dict:
        return {
            "filename": f.stem + "_SAXS.dat",
            "corrections": {
                "i0_corrected": 1.0, "bstop_corrected": 1.0,
                "transmission": 1.0, "thickness_m": 0.001,
            },
            "ctemp": None,
        }

    def process_saxs_file(self, f: Path) -> dict:
        self.calls.append(f)
        if self._fail_with is not None:
            raise self._fail_with
        return self._result_for(f)

    process_waxs_file = process_saxs_file

    def set_fail(self, exc) -> None:
        self._fail_with = exc


def _frame(tmp_path, kw: str, idx: int, mtime: float) -> Path:
    p = tmp_path / f"{kw}_{idx:04d}.raw"
    p.write_bytes(b"x")
    os.utime(p, (mtime, mtime))
    return p


# ── Scenario 1: CSV absent while frames keep arriving, then appears ──────────
def test_csv_absent_frames_still_arriving_all_reduce_zero_skips(tmp_path, monkeypatch):
    m = _load("red_csvrace_1", tmp_path, monkeypatch)
    kw = "Run9_r003_sample_scan1"
    interval = 10.0
    t0 = 1_000_000.0

    exp = _FakeExperiment(tmp_path, fail_with=m.CSVMetadataNotFound("no csv yet"))
    fake_now = {"t": t0}
    monkeypatch.setattr(m.time, "time", lambda: fake_now["t"])

    frames = []
    # 10 polls, one new frame per poll (~100 s of acquisition), CSV still missing.
    for i in range(10):
        fake_now["t"] = t0 + i * interval
        frames.append(_frame(tmp_path, kw, i, fake_now["t"]))
        m._note_prefix_activity(list(frames))
        for f in frames:
            if str(f) not in m._processed_files:
                m._process_one_raw(f, "saxs", exp, {}, "tester", interval)

    assert m._fail_counts == {}, "no missing-CSV attempt should have counted as a failure"
    assert m._processed_files == set()

    # The acquisition finishes: the CSV is now available, all 10 frames reduce.
    fake_now["t"] = t0 + 10 * interval
    exp.set_fail(None)
    for f in frames:
        m._process_one_raw(f, "saxs", exp, {}, "tester", interval)

    assert m._processed_files == {str(f) for f in frames}
    assert m._fail_counts == {}


# ── Scenario 2: CSV never appears, prefix goes quiet → 3 strikes, then skip ──
def test_csv_never_appears_and_prefix_goes_quiet_skips_after_3_strikes(tmp_path, monkeypatch):
    m = _load("red_csvrace_2", tmp_path, monkeypatch)
    kw = "Run9_r004_bkg_scan1"
    interval = 10.0
    t0 = 2_000_000.0

    exp = _FakeExperiment(tmp_path, fail_with=m.CSVMetadataNotFound("no csv yet"))
    fake_now = {"t": t0}
    monkeypatch.setattr(m.time, "time", lambda: fake_now["t"])

    skip_events = []
    monkeypatch.setattr(
        m._bus, "emit_file_skipped",
        lambda *a, **k: skip_events.append((a, k)),
    )

    frame = _frame(tmp_path, kw, 0, t0)
    m._note_prefix_activity([frame])

    # First attempt, right when the frame lands: prefix is fresh — must wait.
    m._process_one_raw(frame, "saxs", exp, {}, "tester", interval)
    assert m._fail_counts == {}
    assert skip_events == []

    # The prefix goes quiet — no further .raw ever arrives for it. Once the
    # quiet window has elapsed, a missing CSV becomes a real failure, subject
    # to the existing 3-strike budget.
    quiet_at = t0 + QUIET_POLLS * interval + 1

    fake_now["t"] = quiet_at
    m._process_one_raw(frame, "saxs", exp, {}, "tester", interval)
    assert m._fail_counts[str(frame)] == 1
    assert skip_events == []

    fake_now["t"] = quiet_at + interval
    m._process_one_raw(frame, "saxs", exp, {}, "tester", interval)
    assert m._fail_counts[str(frame)] == 2
    assert skip_events == []

    fake_now["t"] = quiet_at + 2 * interval
    m._process_one_raw(frame, "saxs", exp, {}, "tester", interval)
    assert m._fail_counts[str(frame)] == 3
    assert len(skip_events) == 1, "the permanent-skip event should fire exactly once"
    args, kwargs = skip_events[0]
    assert args[0] == str(frame)
    assert kwargs["keyword"] == kw
    assert kwargs["n_failures"] == 3


# ── Scenario 3: a genuinely corrupt frame still fails after exactly 3 tries ──
def test_genuinely_corrupt_frame_is_still_skipped_after_3_tries(tmp_path, monkeypatch):
    m = _load("red_csvrace_3", tmp_path, monkeypatch)
    kw = "Run9_r005_sample_scan1"
    interval = 10.0
    t0 = 3_000_000.0

    exp = _FakeExperiment(tmp_path, fail_with=ValueError("corrupt frame: bad header"))
    fake_now = {"t": t0}
    monkeypatch.setattr(m.time, "time", lambda: fake_now["t"])

    skip_events = []
    monkeypatch.setattr(
        m._bus, "emit_file_skipped",
        lambda *a, **k: skip_events.append((a, k)),
    )

    frame = _frame(tmp_path, kw, 0, t0)

    # The prefix stays "active" throughout (new frames keep landing for it) —
    # proving a real failure is NOT exempted by the CSV-wait logic, unlike a
    # missing CSV.
    for i in range(1, 4):
        fake_now["t"] = t0 + i * interval
        sibling = _frame(tmp_path, kw, i, fake_now["t"])
        m._note_prefix_activity([sibling])
        m._process_one_raw(frame, "saxs", exp, {}, "tester", interval)
        assert m._fail_counts[str(frame)] == i

    assert m._fail_counts[str(frame)] == 3
    assert len(skip_events) == 1
    assert str(frame) not in m._processed_files
