"""
tests/test_autopilot_loop.py — the closed loop end-to-end (in-process simulation).

Proves two things:
  1. A condition the optimizer writes is readable by the REACTOR's own parser
     (the cross-app data contract that lets the loop actually close).
  2. Driving analyzer results back into the campaign converges on a reachable
     target — i.e. propose → "measure" → tell → propose … terminates correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.optimizer import ParameterSpace, CampaignController          # noqa: E402
from src.optimizer.io import to_param_file, match_recipe_id           # noqa: E402
from src.reactor.recipe import parse_param_file, Recipe               # noqa: E402

_CFG = {"bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
                   "x_each": [0, 0.3], "x_sum_max": 0.9}}


def test_condition_file_readable_by_reactor():
    params = {"T_reac": 245.0, "F_tot": 80.0, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1}
    text = to_param_file("auto_20260101_000000_abcd", params)
    parsed = parse_param_file(text)                 # the reactor's own parser
    rec = Recipe.from_dict(parsed)                   # and its recipe validation
    assert rec.recipe_id == "auto_20260101_000000_abcd"
    assert abs(rec.T_reac - 245.0) < 1e-6 and abs(rec.F_tot - 80.0) < 1e-6
    assert abs(rec.x_ODE - 0.2) < 1e-6


def _truth(p):                                        # reachable target ~ T280/F40 → 5 nm
    return 2.0 + 0.03 * (p["T_reac"] - 180) - 0.01 * (p["F_tot"] - 40), 0.06


def test_closed_loop_converges_via_file_handshake():
    space = ParameterSpace.from_config(_CFG)
    camp = CampaignController(space, target_size=5.0, tolerance=0.25, pdi_cap=0.2,
                              budget=40, n_init=10, seed=1)
    camp.start()
    pending: dict = {}
    i = 0

    def propose():
        nonlocal i
        p = camp.ask()
        if p is None:
            return False
        rid = f"auto_{i:03d}"; i += 1
        # round-trip through the file format the reactor would consume
        parsed = parse_param_file(to_param_file(rid, p))
        pending[rid] = {k: parsed[k] and float(parsed[k]) for k in
                        ("T_reac", "F_tot", "x_ODE", "x_TOP", "x_oley")}
        return True

    propose()                                         # first condition
    guard = 0
    while camp.status_str == "running" and guard < 100:
        guard += 1
        rid, params = next(iter(pending.items()))
        pending.pop(rid)
        # a measured profile carries the recipe id in its filename
        meas_name = f"Run_{rid}_x-12.3_Average_sub.dat"
        assert match_recipe_id(meas_name, [rid]) == rid
        size, pdi = _truth(params)
        camp.tell(params, size, pdi, confidence=0.9)
        if camp.status_str == "running":
            propose()

    assert camp.status_str == "converged"
    assert abs(camp.converged_condition["size"] - 5.0) <= 0.25
