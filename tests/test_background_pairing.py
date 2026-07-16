"""
tests/test_background_pairing.py — sample↔background pairing for the closed loop.

The reactor names its two per-condition acquisitions {recipe_id}_sample and
{recipe_id}_bkg. This checks the subtraction app pairs them by the shared
recipe_id (deterministic in an autonomous campaign) and still falls back to the
nearest-index heuristic when there's no recipe_id match.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import background.app as bg   # noqa: E402


def test_recipe_key_strips_role_tag():
    assert bg._recipe_key("auto_42_sample_30files_Average.dat") == "auto_42"
    assert bg._recipe_key("auto_42_bkg_30files_Average.dat") == "auto_42"
    assert bg._recipe_key("something_plain.dat") == ""


def test_pairs_by_recipe_id_not_nearest_index():
    sample = Path("auto_007_sample_batch005_30files_Average.dat")   # index 5
    bkgs = [
        Path("auto_003_bkg_batch005_30files_Average.dat"),          # same index, WRONG recipe
        Path("auto_007_bkg_batch012_30files_Average.dat"),          # right recipe, farther index
    ]
    chosen = bg._pick_background(sample, bkgs)
    assert chosen.name.startswith("auto_007_bkg")                   # recipe_id wins over index


def test_falls_back_to_nearest_index_without_recipe_match():
    sample = Path("nylon_sample_batch008_Average.dat")
    bkgs = [Path("buffer_batch003_Average.dat"), Path("buffer_batch009_Average.dat")]
    chosen = bg._pick_background(sample, bkgs)
    assert chosen.name == "buffer_batch009_Average.dat"             # nearest index (9 vs 8)
