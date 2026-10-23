"""The fitter benchmark (src/fitting/benchmark.py, tools/fitter_benchmark.py)."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("sasmodels")

from src.fitting import benchmark as B  # noqa: E402


def test_cases_are_deterministic_and_splits_never_share_a_case():
    assert B.make_case("practice", 3) == B.make_case("practice", 3)
    prac = {json.dumps(B.make_case("practice", i)["params"], sort_keys=True) for i in range(25)}
    held = {json.dumps(B.make_case("heldout", i)["params"], sort_keys=True) for i in range(25)}
    assert not prac & held
    assert [B.make_case("practice", i)["shape"] for i in range(5)] == list(B.SHAPES)


def test_rendered_curve_and_truth_are_sane():
    c = B.make_case("practice", 0)                   # a sphere
    q, I, s, t = B.render(c)
    assert len(q) == c["n_q"] and np.all(I > 0) and np.all(s > 0)
    assert t["R_eq"] == pytest.approx(c["params"]["radius"])
    cyl = B.make_case("practice", 3)                 # a cylinder: R_eq from its Rg
    assert cyl["shape"] == "cylinder"
    assert B.true_equivalent_radius(cyl) > cyl["params"]["radius"]


def test_scoring():
    rows = [
        {"case": "a", "method": "m", "ok": True, "true_shape": "sphere", "pred_shape": "sphere",
         "true_R": 5, "pred_R": 5.5, "true_pdi": 0.1, "pred_pdi": 0.12, "confidence": 0.9, "seconds": 1},
        {"case": "b", "method": "m", "ok": True, "true_shape": "cylinder", "pred_shape": "sphere",
         "true_R": 10, "pred_R": 6, "true_pdi": 0.1, "pred_pdi": 0.2, "confidence": 0.2, "seconds": 3},
        {"case": "c", "method": "m", "ok": False, "true_shape": "sphere", "seconds": 0},
    ]
    s = B.score(rows)["m"]
    assert s["n_cases"] == 3 and s["n_failed"] == 1
    assert s["shape_acc"] == 0.5
    assert s["size_err_median"] == pytest.approx(0.25)
    assert s["shape_acc_when_confident"] == 1.0
    assert s["confusion"] == {"cylinder->sphere": 1, "sphere->sphere": 1}
    assert "| `m` |" in B.report_markdown(B.score(rows))


def test_run_case_scores_the_sphere_fit_on_a_sphere():
    rows = B.run_case(B.make_case("practice", 0), ["ls_sphere"])
    (r,) = rows
    assert r["ok"] and r["pred_shape"] == "sphere"
    assert abs(r["pred_R"] - r["true_R"]) / r["true_R"] < 0.1


def test_cli_resumes_and_writes_a_report(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import fitter_benchmark as cli
    out = tmp_path / "b.jsonl"
    cli.main(["--n", "2", "--methods", "ls_sphere", "--workers", "1", "--out", str(out)])
    n1 = len(out.read_text().splitlines())
    cli.main(["--n", "2", "--methods", "ls_sphere", "--workers", "1", "--out", str(out)])
    assert len(out.read_text().splitlines()) == n1 == 2          # nothing re-run
    assert "Fitter benchmark" in out.with_suffix(".md").read_text()
