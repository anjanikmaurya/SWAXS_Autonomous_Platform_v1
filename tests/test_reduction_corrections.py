"""
tests/test_reduction_corrections.py
===================================
Unit tests for the SAXS/WAXS reduction correction math in
``src/reduction/core.py`` — transmission, normalization factors (bstop / i0 /
absolute), air-path correction, Beer–Lambert thickness, the overlapping-mode
guard, and bad-diode handling.

These tests exercise the *pure* correction logic only. The heavy optional
dependencies imported at the top of ``core.py`` (fabio, pyFAI, xraydb, pandas)
are replaced with lightweight stubs so the suite runs anywhere — no detector
libraries or calibration files required.

Run:
    uv run pytest tests/test_reduction_corrections.py        # in the project venv
    python tests/test_reduction_corrections.py               # standalone (numpy only)
"""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path

# ── Make `import src.reduction.core` work without the heavy stack ──────────────
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _name in ("fabio", "pyFAI", "pandas"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

# xraydb stub with a controllable material_mu (cm⁻¹). Default 10 cm⁻¹.
#
# This used to be installed only `if "xraydb" not in sys.modules`, which made the
# test mean DIFFERENT things depending on import order. When a sibling test had
# already imported the REAL xraydb (test_simulator_reduction_metadata needs it),
# the stub was skipped, `material_mu` returned the true coefficient for H2O at
# 12 keV, and the Beer–Lambert assertions — which hard-code µ = 10 cm⁻¹ — failed
# with a confusing 3.2× mismatch that looks like a science bug in the product.
# The dangerous half is the opposite case: a test whose reference value silently
# depends on import order can also PASS while the code under test is wrong.
#
# The fixture below pins `core`'s view of material_mu for every test in this
# module, whether the stub or the real library is loaded, and restores it after.
if "xraydb" not in sys.modules:
    _xr = types.ModuleType("xraydb")
    _xr._MU_CM = 10.0
    _xr.material_mu = lambda formula, energy=None, density=None, **kw: _xr._MU_CM
    sys.modules["xraydb"] = _xr

import pytest  # noqa: E402

from src.reduction import core  # noqa: E402

#: µ in cm⁻¹ that every Beer–Lambert expectation in this file is computed from.
MU_CM = 10.0


@pytest.fixture(autouse=True)
def _pin_material_mu():
    """Make µ deterministic regardless of which xraydb is in sys.modules."""
    xr = core.xraydb
    had = hasattr(xr, "material_mu")
    prev = getattr(xr, "material_mu", None)
    prev_mu = getattr(xr, "_MU_CM", None)
    xr._MU_CM = MU_CM
    xr.material_mu = lambda formula, energy=None, density=None, **kw: xr._MU_CM
    try:
        yield
    finally:
        if had:
            xr.material_mu = prev
        else:
            delattr(xr, "material_mu")
        if prev_mu is None:
            if hasattr(xr, "_MU_CM"):
                delattr(xr, "_MU_CM")
        else:
            xr._MU_CM = prev_mu

RAW = Path("sample_0001.raw")
TOL = 1e-6


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_exp(**kw):
    """Build an Experiment without running __init__ / loading PyFAI."""
    e = core.Experiment.__new__(core.Experiment)
    e._logs = []
    e._log = lambda msg, tag="info": e._logs.append((tag, msg))
    e.i0_offset         = kw.get("i0_offset", 0.0)
    e.bstop_offset      = kw.get("bstop_offset", 0.0)
    e.i0_air            = kw.get("i0_air", 0.0)
    e.bstop_air         = kw.get("bstop_air", 0.0)
    e.thickness         = kw.get("thickness", 0.001)   # metres (explicit by default)
    e.compound          = kw.get("compound", "H2O")
    e.energy_keV        = kw.get("energy_keV", 12.0)
    e.density_g_cm3     = kw.get("density_g_cm3", 1.0)
    e.normalization     = kw.get("normalization", ["bstop"])
    e.calibration_factor = kw.get("calibration_factor", 1.0)
    return e


def approx(a, b, tol=TOL):
    assert abs(a - b) <= tol * max(1.0, abs(b)), f"{a} != {b} (tol {tol})"


def expect_raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    raise AssertionError(f"expected {exc.__name__} but none was raised")


# ── resolve_normalization (overlap guard, audit 3.1) ──────────────────────────

def test_resolve_single_modes():
    assert core.resolve_normalization(["bstop"])    == (["bstop"], [])
    assert core.resolve_normalization("i0")          == (["i0"], [])
    assert core.resolve_normalization(["absolute"])  == (["absolute"], [])
    assert core.resolve_normalization(None)          == (["bstop"], [])

def test_resolve_dedupe():
    assert core.resolve_normalization(["bstop", "bstop"]) == (["bstop"], [])

def test_resolve_absolute_absorbs_others():
    res, warn = core.resolve_normalization(["bstop", "absolute"])
    assert res == ["absolute"]
    assert len(warn) == 1
    res, warn = core.resolve_normalization(["i0", "absolute"])
    assert res == ["absolute"] and len(warn) == 1

def test_resolve_i0_plus_bstop_collapses_to_bstop():
    res, warn = core.resolve_normalization(["i0", "bstop"])
    assert res == ["bstop"]
    assert len(warn) == 1

def test_resolve_unknown_term_dropped():
    res, warn = core.resolve_normalization(["foo", "bstop"])
    assert res == ["bstop"]
    assert any("Unknown" in w for w in warn)


# ── Transmission ──────────────────────────────────────────────────────────────

def test_transmission_basic():
    e = make_exp()
    c = e._compute_corrections({"i0": 1000.0, "bstop": 800.0}, RAW)
    approx(c["transmission"], 0.8)
    approx(c["i0_corrected"], 1000.0)
    approx(c["bstop_corrected"], 800.0)

def test_transmission_with_offsets():
    e = make_exp(i0_offset=50.0, bstop_offset=5.0)
    c = e._compute_corrections({"i0": 1000.0, "bstop": 800.0}, RAW)
    approx(c["i0_corrected"], 950.0)
    approx(c["bstop_corrected"], 795.0)
    approx(c["transmission"], 795.0 / 950.0)

def test_transmission_gt_one_is_clipped():
    e = make_exp()
    c = e._compute_corrections({"i0": 1000.0, "bstop": 1200.0}, RAW)
    approx(c["transmission"], 1.0)


# ── T > 1 warning is scoped to the selected normalization / thickness ─────────
# `transmission` feeds only the Beer-Lambert thickness (when `thickness` is not
# configured). norm_factor never reads it, so warning otherwise is just noise.

def _warns(e):
    return [(t, m) for t, m in e._logs if t in ("warn", "error") and "T_sample" in m]

def test_t_gt_one_silent_when_thickness_explicit():
    """bstop/i0 mode + explicit thickness → T is provenance-only, no warning."""
    for norm in (["bstop"], ["i0"], ["absolute"]):
        e = make_exp(thickness=0.001, normalization=norm)
        e._compute_corrections({"i0": 1000.0, "bstop": 1800.0}, RAW)
        assert _warns(e) == [], f"unexpected T warning for {norm} with explicit thickness"

def test_t_gt_one_warns_when_thickness_auto_derived():
    e = make_exp(thickness=None, normalization=["bstop"])
    c = e._compute_corrections({"i0": 1000.0, "bstop": 1800.0}, RAW)
    w = _warns(e)
    assert len(w) == 1 and w[0][0] == "warn"
    # names the real cause (no air configured) — not "check air measurement values"
    assert "no air measurement is configured" in w[0][1]
    assert "I(q) is unaffected" in w[0][1] and "'bstop'" in w[0][1]
    approx(c["transmission"], 1.0)

def test_t_gt_one_is_an_error_for_absolute_with_auto_thickness():
    """Here it really does corrupt the data: NF collapses to the t_cm floor."""
    e = make_exp(thickness=None, normalization=["absolute"])
    e._compute_corrections({"i0": 1000.0, "bstop": 1800.0}, RAW)
    w = _warns(e)
    assert len(w) == 1 and w[0][0] == "error"
    assert "ABSOLUTE CALIBRATION WILL BE WRONG" in w[0][1]

def test_t_gt_one_blames_air_values_only_when_air_configured():
    e = make_exp(thickness=None, normalization=["bstop"],
                 i0_air=1000.0, bstop_air=400.0)
    e._compute_corrections({"i0": 1000.0, "bstop": 1800.0}, RAW)
    w = _warns(e)
    assert len(w) == 1 and "check the air measurement values" in w[0][1]

def test_t_below_one_never_warns():
    e = make_exp(thickness=None, normalization=["bstop"])
    c = e._compute_corrections({"i0": 1000.0, "bstop": 600.0}, RAW)
    assert _warns(e) == []
    approx(c["transmission"], 0.6)


# ── Normalization factors ─────────────────────────────────────────────────────

def test_bstop_normalization():
    e = make_exp(normalization=["bstop"])
    c = e._compute_corrections({"i0": 1000.0, "bstop": 800.0}, RAW)
    approx(c["normalization_factor"], 800.0)

def test_i0_normalization():
    e = make_exp(normalization=["i0"])
    c = e._compute_corrections({"i0": 1000.0, "bstop": 800.0}, RAW)
    approx(c["normalization_factor"], 1000.0)

def test_absolute_normalization():
    # thickness 0.001 m -> 0.1 cm; NF = bstop * d_cm / K = 800 * 0.1 / 1 = 80
    e = make_exp(normalization=["absolute"], thickness=0.001, calibration_factor=1.0)
    c = e._compute_corrections({"i0": 1000.0, "bstop": 800.0}, RAW)
    approx(c["normalization_factor"], 80.0)

def test_absolute_with_calibration_K():
    # NF = bstop * d_cm / K = 800 * 0.1 / 2 = 40
    e = make_exp(normalization=["absolute"], thickness=0.001, calibration_factor=2.0)
    c = e._compute_corrections({"i0": 1000.0, "bstop": 800.0}, RAW)
    approx(c["normalization_factor"], 40.0)


# ── Air-path correction ───────────────────────────────────────────────────────

def test_air_path_transmission_and_bstop_norm():
    # air transmission 0.9; raw sample T = 720/1000 = 0.72; true T = 0.72/0.9 = 0.8
    e = make_exp(normalization=["bstop"], i0_air=1000.0, bstop_air=900.0)
    c = e._compute_corrections({"i0": 1000.0, "bstop": 720.0}, RAW)
    approx(c["transmission"], 0.8)
    # bstop_norm = bstop * (i0_air/bstop_air) = 720 * (1000/900) = 800 = I0 * T_sample
    approx(c["normalization_factor"], 800.0)

def test_absolute_is_air_corrected():
    # AUDIT FIX 3.2: absolute must use the air-corrected flux (bstop_norm), not raw bstop.
    # bstop_norm = 800, d_cm = 0.1, K = 1 -> NF = 80 (NOT 720*0.1 = 72)
    e = make_exp(normalization=["absolute"], thickness=0.001,
                 i0_air=1000.0, bstop_air=900.0)
    c = e._compute_corrections({"i0": 1000.0, "bstop": 720.0}, RAW)
    approx(c["normalization_factor"], 80.0)


# ── Beer–Lambert thickness (auto) ─────────────────────────────────────────────

def test_auto_thickness_beer_lambert():
    # thickness=None -> d_m = -ln(T)/mu_m, mu_m = mu_cm*100, stub mu_cm = 10
    sys.modules["xraydb"]._MU_CM = 10.0
    e = make_exp(normalization=["bstop"], thickness=None)
    c = e._compute_corrections({"i0": 1000.0, "bstop": 800.0}, RAW)
    expected_m = -math.log(0.8) / (10.0 * 100.0)
    approx(c["thickness_m"], expected_m, tol=1e-9)


# ── Bad-diode handling (audit 3.7) ────────────────────────────────────────────

def test_nonpositive_i0_raises():
    e = make_exp(i0_offset=50.0)   # 40 - 50 = -10
    expect_raises(ValueError, e._compute_corrections, {"i0": 40.0, "bstop": 800.0}, RAW)

def test_nonpositive_bstop_raises():
    e = make_exp(bstop_offset=900.0)   # 800 - 900 < 0
    expect_raises(ValueError, e._compute_corrections, {"i0": 1000.0, "bstop": 800.0}, RAW)


# ── Provenance captures the operator/user (user-capture feature) ──────────────

def test_make_provenance_records_user():
    from src import manifest
    p = manifest.make_provenance("reduction", user="albert",
                                 input_files=[], config={"npt_radial": 1000})
    assert p["user"] == "albert"
    assert p["app"] == "reduction"
    assert p["run_id"] and p["timestamp"]
    # default is empty string, not missing
    assert manifest.make_provenance("reduction")["user"] == ""


# ── Standalone runner (works without pytest) ──────────────────────────────────

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
