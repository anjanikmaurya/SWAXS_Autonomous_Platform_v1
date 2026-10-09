"""BAX — Bayesian Algorithm eXecution (goal-aware, InfoBAX-style level set).

Unlike EI/UCB, which chase the single lowest-loss recipe, BAX targets the whole
SUBSET of recipe space that meets the experimental goal — here the acceptance
band {loss <= tau}, where tau is the loss at the band edge (size one tolerance
off, PDI at the cap). Mapping that region is exactly what "hit a target size at
low PDI across conditions" needs (Miller et al., npj Comput. Mater. 2024).

This is a tractable InfoBAX-lite: with a GP on the loss, the next recipe is the
one whose band-membership is most uncertain (posterior P(loss<=tau) nearest 0.5)
— i.e. the point that most sharpens the band boundary, maximising information
about the level set. The CampaignController stop logic still ends the run when a
confident measured recipe actually lands in the band.
"""
from __future__ import annotations

import math

import numpy as np

from ..campaign import CampaignController
from . import register


def _norm_cdf(z: np.ndarray) -> np.ndarray:
    # standard-normal CDF via erf, vectorised, no scipy dependency
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))


class BAX(CampaignController):
    def __init__(self, space, *, tau: float | None = None, **kw):
        super().__init__(space, **kw)
        # band edge: size exactly one tolerance off AND PDI at the cap -> loss 1 + w.
        self._tau = float(tau) if tau is not None else (1.0 + self.weight_pdi)

    def _suggest_bo(self) -> dict:
        fit = self.fit_surrogate()
        if fit is None:
            pool = self.candidate_pool(1)
            return dict(pool[0]) if pool else dict(self._seeds[0])
        gp, X, y = fit
        cand = self.candidate_pool(256)
        Xc = np.array([self.space.to_unit(c) for c in cand])
        mu, var = gp.predict(Xc)
        sd = np.sqrt(np.maximum(var, 1e-12))
        tau = self._transform(np.array([self._tau]))[0]      # match the loss transform
        p_in = _norm_cdf((tau - mu) / sd)                    # P(loss <= tau) = P(in band)
        # Bernoulli entropy — maximised where membership is most uncertain (p≈0.5):
        # the recipe that most sharpens the acceptance-band boundary.
        eps = 1e-9
        p = np.clip(p_in, eps, 1.0 - eps)
        entropy = -(p * np.log(p) + (1.0 - p) * np.log(1.0 - p))
        return cand[int(np.argmax(entropy))]


register("bax", lambda space, **cfg: BAX(space, **cfg),
         "BAX (Bayesian Alg. eXecution)")
