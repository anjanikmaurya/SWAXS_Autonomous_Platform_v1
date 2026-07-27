"""
tests/test_preprocess.py — raw→CBF conversion + calibration helpers (calibration app).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.preprocess import (   # noqa: E402
    DEFAULT_SHAPES, find_raw_files, detect_shape, read_raw, frame_stats,
    raw_to_cbf, convert_dir, CALIBRANTS, build_calib2_command, list_poni_files,
)

SAXS = DEFAULT_SHAPES["SAXS"]      # (1043, 981)
WAXS = DEFAULT_SHAPES["WAXS"]      # (195, 487)


def _write_raw(path, shape, val=7):
    arr = np.full(shape[0] * shape[1], val, dtype=np.int32)
    arr[:10] = np.arange(10)                       # a little structure
    arr.tofile(str(path))


def test_keyword_filter_and_shape_detect(tmp_path):
    _write_raw(tmp_path / "out_AgBeh_12keV_scan1_0000.raw", SAXS)
    _write_raw(tmp_path / "LaB6_waxs_0000.raw", WAXS)
    _write_raw(tmp_path / "sample_water_0000.raw", SAXS)
    # keyword filter (case-insensitive, any-match)
    assert find_raw_files(tmp_path, ["agbeh", "lab6"]) == \
        ["LaB6_waxs_0000.raw", "out_AgBeh_12keV_scan1_0000.raw"]
    assert len(find_raw_files(tmp_path, None)) == 3      # no keywords → all
    # shape detection by pixel count
    assert detect_shape(SAXS[0] * SAXS[1])[0] == "SAXS"
    assert detect_shape(WAXS[0] * WAXS[1])[0] == "WAXS"
    assert detect_shape(12345)[0] is None                # unknown size


def test_read_raw_and_stats(tmp_path):
    _write_raw(tmp_path / "s_SAXS_0000.raw", SAXS, val=3)
    det, data = read_raw(tmp_path / "s_SAXS_0000.raw")
    assert det == "SAXS" and data.shape == SAXS
    st = frame_stats(data)
    assert st["max"] == 9 and st["shape"] == [SAXS[0], SAXS[1]]


def test_raw_to_cbf_roundtrip(tmp_path):
    fabio = pytest.importorskip("fabio")
    _write_raw(tmp_path / "cal_AgBeh_SAXS_0000.raw", SAXS, val=5)
    res = raw_to_cbf(tmp_path / "cal_AgBeh_SAXS_0000.raw", tmp_path / "cbf_output")
    assert res["ok"] and res["detector"] == "SAXS"
    out = Path(res["out"]); assert out.exists() and out.suffix == ".cbf"
    back = fabio.open(str(out)).data
    assert back.shape == SAXS and int(back.max()) == 9   # seeded pixels 0..9


def test_convert_dir_failsoft(tmp_path):
    pytest.importorskip("fabio")
    _write_raw(tmp_path / "AgBeh_SAXS_0000.raw", SAXS)
    (tmp_path / "AgBeh_bad_0000.raw").write_bytes(b"\x01\x02\x03\x04\x05")   # wrong size
    results, out_dir = convert_dir(tmp_path, ["agbeh"])
    ok = [r for r in results if r.get("ok")]
    bad = [r for r in results if not r.get("ok")]
    assert len(ok) == 1 and len(bad) == 1 and "unexpected size" in bad[0]["error"]


def test_calib_helpers(tmp_path):
    assert "AgBehenate" in CALIBRANTS and "LaB6" in CALIBRANTS
    cmd = build_calib2_command("/x/img.cbf", "AgBehenate", 12.0, pixel_m=172e-6)
    assert cmd[0] == "pyFAI-calib2" and "--calibrant" in cmd and "AgBehenate" in cmd
    assert "12.0" in cmd and cmd[-1] == "/x/img.cbf"
    assert list_poni_files(tmp_path) == []               # empty dir
    (tmp_path / "atT_SAXS.poni").write_text("# poni\n")
    got = list_poni_files(tmp_path)
    assert len(got) == 1 and got[0]["name"] == "atT_SAXS.poni"
