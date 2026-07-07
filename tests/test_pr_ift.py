"""
tests/test_pr_ift.py
====================
Validate the pair-distance distribution p(r) indirect Fourier transform
(`src.analysis.core.pair_distance_ift`) against an analytic solid sphere, for
which Rg = R·sqrt(3/5) and Dmax = 2R.

The analysis module imports scipy at top level; we stub it (the IFT itself is
numpy-only) so the suite runs without the heavy stack.

Run:
    python tests/test_pr_ift.py
    uv run pytest tests/test_pr_ift.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Stub scipy.optimize / scipy.stats so `import src.analysis.core` succeeds.
if "scipy" not in sys.modules:
    _sp = types.ModuleType("scipy"); _sp.__path__ = []
    _opt = types.ModuleType("scipy.optimize")
    _opt.curve_fit = lambda *a, **k: (None, None)
    _opt.minimize = lambda *a, **k: None
    _st = types.ModuleType("scipy.stats")
    _st.linregress = lambda *a, **k: None
    sys.modules["scipy"] = _sp
    sys.modules["scipy.optimize"] = _opt
    sys.modules["scipy.stats"] = _st

import numpy as np  # noqa: E402
from src.analysis.core import pair_distance_ift  # noqa: E402


def _sphere_I(q, R):
    x = q * R
    F = 3 * (np.sin(x) - x * np.cos(x)) / x**3
    return F**2


def test_pr_sphere_rg_and_dmax():
    R = 8.0
    q = np.linspace(0.02, 2.0, 400)
    I = _sphere_I(q, R)
    sigma = I * 0.01 + 1e-9
    res = pair_distance_ift(q, I, sigma, dmax=2 * R)
    assert "error" not in res, res
    Rg_true = R * np.sqrt(3 / 5)
    # within 3% of the analytic Rg
    assert abs(res["Rg"] - Rg_true) / Rg_true < 0.03, (res["Rg"], Rg_true)
    assert abs(res["Dmax"] - 2 * R) < 1e-6
    # p(r) returns to zero at both ends
    pr = np.array(res["pr"])
    assert pr[0] == 0.0 and pr[-1] == 0.0
    assert (pr >= 0).all(), "p(r) must be non-negative"


def test_pr_auto_dmax_runs():
    R = 5.0
    q = np.linspace(0.03, 2.0, 300)
    I = _sphere_I(q, R)
    res = pair_distance_ift(q, I, None, dmax=None)
    assert "error" not in res
    assert res["Dmax"] > 0 and res["Rg"] > 0


def test_pr_too_few_points():
    res = pair_distance_ift(np.array([0.1, 0.2]), np.array([1.0, 0.5]), None)
    assert "error" in res


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
