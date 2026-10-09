"""GP + Upper/Lower-Confidence-Bound optimiser.

Same GP surrogate as the GP+EI method, but a confidence-bound acquisition with an
explicit explore/exploit knob (kappa). Because the campaign MINIMISES the loss,
the acquisition is the Lower Confidence Bound: pick argmin(mu - kappa*sd). Larger
kappa = more exploration. Subclasses CampaignController and overrides only the
proposal step.
"""
from __future__ import annotations

import numpy as np

from ..campaign import CampaignController
from . import register


class GPUCB(CampaignController):
    def __init__(self, space, *, kappa: float = 2.0, **kw):
        super().__init__(space, **kw)
        self.kappa = float(kappa)

    def _suggest_bo(self) -> dict:
        fit = self.fit_surrogate()
        if fit is None:
            # No sized result yet → keep exploring with a fresh valid point.
            pool = self.candidate_pool(1)
            return dict(pool[0]) if pool else dict(self._seeds[0])
        gp, X, y = fit
        cand = self.candidate_pool(256)
        Xc = np.array([self.space.to_unit(c) for c in cand])
        mu, var = gp.predict(Xc)
        score = mu - self.kappa * np.sqrt(np.maximum(var, 0.0))   # LCB (minimisation)
        return cand[int(np.argmin(score))]


register("gp_ucb", lambda space, **cfg: GPUCB(space, **cfg),
         "Bayesian GP + UCB")
