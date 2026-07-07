"""
tests/test_atsas.py
===================
ATSAS wrappers: availability detection, graceful behaviour when binaries are
absent, and the output parsers (GNOM .out → Dmax/Rg/p(r); autorg CSV; number
extraction). The actual binaries aren't run here — parsing is validated against
representative ATSAS-format text.

scipy is stubbed (the analysis package imports it at load; atsas.py is stdlib).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if "scipy" not in sys.modules:
    _sp = types.ModuleType("scipy"); _sp.__path__ = []
    for _m, _fns in {"scipy.optimize": ["curve_fit", "minimize"],
                     "scipy.stats": ["linregress"]}.items():
        _mod = types.ModuleType(_m)
        for _fn in _fns:
            setattr(_mod, _fn, lambda *a, **k: None)
        sys.modules[_m] = _mod
    sys.modules["scipy"] = _sp

from src.analysis import atsas  # noqa: E402

_GNOM = """   Total  estimate : 0.85  which is  A GOOD  solution
   Reciprocal space: Rg =   3.21    , I(0) =   1.42E-02
   Real space: Rg =   3.25 +- 0.02  I(0) =   1.43E-02 +- 1.0E-04

####      Distance distribution  function of particle       ####

   R          P(R)      ERROR
  0.0000   0.0000E+00  0.0000E+00
  2.0000   8.0000E-02  1.0E-03
  4.0000   1.2000E-01  1.0E-03
  8.0000   0.0000E+00  0.0000E+00
"""


def test_available_returns_all_tools():
    av = atsas.available()
    for t in ("autorg", "datgnom", "datporod", "datvc", "datmw", "dammif"):
        assert t in av                       # value is path or None


def test_runners_graceful_without_binaries():
    # In CI/sandbox ATSAS isn't installed → must return a clean error, not raise.
    if atsas.available()["autorg"] is None:
        r = atsas.run_autorg("nope.dat")
        assert "error" in r and "autorg" in r["error"]


def test_parse_gnom_out():
    g = atsas._parse_gnom_out(_GNOM)
    assert g["Dmax"] == 8.0
    assert abs(g["Rg_real"] - 3.25) < 1e-6
    assert len(g["r"]) == 4 and max(g["pr"]) == 0.12


def test_first_float_after():
    assert atsas._first_float_after("Vc = 11.2 nm^2", "Vc") == 11.2
    assert atsas._first_float_after("MW = 66.5 kDa", "MW", "Mass") == 66.5
    assert atsas._first_float_after("no number here", "Rg") is None


if __name__ == "__main__":
    tests = sorted(n for n in globals() if n.startswith("test_"))
    passed = failed = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {name}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed  ({len(tests)} total)")
    sys.exit(1 if failed else 0)
