"""Random / Sobol baseline optimiser.

The honest baseline for the comparison study (docs/AUTOFIT_REDESIGN.md): if a
model-based method can't beat space-filling random sampling, it isn't earning its
cost. Subclasses CampaignController and overrides ONLY the proposal step, so all
the loss / history / budget / convergence / best bookkeeping is identical to the
GP+EI method — a fair comparison.
"""
from __future__ import annotations

from ..campaign import CampaignController
from . import register


class RandomSearch(CampaignController):
    def _suggest_bo(self) -> dict:
        # Ignore the surrogate; draw the next constraint-valid point. candidate_pool
        # advances its Sobol offset with _n_asked, so successive asks differ and the
        # sequence is reproducible from the seed.
        pool = self.candidate_pool(1)
        return dict(pool[0]) if pool else super()._suggest_bo()


register("random", lambda space, **cfg: RandomSearch(space, **cfg),
         "Random / Sobol search")
