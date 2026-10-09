"""src/optimizer/methods/ — pluggable optimising methods.

Each method is a FACTORY that builds an object implementing the controller
protocol the analyzer already uses:
    start()/ask()->dict|None / tell(params,size,pdi,confidence,recipe_id="")
    .history / .status_str / .best / .budget / .converged_condition

The current CampaignController already satisfies this, so it registers as
"gp_ei" unchanged. New methods (random, gp_ucb, turbo, bax) register beside it.

Registry maps id -> {factory, label, ready}. The analyzer calls
`make(id, space, **cfg)` to build the active optimiser; `available()` feeds the
Setup tab's dropdown.
"""
from __future__ import annotations

from typing import Callable

DEFAULT_ID = "gp_ei"

_REGISTRY: dict = {}        # id -> {"factory","label"}
_SOON: list = []            # [{"id","label"}]


def register(method_id: str, factory: Callable, label: str) -> None:
    _REGISTRY[method_id] = {"factory": factory, "label": label}


def register_soon(method_id: str, label: str) -> None:
    _SOON.append({"id": method_id, "label": label})


def make(method_id: str | None, space, **cfg):
    """Instantiate the chosen optimiser (default when unknown/empty)."""
    entry = _REGISTRY.get(method_id or "") or _REGISTRY[DEFAULT_ID]
    return entry["factory"](space, **cfg)


#: one-line, plain-language description shown under the Setup dropdown.
_DESC = {
    "random": "Space-filling Sobol baseline with no model. The honest reference: "
              "a method must beat this to earn its cost.",
    "gp_ei":  "Gaussian-process surrogate with Expected Improvement. Balanced "
              "explore / exploit, and the default.",
    "gp_ucb": "GP surrogate with a confidence-bound acquisition and an explicit "
              "explore / exploit knob (kappa).",
    "turbo":  "Trust-region local BO: searches near the best point and grows or "
              "shrinks the region. Scales better in higher dimensions.",
    "bax":    "Goal-aware (Bayesian Algorithm eXecution): maps the whole "
              "acceptance band (target size at low PDI), not just one optimum.",
    "pareto_ehvi": "Goal-aware (Expected Hypervolume Improvement): maps the "
                   "size vs PDI trade-off across a diameter range. The real "
                   "multi-objective acquisition for the Pareto goal.",
}


#: the acquisition function each strategy maximises to choose the next recipe —
#: {name, formula, picks} — surfaced in the Setup tab so the maths is explicit.
#: f = loss (minimised); μ,σ = GP posterior mean/sd; f* = best loss so far;
#: Φ,φ = standard-normal CDF/PDF.
_ACQ = {
    "random": {
        "name": "none (Sobol space-filling)",
        "formula": "next = quasi-random point in the valid box (no model)",
        "picks": "even coverage, the baseline every model must beat",
    },
    "gp_ei": {
        "name": "Expected Improvement",
        "formula": "EI(x) = (f*−μ)·Φ(z) + σ·φ(z),   z = (f*−μ)/σ;   next = argmax EI",
        "picks": "the recipe expected to beat the current best by the most",
    },
    "gp_ucb": {
        "name": "Lower Confidence Bound",
        "formula": "LCB(x) = μ(x) − κ·σ(x);   next = argmin LCB",
        "picks": "low predicted loss, with κ buying extra exploration",
    },
    "turbo": {
        "name": "Expected Improvement in a trust region",
        "formula": "next = argmax EI(x)  for x in trust region R;  R grows on a "
                   "win, shrinks on a miss",
        "picks": "the best EI point near the current best; the box adapts",
    },
    "bax": {
        "name": "Band-membership entropy (InfoBAX)",
        "formula": "p(x) = P(f≤τ) = Φ((τ−μ)/σ);   H = −p·ln p − (1−p)·ln(1−p);   "
                   "next = argmax H",
        "picks": "where 'in-spec?' is least certain, sharpening the band edge",
    },
    "pareto_ehvi": {
        "name": "Expected Hypervolume Improvement",
        "formula": "EHVI(x) = E[ HV(P ∪ {f(x)}) − HV(P) ]  over two GPs "
                   "(size, PDI);   next = argmax EHVI",
        "picks": "the recipe expected to widen the size–PDI Pareto front most",
    },
}


def acq_for(method_id: str) -> dict:
    return _ACQ.get(method_id, {})


# ── goal → search-strategy hierarchy ─────────────────────────────────────────────
# An acquisition is not a free-floating knob: it only makes sense for certain
# goals. EI/UCB/TuRBO chase ONE best point (target-hit). BAX maps a REGION
# (level-set). EHVI maps a TRADE-OFF curve (pareto). So the goal decides which
# strategies are even applicable; within a goal the operator may pick a style.
# The UI shows the goal first, then filters the strategy dropdown to this list
# with ``GOAL_DEFAULT`` preselected — a user can never pair Pareto with EI.
GOAL_METHODS = {
    "target-hit": ["gp_ei", "gp_ucb", "turbo", "random"],
    "level-set":  ["bax"],
    "pareto":     ["pareto_ehvi"],
    # benchmark races search styles against each other (Report → comparison);
    # the single campaign it starts just needs a sensible driver.
    "benchmark":  ["gp_ei", "gp_ucb", "turbo", "random", "bax", "pareto_ehvi"],
}
GOAL_DEFAULT = {
    "target-hit": "gp_ei",
    "level-set":  "bax",
    "pareto":     "pareto_ehvi",
    "benchmark":  "gp_ei",
}


def methods_for_goal(goal: str | None) -> list[str]:
    """The strategy ids valid for a goal (only those actually registered)."""
    ids = GOAL_METHODS.get(goal or "", list(_REGISTRY))
    return [m for m in ids if m in _REGISTRY]


def for_goal(goal: str | None, chosen: str | None = None) -> str:
    """Resolve the strategy a campaign should actually run for this goal.

    The chosen (dropdown) strategy is honoured when it is valid for the goal;
    otherwise the goal's default is used. This enforces the hierarchy so an
    inapplicable pairing (e.g. Pareto + EI) can never reach the loop.
    """
    valid = methods_for_goal(goal)
    if chosen and chosen in valid:
        return chosen
    dflt = GOAL_DEFAULT.get(goal or "", DEFAULT_ID)
    if dflt in _REGISTRY:
        return dflt
    return valid[0] if valid else DEFAULT_ID


def goals_payload() -> dict:
    """Goal → {methods, default} for the Setup tab's strategy filter."""
    return {g: {"methods": methods_for_goal(g),
                "default": GOAL_DEFAULT.get(g, DEFAULT_ID)}
            for g in GOAL_METHODS}


#: extra campaign inputs each method exposes in the Setup tab. Keys match the
#: optimiser constructor's keyword arguments, so the analyzer can splat the
#: operator's values straight into make(). Methods not listed take no extra input.
_PARAMS = {
    "gp_ucb": [{"key": "kappa", "label": "UCB κ (exploration)", "default": 2.0,
                "min": 0.0, "max": 10.0, "step": 0.5,
                "help": "higher = more exploration, lower = more exploitation"}],
    "turbo": [{"key": "tr_init", "label": "Trust-region size (init)", "default": 0.5,
               "min": 0.1, "max": 1.0, "step": 0.1,
               "help": "fraction of the box the local search starts from"}],
    "bax":   [{"key": "tau", "label": "Acceptance-band threshold (loss)", "default": 2.0,
               "min": 0.1, "max": 10.0, "step": 0.1,
               "help": "a recipe is 'in spec' when its loss ≤ this; BAX maps that set"}],
}


def params_for(method_id: str) -> list:
    return _PARAMS.get(method_id, [])


def available() -> list[dict]:
    out = [{"id": k, "label": v["label"], "ready": True, "desc": _DESC.get(k, ""),
            "params": _PARAMS.get(k, []), "acq": _ACQ.get(k, {})}
           for k, v in _REGISTRY.items()]
    out += [{"id": s["id"], "label": s["label"], "ready": False,
             "desc": _DESC.get(s["id"], ""), "params": _PARAMS.get(s["id"], []),
             "acq": _ACQ.get(s["id"], {})}
            for s in _SOON]
    return out


# ── built-ins ──────────────────────────────────────────────────────────────────
from ..campaign import CampaignController          # noqa: E402

register("gp_ei", lambda space, **cfg: CampaignController(space, **cfg),
         "Bayesian GP + EI")

# ready methods registered by importing their modules
from . import random as _random          # noqa: E402,F401  (registers "random")
from . import gp_ucb as _gp_ucb          # noqa: E402,F401  (registers "gp_ucb")
from . import turbo as _turbo            # noqa: E402,F401  (registers "turbo")
from . import bax as _bax                # noqa: E402,F401  (registers "bax")
from . import pareto_ehvi as _pareto     # noqa: E402,F401  (registers "pareto_ehvi")
