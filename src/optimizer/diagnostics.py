"""
src/optimizer/diagnostics.py — what the optimizer currently believes, as numbers.

The campaign's decisions are made in a 5-D space (T_reac, F_tot, x_ODE, x_TOP,
x_oley) that nobody can see. This module turns the live surrogate into the four
quantities the autonomous-experimentation literature reports:

  1. POSTERIOR MEAN of the objective on a 2-D slice — where the loop thinks the
     good recipes are.
  2. POSTERIOR STANDARD DEVIATION on the same slice — where it is still ignorant.
     Noack et al. make the case that the uncertainty surface, not the mean, is
     what justifies the next measurement, and that its decay is the honest
     convergence metric for a steered experiment.
  3. ACQUISITION (Expected Improvement) with the next proposal marked — the
     mean/uncertainty trade-off the loop actually resolves.
  4. A SECOND SURROGATE ON MEASURED SIZE, so the slice can carry the
     ``predicted size = target`` iso-contour: the set of recipes the loop
     believes would hit the requested radius. That contour is the direct answer
     to "where is the measurement predicted?" — the loss surface alone hides it,
     because loss is symmetric about the target and so two-valued in size.

Everything here is READ-ONLY with respect to the campaign: no ask(), no state
change. A diagnostics call during an autonomous run must never alter what the
reactor is told to do next.

Design notes
------------
* The surrogate is obtained from ``CampaignController.fit_surrogate()``, i.e. it
  is the same GP, length scale and per-point noise that produced the proposal.
  Re-deriving it here would let the picture drift away from the decision.
* Grid points that violate the composition constraint (Σx ≤ x_sum_max) are
  masked as NaN rather than plotted. The optimizer can never propose them, so
  showing a smooth surrogate there would invite a recipe the reactor rejects.
* Loss is reported on the scale the campaign minimises. ``_FAIL_LOSS`` points
  (profiles that could not be sized) are real observations to the GP; they are
  flagged separately so a plot can draw them differently instead of letting a
  single 1e3 outlier flatten the whole colour scale.
"""

from __future__ import annotations

import numpy as np

from .campaign import _FAIL_LOSS, CampaignController
from .gp import GP, expected_improvement
from .space import NAMES

#: Slice defaults chosen because the simulator's hidden optimum lives in
#: (T_reac, x_TOP) — the pair a hot-injection landscape actually turns on.
DEFAULT_SLICE = ("T_reac", "x_TOP")


# ── helpers ──────────────────────────────────────────────────────────────────
def _valid_mask(space, pts: list[dict]) -> np.ndarray:
    return np.array([space.valid(p) for p in pts], bool)


def anchor(campaign: CampaignController, mode: str = "best") -> dict:
    """Values held fixed for the dimensions not on the slice axes.

    ``best`` cuts the space through the best recipe found so far — the slice an
    operator actually wants, because it shows the neighbourhood the loop is
    working in. ``mid`` cuts through the centre of the box, which is the right
    choice before any result exists.
    """
    if mode == "best" and campaign.best is not None:
        return dict(campaign.best["params"])
    return {k: 0.5 * (lo + hi) for k, (lo, hi) in campaign.space.bounds.items()}


def fit_size_surrogate(campaign: CampaignController):
    """GP on MEASURED SIZE (nm), for the ``predicted size = target`` contour.

    Uses the same unit-cube inputs and confidence-as-noise convention as the
    loss surrogate. Unsized profiles carry no size information and are dropped
    here (unlike in the loss GP, where failure is itself informative).
    """
    hist = [h for h in campaign.history if h.get("size") is not None]
    if len(hist) < 3:
        return None
    X = np.array([campaign.space.to_unit(h["params"]) for h in hist])
    y = np.array([float(h["size"]) for h in hist])
    conf = np.array([max(h["confidence"], 0.05) for h in hist])
    base = 0.05 * max(np.var(y), 1e-6) + 1e-6
    return GP(length_scale=0.3).fit(X, y, base / conf), X, y


# ── 1-3. the slice ───────────────────────────────────────────────────────────
def slice_surfaces(campaign: CampaignController, xname: str = DEFAULT_SLICE[0],
                   yname: str = DEFAULT_SLICE[1], *, n: int = 60,
                   anchor_mode: str = "best") -> dict | None:
    """Posterior mean / sd / EI / predicted-size on a 2-D cut through the space.

    Returns None until there is something to fit — an empty plot is more
    misleading than no plot.
    """
    if xname == yname or xname not in NAMES or yname not in NAMES:
        raise ValueError(f"bad slice axes: {xname!r}, {yname!r}")
    fit = campaign.fit_surrogate()
    if fit is None:
        return None
    gp, X, y = fit
    space = campaign.space
    fixed = anchor(campaign, anchor_mode)

    xlo, xhi = space.bounds[xname]
    ylo, yhi = space.bounds[yname]
    xs = np.linspace(xlo, xhi, n)
    ys = np.linspace(ylo, yhi, n)

    pts, feas = [], []
    for yv in ys:                       # row-major: index [row=y, col=x]
        for xv in xs:
            p = dict(fixed); p[xname] = float(xv); p[yname] = float(yv)
            pts.append(p)
            feas.append(space.valid(p))
    feas = np.array(feas, bool)
    U = np.array([space.to_unit(p) for p in pts])

    mu, var = gp.predict(U)
    sd = np.sqrt(var)
    ei = expected_improvement(mu, var, float(np.min(y)))

    def _grid(v):
        g = np.asarray(v, float).copy()
        g[~feas] = np.nan                       # never show an unproposable recipe
        return g.reshape(n, n)

    out = {
        "x_name": xname, "y_name": yname,
        "x": xs.tolist(), "y": ys.tolist(),
        "fixed": {k: v for k, v in fixed.items() if k not in (xname, yname)},
        "anchor_mode": anchor_mode,
        "loss_mean": _grid(mu), "loss_sd": _grid(sd), "ei": _grid(ei),
        # the surrogate is fitted on whatever scale the campaign chose, so the
        # colour bar must say which one — a log1p surface labelled "loss" would
        # be read off wrongly by a factor of e
        "loss_scale": campaign.loss_transform,
        "feasible_frac": float(feas.mean()),
        "n_observations": len(campaign.history),
        "target_size": campaign.target_size,
        "tolerance": campaign.tolerance,
        "size_mean": None, "size_sd": None,
    }

    sfit = fit_size_surrogate(campaign)
    if sfit is not None:
        sgp, _, _ = sfit
        smu, svar = sgp.predict(U)
        out["size_mean"] = _grid(smu)
        out["size_sd"] = _grid(np.sqrt(svar))

    # observed points and the pending proposal, projected onto the slice
    out["samples"] = [{"x": float(h["params"][xname]), "y": float(h["params"][yname]),
                       "loss": float(h["loss"]), "run": i + 1,
                       "failed": float(h["loss"]) >= _FAIL_LOSS,
                       "confidence": float(h["confidence"])}
                      for i, h in enumerate(campaign.history)]
    nxt = campaign.peek()
    out["proposal"] = ({"x": float(nxt[xname]), "y": float(nxt[yname]), "params": nxt}
                       if nxt else None)
    return out


def target_contour_points(surf: dict) -> list[list[float]]:
    """Where the size surrogate crosses the target, as (x, y) pairs.

    A marching-free crossing detector: for each grid row and column, linearly
    interpolate the sign change of ``size_mean − target``. Good enough to draw,
    and it has no dependency beyond numpy (the browser panel gets the same
    points, so it does not need a contouring library either).
    """
    Z = surf.get("size_mean")
    if Z is None:
        return []
    Z = np.asarray(Z, float) - float(surf["target_size"])
    xs = np.asarray(surf["x"], float)
    ys = np.asarray(surf["y"], float)
    out: list[list[float]] = []
    for i in range(Z.shape[0]):                       # scan along x
        row = Z[i]
        for j in range(len(xs) - 1):
            a, b = row[j], row[j + 1]
            if np.isnan(a) or np.isnan(b) or a == b or (a > 0) == (b > 0):
                continue
            t = a / (a - b)
            out.append([float(xs[j] + t * (xs[j + 1] - xs[j])), float(ys[i])])
    for j in range(Z.shape[1]):                       # scan along y
        col = Z[:, j]
        for i in range(len(ys) - 1):
            a, b = col[i], col[i + 1]
            if np.isnan(a) or np.isnan(b) or a == b or (a > 0) == (b > 0):
                continue
            t = a / (a - b)
            out.append([float(xs[j]), float(ys[i] + t * (ys[i + 1] - ys[i]))])
    return out


# ── 4. convergence ───────────────────────────────────────────────────────────
def convergence(campaign: CampaignController, *, pool: int = 256,
                uncertainty: bool = True) -> dict:
    """Per-run traces plus the retrospective uncertainty decay.

    * ``loss`` / ``best_loss`` — the standard best-so-far curve. Reported on a
      log axis by the plots because the first Sobol points are typically orders
      of magnitude worse than the converged region.
    * ``size`` against ``target ± tolerance`` — the acceptance band the stop
      rule actually tests, so a reader can see convergence in physical units
      rather than in loss.
    * ``pdi`` against the cap — the second half of the stop rule.
    * ``max_sd`` — max posterior sd over a FIXED candidate pool, refitting the
      surrogate on the first k observations. This is the model-side convergence
      signal: it keeps falling even while the loss curve is flat, and it is what
      distinguishes "converged" from "stuck in one basin".
    * ``step`` — distance in unit space between consecutive proposals: large
      while exploring, small once exploiting.
    """
    h = campaign.history
    runs = list(range(1, len(h) + 1))
    loss = [float(r["loss"]) for r in h]
    best = np.minimum.accumulate(loss).tolist() if loss else []
    failed = [bool(float(r["loss"]) >= _FAIL_LOSS) for r in h]

    U = [campaign.space.to_unit(r["params"]) for r in h]
    step = [float(np.linalg.norm(U[i] - U[i - 1])) for i in range(1, len(U))]

    # Raw max posterior sd is NOT comparable across refits in this
    # implementation: GP.fit sets the signal variance to var(y), which grows as
    # the campaign discovers a wider range of losses. Early in a run the raw sd
    # therefore RISES, which reads as the model getting worse when in fact the
    # prior it is measured against got bigger. The normalised version — max sd as
    # a fraction of the GP's own prior sd — is the metric that means "how much of
    # the prior uncertainty has been removed", and it is bounded in [0, 1].
    # Two statistics, because they answer different questions. MAX sd is the
    # worst-covered corner of the space; with a fixed length scale of 0.3 in a
    # 5-D cube it barely moves, because a random far corner is essentially
    # uncorrelated with every observation. MEAN sd is coverage — the fraction of
    # the pool the surrogate can now say something about — and it is the one that
    # actually tracks learning.
    max_sd: list[float] = []
    max_sd_rel: list[float] = []
    mean_sd_rel: list[float] = []
    if uncertainty and h:
        cand = campaign.candidate_pool(pool, seed_offset=0)
        Xc = np.array([campaign.space.to_unit(c) for c in cand])
        for k in range(1, len(h) + 1):
            fit = campaign.fit_surrogate(upto=k)
            if fit is None:
                max_sd.append(float("nan")); max_sd_rel.append(float("nan"))
                mean_sd_rel.append(float("nan")); continue
            gp, _, _ = fit
            _, var = gp.predict(Xc)
            m = float(np.sqrt(var).max())
            max_sd.append(m)
            prior_sd = float(np.sqrt(gp.var))
            ok = prior_sd > 0
            max_sd_rel.append(m / prior_sd if ok else float("nan"))
            mean_sd_rel.append(float(np.sqrt(var).mean()) / prior_sd if ok
                               else float("nan"))

    return {
        "runs": runs,
        "loss": loss,
        "best_loss": best,
        "failed": failed,
        "size": [None if r["size"] is None else float(r["size"]) for r in h],
        "pdi": [None if r["pdi"] is None else float(r["pdi"]) for r in h],
        "confidence": [float(r["confidence"]) for r in h],
        "step": step,
        "max_sd": max_sd,
        "max_sd_rel": max_sd_rel,
        "mean_sd_rel": mean_sd_rel,
        "target_size": campaign.target_size,
        "tolerance": campaign.tolerance,
        "pdi_cap": campaign.pdi_cap,
        "n_init": campaign.n_init,
        "budget": campaign.budget,
        "status": campaign.status_str,
        "confidence_min": campaign.confidence_min,
    }


# ── 5. sampling trajectory ───────────────────────────────────────────────────
def trajectory(campaign: CampaignController) -> dict:
    """Every recipe tried, in real units and in the unit cube.

    Plotted as parallel coordinates: with 5 knobs it is the only view that shows
    all of them at once, and the collapse of the lines onto a narrow bundle late
    in a campaign is the visual signature of convergence in parameter space
    (as opposed to convergence in objective value, which the loss curve shows).
    """
    names = campaign.space.names()
    rows = []
    for i, r in enumerate(campaign.history):
        p = r["params"]
        rows.append({
            "run": i + 1,
            "phase": "sobol" if i < campaign.n_init else "bo",
            "params": {k: float(p[k]) for k in names},
            "unit": [float(v) for v in campaign.space.to_unit(p)],
            "loss": float(r["loss"]),
            "size": None if r["size"] is None else float(r["size"]),
            "pdi": None if r["pdi"] is None else float(r["pdi"]),
        })
    nxt = campaign.peek()
    return {
        "names": names,
        "bounds": {k: list(v) for k, v in campaign.space.bounds.items()},
        "rows": rows,
        "n_init": campaign.n_init,
        "best": campaign.best,
        "proposal": nxt,
    }


def summary(campaign: CampaignController) -> dict:
    """Small dict for a status line: is the loop still learning anything?"""
    c = convergence(campaign)
    sd = [v for v in c["mean_sd_rel"] if v == v]     # coverage, not worst case
    early = float(np.mean(sd[:max(1, len(sd) // 4)])) if sd else float("nan")
    late = float(np.mean(sd[-max(1, len(sd) // 4):])) if sd else float("nan")
    return {
        "n": len(c["runs"]),
        "budget": campaign.budget,
        "status": campaign.status_str,
        "best_loss": (min(c["loss"]) if c["loss"] else None),
        "uncertainty_early": early,
        "uncertainty_late": late,
        "uncertainty_drop": (None if not sd or early <= 0 else 1.0 - late / early),
        "mean_step_early": (float(np.mean(c["step"][:max(1, len(c["step"]) // 4)]))
                            if c["step"] else None),
        "mean_step_late": (float(np.mean(c["step"][-max(1, len(c["step"]) // 4):]))
                           if c["step"] else None),
    }
