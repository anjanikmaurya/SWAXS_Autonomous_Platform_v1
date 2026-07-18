"""
tests/test_truncation.py — ML truncate + rebin in the subtraction app.

Verifies the subtracted curve is truncated to the requested q-range and resampled
onto exactly N points, with the nm⁻¹→Å⁻¹ unit conversion and a linear grid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

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
    q, Ig, sg = bg.truncate_rebin(q_nm, I, sig, 0.03, 0.6, 549,
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
    qA, IA, _ = bg.truncate_rebin(q_nm, I, sig, 0.03, 0.6, 549, "linear", "A")
    qn, In, _ = bg.truncate_rebin(q_nm, I, sig, 0.3, 6.0, 549, "linear", "nm")
    assert abs(qA[0] * 10 - qn[0]) < 1e-9              # 0.03 Å⁻¹ == 0.3 nm⁻¹
    assert np.allclose(IA, In, rtol=1e-6)             # same intensities


def test_truncation_defaults():
    assert bg._TRUNC["q_min"] == 0.03 and bg._TRUNC["q_max"] == 0.6
    assert bg._TRUNC["n_points"] == 549 and bg._TRUNC["spacing"] == "linear"
    assert bg._TRUNC["q_unit"] == "A" and bg._TRUNC["enabled"] is True


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
