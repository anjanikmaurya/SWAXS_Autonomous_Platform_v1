"""Catalogue + BIC fitter: try every structure in the catalogue, keep the best.

No AI. Every model in ai_knowledge/rules/saxs_models.yml (sphere, core-shell
sphere, ellipsoid, cylinder, mass fractal) is fitted with the robust fitter in
src/analysis/model_fit.py; the winner is the lowest BIC (χ² plus a penalty for
each extra parameter, so a 4-parameter core-shell cannot beat a sphere on noise
alone). The best form is then also tried with the hard-sphere and sticky
hard-sphere structure factors, and those are kept only if BIC improves.

This is the baseline the LLM-assisted fitter must beat (docs/design/
LLM_FITTER_PLAN.md), and a usable method on its own.

Returns the standard fitter result. `size.radius` is the EQUIVALENT radius
(the radius itself for a sphere; otherwise √(5/3)·Rg of the fitted shape), so
the optimiser keeps working on size whatever the shape. The fitted model, its
own parameters ± errors and every candidate's χ²/BIC are kept beside it.
"""
from __future__ import annotations

import math

import numpy as np

from src.analysis.nanoparticle import analyze_profile
from . import register

STRUCTURE_FACTORS_TRIED = ("hardsphere", "stickyhardsphere")
S_Q_FORMS = ("sphere", "core_shell_sphere", "ellipsoid", "cylinder")


def _confidence(win: dict, runner_up: dict | None, guinier_valid: bool) -> float:
    """0 to 1. Good fit (reduced χ² near 1), clear winner (BIC gap), not railed."""
    chi2 = float(win.get("chi2") or 99)
    q_fit = 1.0 / (1.0 + max(chi2 - 1.0, 0.0) / 3.0)
    gap = (runner_up["bic"] - win["bic"]) if runner_up else 20.0
    q_sep = 0.5 + 0.5 * (1.0 - math.exp(-max(gap, 0.0) / 6.0))   # ΔBIC 6 ≈ strong evidence
    c = q_fit * q_sep
    if win.get("at_bounds"):
        c *= 0.5
    if not win.get("converged", True):
        c *= 0.8
    if not guinier_valid:
        c *= 0.9
    return round(float(min(max(c, 0.0), 1.0)), 3)


def select(q, I, sigma=None, catalogue=None, features=None, with_sq=True) -> dict:
    """Fit every catalogue model; return {winner, candidates, features}."""
    from src.analysis.features import compute_features
    from src.analysis.model_fit import fit_model, load_catalogue, seeds_from_features
    cat = catalogue or load_catalogue()
    feats = features or compute_features(q, I, sigma, with_pr=False)
    seeds = seeds_from_features(feats)
    cands = [fit_model(q, I, sigma, m, cat, seeds=seeds) for m in cat["models"]]
    ok = sorted((c for c in cands if "error" not in c), key=lambda c: c["bic"])
    if with_sq and ok and ok[0]["model"] in S_Q_FORMS:
        base = ok[0]
        start = dict(base["params"])
        for sq in STRUCTURE_FACTORS_TRIED:
            if sq in cat.get("structure_factors", {}):
                r = fit_model(q, I, sigma, base["model"], cat, structure_factor=sq,
                              seeds=seeds, starts=[start], n_extra=1)
                cands.append(r)
        ok = sorted((c for c in cands if "error" not in c), key=lambda c: c["bic"])
    return {"winner": ok[0] if ok else None, "ranked": ok, "candidates": cands,
            "features": feats}


class CatalogueBIC:
    id = "catalogue_bic"
    label = "Catalogue + BIC"
    ready = True

    def fit(self, q, I, sigma=None, **opts) -> dict:
        # Only the cheap parts of the sphere analysis (Guinier, invariant,
        # phase); the slow sphere fit runs only if the catalogue fails.
        res = analyze_profile(q, I, sigma, dist=opts.get("dist", "auto"), fit_form_factor=False)
        try:
            sel = select(q, I, sigma)
            err = None if (sel["winner"] and sel["winner"].get("equivalent_radius")) \
                else "no catalogue model fitted"
        except Exception as exc:                          # noqa: BLE001
            sel, err = None, str(exc)
        if err:
            res = analyze_profile(q, I, sigma, dist=opts.get("dist", "auto"))
            res.setdefault("diagnostics", {})["catalogue_error"] = err
            res["diagnostics"]["method"] = self.id
            return res                                    # the sphere fit stands
        win, ranked = sel["winner"], sel["ranked"]
        return apply_winner(res, win, ranked, sel["features"], method=self.id)


def apply_winner(res: dict, win: dict, ranked: list, features: dict, method: str) -> dict:
    """Overwrite the size/fit parts of a sphere-fit result with a catalogue winner."""
    R = float(win["equivalent_radius"])
    is_sphere = win["model"] == "sphere"
    err = win.get("errors") or {}
    # the runner-up is the best DIFFERENT shape: "sphere" vs "sphere@hardsphere"
    # is a question about interactions, not about which structure it is
    base = win["model"].split("@")[0]
    runner = next((c for c in ranked if c["model"].split("@")[0] != base), None)
    gv = bool((features.get("guinier") or {}).get("valid"))
    res["model"] = win["model"]
    res["model_params"] = win["params"]
    res["model_errors"] = err
    res["equivalent_radius"] = R
    res["size"] = {"radius": R, "diameter": 2 * R,
                   "source": "form_factor" if is_sphere else "equivalent_rg",
                   "shape_rg": win.get("rg")}
    if win.get("pdi") is not None:
        res["pdi"] = float(win["pdi"])
    res["distribution"] = "schulz"
    I_fit = np.asarray(win["I_fit"], float)
    qd = np.asarray(win["q"], float)
    res["fit"] = {"scale": win["scale"], "background": win["background"],
                  "chi2": win["chi2"], "bic": win["bic"],
                  "rms_log10": None, "q": qd.tolist(), "I_fit": I_fit.tolist()}
    res["fit_curve"] = {"q": qd.tolist(), "I": I_fit.tolist()}
    unc = {}
    if is_sphere and "radius" in err:
        unc["radius"] = err["radius"]
    for k, v in err.items():
        if k.endswith("_pd"):
            unc["pdi"] = v
    res["uncertainty"] = unc or res.get("uncertainty", {})
    res["confidence"] = _confidence(win, runner, gv)
    res["candidates"] = [{"model": c["model"], "chi2": c["chi2"], "bic": c["bic"],
                          "equivalent_radius": c.get("equivalent_radius"),
                          "at_bounds": c.get("at_bounds")} for c in ranked]
    res["features"] = features
    d = res.setdefault("diagnostics", {})
    d.update({"method": method, "model": win["model"], "chi2": win["chi2"],
              "bic_gap": (runner["bic"] - win["bic"]) if runner else None,
              "at_bounds": bool(win.get("at_bounds")),
              "converged": bool(win.get("converged"))})
    return res


register(CatalogueBIC())
