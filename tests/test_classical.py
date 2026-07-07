"""
tests/test_classical.py
=======================
Classical-analysis core extensions: Porod invariant/volume, volume-of-correlation,
dimensionless Kratky, and Guinier QC. Validated against an analytic solid sphere
(Porod volume must recover the sphere volume).

scipy is stubbed (the analysis package imports it at load; these functions are
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
from src.analysis.core import (  # noqa: E402
    classical_invariants, dimensionless_kratky, guinier_quality,
)


def _sphere(R, qmax=8.0, n=4000):
    q = np.linspace(0.01, qmax, n)
    x = q * R
    F = 3 * (np.sin(x) - x * np.cos(x)) / x ** 3
    return q, F ** 2


def test_porod_volume_recovers_sphere_volume():
    R = 5.0
    q, I = _sphere(R)
    Rg = R * np.sqrt(3 / 5)
    inv = classical_invariants(q, I, Rg, I0=1.0)
    assert "error" not in inv
    v_true = 4 / 3 * np.pi * R ** 3
    assert abs(inv["porod_volume"] - v_true) / v_true < 0.05      # within 5%
    assert inv["Vc"] > 0 and inv["Qr"] > 0 and inv["porod_tail_reached"]


def test_dimensionless_kratky_peaks_near_root3():
    R = 5.0
    q, I = _sphere(R)
    Rg = R * np.sqrt(3 / 5)
    dk = dimensionless_kratky(q, I, Rg, I0=1.0)
    assert "error" not in dk
    assert 1.3 < dk["peak_qRg"] < 2.1            # near √3 ≈ 1.73


def test_dimensionless_kratky_needs_rg():
    assert "error" in dimensionless_kratky([0.1, 0.2, 0.3], [1, 0.5, 0.2], Rg=0, I0=1)


def test_guinier_quality_flags():
    Rg = 3.87
    good = guinier_quality({"Rg": Rg, "q_range": [0.3 / Rg, 1.2 / Rg], "r2": 0.999})
    assert good["verdict"] == "PASS"
    bad = guinier_quality({"Rg": Rg, "q_range": [0.05, 0.6], "r2": 0.97})
    assert bad["verdict"] == "WARN" and bad["warnings"]


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
