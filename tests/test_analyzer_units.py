"""
tests/test_analyzer_units.py — the analyzer normalizes q to nm⁻¹ before fitting.

Background can truncate subtracted files to Å⁻¹ (q_A-1) for the ML model; the
nanoparticle fit + optimizer target work in nm⁻¹, so the analyzer must detect the
unit from the header and convert, otherwise sizes come out 10× off.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import analyzer.app as az   # noqa: E402


def test_q_unit_detection():
    assert az._q_is_angstrom(["# SAXS data", "# Columns: q_A-1  I  sigma"])
    assert az._q_is_angstrom(["# q in A^-1"])
    assert not az._q_is_angstrom(["# Columns: q_nm-1  I  sigma"])
    assert not az._q_is_angstrom([])            # missing header → assume nm⁻¹ (default)
