"""
src/optimizer/space.py — the synthesis parameter space the optimizer searches.

Five knobs, with the reactor's own bounds and composition constraint so the
optimizer can NEVER propose a recipe the reactor would reject:
    T_reac, F_tot                    — box bounds
    x_ODE, x_TOP, x_oley             — each in [x_lo, x_hi]; their sum ≤ x_sum_max
                                       (precursor fraction = 1 − Σx is the remainder)
"""

from __future__ import annotations

import warnings

import numpy as np
from scipy.stats import qmc

NAMES = ["T_reac", "F_tot", "x_ODE", "x_TOP", "x_oley"]


class ParameterSpace:
    def __init__(self, t_reac, f_tot, x_each=(0.0, 0.3), x_sum_max=0.9):
        self.bounds = {"T_reac": tuple(map(float, t_reac)),
                       "F_tot": tuple(map(float, f_tot)),
                       "x_ODE": tuple(map(float, x_each)),
                       "x_TOP": tuple(map(float, x_each)),
                       "x_oley": tuple(map(float, x_each))}
        self.x_sum_max = float(x_sum_max)

    @classmethod
    def from_config(cls, cfg: dict) -> "ParameterSpace":
        b = cfg.get("bounds", {})
        return cls(t_reac=b.get("T_reac", [180.0, 300.0]),
                   f_tot=b.get("F_tot", [40.0, 120.0]),
                   x_each=b.get("x_each", [0.0, 0.3]),
                   x_sum_max=b.get("x_sum_max", 0.9))

    def names(self):
        return list(NAMES)

    # ── validity / constraints ────────────────────────────────────────────────
    def valid(self, p: dict) -> bool:
        for k, (lo, hi) in self.bounds.items():
            if k not in p or not (lo - 1e-9 <= float(p[k]) <= hi + 1e-9):
                return False
        return (p["x_ODE"] + p["x_TOP"] + p["x_oley"]) <= self.x_sum_max + 1e-9

    # ── unit-cube <-> real mapping (for the surrogate) ─────────────────────────
    def to_unit(self, p: dict) -> np.ndarray:
        return np.array([(float(p[k]) - lo) / (hi - lo) if hi > lo else 0.0
                         for k, (lo, hi) in self.bounds.items()])

    def from_unit(self, u) -> dict:
        u = np.asarray(u, float)
        return {k: float(lo + u[i] * (hi - lo))
                for i, (k, (lo, hi)) in enumerate(self.bounds.items())}

    # ── space-filling sample that satisfies the constraint ─────────────────────
    def sobol(self, n: int, seed: int = 0) -> list[dict]:
        """n constraint-valid points via Sobol sampling (with rejection)."""
        out: list[dict] = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")           # Sobol non-power-of-2 balance warning
            eng = qmc.Sobol(d=len(NAMES), seed=seed)
            batch = max(16, n * 4)
            while len(out) < n:
                for u in eng.random(batch):
                    p = self.from_unit(u)
                    if self.valid(p):
                        out.append(p)
                        if len(out) >= n:
                            break
        return out[:n]
