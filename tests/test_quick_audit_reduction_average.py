"""
tests/test_quick_audit_reduction_average.py

Pins the October 2026 quick-audit fixes for Reduction and Average
(docs/audits/PLATFORM_QUICK_AUDIT_2026_10.md):

  Q3   a WAXS .dat no longer marks the SAXS frame as already reduced
  Q4   CSV metadata matched on "_" token boundaries, never a substring
  Q5   Stop then a quick Start never leaves two monitor loops
  Q6   monitor Start refused while Run now is active; run_pipeline takes the lock
  Q12  SAXS and WAXS averages in one folder get distinct (_SAXS/_WAXS) names
  Q13  a fresh Average start with a different N does not re-average history
  Q27  average_batch reports the frames actually used
  Q29  frames of one group are ordered by (scan, index), not index alone
"""
from __future__ import annotations

import importlib.util as u
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")


@pytest.fixture(scope="module")
def red():
    spec = u.spec_from_file_location("red_quick_audit", _ROOT / "reduction" / "app.py")
    mod = u.module_from_spec(spec)
    sys.modules["red_quick_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


def _touch(p: Path, mtime: float | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# ── Q3 ──────────────────────────────────────────────────────────────────────
def test_q3_waxs_dat_does_not_mark_the_saxs_frame_done(red, tmp_path):
    old = time.time() - 3600
    raw_s = _touch(tmp_path / "2D" / "SAXS" / "sone_run_scan1_0001.raw", old)
    raw_w = _touch(tmp_path / "2D" / "WAXS" / "wone_run_scan1_0001.raw", old)
    out = tmp_path / "1D"
    # Prefixes stripped: both detectors share the stem "run_scan1_0001".
    _touch(out / "WAXS" / "Reduction" / "run_scan1_0001_WAXS.dat")
    (out / "SAXS" / "Reduction").mkdir(parents=True)
    pre = ("sone_", "wone_")
    assert red._already_reduced(raw_w, out, pre) is True
    assert red._already_reduced(raw_s, out, pre) is False, \
        "the WAXS .dat must not mark the SAXS frame as done"
    _touch(out / "SAXS" / "Reduction" / "run_scan1_0001_SAXS.dat")
    assert red._already_reduced(raw_s, out, pre) is True


def test_q3_only_the_exact_output_name_counts(red, tmp_path):
    old = time.time() - 3600
    raw = _touch(tmp_path / "2D" / "SAXS" / "run_0001.raw", old)
    out = tmp_path / "1D"
    # A longer stem that merely starts with ours ("run_00010") is not ours.
    _touch(out / "SAXS" / "Reduction" / "run_00010_SAXS.dat")
    assert red._already_reduced(raw, out) is False
    # Explicit detector overrides the folder-derived one.
    _touch(out / "WAXS" / "Reduction" / "run_0001_WAXS.dat")
    assert red._already_reduced(raw, out) is False
    assert red._already_reduced(raw, out, detector="WAXS") is True


def test_q3_seed_uses_each_frames_own_detector(red, tmp_path, monkeypatch):
    old = time.time() - 3600
    raw_s = _touch(tmp_path / "2D" / "SAXS" / "run_scan1_0001.raw", time.time())
    raw_w = _touch(tmp_path / "2D" / "WAXS" / "run_scan1_0001.raw", time.time())
    out = tmp_path / "1D"
    _touch(out / "WAXS" / "Reduction" / "run_scan1_0001_WAXS.dat",
           time.time() + 10)
    (out / "SAXS" / "Reduction").mkdir(parents=True)
    cfg = {"data_directory": str(tmp_path / "2D"), "mode": "SWAXS",
           "beamline": {"data_format": "raw"}}
    monkeypatch.setattr(red, "_processed_files", set())
    red._seed_processed_from_disk(cfg, out)
    assert str(raw_w) in red._processed_files
    assert str(raw_s) not in red._processed_files, \
        "a recent SAXS frame with no SAXS .dat must be left for reduction"
    del old


# ── Q4 ──────────────────────────────────────────────────────────────────────
def test_q4_csv_matching_is_on_token_boundaries():
    from src.reduction.process_metadata import _csv_stem_matches as m
    assert m("run_scan1", "run_scan1")                     # exact
    assert m("run_scan1", "sone_run_scan1")                # raw-only prefix
    assert m("sim_r001", "sim_r001_scan1")                 # simulator per-prefix CSV
    assert not m("run_scan1", "run_scan10"), "scan1 must not match scan10"
    assert not m("un_scan1", "run_scan1")
    assert not m("", "run_scan1")


def test_q4_scan10_frames_wait_instead_of_taking_scan1_metadata(tmp_path):
    from src.reduction.process_metadata import (CSVMetadataNotFound,
                                                process_csv_metadata)
    two_d = tmp_path / "2D"
    (two_d / "SAXS").mkdir(parents=True)
    (two_d / "sone_run_scan1.csv").write_text("i0,bstop\n1,2\n3,4\n")
    raw1 = _touch(two_d / "SAXS" / "sone_run_scan1_0001.raw")
    raw10 = _touch(two_d / "SAXS" / "sone_run_scan10_0001.raw")
    assert process_csv_metadata(raw1)["i0"] == 3
    with pytest.raises(CSVMetadataNotFound):
        process_csv_metadata(raw10)
    (two_d / "run_scan10.csv").write_text("i0,bstop\n5,6\n7,8\n")   # no prefix
    assert process_csv_metadata(raw10)["i0"] == 7


def test_q4_frame_index_past_9999_still_finds_its_csv(tmp_path):
    from src.reduction.process_metadata import process_csv_metadata
    two_d = tmp_path / "2D"
    (two_d / "run_scan1.csv").parent.mkdir(parents=True, exist_ok=True)
    rows = "i0,bstop\n" + "\n".join(f"{i},1" for i in range(10002))
    (two_d / "run_scan1.csv").write_text(rows + "\n")
    raw = _touch(two_d / "SAXS" / "run_scan1_10001.raw")
    assert process_csv_metadata(raw)["i0"] == 10001


# ── Q5 / Q6 ─────────────────────────────────────────────────────────────────
_CFG = {"data_directory": "/nonexistent/2D", "mode": "SAXS"}


@pytest.fixture
def mon(red, monkeypatch):
    """A monitor whose loop blocks inside _get_experiment until released."""
    monkeypatch.delenv("SWAXS_PROJECT", raising=False)
    gate = threading.Event()
    entered = threading.Event()

    def _blocking_experiment(_config):
        entered.set()
        gate.wait(5)
        raise RuntimeError("test: no integrators")

    monkeypatch.setattr(red, "_get_experiment", _blocking_experiment)
    monkeypatch.setattr(red, "_MONITOR_JOIN_S", 0.2)
    client = red.app.test_client()
    yield red, client, gate, entered
    gate.set()
    client.post("/api/monitor/stop")
    t = red._monitor_thread
    if t is not None:
        t.join(3)
    red._monitoring = False


def test_q5_stopped_loop_exits_even_if_the_flag_is_set_again(mon):
    red, client, gate, entered = mon
    assert client.post("/api/monitor/start",
                       json={"config": dict(_CFG), "interval": 1}).status_code == 200
    assert entered.wait(2)
    old = red._monitor_thread
    client.post("/api/monitor/stop")
    red._monitoring = True            # what a quick Start used to do to the old loop
    gate.set()
    old.join(3)
    assert not old.is_alive(), "the stopped loop must not carry on beside a new one"


def test_q5_start_refuses_while_the_old_loop_is_still_finishing(mon):
    red, client, gate, entered = mon
    client.post("/api/monitor/start", json={"config": dict(_CFG), "interval": 1})
    assert entered.wait(2)
    old = red._monitor_thread
    client.post("/api/monitor/stop")
    rv = client.post("/api/monitor/start",
                     json={"config": dict(_CFG), "interval": 1})
    assert rv.status_code == 409
    assert "still finishing" in rv.get_json()["error"]
    assert red._monitor_thread is old, "no second loop was started"
    gate.set()
    old.join(3)
    entered.clear()
    rv = client.post("/api/monitor/start",
                     json={"config": dict(_CFG), "interval": 1})
    assert rv.status_code == 200 and red._monitor_thread is not old


def test_q6_monitor_start_refused_while_run_now_is_active(red, monkeypatch):
    monkeypatch.delenv("SWAXS_PROJECT", raising=False)
    monkeypatch.setattr(red, "_run_active", True)
    rv = red.app.test_client().post("/api/monitor/start",
                                    json={"config": dict(_CFG), "interval": 1})
    assert rv.status_code == 409
    assert "Run now" in rv.get_json()["error"]
    assert red._monitor_thread is None or not red._monitor_thread.is_alive()


def test_q6_run_pipeline_marks_files_under_the_given_lock(tmp_path):
    from src.reduction.core import run_pipeline
    _touch(tmp_path / "2D" / "SAXS" / "a_0001.raw")

    class _Exp:
        output_dir_1d = tmp_path / "1D"

        def process_saxs_file(self, f):
            return {"filename": f.name, "corrections": {
                "i0_corrected": 1.0, "bstop_corrected": 1.0,
                "transmission": 1.0, "thickness_m": 0.001}}

    class _SpyLock:
        def __init__(self):
            self.held, self.entries = False, 0

        def __enter__(self):
            self.held = True
            self.entries += 1

        def __exit__(self, *a):
            self.held = False

    lock = _SpyLock()

    class _CheckedSet(set):
        def add(self, x):
            assert lock.held, "processed_files mutated without the lock"
            super().add(x)

    done = _CheckedSet()
    res = run_pipeline({"data_directory": str(tmp_path / "2D"), "mode": "SAXS"},
                       processed_files=done, experiment=_Exp(),
                       processed_lock=lock)
    assert res["saxs_count"] == 1 and lock.entries == 1 and len(done) == 1


def test_q6_app_passes_its_lock_to_run_pipeline():
    src = (_ROOT / "reduction" / "app.py").read_text()
    assert "processed_lock    = _processed_lock" in src


# ── Average helpers ─────────────────────────────────────────────────────────
@pytest.fixture
def av(monkeypatch):
    import average.app as av_mod
    import src.plot_reduction as pr
    pr.clear_read_cache()
    monkeypatch.setattr(av_mod, "_avg_batch_state", {})
    monkeypatch.setattr(av_mod, "_save_batch_state", lambda: None)
    return av_mod


def _reduced(folder: Path, name: str, mtime: float | None = None) -> Path:
    q = np.linspace(0.1, 2.0, 10)
    I = np.exp(-q)
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# q\tI\tsigma\n"
                 + "\n".join(f"{a:.6f}\t{b:.6e}\t{0.01*b:.6e}" for a, b in zip(q, I))
                 + "\n# METADATA INFORMATION (YML FORMAT)\n# i0: 1000.0\n",
                 encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# ── Q12 ─────────────────────────────────────────────────────────────────────
def test_q12_shared_output_folder_tags_names_by_detector(av, tmp_path):
    shared = str(tmp_path / "Averaged")
    tags = av._detector_tags([("saxs", str(tmp_path / "S" / "Reduction"), shared),
                              ("waxs", str(tmp_path / "W" / "Reduction"), shared)])
    assert tags == {"saxs": "SAXS", "waxs": "WAXS"}
    # Default layout: separate Averaged/ folders, names unchanged.
    tags = av._detector_tags([("saxs", str(tmp_path / "S" / "Reduction"), None),
                              ("waxs", str(tmp_path / "W" / "Reduction"), None)])
    assert tags == {"saxs": None, "waxs": None}


def test_q12_manual_average_into_one_folder_keeps_both(av, tmp_path):
    for det in ("SAXS", "WAXS"):
        for i in (1, 2):
            _reduced(tmp_path / det / "Reduction", f"BSA_{i:04d}_{det}.dat")
    out = tmp_path / "Averaged"
    rv = av.app.test_client().post("/api/average", json={
        "keywords": ["BSA"], "output_dir": str(out),
        "saxs_folder": str(tmp_path / "SAXS" / "Reduction"),
        "waxs_folder": str(tmp_path / "WAXS" / "Reduction")})
    body = rv.get_json()
    assert not body["errors"], body["errors"]
    names = sorted(p.name for p in out.glob("*.dat"))
    assert names == ["BSA_2files_Average_SAXS.dat", "BSA_2files_Average_WAXS.dat"]


def test_q12_tagged_names_stay_parseable():
    from src.loop_naming import condition_keyword, is_background, recipe_id_of
    from src.plot_reduction import detector_tagged, keyword_and_index
    n = detector_tagged("r001_bkg_batch002_10files_Average.dat", "WAXS")
    assert n == "r001_bkg_batch002_10files_Average_WAXS.dat"
    assert recipe_id_of(n) == "r001" and is_background(n)
    assert condition_keyword(n) == "r001_bkg"
    assert keyword_and_index("BSA_2files_Average_SAXS.dat") == ("BSA", 0)
    assert keyword_and_index("BSA_2files_Average.dat") == ("BSA", 0)
    assert detector_tagged("x.dat", None) == "x.dat"


def test_q12_seed_counts_only_its_own_detectors_batches(av, tmp_path):
    shared = tmp_path / "Averaged"
    for det in ("SAXS", "WAXS"):
        for i in range(1, 6):
            _reduced(tmp_path / det / "Reduction", f"r001_sample_{i:04d}_{det}.dat")
    _touch(shared / "r001_sample_batch001_5files_Average_WAXS.dat")
    av._seed_batch_state_from_disk(
        [("saxs", str(tmp_path / "SAXS" / "Reduction"), str(shared)),
         ("waxs", str(tmp_path / "WAXS" / "Reduction"), str(shared))], 5)
    assert av._avg_batch_state[("waxs", "r001_sample")]["n"] == 1
    assert ("saxs", "r001_sample") not in av._avg_batch_state, \
        "the WAXS batch must not mark the (recent) SAXS frames consumed"


# ── Q13 ─────────────────────────────────────────────────────────────────────
def test_q13_fresh_start_with_a_different_n_skips_history(av, tmp_path):
    red_dir = tmp_path / "1D" / "SAXS" / "Reduction"
    avg_dir = tmp_path / "1D" / "SAXS" / "Averaged"
    old = time.time() - 7200
    for i in range(1, 11):
        _reduced(red_dir, f"Run9_r001_sample_{i:04d}_SAXS.dat", old)
    # Previous run averaged at N=10; this run uses N=5.
    _touch(avg_dir / "Run9_r001_sample_batch001_10files_Average.dat")
    # Two frames that landed just now are real work.
    for i in (11, 12):
        _reduced(red_dir, f"Run9_r001_sample_{i:04d}_SAXS.dat")
    av._seed_batch_state_from_disk([("saxs", str(red_dir), str(avg_dir))], 5)
    rec = av._avg_batch_state[("saxs", "Run9_r001_sample")]
    assert len(rec["files"]) == 10, "the 10 old frames are history"
    assert rec["n"] == 0, "no batch at N=5 exists, numbering starts at 001"
    left = av._unconsumed(("saxs", "Run9_r001_sample"),
                          sorted(p.name for p in red_dir.glob("*.dat")))
    assert left == ["Run9_r001_sample_0011_SAXS.dat", "Run9_r001_sample_0012_SAXS.dat"]


def test_q13_history_counts_even_with_no_averaged_folder(av, tmp_path):
    red_dir = tmp_path / "1D" / "SAXS" / "Reduction"
    old = time.time() - 7200
    for i in range(1, 4):
        _reduced(red_dir, f"buffer_{i:04d}_SAXS.dat", old)
    av._seed_batch_state_from_disk([("saxs", str(red_dir), None)], 5)
    assert len(av._avg_batch_state[("saxs", "buffer")]["files"]) == 3


# ── Q27 ─────────────────────────────────────────────────────────────────────
def test_q27_average_batch_reports_frames_used(tmp_path):
    from src.plot_reduction import average_batch, read_folder
    d = tmp_path / "Reduction"
    for i in range(1, 4):
        _reduced(d, f"s_{i:04d}_SAXS.dat")
    frames = read_folder(d)
    frames[0] = {**frames[0], "I": np.full_like(frames[0]["I"], -1.0)}  # unusable
    out, n = average_batch(frames, "s", tmp_path / "o.dat", return_n_used=True)
    assert out is not None and n == 2
    assert average_batch(frames, "s", tmp_path / "p.dat") == tmp_path / "p.dat"
    assert average_batch([], "s", tmp_path / "q.dat", return_n_used=True) == (None, 0)


def test_q27_loop_reports_n_used_not_batch_size():
    src = (_ROOT / "average" / "app.py").read_text()
    assert "n_files=int(n_used)" in src
    assert "n_files=n_per_batch" not in src


# ── Q29 ─────────────────────────────────────────────────────────────────────
def test_q29_frames_sort_by_scan_then_index():
    from src.plot_reduction import acquisition_order_key, keyword_and_index
    names = ["r1_sample_scan10_0001_SAXS.dat", "r1_sample_scan2_0002_SAXS.dat",
             "r1_sample_scan2_0001_SAXS.dat", "r1_sample_scan1_0002_SAXS.dat",
             "r1_sample_scan1_0001_SAXS.dat"]
    fds = []
    for n in names:
        kw, idx = keyword_and_index(n)
        fds.append({"filename": n, "keyword": kw, "scan_idx": idx})
    ordered = [fd["filename"] for fd in sorted(fds, key=acquisition_order_key)]
    assert ordered == ["r1_sample_scan1_0001_SAXS.dat", "r1_sample_scan1_0002_SAXS.dat",
                       "r1_sample_scan2_0001_SAXS.dat", "r1_sample_scan2_0002_SAXS.dat",
                       "r1_sample_scan10_0001_SAXS.dat"]


# ── Q28 ─────────────────────────────────────────────────────────────────────
def test_q28_template_defaults_match_the_backend():
    html = (_ROOT / "average" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "+aaVal('aa-interval')||3" in html
    assert 'id="out-dir" placeholder="(default: …/Averaged)"' in html
