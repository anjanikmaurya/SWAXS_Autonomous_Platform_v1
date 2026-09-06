"""
tests/test_target_run_tag.py — every optimization is a Target Run (RunN), and the
tag is baked into recipe_id so it flows into the 2D/SAXS filenames.

  - _next_run_no() derives N from disk (max existing + 1), so it survives a restart
    without storing a counter.
  - _new_rid() mints Run{N}_r{seq} while a run is active.
  - the tag round-trips through every downstream parser (loop_naming, optimizer.io)
    because recipe_id is "everything before the role tag".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.loop_naming import split_role, condition_keyword, is_background   # noqa: E402
from src.optimizer.io import recipe_id_from_filename, match_recipe_id      # noqa: E402


# ── _next_run_no derives from disk ────────────────────────────────────────────

def test_next_run_no_from_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    import analyzer.app as az
    az._project_root = str(tmp_path)

    assert az._next_run_no() == 1                       # empty project

    cond = az._resolve_cond(); cond.mkdir(parents=True, exist_ok=True)
    (cond / "Run2_r001.txt").write_text("recipe_id = Run2_r001\n")
    (cond / "Run5_r003.txt").write_text("recipe_id = Run5_r003\n")
    assert az._next_run_no() == 6                        # max(2,5)+1

    res = az._resolve_results(); res.mkdir(parents=True, exist_ok=True)
    (res / "campaign_abc.json").write_text(json.dumps({"run_no": 7}))
    assert az._next_run_no() == 8                        # run_no in a record wins


# ── _new_rid mints Run{N}_r{seq} ──────────────────────────────────────────────

def test_new_rid_sequences_within_a_run(monkeypatch):
    import analyzer.app as az
    monkeypatch.setattr(az, "_run_tag", "Run3")
    monkeypatch.setattr(az, "_run_seq", 0)
    assert az._new_rid() == "Run3_r001"
    assert az._new_rid() == "Run3_r002"
    assert az._new_rid() == "Run3_r003"


def test_new_rid_falls_back_without_a_run(monkeypatch):
    import analyzer.app as az
    monkeypatch.setattr(az, "_run_tag", "")
    rid = az._new_rid()
    assert rid.startswith("auto_") and "Run" not in rid


# ── the tag round-trips through the parsers → filenames ───────────────────────

def test_run_tag_round_trips_through_parsers():
    fname = "Run3_r007_sample_x-113.06_y61.78_scan1_0000_SAXS"
    rid, role = split_role(fname)
    assert rid == "Run3_r007" and role == "sample"
    assert condition_keyword(fname) == "Run3_r007_sample"
    assert not is_background(fname)
    assert recipe_id_from_filename("Run3_r007_sample_scan1_0000_SAXS.dat") == "Run3_r007"
    assert match_recipe_id("Run3_r007_sample_0000_SAXS.dat", ["Run3_r007", "Run3_r006"]) == "Run3_r007"

    bkg = "Run3_r007_bkg_scan1_0000_SAXS"
    assert split_role(bkg) == ("Run3_r007", "bkg")
    assert is_background(bkg)


def test_run_tag_digits_only_never_aliases_a_role_token():
    # "Run<digits>" cannot contain a role token, so split_role never truncates
    # inside the tag. (A word tag like "Runwater" would — hence digits-only.)
    rid, role = split_role("Run12_r001_background_0001_WAXS")
    assert rid == "Run12_r001" and role == "background"
