"""
tests/test_truncation.py — ML truncate + rebin in the subtraction app.

Verifies the subtracted curve is truncated to the requested q-range and resampled
onto exactly N points, with the nm⁻¹→Å⁻¹ unit conversion and a linear grid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import background.app as bg   # noqa: E402


def _src():
    # source curve in nm⁻¹ spanning well beyond the target window (0.3–6 nm⁻¹)
    q_nm = np.linspace(0.1, 10.0, 2000)
    I = 100.0 / (1.0 + q_nm**2)      # smooth, positive
    sig = 0.01 * I
    return q_nm, I, sig


def test_truncate_rebin_linear_A_grid():
    q_nm, I, sig = _src()
    q, Ig, sg, _clipped, _rng = bg.truncate_rebin(q_nm, I, sig, 0.03, 0.6, 549,
                                  spacing="linear", q_unit="A")
    # exactly 549 points, exact endpoints, linear spacing, in Å⁻¹
    assert len(q) == 549 and len(Ig) == 549 and len(sg) == 549
    assert abs(q[0] - 0.03) < 1e-12 and abs(q[-1] - 0.6) < 1e-12
    dq = np.diff(q)
    assert np.allclose(dq, dq[0])                       # linear (constant Δq)
    assert np.all(Ig > 0)                               # intensity preserved positive


def test_truncate_rebin_unit_conversion_matches():
    # a target of 0.3 nm⁻¹ must equal 0.03 Å⁻¹ in intensity (same physical q)
    q_nm, I, sig = _src()
    qA, IA, *_ = bg.truncate_rebin(q_nm, I, sig, 0.03, 0.6, 549, "linear", "A")
    qn, In, *_ = bg.truncate_rebin(q_nm, I, sig, 0.3, 6.0, 549, "linear", "nm")
    assert abs(qA[0] * 10 - qn[0]) < 1e-9              # 0.03 Å⁻¹ == 0.3 nm⁻¹
    assert np.allclose(IA, In, rtol=1e-6)             # same intensities


def test_truncation_defaults():
    assert bg._TRUNC["q_min"] == 0.03 and bg._TRUNC["q_max"] == 0.6
    assert bg._TRUNC["n_points"] == 549 and bg._TRUNC["spacing"] == "linear"
    assert bg._TRUNC["q_unit"] == "A" and bg._TRUNC["enabled"] is True


def test_automated_subtract_applies_truncation(tmp_path):
    """The auto-subtraction worker (_process_one) must produce the same fixed
    ML grid as the manual routes — proving truncation is on in AUTOMATED mode."""
    saved = dict(bg._TRUNC)
    try:
        q_nm, I, sig = _src()
        bg._TRUNC["enabled"] = False                       # write untruncated inputs
        sam = tmp_path / "sample_avg.dat"
        bkf = tmp_path / "buffer_avg.dat"
        bg._write_dat(sam, q_nm, I * 1.05, sig)
        bg._write_dat(bkf, q_nm, I * 0.50, sig)
        bg._TRUNC.update(saved); bg._TRUNC["enabled"] = True   # ML truncation ON
        out_dir = tmp_path / "Subtracted"
        rec = bg._process_one(sam, bkf, out_dir, "saxs", scale_mode="fixed", fixed_scale=1.0)
        assert rec is not None
        data = np.loadtxt((out_dir / "sample_avg_sub.dat").as_posix(), comments="#")
        assert data.shape == (549, 3)                      # fixed ML grid in auto mode
        assert abs(data[0, 0] - 0.03) < 1e-9 and abs(data[-1, 0] - 0.6) < 1e-9
    finally:
        bg._TRUNC.clear(); bg._TRUNC.update(saved)


def test_write_dat_applies_truncation(tmp_path):
    q_nm, I, sig = _src()
    out = tmp_path / "s_sub.dat"
    qw, Iw, sw = bg._write_dat(out, q_nm, I, sig, ["# test"])
    assert len(qw) == 549                              # returned arrays are truncated
    txt = out.read_text()
    assert "q_A-1" in txt                              # header reflects Å⁻¹
    data = np.loadtxt(out.as_posix(), comments="#")
    assert data.shape == (549, 3)
    assert abs(data[0, 0] - 0.03) < 1e-9 and abs(data[-1, 0] - 0.6) < 1e-9


# ── regressions: the truncation must never invent data ───────────────────────
def test_window_is_clipped_to_the_measured_range_not_extrapolated():
    """np.interp holds the edge value flat outside the source range, so a window
    wider than the detector's coverage used to produce a long fabricated plateau
    (74% of the shipped default grid on a 3 m camera). That biased the fitted PDI
    ~2x and corrupted the confidence the optimizer gates on."""
    import numpy as np
    q_nm = np.linspace(0.1, 1.8, 400)          # real coverage
    I = 1e4 / (1 + (q_nm * 6.0) ** 4) + 30.0
    sig = np.sqrt(I)
    g, Ig, sg, clipped, (lo, hi) = bg.truncate_rebin(
        q_nm, I, sig, 0.03, 0.6, 549, "linear", "A")   # asks for 0.3-6.0 nm^-1
    assert clipped is True
    assert g.max() <= 0.18 + 1e-9, "grid extends past the measured q_max"
    assert (lo, hi) == pytest.approx((0.03, 0.18), abs=1e-6)
    # no flat plateau: every point is distinct
    assert len(np.unique(np.round(Ig, 9))) == len(Ig)


def test_a_window_with_no_overlap_is_refused():
    import numpy as np
    q_nm = np.linspace(0.1, 1.8, 200)
    I = np.ones_like(q_nm); sig = np.ones_like(q_nm)
    with pytest.raises(ValueError, match="does not overlap"):
        bg.truncate_rebin(q_nm, I, sig, 10.0, 20.0, 100, "linear", "A")


def test_label_reflects_what_was_written_not_the_config():
    """If truncation fails, nm^-1 data must NOT be labelled q_A-1 — the analyzer
    multiplies Angstrom data by 10 and would report a 10x-too-small radius."""
    import numpy as np
    bg._TRUNC.update(enabled=True, q_unit="A", q_min=0.03, q_max=0.6,
                     n_points=64, spacing="linear")
    q = np.linspace(0.1, 1.8, 50); I = np.ones_like(q); s = np.ones_like(q)
    orig = bg.truncate_rebin
    try:
        bg.truncate_rebin = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        q2, I2, s2, applied = bg._apply_truncation(q, I, s)
        assert applied is False
        assert np.allclose(q2, q), "arrays should be the untouched nm^-1 ones"
    finally:
        bg.truncate_rebin = orig
        bg._TRUNC["enabled"] = False
