"""Pareto / EHVI — a goal-aware acquisition for the size-vs-dispersity trade-off.

EI, UCB and TuRBO all chase ONE lowest-loss recipe; BAX maps one acceptance
band. Neither answers the Pareto question the proposal's size-range goal asks:
"across the requested diameter range, what is the lowest PDI achievable at each
size?" That is a genuine bi-objective problem — bigger particles and lower
dispersity trade off against each other — and its acquisition is Expected
Hypervolume Improvement (EHVI), the multi-objective analogue of EI
(Emmerich et al. 2011; the standard choice in BoTorch / Ax).

Two objectives, both MINIMISED:
    m1 = PDI                              (lower dispersity is better)
    m2 = size_hi - R   for R inside [size_lo, size_hi]   (larger R within the range is better)
    m2 = size_hi - size_lo (the reference, i.e. no hypervolume) for R outside the range
with reference point r = (pdi_cap, size_hi - size_lo): a recipe above the PDI cap
or below the range contributes no hypervolume, exactly the "not interesting" set.

The acquisition fits TWO GPs — one on measured radius, one on PDI — over the
same unit-cube inputs, then for each candidate estimates, by Monte-Carlo over the
two posteriors, how much it is expected to grow the dominated hypervolume of the
current Pareto front. The recipe with the largest EHVI is proposed next, so the
loop spends its budget filling and extending the front rather than piling points
onto one size. The CampaignController stop/best logic is unchanged — the run
still ends on a confident in-spec hit or at the budget.

If no size range is supplied (range width <= 0) the problem is single-objective
again, so this degrades to the inherited GP + EI proposal rather than inventing a
second axis.
"""
from __future__ import annotations

import numpy as np

from ..campaign import CampaignController, _FAIL_LOSS
from ..gp import GP
from . import register


def _hv2d(points: list[tuple[float, float]], ref: tuple[float, float]) -> float:
    """Dominated hypervolume (area) of a 2-D MINIMISATION set, toward ``ref``.

    Only points that strictly dominate the reference (both coords below it)
    contribute. Computed by the standard sweep over the non-dominated front.
    """
    pts = [(a, b) for a, b in points if a < ref[0] and b < ref[1]]
    if not pts:
        return 0.0
    pts.sort()                                  # by m1 ascending, then m2
    front, best_b = [], float("inf")
    for a, b in pts:                            # keep the non-dominated staircase
        if b < best_b:
            front.append((a, b)); best_b = b
    area, prev_b = 0.0, ref[1]
    for a, b in front:                          # m2 strictly decreasing along it
        area += (ref[0] - a) * (prev_b - b)
        prev_b = b
    return area


class ParetoEHVI(CampaignController):
    def __init__(self, space, *, size_lo: float | None = None,
                 size_hi: float | None = None, mc_samples: int = 48, **kw):
        super().__init__(space, **kw)
        lo = self.target_size if size_lo is None else float(size_lo)
        hi = self.target_size if size_hi is None else float(size_hi)
        if hi < lo:
            lo, hi = hi, lo
        self.size_lo, self.size_hi = lo, hi
        self._range = hi - lo                   # <= 0 → degrade to single-objective EI
        self.mc_samples = int(mc_samples)
        self._ref = (self.pdi_cap, self._range) # objective-space reference point

    # objective vectors (both minimised) from a measured/sampled (R, pdi) pair
    def _obj(self, R: np.ndarray, pdi: np.ndarray):
        m1 = np.clip(pdi, 0.0, None)
        R = np.asarray(R, float)
        inside = (R >= self.size_lo) & (R <= self.size_hi)
        # Outside the requested range on EITHER side sits at the reference (no
        # hypervolume). Clipping alone scored oversized particles as the best
        # possible m2, pulling EHVI toward sizes the operator didn't ask for.
        m2 = np.where(inside, self.size_hi - R, self._range)
        return m1, m2

    def _front_points(self) -> list[tuple[float, float]]:
        pts = []
        for h in self.history:
            R, pdi = h.get("size"), h.get("pdi")
            if R is None or h.get("loss", 0.0) >= _FAIL_LOSS:
                continue
            m1, m2 = self._obj(np.array([R]), np.array([pdi if pdi is not None else 1.0]))
            pts.append((float(m1[0]), float(m2[0])))
        return pts

    def _two_surrogates(self):
        """GPs on measured radius and PDI (sized, non-failed points only), with
        confidence-weighted noise — same recipe as the loss GP."""
        hist = [h for h in self.history
                if h.get("size") is not None and h.get("loss", 0.0) < _FAIL_LOSS]
        if len(hist) < 3:
            return None
        X = np.array([self.space.to_unit(h["params"]) for h in hist])
        R = np.array([float(h["size"]) for h in hist])
        P = np.array([float(h["pdi"]) if h.get("pdi") is not None else 1.0 for h in hist])
        conf = np.array([max(h["confidence"], 0.05) for h in hist])
        nR = (0.05 * max(np.var(R), 1e-6) + 1e-6) / conf
        nP = (0.05 * max(np.var(P), 1e-6) + 1e-6) / conf
        gpR = GP(length_scale=0.3).fit(X, R, nR)
        gpP = GP(length_scale=0.3).fit(X, P, nP)
        return gpR, gpP

    def _suggest_bo(self) -> dict:
        # No usable second axis, or not enough data for two GPs → inherited EI.
        if self._range <= 0:
            return super()._suggest_bo()
        surr = self._two_surrogates()
        if surr is None:
            return super()._suggest_bo()
        gpR, gpP = surr
        cand = self.candidate_pool(128)
        Xc = np.array([self.space.to_unit(c) for c in cand])
        muR, vR = gpR.predict(Xc)
        muP, vP = gpP.predict(Xc)
        sdR, sdP = np.sqrt(np.maximum(vR, 1e-12)), np.sqrt(np.maximum(vP, 1e-12))

        hv0 = _hv2d(self._front_points(), self._ref)
        rng = np.random.default_rng(self.seed + self._n_asked + 1)
        S = self.mc_samples
        # sample both posteriors: shape (S, n_candidates)
        Rs = muR[None, :] + sdR[None, :] * rng.standard_normal((S, len(cand)))
        Ps = muP[None, :] + sdP[None, :] * rng.standard_normal((S, len(cand)))
        m1, m2 = self._obj(Rs, Ps)             # vectorised objective transform

        base = self._front_points()
        ehvi = np.zeros(len(cand))
        for j in range(len(cand)):
            acc = 0.0
            for s in range(S):
                hv = _hv2d(base + [(float(m1[s, j]), float(m2[s, j]))], self._ref)
                if hv > hv0:
                    acc += hv - hv0
            ehvi[j] = acc / S
        if not np.any(ehvi > 0):               # flat EHVI (front already covers ref) → EI
            return super()._suggest_bo()
        return cand[int(np.argmax(ehvi))]


register("pareto_ehvi", lambda space, **cfg: ParetoEHVI(space, **cfg),
         "Pareto GP + EHVI (size vs PDI)")
