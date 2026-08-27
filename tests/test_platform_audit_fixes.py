"""
tests/test_platform_audit_fixes.py — regressions for the platform-wide audit.

Each test corresponds to a "silently wrong science" defect that was CONFIRMED by
reproduction. These are the failure modes that produce a plausible-looking but
incorrect number, which is worse than a crash because the Bayesian optimizer
trains on it.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


# ── CSV metadata row index (wrong i0/bstop for EVERY frame) ──────────────────
def test_csv_row_index_is_parsed_from_the_filename_not_the_path():
    """A directory containing a '_NNNN.' token matched first (re.search returns
    the leftmost hit), so every frame in the run read the SAME CSV row — the same
    i0/bstop — wrecking normalisation and defeating the I0 outlier filter."""
    for _name in ("fabio", "pyFAI", "pandas"):
        sys.modules.setdefault(_name, types.ModuleType(_name))
    from src.reduction.process_metadata import find_row_number_to_read as row

    # the dangerous case: a versioned project folder
    assert row(Path("/data/Auto_Run_0002.5/2D/SAXS/x_sample_scan1_0007.raw")) == 7
    assert row(Path("/d/run_0042.raw")) == 42
    assert row(Path("C:/beam_0001.2/2D/SAXS/s_scan1_0123.raw")) == 123


# ── ML truncation must never invent data ─────────────────────────────────────
def _bg():
    import background.app as bg
    return bg


def test_truncation_clips_to_the_measured_range(monkeypatch):
    """np.interp flat-extrapolates outside the source range. The shipped default
    window (0.03-0.6 A^-1 = 0.3-6.0 nm^-1) exceeds a 3 m camera's ~1.8 nm^-1, so
    ~74% of every profile was a fabricated plateau — biasing the fitted PDI ~2x
    and corrupting the confidence the optimizer gates on."""
    import numpy as np
    bg = _bg()
    q_nm = np.linspace(0.1, 1.8, 400)
    I = 1e4 / (1 + (q_nm * 6.0) ** 4) + 30.0
    g, Ig, sg, clipped, (lo, hi) = bg.truncate_rebin(
        q_nm, I, np.sqrt(I), 0.03, 0.6, 549, "linear", "A")
    assert clipped is True
    assert g.max() <= 0.18 + 1e-9
    # a flat plateau would collapse to a handful of distinct values
    assert len(np.unique(np.round(Ig, 9))) == len(Ig)


def test_truncation_refuses_a_non_overlapping_window():
    import numpy as np
    bg = _bg()
    q = np.linspace(0.1, 1.8, 100)
    with pytest.raises(ValueError, match="does not overlap"):
        bg.truncate_rebin(q, np.ones_like(q), np.ones_like(q),
                          10.0, 20.0, 64, "linear", "A")


def test_failed_truncation_is_not_labelled_angstrom(monkeypatch):
    """Writing nm^-1 data under a 'q_A-1' header makes the analyzer multiply q by
    10 → a radius 10x too small, for the rest of the night."""
    import numpy as np
    bg = _bg()
    monkeypatch.setitem(bg._TRUNC, "enabled", True)
    monkeypatch.setitem(bg._TRUNC, "q_unit", "A")
    monkeypatch.setattr(bg, "truncate_rebin",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    q = np.linspace(0.1, 1.8, 40)
    q2, I2, s2, applied = bg._apply_truncation(q, np.ones_like(q), np.ones_like(q))
    assert applied is False, "must report that truncation did not happen"
    assert np.allclose(q2, q), "arrays must be the untouched nm^-1 ones"


# ── background pairing must not cross recipes ────────────────────────────────
def test_a_sample_is_never_paired_with_another_recipes_background():
    """In an autonomous campaign the blanks are recipe-tagged. If this recipe's
    own blank is missing, subtracting a DIFFERENT recipe's blank (different
    temperature and composition, possibly hours old) is silently wrong."""
    bg = _bg()
    sample = Path("auto_20260730_013000_b222_sample_batch001_Average.dat")
    foreign = Path("auto_20260730_010000_a111_bkg_batch001_Average.dat")
    assert bg._pick_background(sample, [foreign]) is None, \
        "paired across recipes — the monitor should wait instead"

    own = Path("auto_20260730_013000_b222_bkg_batch001_Average.dat")
    chosen = bg._pick_background(sample, [foreign, own])
    assert chosen == own


def test_manual_datasets_still_use_the_nearest_index_heuristic():
    """Guard against over-correcting: when NO background carries a recipe key we
    are in a manual dataset (e.g. nylon_sample_* vs buffer_*), where nearest
    index is exactly what the operator wants."""
    bg = _bg()
    sample = Path("nylon_sample_batch008_Average.dat")
    bkgs = [Path("buffer_batch003_Average.dat"), Path("buffer_batch009_Average.dat")]
    chosen = bg._pick_background(sample, bkgs)
    assert chosen is not None and chosen.name == "buffer_batch009_Average.dat"


# ── the quality gate must actually gate ──────────────────────────────────────
def test_analyzer_prefers_the_quality_gates_good_folder(tmp_path, monkeypatch):
    """The gate COPIES into Good/ and NeedsReview/ and leaves the original in
    place, so watching the flat folder meant every rejected profile was still
    fitted and fed to the campaign — the gate had no effect on the data path."""
    import analyzer.app as az
    monkeypatch.setattr(az, "_project_root", str(tmp_path))
    monkeypatch.setattr(az, "_sub_folder", "1D/SAXS/Subtracted")
    monkeypatch.setattr(az, "_gate_mode", "auto")
    monkeypatch.setattr(az, "_gate_note_shown", True)

    base = tmp_path / "1D" / "SAXS" / "Subtracted"
    base.mkdir(parents=True)
    assert az._resolve_sub() == base, "no Good/ yet → flat folder, with a warning"

    (base / "Good").mkdir()
    assert az._resolve_sub() == base / "Good", "Good/ exists → must analyse only it"


def test_gate_mode_off_restores_the_flat_folder(tmp_path, monkeypatch):
    import analyzer.app as az
    monkeypatch.setattr(az, "_project_root", str(tmp_path))
    monkeypatch.setattr(az, "_sub_folder", "1D/SAXS/Subtracted")
    monkeypatch.setattr(az, "_gate_note_shown", True)
    base = tmp_path / "1D" / "SAXS" / "Subtracted"
    (base / "Good").mkdir(parents=True)
    monkeypatch.setattr(az, "_gate_mode", "off")
    assert az._resolve_sub() == base


# ── the hub must notice a dead child ─────────────────────────────────────────
def test_hub_detects_a_child_that_exited_on_its_own():
    """Nothing watched the subprocesses: a reduction crash at 02:00 meant frames
    kept landing, nothing processed them, and the card just went grey."""
    import importlib.util as u
    spec = u.spec_from_file_location("hb_audit", "hub/app.py")
    hb = u.module_from_spec(spec)
    sys.modules["hb_audit"] = hb
    spec.loader.exec_module(hb)

    class Dying:
        pid = 999
        def __init__(self): self._n = 0
        def poll(self):
            self._n += 1
            return None if self._n <= 1 else 3      # alive once, then exit code 3

    aid = hb.APPS[0]["id"]
    hb._procs[aid] = Dying()
    hb._crashed.clear(); hb._last_running.clear()
    hb._detect_crashes()                            # observes it running
    assert hb._crashed.get(aid) is None
    hb._detect_crashes()                            # observes it dead
    assert hb._crashed[aid]["exit_code"] == 3


def test_hub_reports_free_disk_space():
    """A full disk truncates .dat files that still look stable to every watcher."""
    import importlib.util as u
    spec = u.spec_from_file_location("hb_audit2", "hub/app.py")
    hb = u.module_from_spec(spec)
    sys.modules["hb_audit2"] = hb
    spec.loader.exec_module(hb)
    free = hb._disk_free_gb()
    assert free is None or free >= 0
