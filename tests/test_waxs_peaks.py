"""
tests/test_waxs_peaks.py
========================
WAXS peak-fitting helpers: numpy-only peak auto-detection and the per-shape
area/FWHM formulas (Gaussian / Lorentzian / pseudo-Voigt). The full peak_fit
needs scipy.curve_fit (validated separately in the venv); here we lock the
detection and the analytic area relations.

scipy is stubbed (analysis package imports it at load; these helpers are
numpy-only).
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

import numpy as np  # noqa: E402
from src.analysis.core import _detect_peaks, _peak_shapes  # noqa: E402

# NumPy 2.0 renamed trapz -> trapezoid (old name deprecated).
_trapezoid = getattr(np, "trapezoid", getattr(np, "trapz", None))


def _two_peaks():
    q = np.linspace(8, 30, 500)
    g = lambda A, q0, f: A * np.exp(-4 * np.log(2) * ((q - q0) / f) ** 2)
    I = 2.0 + g(50, 15.0, 1.2) + g(30, 22.0, 1.6)
    return q, I


def test_detect_finds_both_peaks():
    q, I = _two_peaks()
    centers = _detect_peaks(q, I, max_peaks=6)
    assert len(centers) == 2
    assert abs(centers[0] - 15.0) < 0.3 and abs(centers[1] - 22.0) < 0.3


def test_detect_respects_max_peaks():
    q, I = _two_peaks()
    assert len(_detect_peaks(q, I, max_peaks=1)) == 1


def test_shape_areas_match_numeric_integral():
    # Lorentzian/Voigt have heavy tails — integrate over a wide range to capture them.
    q = np.linspace(-2000, 2000, 800001)
    A, q0, f = 3.0, 0.0, 2.0
    for shape in ("gaussian", "lorentzian"):
        npar, unit, area = _peak_shapes(shape)
        num = _trapezoid(A * unit(q, q0, f), q)
        assert abs(area(A, f) - num) / num < 0.02, (shape, area(A, f), num)
    _, unitv, areav = _peak_shapes("voigt")
    for eta in (0.0, 0.5, 1.0):
        num = _trapezoid(A * unitv(q, q0, f, eta), q)
        assert abs(areav(A, f, eta) - num) / num < 0.02, (eta, areav(A, f, eta), num)


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
