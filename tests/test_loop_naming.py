"""
tests/test_loop_naming.py — the filename convention that ties the loop together.

condition_keyword drives the automated averaging grouping; recipe_id_of drives
the sample↔background pairing. Both must agree with the reactor's naming.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.loop_naming import condition_keyword, recipe_id_of, is_background, split_role  # noqa: E402


def test_sample_and_background_share_recipe_id_but_group_apart():
    sname = "auto_20260716_070621_8c3401_sample_scan1_0003_SAXS.dat"
    bname = "auto_20260716_070621_8c3401_bkg_scan1_0002_SAXS.dat"
    # same condition
    assert recipe_id_of(sname) == recipe_id_of(bname) == "auto_20260716_070621_8c3401"
    # but distinct averaging keywords
    assert condition_keyword(sname) == "auto_20260716_070621_8c3401_sample"
    assert condition_keyword(bname) == "auto_20260716_070621_8c3401_bkg"
    assert condition_keyword(sname) != condition_keyword(bname)


def test_role_detection():
    assert is_background("auto_1_bkg_0001.dat") is True
    assert is_background("auto_1_sample_0001.dat") is False
    assert split_role("auto_1_sample_0001.dat")[1] == "sample"


def test_recipe_id_not_confused_by_underscores_or_bg_substrings():
    # recipe_id itself has underscores; 'background' must win over 'bg'
    assert recipe_id_of("run_A_2026_background_0001.dat") == "run_A_2026"
    assert split_role("run_A_2026_background_0001.dat")[1] == "background"


def test_non_loop_file_has_no_keyword():
    assert condition_keyword("Nylon6_membrane_0004.dat") is None
    assert recipe_id_of("Nylon6_membrane_0004.dat") is None
