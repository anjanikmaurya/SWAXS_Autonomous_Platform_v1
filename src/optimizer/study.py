"""src/optimizer/study.py — in-silico method-comparison runner.

Runs several optimiser methods through a full ask/tell loop against the hidden
ground-truth simulator, scoring every proposed recipe the SAME way, so their
sample-efficiency can be compared fairly (docs/AUTOFIT_REDESIGN.md, Step 5).

In-silico ONLY: it drives `src.simulator.ground_truth`, never real hardware. It
does not touch the live campaign, the watched folder, the reactor, or the bus —
it is a pure computation that returns per-method histories for the Report tab.
"""
from __future__ import annotations

from . import methods as _methods
from .space import ParameterSpace
from ..simulator.ground_truth import truth_from_recipe, DEFAULTS


def _running_min(xs):
    out, m = [], None
    for x in xs:
        m = x if m is None else min(m, x)
        out.append(m)
    return out


def run_study(space: ParameterSpace, method_ids, cfg: dict, *,
              repeats: int = 1, confidence: float = 0.9,
              truth_cfg: dict | None = None, max_iter: int = 500) -> dict:
    """Run each optimiser id through a campaign against the simulator.

    `cfg` holds target_size/tolerance/pdi_cap/budget/n_init (as the live campaign).
    `repeats` runs each method several times with different seed keys so a curve
    can be averaged (the caller may average; here we return every repeat).

    Returns {method_id: {repeats:[{best_loss_curve, status, n, best}], label}}.
    """
    labels = {m["id"]: m["label"] for m in _methods.available()}
    results: dict = {}
    for mid in method_ids:
        runs = []
        for r in range(max(1, int(repeats))):
            camp = _methods.make(mid, space, **cfg)
            camp.start()
            losses, n = [], 0
            while n <= max_iter:
                rec = camp.ask()
                if rec is None:
                    break
                t = truth_from_recipe(rec, truth_cfg, seed_key=f"{mid}_{r}_{n}")
                camp.tell(rec, t["R_nm"], t["pdi"], confidence,
                          recipe_id=f"{mid}_r{r}_{n:03d}")
                losses.append(camp.history[-1]["loss"])
                n += 1
            best = camp.best or {}
            runs.append({
                "best_loss_curve": _running_min(losses),
                "status": camp.status_str,
                "n": len(camp.history),
                "best": {"size": best.get("size"), "pdi": best.get("pdi"),
                         "loss": best.get("loss")},
            })
        results[mid] = {"label": labels.get(mid, mid), "repeats": runs}
    return results


def default_cfg() -> dict:
    """Sensible study defaults keyed to the simulator optimum (so convergence is
    checkable): target = the simulator's R_opt."""
    return {"target_size": float(DEFAULTS["R_opt"]), "tolerance": 0.3,
            "pdi_cap": 0.15, "budget": 30, "n_init": 8}
