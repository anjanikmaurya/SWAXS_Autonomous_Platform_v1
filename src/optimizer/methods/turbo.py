"""TuRBO — Trust-Region Bayesian Optimization (a compact TuRBO-1).

Classic global BO can over-explore as the recipe space grows. TuRBO keeps a
local trust region (a box in the unit cube) centred on the current best and only
proposes inside it; the region grows after a run of improvements and shrinks
after a run of failures, and restarts when it collapses. This is the numpy-only,
single-trust-region flavour (Eriksson et al., arXiv:1910.01739), adapted to the
CampaignController loop by overriding only the proposal + a post-tell update.
"""
from __future__ import annotations

import numpy as np

from ..campaign import CampaignController
from ..gp import expected_improvement
from . import register


class TuRBO(CampaignController):
    def __init__(self, space, *, tr_init: float = 0.5, tr_min: float = 0.05,
                 tr_max: float = 1.0, succ_tol: int = 3, fail_tol: int = 3, **kw):
        super().__init__(space, **kw)
        self.tr_len = float(tr_init)
        self.tr_min, self.tr_max = float(tr_min), float(tr_max)
        self.succ_tol, self.fail_tol = int(succ_tol), int(fail_tol)
        self._succ = self._fail = 0
        self._best_loss = np.inf

    def _local_candidates(self, center_u: np.ndarray, n: int = 256) -> list[dict]:
        """Constraint-valid recipes drawn inside the trust-region box around the
        incumbent (clipped to the unit cube), mapped back to parameters."""
        pool = self.candidate_pool(n)                       # global Sobol (valid recipes)
        half = self.tr_len / 2.0
        out = []
        for c in pool:
            u = self.space.to_unit(c)
            u = np.clip(center_u + (u - 0.5) * self.tr_len, 0.0, 1.0)  # squeeze into the box
            p = self.space.from_unit(u)
            if self.space.valid(p):
                out.append(p)
        return out or pool                                  # fall back to global if the box is empty

    def _suggest_bo(self) -> dict:
        fit = self.fit_surrogate()
        if fit is None or self.best is None:
            pool = self.candidate_pool(1)
            return dict(pool[0]) if pool else dict(self._seeds[0])
        gp, X, y = fit
        center_u = self.space.to_unit(self.best["params"])
        cand = self._local_candidates(center_u)
        Xc = np.array([self.space.to_unit(c) for c in cand])
        mu, var = gp.predict(Xc)
        ei = expected_improvement(mu, var, float(np.min(y)))
        return cand[int(np.argmax(ei))]

    def tell(self, params, size, pdi, confidence, recipe_id: str = ""):
        rec = super().tell(params, size, pdi, confidence, recipe_id=recipe_id)
        # Count an improvement on the incumbent loss as a success, else a failure;
        # resize the trust region on a run of either, and restart if it collapses.
        loss = rec["loss"]
        if loss < self._best_loss - 1e-9:
            self._best_loss = loss
            self._succ += 1; self._fail = 0
        else:
            self._fail += 1; self._succ = 0
        if self._succ >= self.succ_tol:
            self.tr_len = min(self.tr_max, self.tr_len * 2.0); self._succ = 0
        elif self._fail >= self.fail_tol:
            self.tr_len = self.tr_len / 2.0; self._fail = 0
            if self.tr_len < self.tr_min:                   # collapsed → restart the region
                self.tr_len = 0.5
        return rec


register("turbo", lambda space, **cfg: TuRBO(space, **cfg),
         "TuRBO (trust-region BO)")
