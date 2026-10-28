"""
src/optimizer/intake_rules.py: which measured results may steer a campaign, and
which operator-typed starting recipes are allowed.

Two rules, both pure functions so the analyzer and the tests share them:

* ``held_reason(model, expected_shape)``: a fit whose structure is not the
  shape the campaign is about is HELD. Its equivalent size would otherwise
  steer the optimiser (a rod with R_eq 6.9 nm read as a 13.8 nm particle aimed
  at a 5 nm target). A held result still counts against the budget (it was
  measured) but does not train the surrogate and cannot be the best or the hit
  until the operator accepts it (CampaignController.accept).

* ``check_seeds(seeds, space, safety)``: operator-typed starting recipes must be
  complete, finite, inside the search bounds (ParameterSpace.valid: box bounds,
  composition sum, per-pump windows) AND under the reactor's hard safety caps.
  A bad recipe is refused with a reason; it is never clipped silently.
"""
from __future__ import annotations

import math

from .space import NAMES

#: Setup "Expected shape" choices. Values are the catalogue model names
#: (ai_knowledge/rules/saxs_models.yml) that count as that shape; a structure
#: factor suffix ("sphere@hardsphere") is the same shape with interactions.
EXPECTED_SHAPES = {
    "sphere_cs": {"label": "sphere (core-shell counts)",
                  "models": {"sphere", "polydisperse_sphere", "core_shell_sphere"}},
    "sphere":    {"label": "sphere only",
                  "models": {"sphere", "polydisperse_sphere"}},
    "any":       {"label": "any shape (equivalent size)", "models": None},
}
DEFAULT_SHAPE = "sphere"            # sphere only: a core-shell or other fit is held

MAX_SEEDS = 5


def base_model(model: str | None) -> str:
    """Catalogue model without its structure factor: 'sphere@hardsphere' -> 'sphere'."""
    return str(model or "polydisperse_sphere").split("@")[0].strip().lower()


def held_reason(model: str | None, expected_shape: str | None) -> str | None:
    """'shape' when the fitted structure is not the expected shape, else None."""
    rule = EXPECTED_SHAPES.get(expected_shape or DEFAULT_SHAPE) or EXPECTED_SHAPES[DEFAULT_SHAPE]
    allowed = rule["models"]
    if allowed is None:
        return None
    return None if base_model(model) in allowed else "shape"


def check_seeds(seeds, space, safety: dict | None = None) -> tuple[list[dict], list[str]]:
    """Validate operator-typed starting recipes.

    Returns ``(clean, errors)``. ``clean`` holds float-valued recipes with exactly
    the five optimiser settings; ``errors`` says, per recipe, why it was refused.
    The caller refuses the whole request when ``errors`` is not empty: a campaign
    must never start with a recipe the operator did not intend.
    """
    clean, errors = [], []
    if seeds is None:
        return clean, errors
    if not isinstance(seeds, (list, tuple)):
        return [], ["starting recipes must be a list"]
    if len(seeds) > MAX_SEEDS:
        errors.append(f"at most {MAX_SEEDS} of your own starting recipes")
    caps = safety or {}
    t_max = caps.get("T_max")
    f_max = caps.get("F_tot_max")
    for i, s in enumerate(list(seeds)[:MAX_SEEDS], start=1):
        if not isinstance(s, dict):
            errors.append(f"recipe {i}: not a set of values")
            continue
        rec, bad = {}, []
        for k in NAMES:
            try:
                v = float(s.get(k))
            except (TypeError, ValueError):
                bad.append(f"{k} missing")
                continue
            if not math.isfinite(v):
                bad.append(f"{k} not a number")
                continue
            rec[k] = v
        if bad:
            errors.append(f"recipe {i}: " + ", ".join(bad))
            continue
        if t_max is not None and rec["T_reac"] > float(t_max):
            errors.append(f"recipe {i}: T_reac {rec['T_reac']:g} °C is above the safety cap {float(t_max):g} °C")
            continue
        if f_max is not None and rec["F_tot"] > float(f_max):
            errors.append(f"recipe {i}: F_tot {rec['F_tot']:g} µL/min is above the safety cap {float(f_max):g} µL/min")
            continue
        if not space.valid(rec):
            errors.append(f"recipe {i}: outside the search range or a pump's flow window "
                          f"(each setting within its bounds, x_ODE + x_TOP + x_oley "
                          f"≤ {space.x_sum_max:g})")
            continue
        clean.append(rec)
    return clean, errors
