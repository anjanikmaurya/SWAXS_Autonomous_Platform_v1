"""
tests/test_optimizer.py — the BO campaign controller (decision half of the loop).

Checks the parameter space respects the reactor's bounds+constraint, that a
reachable size target is FOUND, an unreachable one EXHAUSTS the budget (no false
convergence), and the GP proposal path runs past the cold-start.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.optimizer import ParameterSpace, CampaignController, NAMES   # noqa: E402

_CFG = {"bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
                   "x_each": [0, 0.3], "x_sum_max": 0.9}}


def _truth(p):
    size = 2.0 + 0.03 * (p["T_reac"] - 180) - 0.01 * (p["F_tot"] - 40)
    pdi = 0.05 + 0.002 * (p["F_tot"] - 40)
    return size, pdi


def _run(target, tol, budget, n_init, seed):
    sp = ParameterSpace.from_config(_CFG)
    c = CampaignController(sp, target_size=target, tolerance=tol, pdi_cap=0.2,
                           budget=budget, n_init=n_init, seed=seed)
    c.start()
    while True:
        p = c.ask()
        if p is None:
            break
        s, pdi = _truth(p)
        c.tell(p, s, pdi, confidence=0.9)
    return c


def test_space_respects_bounds_and_constraint():
    sp = ParameterSpace.from_config(_CFG)
    assert sp.valid({"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1})
    assert not sp.valid({"T_reac": 400, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1})
    assert not sp.valid({"T_reac": 240, "F_tot": 80, "x_ODE": 0.3, "x_TOP": 0.3, "x_oley": 0.31})
    pts = sp.sobol(12, seed=0)
    assert len(pts) == 12 and all(sp.valid(p) for p in pts)


def test_converges_on_reachable_target():
    c = _run(target=5.0, tol=0.25, budget=40, n_init=10, seed=1)
    st = c.status()
    assert st["status"] == "converged"
    assert abs(st["converged_condition"]["size"] - 5.0) <= 0.25


def test_exhausts_on_unreachable_target():
    c = _run(target=50.0, tol=0.1, budget=15, n_init=8, seed=2)
    assert c.status()["status"] == "exhausted"
    assert c.status()["n_evaluations"] == 15


def test_bo_suggestions_are_valid_after_coldstart():
    sp = ParameterSpace.from_config(_CFG)
    c = CampaignController(sp, target_size=6.0, tolerance=0.05, pdi_cap=0.2,
                           budget=20, n_init=6, seed=3)
    c.start()
    seen_bo = 0
    for _ in range(14):
        p = c.ask()
        if p is None:
            break
        assert sp.valid(p) and set(p) == set(NAMES)
        if len(c.history) >= c.n_init:
            seen_bo += 1                       # this proposal came from the GP, not Sobol
        s, pdi = _truth(p)
        c.tell(p, s, pdi, confidence=0.7)
    assert seen_bo >= 1                          # exercised the GP proposal path


def test_low_confidence_still_recorded_not_dropped():
    sp = ParameterSpace.from_config(_CFG)
    c = CampaignController(sp, target_size=5.0, tolerance=0.25, pdi_cap=0.2,
                           budget=10, n_init=4, seed=4)
    c.start()
    p = c.ask()
    c.tell(p, size=None, pdi=None, confidence=0.0)   # failed fit
    assert len(c.history) == 1 and c.history[0]["loss"] >= 1e3   # penalized, not dropped
