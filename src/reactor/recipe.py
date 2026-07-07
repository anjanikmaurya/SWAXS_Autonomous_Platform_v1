"""
src/reactor/recipe.py — recipe validation and setpoint conversion.

A *recipe* (predicted by the external BO/SAXS step) carries:
    T_reac  (°C), F_tot (µL/min), x_ODE, x_TOP, x_oley  (fractions)

Conversion to per-pump flow rates (µL/min):
    ode_dilution     = x_ODE  · F_tot
    top              = x_TOP  · F_tot
    oleylamine       = x_oley · F_tot
    pd_top_precursor = (1 − x_ODE − x_TOP − x_oley) · F_tot
    ode_flush        = 0            (during synthesis)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .config import PUMP_NAMES, REAGENT_PUMPS, FLUSH_PUMP


class RecipeError(ValueError):
    """Raised when a recipe is out of bounds or breaches a hard safety cap."""


@dataclass
class Recipe:
    T_reac: float
    F_tot: float
    x_ODE: float
    x_TOP: float
    x_oley: float
    recipe_id: str = ""
    run_duration: float | None = None     # s, optional override
    flush_rate: float | None = None       # µL/min, optional override
    flush_duration: float | None = None   # s, optional override
    source: str = "api"
    received_at: float = field(default_factory=time.time)
    clamps: list = field(default_factory=list)   # setpoints raised to sensor min

    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        def num(k, required=True):
            if k not in d or d[k] in (None, ""):
                if required:
                    raise RecipeError(f"missing required field: {k}")
                return None
            try:
                return float(d[k])
            except (TypeError, ValueError):
                raise RecipeError(f"field {k} is not a number: {d[k]!r}")
        rid = str(d.get("recipe_id") or d.get("id") or "").strip() \
            or f"rcp_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        return cls(
            T_reac=num("T_reac"), F_tot=num("F_tot"),
            x_ODE=num("x_ODE"), x_TOP=num("x_TOP"), x_oley=num("x_oley"),
            recipe_id=rid,
            run_duration=num("run_duration", required=False),
            flush_rate=num("flush_rate", required=False),
            flush_duration=num("flush_duration", required=False),
            source=str(d.get("source", "api")),
        )

    @property
    def x_precursor(self) -> float:
        return 1.0 - self.x_ODE - self.x_TOP - self.x_oley

    def to_dict(self) -> dict:
        return {"recipe_id": self.recipe_id, "T_reac": self.T_reac,
                "F_tot": self.F_tot, "x_ODE": self.x_ODE, "x_TOP": self.x_TOP,
                "x_oley": self.x_oley, "run_duration": self.run_duration,
                "flush_rate": self.flush_rate, "flush_duration": self.flush_duration,
                "source": self.source, "clamps": self.clamps}


def validate(recipe: Recipe, cfg: dict) -> None:
    """Raise RecipeError if the recipe is out of bounds or breaches a hard cap."""
    b = cfg.get("bounds", {})
    s = cfg.get("safety", {})
    t_lo, t_hi = b.get("T_reac", [180.0, 300.0])
    f_lo, f_hi = b.get("F_tot", [40.0, 120.0])
    x_lo, x_hi = b.get("x_each", [0.0, 0.3])
    x_sum_max = b.get("x_sum_max", 0.9)

    if not (t_lo <= recipe.T_reac <= t_hi):
        raise RecipeError(f"T_reac {recipe.T_reac} outside [{t_lo}, {t_hi}] °C")
    if not (f_lo <= recipe.F_tot <= f_hi):
        raise RecipeError(f"F_tot {recipe.F_tot} outside [{f_lo}, {f_hi}] µL/min")
    for nm, x in (("x_ODE", recipe.x_ODE), ("x_TOP", recipe.x_TOP), ("x_oley", recipe.x_oley)):
        if not (x_lo <= x <= x_hi):
            raise RecipeError(f"{nm} {x} outside [{x_lo}, {x_hi}]")
    xsum = recipe.x_ODE + recipe.x_TOP + recipe.x_oley
    if xsum > x_sum_max + 1e-9:
        raise RecipeError(f"x_ODE+x_TOP+x_oley = {xsum:.3f} exceeds {x_sum_max}")
    if recipe.x_precursor < -1e-9:
        raise RecipeError(f"precursor fraction negative ({recipe.x_precursor:.3f})")

    # ── Hard safety caps ─────────────────────────────────────────────────────
    if recipe.T_reac > s.get("T_max", 320.0):
        raise RecipeError(f"SAFETY: T_reac {recipe.T_reac} exceeds T_max {s.get('T_max')}")
    if recipe.F_tot > s.get("F_tot_max", 150.0):
        raise RecipeError(f"SAFETY: F_tot {recipe.F_tot} exceeds F_tot_max {s.get('F_tot_max')}")


def recipe_to_setpoints(recipe: Recipe, cfg: dict, *, validate_first: bool = True,
                        clamps: list | None = None) -> dict:
    """
    Convert a recipe to per-pump flow rates (µL/min).

    Each pump's ``[sensor_min, max_flow]`` is a HARD LIMIT: any nonzero setpoint
    below the pump minimum, or above the pump maximum, **rejects the whole
    recipe** (raises RecipeError) — nothing is clamped and nothing is sent to
    hardware.  A true 0 (pump off) is always allowed.  Returns
    ``{pump_name: rate_uL_min}`` with ode_flush = 0.

    (``clamps`` is accepted for backward compatibility and left empty, since
    out-of-range setpoints are now rejected rather than clamped.)
    """
    if validate_first:
        validate(recipe, cfg)

    raw = {
        "ode_dilution":     recipe.x_ODE * recipe.F_tot,
        "top":              recipe.x_TOP * recipe.F_tot,
        "oleylamine":       recipe.x_oley * recipe.F_tot,
        "pd_top_precursor": recipe.x_precursor * recipe.F_tot,
        FLUSH_PUMP:         0.0,
    }

    pumps_cfg = cfg.get("pumps", {})
    per_pump_max = cfg.get("safety", {}).get("per_pump_max", 1000.0)
    out: dict[str, float] = {}
    for name in PUMP_NAMES:
        rate = float(raw.get(name, 0.0))
        pc = pumps_cfg.get(name, {})
        smin = float(pc.get("sensor_min", 0.0))
        pmax = float(pc.get("max_flow", per_pump_max))
        # ── hard pump min/max: reject the recipe if outside the window ────────
        if 0.0 < rate < smin:
            raise RecipeError(
                f"{name} setpoint {rate:.3g} µL/min is below its minimum "
                f"{smin:g} µL/min (pump min/max are hard limits — recipe rejected). "
                f"A nonzero pump must be ≥ its flow-sensor minimum.")
        if rate > pmax:
            raise RecipeError(
                f"{name} setpoint {rate:.3g} µL/min exceeds its maximum "
                f"{pmax:g} µL/min (recipe rejected).")
        # absolute platform-wide safety cap
        if rate > per_pump_max:
            raise RecipeError(
                f"SAFETY: {name} setpoint {rate:.3g} exceeds per_pump_max {per_pump_max}")
        out[name] = round(rate, 4)
    return out
