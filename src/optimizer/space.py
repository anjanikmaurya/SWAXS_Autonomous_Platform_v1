"""
src/optimizer/space.py — the synthesis parameter space the optimizer searches.

Five knobs, with the reactor's own bounds, composition constraint and per-pump
flow window, so the optimizer does not propose a recipe the reactor would reject:
    T_reac, F_tot                    — box bounds
    x_ODE, x_TOP, x_oley             — each in [x_lo, x_hi]; their sum ≤ x_sum_max
                                       (precursor fraction = 1 − Σx is the remainder)
    per-pump rate = x · F_tot        — a NONZERO rate must lie in that pump's
                                       [sensor_min, max_flow] and ≤ per_pump_max,
                                       the same hard rule as
                                       src/reactor/recipe.py::compute_pump_rates
                                       (a true 0, pump off, is always allowed)

The pump window is only as current as the config handed to ``from_config``:
pass the reactor's live limits (reactor_limits.json overlaid on config.yml) to
match what the reactor will enforce. The T / F_tot safety caps are not
re-checked here; the reactor's validate() still has the last word.
"""

from __future__ import annotations

import warnings

import numpy as np
from scipy.stats import qmc

NAMES = ["T_reac", "F_tot", "x_ODE", "x_TOP", "x_oley"]

#: Which reactor pump each fraction drives (src/reactor/recipe.py raw rates).
#: "x_precursor" is the remainder 1 − (x_ODE + x_TOP + x_oley).
_PUMP_OF = {"x_ODE": "ode_dilution", "x_TOP": "top", "x_oley": "oleylamine",
            "x_precursor": "pd_top_precursor"}


class ParameterSpace:
    def __init__(self, t_reac, f_tot, x_each=(0.0, 0.3), x_sum_max=0.9,
                 pump_limits: dict | None = None):
        self.bounds = {"T_reac": tuple(map(float, t_reac)),
                       "F_tot": tuple(map(float, f_tot)),
                       "x_ODE": tuple(map(float, x_each)),
                       "x_TOP": tuple(map(float, x_each)),
                       "x_oley": tuple(map(float, x_each))}
        self.x_sum_max = float(x_sum_max)
        #: {fraction_name: (sensor_min, max_flow)} in µL/min; empty = no check.
        self.pump_limits = {k: (float(v[0]), float(v[1]))
                            for k, v in (pump_limits or {}).items()}

    @staticmethod
    def pump_limits_from_config(cfg: dict) -> dict:
        """Per-fraction [sensor_min, max_flow] from a reactor config dict, with
        max_flow capped by safety.per_pump_max (same defaults as the reactor)."""
        pumps = (cfg or {}).get("pumps") or {}
        cap = float(((cfg or {}).get("safety") or {}).get("per_pump_max", 1000.0))
        out = {}
        for frac, pump in _PUMP_OF.items():
            pc = pumps.get(pump)
            if pc is None:
                continue
            smin = float(pc.get("sensor_min", 0.0))
            pmax = min(float(pc.get("max_flow", cap)), cap)
            out[frac] = (smin, pmax)
        return out

    @classmethod
    def from_config(cls, cfg: dict) -> "ParameterSpace":
        b = cfg.get("bounds", {})
        return cls(t_reac=b.get("T_reac", [180.0, 300.0]),
                   f_tot=b.get("F_tot", [40.0, 120.0]),
                   x_each=b.get("x_each", [0.0, 0.3]),
                   x_sum_max=b.get("x_sum_max", 0.9),
                   pump_limits=cls.pump_limits_from_config(cfg))

    def names(self):
        return list(NAMES)

    def sub(self, bounds: dict) -> "ParameterSpace":
        """The same space (composition rule, pump windows) inside narrower bounds.

        ``bounds`` maps setting -> (lo, hi); each is intersected with this space's
        own bounds, so a sub-space can never reach outside the search range."""
        sp = ParameterSpace(t_reac=self.bounds["T_reac"], f_tot=self.bounds["F_tot"],
                            x_sum_max=self.x_sum_max, pump_limits=dict(self.pump_limits))
        nb = {}
        for k, (lo, hi) in self.bounds.items():
            blo, bhi = (bounds or {}).get(k, (lo, hi))
            nb[k] = (max(float(lo), float(blo)), min(float(hi), float(bhi)))
            if nb[k][0] > nb[k][1]:
                raise ValueError(f"{k}: the narrower range lies outside the search range")
        sp.bounds = nb
        return sp

    # ── validity / constraints ────────────────────────────────────────────────
    def valid(self, p: dict) -> bool:
        for k, (lo, hi) in self.bounds.items():
            if k not in p or not (lo - 1e-9 <= float(p[k]) <= hi + 1e-9):
                return False
        xs = float(p["x_ODE"]) + float(p["x_TOP"]) + float(p["x_oley"])
        if xs > self.x_sum_max + 1e-9:
            return False
        if self.pump_limits:
            F = float(p["F_tot"])
            fr = {"x_ODE": float(p["x_ODE"]), "x_TOP": float(p["x_TOP"]),
                  "x_oley": float(p["x_oley"]), "x_precursor": 1.0 - xs}
            for k, (smin, pmax) in self.pump_limits.items():
                rate = fr[k] * F
                # the reactor rounds to 4 dp; mirror its strict comparisons
                if 0.0 < rate < smin - 1e-9 or rate > pmax + 1e-9:
                    return False
        return True

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
            tries = 0
            while len(out) < n:
                tries += 1
                if tries > 500:
                    # The pump windows can leave the box (nearly) empty. Never
                    # spin forever: say so, or hand back what was found.
                    if not out:
                        raise ValueError(
                            "no recipe in the search bounds satisfies the per-pump "
                            "flow limits [sensor_min, max_flow]; widen the bounds "
                            "or the pump limits")
                    break
                for u in eng.random(batch):
                    p = self.from_unit(u)
                    if self.valid(p):
                        out.append(p)
                        if len(out) >= n:
                            break
        return out[:n]
