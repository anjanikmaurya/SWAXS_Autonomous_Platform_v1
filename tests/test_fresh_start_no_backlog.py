"""
tests/test_fresh_start_no_backlog.py

One bug, four apps. Reported twice from the beamline: pressing Start on a
project folder that already held data made reduce, average, subtract AND the
auto-fit app work through the ENTIRE folder — every file of every previous run,
oldest first — before touching the frames of the run just started. Each stage
is one worker thread, so the live data sat behind the whole back-catalogue and
the loop looked stalled.

Each app forgot for its own reason:

  average     monitor_start set `_avg_batch_state = {}` unless resume=True,
              and `n` resetting to 0 ALSO made it rewrite batch001 over the
              existing one — lossy, not just slow
  background  monitor_start set `_sub_done = {}` on the same condition
  analyzer    cleared `_handled` on abort / folder change without reseeding
              (covered by tests/test_analyzer_fresh_run_backlog.py)
  reduction   survived on `_already_reduced`'s disk check — except with a
              filename prefix configured, where the glob matched nothing and
              the check silently always returned False

The shared rule now lives in src/backlog.py; these tests pin it per app.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")

from src.backlog import partition_backlog, CRASH_GAP_WINDOW_S     # noqa: E402


# ── the shared rule ─────────────────────────────────────────────────────────
def test_an_input_with_an_output_is_done():
    done, todo = partition_backlog([("a", 0.0)], has_output=lambda k: True, now=0.0)
    assert (done, todo) == (["a"], [])


def test_an_old_input_with_no_output_is_history_not_work():
    now = 1_000_000.0
    done, todo = partition_backlog([("old", now - 7200)],
                                   has_output=lambda k: False, now=now)
    assert (done, todo) == (["old"], []), \
        "a new run must not work through the back-catalogue to reach live data"


def test_a_recent_input_with_no_output_is_left_for_processing():
    now = 1_000_000.0
    done, todo = partition_backlog([("fresh", now - 5)],
                                   has_output=lambda k: False, now=now)
    assert (done, todo) == ([], ["fresh"]), \
        "a file that landed just before a crash was never processed — keep it"


def test_the_window_boundary_and_order_are_stable():
    now = 1_000_000.0
    cands = [("a", now - CRASH_GAP_WINDOW_S - 1), ("b", now - 1),
             ("c", now - CRASH_GAP_WINDOW_S), ("d", now - 2)]
    done, todo = partition_backlog(cands, has_output=lambda k: False, now=now)
    assert done == ["a", "c"] and todo == ["b", "d"], "input order must be preserved"


def test_an_unusable_mtime_is_treated_as_recent_not_dropped():
    done, todo = partition_backlog([("x", None)], has_output=lambda k: False, now=0.0)
    assert todo == ["x"], "when in doubt, process it — never silently skip data"


# ── average: batch state is rebuilt, and numbering continues ───────────────
def _reduced(folder: Path, kw: str, idx: int) -> Path:
    q = np.linspace(0.1, 2.0, 10)
    I = np.exp(-q)
    p = folder / f"{kw}_{idx:04d}_SAXS.dat"
    p.write_text("# q\tI\tsigma\n"
                 + "\n".join(f"{a:.6f}\t{b:.6e}\t{0.01*b:.6e}" for a, b in zip(q, I))
                 + "\n# METADATA INFORMATION (YML FORMAT)\n# i0: 1000.0\n",
                 encoding="utf-8")
    return p


def test_average_rebuilds_its_batch_state_from_the_averaged_folder(tmp_path, monkeypatch):
    import average.app as av
    import src.plot_reduction as pr
    pr.clear_read_cache()

    red = tmp_path / "1D" / "SAXS" / "Reduction"; red.mkdir(parents=True)
    avg = tmp_path / "1D" / "SAXS" / "Averaged";  avg.mkdir(parents=True)
    for i in range(1, 11):
        _reduced(red, "Run9_r001_sample", i)
    # Two batches of 5 already written by a previous run.
    for n in (1, 2):
        (avg / f"Run9_r001_sample_batch{n:03d}_5files_Average.dat").write_text("x")

    monkeypatch.setattr(av, "_project_root", str(tmp_path))
    monkeypatch.setattr(av, "_avg_batch_state", {})
    monkeypatch.setattr(av, "_save_batch_state", lambda: None)

    av._seed_batch_state_from_disk([("saxs", str(red), str(avg))], 5)

    rec = av._avg_batch_state[("saxs", "Run9_r001_sample")]
    assert rec["n"] == 2, "batch numbering must CONTINUE at 003, not overwrite 001"
    assert av._batch_number(("saxs", "Run9_r001_sample")) == 3
    assert len(rec["files"]) == 10, \
        "the 10 frames those two batches consumed must be marked consumed"
    assert av._unconsumed(("saxs", "Run9_r001_sample"),
                          [p.name for p in sorted(red.glob("*.dat"))]) == [], \
        "nothing is left to re-average — that was the stall"


def test_average_leaves_frames_that_no_existing_batch_consumed(tmp_path, monkeypatch):
    import average.app as av
    import src.plot_reduction as pr
    pr.clear_read_cache()

    red = tmp_path / "1D" / "SAXS" / "Reduction"; red.mkdir(parents=True)
    avg = tmp_path / "1D" / "SAXS" / "Averaged";  avg.mkdir(parents=True)
    for i in range(1, 13):                       # 12 frames
        _reduced(red, "Run9_r001_sample", i)
    (avg / "Run9_r001_sample_batch001_5files_Average.dat").write_text("x")

    monkeypatch.setattr(av, "_project_root", str(tmp_path))
    monkeypatch.setattr(av, "_avg_batch_state", {})
    monkeypatch.setattr(av, "_save_batch_state", lambda: None)

    av._seed_batch_state_from_disk([("saxs", str(red), str(avg))], 5)

    rec = av._avg_batch_state[("saxs", "Run9_r001_sample")]
    assert rec["n"] == 1 and len(rec["files"]) == 5
    left = av._unconsumed(("saxs", "Run9_r001_sample"),
                          [p.name for p in sorted(red.glob("*.dat"))])
    assert len(left) == 7, "the 7 un-averaged frames are real work and must remain"


def test_average_ignores_batches_written_at_a_different_batch_size(tmp_path, monkeypatch):
    """A group averaged at 30/batch says nothing about how many frames THIS
    run's 5-frame batches consumed, so it must not be guessed at."""
    import average.app as av
    import src.plot_reduction as pr
    pr.clear_read_cache()

    red = tmp_path / "1D" / "SAXS" / "Reduction"; red.mkdir(parents=True)
    avg = tmp_path / "1D" / "SAXS" / "Averaged";  avg.mkdir(parents=True)
    for i in range(1, 6):
        _reduced(red, "Run9_r001_sample", i)
    (avg / "Run9_r001_sample_batch001_30files_Average.dat").write_text("x")

    monkeypatch.setattr(av, "_project_root", str(tmp_path))
    monkeypatch.setattr(av, "_avg_batch_state", {})
    monkeypatch.setattr(av, "_save_batch_state", lambda: None)

    av._seed_batch_state_from_disk([("saxs", str(red), str(avg))], 5)
    assert av._avg_batch_state == {}


# ── background: the memo is rebuilt from the _sub.dat files ────────────────
def test_background_rebuilds_its_memo_from_the_subtracted_folder(tmp_path, monkeypatch):
    import background.app as bg

    avg = tmp_path / "Averaged";   avg.mkdir(parents=True)
    sub = tmp_path / "Subtracted"; sub.mkdir(parents=True)
    old = time.time() - 7200
    names = [f"Run9_r{i:03d}_sample_batch001_5files_Average" for i in range(1, 6)]
    for n in names:
        p = avg / f"{n}.dat"; p.write_text("x"); os.utime(p, (old, old))
    # Three of the five were already subtracted.
    for n in names[:3]:
        (sub / f"{n}_sub.dat").write_text("x")

    monkeypatch.setattr(bg, "_sub_done", {})
    monkeypatch.setattr(bg, "_save_sub_done", lambda: None)
    monkeypatch.setattr(bg, "_state_root", lambda: str(tmp_path))

    bg._seed_sub_done_from_disk([("saxs", str(avg), str(sub))])

    assert len(bg._sub_done) == 5, (
        "3 have a _sub.dat and 2 are older than the crash-gap window — all 5 are "
        "history; a new run must not re-subtract the folder")
    for n in names:
        assert str((avg / f"{n}.dat").resolve()) in bg._sub_done


def test_background_leaves_a_recent_unsubtracted_average_alone(tmp_path, monkeypatch):
    import background.app as bg

    avg = tmp_path / "Averaged";   avg.mkdir(parents=True)
    sub = tmp_path / "Subtracted"; sub.mkdir(parents=True)
    old = time.time() - 7200
    stale = avg / "Run9_r001_sample_Average.dat"; stale.write_text("x")
    os.utime(stale, (old, old))
    fresh = avg / "Run10_r001_sample_Average.dat"; fresh.write_text("x")   # just landed

    monkeypatch.setattr(bg, "_sub_done", {})
    monkeypatch.setattr(bg, "_save_sub_done", lambda: None)
    monkeypatch.setattr(bg, "_state_root", lambda: str(tmp_path))

    bg._seed_sub_done_from_disk([("saxs", str(avg), str(sub))])

    assert str(fresh.resolve()) not in bg._sub_done, \
        "a curve that landed seconds ago was never subtracted — keep it"
    assert str(stale.resolve()) in bg._sub_done


def test_background_records_the_current_signature_so_a_rewrite_still_runs(tmp_path, monkeypatch):
    """decide_intake remembers by signature: if the averager rewrites the same
    filename with more frames, the new version must subtract again."""
    import background.app as bg
    from src.reactor.intake import decide_intake

    avg = tmp_path / "Averaged";   avg.mkdir(parents=True)
    sub = tmp_path / "Subtracted"; sub.mkdir(parents=True)
    p = avg / "Run9_r001_sample_Average.dat"; p.write_text("x")
    (sub / "Run9_r001_sample_Average_sub.dat").write_text("x")

    monkeypatch.setattr(bg, "_sub_done", {})
    monkeypatch.setattr(bg, "_save_sub_done", lambda: None)
    monkeypatch.setattr(bg, "_state_root", lambda: str(tmp_path))
    bg._seed_sub_done_from_disk([("saxs", str(avg), str(sub))])

    rp = str(p.resolve())
    st = p.stat()
    assert decide_intake(rp, (st.st_size, st.st_mtime_ns), bg._sub_done, {}) == "skip"

    p.write_text("x" * 500)                      # the averager rewrote it
    st2 = p.stat()
    assert decide_intake(rp, (st2.st_size, st2.st_mtime_ns), bg._sub_done,
                         {rp: (st2.st_size, st2.st_mtime_ns)}) == "go", \
        "a genuinely rewritten average must be subtracted again"


# ── reduction: the disk check must work with a filename prefix ─────────────
def test_reduction_already_reduced_honours_a_configured_prefix(tmp_path):
    """Experiment._make_output_path strips a configured prefix from the output
    stem, so with a prefix set the old glob matched nothing and the only
    defence against reprocessing the folder was silently inoperative."""
    import reduction.app as rd

    raw_dir = tmp_path / "2D" / "SAXS"; raw_dir.mkdir(parents=True)
    out = tmp_path / "1D" / "SAXS" / "Reduction"; out.mkdir(parents=True)
    raw = raw_dir / "beam_Run9_r001_sample_0001.raw"
    raw.write_text("x")
    old = time.time() - 600
    os.utime(raw, (old, old))
    # Output written with the prefix stripped, as core.py does.
    (out / "Run9_r001_sample_0001_SAXS.dat").write_text("x")

    assert rd._already_reduced(raw, tmp_path / "1D", ("beam_", "")) is True, \
        "the prefix-stripped output must be recognised"
    assert rd._already_reduced(raw, tmp_path / "1D") is False, \
        "and without the prefix it cannot be found — which was the bug"


def test_reduction_still_matches_an_unprefixed_output(tmp_path):
    import reduction.app as rd
    raw_dir = tmp_path / "2D" / "SAXS"; raw_dir.mkdir(parents=True)
    out = tmp_path / "1D" / "SAXS" / "Reduction"; out.mkdir(parents=True)
    raw = raw_dir / "Run9_r001_sample_0001.raw"; raw.write_text("x")
    old = time.time() - 600
    os.utime(raw, (old, old))
    (out / "Run9_r001_sample_0001_SAXS.dat").write_text("x")
    assert rd._already_reduced(raw, tmp_path / "1D", ("beam_", "")) is True


def test_reduction_treats_a_re_acquired_frame_as_new(tmp_path):
    import reduction.app as rd
    raw_dir = tmp_path / "2D" / "SAXS"; raw_dir.mkdir(parents=True)
    out = tmp_path / "1D" / "SAXS" / "Reduction"; out.mkdir(parents=True)
    raw = raw_dir / "Run9_r001_sample_0001.raw"; raw.write_text("x")
    dat = out / "Run9_r001_sample_0001_SAXS.dat"; dat.write_text("x")
    old = time.time() - 3600
    os.utime(dat, (old, old))                    # .dat older than the .raw
    assert rd._already_reduced(raw, tmp_path / "1D") is False, \
        "a re-acquired frame must be reduced again"
