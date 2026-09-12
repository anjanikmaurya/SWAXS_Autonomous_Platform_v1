"""
tests/test_analyzer_resume.py — analyzer startup: FRESH default + Continue a
stopped Target Run.

  1. No incomplete run: every pre-existing subtracted profile that already has
     a Results/Fit/ record is seeded into `_handled` at boot WITHOUT fitting
     it — instant startup, no re-fit storm.
  2. An incomplete Run9: `_continue_run()` rebuilds the campaign from durable
     records only (campaign record + Fit records + reactor feedback files),
     replays every measurement via tell() without re-fitting, and advances
     `_n_asked` so the next ask() proposes a genuinely new point.
  3. An orphan condition file (proposed, never picked up by the reactor) is
     re-issued into `_pending` with a fresh timeout, not silently dropped.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import analyzer.app as az                                       # noqa: E402
from src.optimizer.io import to_param_file                      # noqa: E402

PARAMS = {"T_reac": 240.0, "F_tot": 80.0, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1}


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path, monkeypatch):
    """Every test gets a fresh project root and a clean slate of the module's
    global campaign/intake state — these are shared module-level singletons."""
    monkeypatch.setattr(az, "_project_root", str(tmp_path))
    monkeypatch.setattr(az, "_sub_folder", "1D/SAXS/Subtracted")
    monkeypatch.setattr(az, "_cond_folder", "1D/SAXS/Conditions")
    monkeypatch.setattr(az, "_results_folder", "1D/SAXS/Results")
    monkeypatch.setattr(az, "_gate_mode", "off")     # no Good/ folder in these fixtures
    monkeypatch.setattr(az, "_campaign", None)
    monkeypatch.setattr(az, "_campaign_cfg", {})
    monkeypatch.setattr(az, "_campaign_meta", {})
    monkeypatch.setattr(az, "_campaign_id", "")
    monkeypatch.setattr(az, "_run_tag", "")
    monkeypatch.setattr(az, "_run_seq", 0)
    monkeypatch.setattr(az, "_pending", {})
    monkeypatch.setattr(az, "_pending_at", {})
    az._handled.clear()
    az._lastsig.clear()
    yield
    az._handled.clear()
    az._lastsig.clear()


def _sub_dir(tmp_path) -> Path:
    d = tmp_path / "1D" / "SAXS" / "Subtracted"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fit_dir(tmp_path) -> Path:
    d = tmp_path / "1D" / "SAXS" / "Results" / "Fit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_dat(path: Path) -> None:
    path.write_text("# header\n1.0 2.0 0.1\n", encoding="utf-8")


def _write_fit_record(fit_dir: Path, recipe_id: str, campaign_id: str,
                       size, pdi, confidence, written: str) -> Path:
    stem = f"{recipe_id}_sample_20260101_000000"
    path = fit_dir / f"fit_{stem}.dat"
    lines = [
        "# Nanoparticle fit record -- Auto-Fit & Optimiser (analyzer)",
        f"# Source file     : /tmp/{stem}.dat",
        f"# Written         : {written}",
        f"# Radius (nm)     : {size}",
        f"# Diameter (nm)   : {'' if size is None else size * 2}",
        f"# PDI             : {pdi}",
        "# Distribution    : schulz",
        "# Phase           : sphere",
        f"# Confidence      : {confidence}",
        "# Invariant (rel) : 1.0",
        "# Guinier Rg (nm) : 3.0",
        f"# Campaign ID     : {campaign_id}",
        "# Target size (nm): 5.0",
        "# Tolerance       : 0.3",
        "# PDI cap         : 0.15",
        "# Columns: q_nm-1  I_data  sigma  I_fit  (I_fit is NaN if no form-factor fit)",
    ]
    path.write_text("\n".join(lines) + "\n1.0 2.0 0.1 2.0\n", encoding="utf-8")
    return path


def _write_feedback(project: Path, recipe_id: str, params: dict) -> None:
    d = project / "reactor" / "feedback"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"recipe_id": recipe_id, "recipe": {**params, "recipe_id": recipe_id}}
    (d / f"{recipe_id}.done.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_campaign_record(project: Path, *, campaign_id: str, run_no: int,
                            run_tag: str, budget: int, outcome: dict | None = None) -> Path:
    d = project / "1D" / "SAXS" / "Results"
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "campaign_id": campaign_id,
        "target_size": 5.0, "tolerance": 0.3, "pdi_cap": 0.15,
        "budget": budget, "n_init": 10,
        "objective": "min ((size - target_size)/tolerance)^2 + w*(PDI/pdi_cap)",
        "started_at": "2026-09-01T00:00:00", "operator": "tester",
        "run_no": run_no, "run_tag": run_tag,
        "status": "running",
    }
    if outcome:
        rec.update(outcome)
    path = d / f"campaign_{campaign_id}.json"
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path


# ── 1. No incomplete run: FRESH default, instant startup ──────────────────────

def test_seed_handled_at_boot_skips_already_fit_profiles_without_fitting(tmp_path, monkeypatch):
    sub = _sub_dir(tmp_path)
    fit_dir = _fit_dir(tmp_path)
    files = []
    for i in range(3):
        f = sub / f"Run8_r00{i}_sample_20260101_000000.dat"
        _write_dat(f)
        files.append(f)
        _write_fit_record(fit_dir, f"Run8_r00{i}", "old_campaign",
                           4.0 + i, 0.1, 0.9, f"2026-09-0{i+1}T00:00:00")

    _write_campaign_record(tmp_path, campaign_id="old_campaign", run_no=8,
                            run_tag="Run8", budget=10,
                            outcome={"outcome": "converged"})

    calls = []
    monkeypatch.setattr(az, "_analyze_file", lambda p: calls.append(p))

    t0 = time.monotonic()
    az._seed_handled_at_boot()
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0
    assert set(az._handled.keys()) == {str(f) for f in files}
    assert calls == []          # _analyze_file must never be called

    # And the watcher's own intake decision agrees: every file is now a "skip".
    from src.reactor.intake import decide_intake
    for f in files:
        st = f.stat()
        sig = (st.st_size, st.st_mtime_ns)
        assert decide_intake(str(f), sig, az._handled, az._lastsig) == "skip"

    assert az._latest_incomplete_run() is None


def test_seed_handled_at_boot_leaves_unfit_files_for_the_watcher(tmp_path, monkeypatch):
    """A file with no Fit record (the crash-gap case) is NOT marked handled —
    it must still be fit normally on the watcher's first poll."""
    sub = _sub_dir(tmp_path)
    _fit_dir(tmp_path)          # exists, but empty — no fit record for this file
    f = sub / "Run8_r099_sample_20260101_000000.dat"
    _write_dat(f)

    az._seed_handled_at_boot()

    assert str(f) not in az._handled


def test_seed_handled_at_boot_seeds_old_unfit_files_too(tmp_path, monkeypatch):
    """A file with no Fit record that is NOT recent (older than the crash-gap
    window) is historical, most commonly fit before "every fit gets a durable
    record" existed — it must still be seeded as handled, or every restart
    re-fits the project's entire pre-that-feature history."""
    sub = _sub_dir(tmp_path)
    _fit_dir(tmp_path)
    f = sub / "Run3_r007_sample_20260101_000000.dat"
    _write_dat(f)
    old = time.time() - az._CRASH_GAP_WINDOW_S - 3600.0
    os.utime(f, (old, old))

    calls = []
    monkeypatch.setattr(az, "_analyze_file", lambda p: calls.append(p))

    az._seed_handled_at_boot()

    assert str(f) in az._handled
    assert calls == []


# ── 2. Continue an incomplete Run9 ─────────────────────────────────────────────

def test_continue_run_replays_measurements_without_refitting(tmp_path, monkeypatch):
    fit_dir = _fit_dir(tmp_path)
    campaign_id = "cid_run9"
    n = 12
    for i in range(n):
        rid = f"Run9_r{i:03d}"
        _write_feedback(tmp_path, rid, PARAMS)
        _write_fit_record(fit_dir, rid, campaign_id, size=4.0 + i * 0.05, pdi=0.1,
                           confidence=0.9, written=f"2026-09-01T00:{i:02d}:00")
    _write_campaign_record(tmp_path, campaign_id=campaign_id, run_no=9,
                            run_tag="Run9", budget=25)

    calls = []
    monkeypatch.setattr(az, "_analyze_file", lambda p: calls.append(p))

    rec = az._latest_incomplete_run()
    assert rec is not None
    assert rec["run_no"] == 9

    summary = az._continue_run()

    assert calls == []                              # no re-fitting
    assert az._campaign is not None
    assert len(az._campaign.history) == n
    assert az._campaign.budget - len(az._campaign.history) == 25 - n
    # Nothing was pending, so _continue_run() immediately proposes the next
    # condition — one more ask() on top of the replay. Without the _n_asked
    # fix, ask() would have re-proposed the very first (already-measured)
    # Sobol seed instead of a new point, and _n_asked would sit at 1, not 13.
    assert az._campaign._n_asked == n + 1
    assert summary["replayed"] == n
    assert summary["skipped"] == 0
    assert summary["used"] == n
    assert summary["budget"] == 25


def test_continue_run_skips_measurements_with_no_feedback_record(tmp_path, monkeypatch):
    """A Fit record whose reactor feedback file is missing is an unrecoverable
    observation — skipped, never fabricated, and not counted toward the budget."""
    fit_dir = _fit_dir(tmp_path)
    campaign_id = "cid_run9"
    _write_feedback(tmp_path, "Run9_r000", PARAMS)
    _write_fit_record(fit_dir, "Run9_r000", campaign_id, 4.0, 0.1, 0.9,
                       "2026-09-01T00:00:00")
    # Second recipe has a Fit record but NO feedback file.
    _write_fit_record(fit_dir, "Run9_r001", campaign_id, 4.2, 0.1, 0.9,
                       "2026-09-01T00:01:00")
    _write_campaign_record(tmp_path, campaign_id=campaign_id, run_no=9,
                            run_tag="Run9", budget=25)

    summary = az._continue_run()

    assert summary["replayed"] == 1
    assert summary["skipped"] == 1
    assert len(az._campaign.history) == 1


# ── 3. Orphan condition file: re-issued, not dropped ───────────────────────────

def test_continue_run_reissues_an_orphan_condition_file(tmp_path, monkeypatch):
    fit_dir = _fit_dir(tmp_path)
    campaign_id = "cid_run9"
    for i in range(2):
        rid = f"Run9_r00{i}"
        _write_feedback(tmp_path, rid, PARAMS)
        _write_fit_record(fit_dir, rid, campaign_id, 4.0 + i, 0.1, 0.9,
                           f"2026-09-01T00:0{i}:00")
    _write_campaign_record(tmp_path, campaign_id=campaign_id, run_no=9,
                            run_tag="Run9", budget=25)

    cond_dir = tmp_path / "1D" / "SAXS" / "Conditions"
    cond_dir.mkdir(parents=True, exist_ok=True)
    orphan_id = "Run9_r013"
    (cond_dir / f"{orphan_id}.txt").write_text(
        to_param_file(orphan_id, PARAMS), encoding="utf-8")
    # No counterpart in Conditions/done/ — this recipe was proposed but the
    # reactor never even started it.

    summary = az._continue_run()

    assert orphan_id in az._pending
    assert orphan_id in az._pending_at
    assert orphan_id in summary["reissued"]
    # Not counted as a measurement.
    recipe_ids_told = {h.get("recipe_id") for h in az._campaign.history}
    assert orphan_id not in recipe_ids_told


def test_continue_run_does_not_reissue_a_condition_already_picked_up(tmp_path, monkeypatch):
    """A condition file already moved to Conditions/done/ was submitted to the
    reactor — it must be left alone, not re-issued as a duplicate proposal."""
    fit_dir = _fit_dir(tmp_path)
    campaign_id = "cid_run9"
    rid = "Run9_r000"
    _write_feedback(tmp_path, rid, PARAMS)
    _write_fit_record(fit_dir, rid, campaign_id, 4.0, 0.1, 0.9, "2026-09-01T00:00:00")
    _write_campaign_record(tmp_path, campaign_id=campaign_id, run_no=9,
                            run_tag="Run9", budget=25)

    cond_dir = tmp_path / "1D" / "SAXS" / "Conditions"
    done_dir = cond_dir / "done"
    cond_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)
    picked_up_id = "Run9_r005"
    # A file sitting in both places means the reactor already picked it up
    # (moved a copy into done/) — must not be treated as an orphan.
    (cond_dir / f"{picked_up_id}.txt").write_text(
        to_param_file(picked_up_id, PARAMS), encoding="utf-8")
    (done_dir / f"{picked_up_id}.txt").write_text(
        to_param_file(picked_up_id, PARAMS), encoding="utf-8")

    az._continue_run()

    assert picked_up_id not in az._pending
