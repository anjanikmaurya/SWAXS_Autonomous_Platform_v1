"""
tests/test_analysis_io.py
=========================
Foundation for the analysis-app redesign: saving results to Analysed/, source
.dat annotation, manifest registration, and the batch summary table.

scipy is stubbed (the analysis package imports it at module load; the io layer
itself is numpy/stdlib only).

Run:
    python tests/test_analysis_io.py
    uv run pytest tests/test_analysis_io.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
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
from src.analysis import io  # noqa: E402


def _project_with_subtracted():
    d = Path(tempfile.mkdtemp())
    sub = d / "1D" / "SAXS" / "Subtracted"
    sub.mkdir(parents=True)
    src = sub / "BSA_sub_SAXS.dat"
    q = np.linspace(0.05, 3, 120)
    with src.open("w") as f:
        f.write("# Subtracted SAXS\n# q_nm-1  I  sigma\n")
        for a in q:
            f.write(f"{a:.5e} {a**-2:.5e} {a**-2*0.03:.5e}\n")
    return d, src, q


def test_save_analysis_writes_bundle_to_analysed():
    d, src, q = _project_with_subtracted()
    res = {"Rg": 3.21, "I0": 0.0142, "chi2": 1.05, "q_range": [0.05, 0.3],
           "pr_array": [1, 2, 3]}        # array must be dropped from scalars
    out = io.save_analysis(d, src, "saxs", "guinier", {"auto_range": True}, res,
                           fit_curve=(q.tolist(), (q**-2).tolist()), user="t")
    analysed = d / "1D" / "SAXS" / "Analysed" / "Guinier"
    assert (analysed / "BSA_sub_SAXS_guinier.json").is_file()
    assert (analysed / "BSA_sub_SAXS_guinier_fit.dat").is_file()
    rec = json.loads((analysed / "BSA_sub_SAXS_guinier.json").read_text())
    assert rec["results"]["Rg"] == 3.21 and "pr_array" not in rec["results"]


def test_source_dat_annotated_idempotently():
    d, src, q = _project_with_subtracted()
    io.save_analysis(d, src, "saxs", "guinier", {}, {"Rg": 3.21}, user="t")
    foot = src.read_text()
    assert "# ANALYSIS INFORMATION" in foot and "# guinier.Rg: 3.21" in foot
    # re-run updates in place, does not duplicate
    io.save_analysis(d, src, "saxs", "guinier", {}, {"Rg": 3.50}, user="t")
    foot2 = src.read_text()
    assert foot2.count("# guinier.Rg") == 1 and "# guinier.Rg: 3.5" in foot2


def test_registered_in_manifest():
    d, src, q = _project_with_subtracted()
    io.save_analysis(d, src, "saxs", "guinier", {}, {"Rg": 3.2, "q_range": [0.05, 0.3]},
                     user="t")
    mf = json.loads((d / "manifest.json").read_text())
    assert len(mf.get("analyses", {})) == 1
    entry = next(iter(mf["analyses"].values()))
    assert entry["type"] == "guinier" and entry["results"]["Rg"] == 3.2


def test_batch_summary_csv_and_xlsx():
    d, src, q = _project_with_subtracted()
    out_dir = d / "1D" / "SAXS" / "Analysed" / "Guinier"
    rows = [{"file": "a", "Rg": 3.2}, {"file": "b", "Rg": 4.1, "I0": 0.02}]
    bs = io.write_batch_summary(out_dir, "guinier", rows)
    assert Path(bs["csv"]).is_file()
    header = Path(bs["csv"]).read_text().splitlines()[0]
    assert header == "file,Rg,I0"          # union of keys, first-seen order
    if bs["xlsx"]:
        assert Path(bs["xlsx"]).read_bytes()[:2] == b"PK"


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
