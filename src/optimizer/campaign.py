"""
src/optimizer/campaign.py — the Bayesian-optimization campaign controller.

Owns the closed-loop decision state: the target, the history, the budget, and
the stop flag. It maps each analyzer result (size, PDI, confidence) to a scalar
loss, proposes the next synthesis condition (Sobol cold-start → GP + Expected
Improvement), and decides when to stop.

Design choices (from the loop spec):
  • Objective (minimize):  loss = ((size − target)/tolerance)² + w·(PDI/pdi_cap)
  • Confidence is a WEIGHT, not a gate: a low-confidence point becomes a
    high-noise GP observation (trusted less) instead of being dropped.
  • Stop when a CONFIDENT run hits size ±tolerance AND PDI ≤ cap; else stop at
    the run budget (or on manual abort).

Engine is behind ``ask()/tell()`` so a heavier backend (Ax/BoTorch) could drop
in without touching the app.
"""

from __future__ import annotations

import numpy as np

from .gp import GP, expected_improvement
from .space import ParameterSpace

_FAIL_LOSS = 1e3      # loss assigned when a profile could not be sized


class CampaignController:
    def __init__(self, space: ParameterSpace, *, target_size: float, tolerance: float,
                 pdi_cap: float, budget: int = 25, n_init: int = 10,
                 confidence_min: float = 0.5, weight_pdi: float = 1.0, seed: int = 0,
                 loss_transform: str = "none"):
        self.space = space
        self.target_size = float(target_size)
        self.tolerance = float(tolerance)
        self.pdi_cap = float(pdi_cap)
        self.budget = int(budget)
        self.n_init = int(n_init)
        self.confidence_min = float(confidence_min)
        self.weight_pdi = float(weight_pdi)
        self.seed = int(seed)
        self.loss_transform = str(loss_transform)

        self.status_str = "idle"          # idle | running | converged | exhausted | aborted
        self.history: list[dict] = []     # {params, size, pdi, confidence, loss}
        self.best: dict | None = None
        self.converged_condition: dict | None = None
        self._seeds: list[dict] = []
        self._n_asked = 0

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def start(self, seeds: list[dict] | None = None):
        """Begin a campaign. Optional ``seeds`` (e.g. a literature-informed prior)
        are used first, then Sobol fills the rest of the cold-start budget."""
        self.status_str = "running"
        self.history.clear(); self.best = None; self.converged_condition = None
        self._n_asked = 0
        seeds = [s for s in (seeds or []) if self.space.valid(s)]
        need = max(0, self.n_init - len(seeds))
        self._seeds = seeds + self.space.sobol(need, seed=self.seed)
        return self

    def abort(self):
        self.status_str = "aborted"

    def loss(self, size, pdi) -> float:
        if size is None:
            return _FAIL_LOSS
        size_term = ((float(size) - self.target_size) / self.tolerance) ** 2
        pdi_term = self.weight_pdi * (float(pdi) / self.pdi_cap if pdi is not None else 1.0)
        return float(size_term + pdi_term)

    # ── ask / tell ───────────────────────────────────────────────────────────
    def ask(self) -> dict | None:
        """Next condition to synthesize, or None if the campaign is finished."""
        if self.status_str != "running":
            return None
        cond = (self._seeds[self._n_asked] if self._n_asked < len(self._seeds)
                else self._suggest_bo())
        self._n_asked += 1
        return dict(cond)

    def tell(self, params: dict, size, pdi, confidence: float) -> dict:
        """Record a measured result and update stop state."""
        loss = self.loss(size, pdi)
        rec = {"params": dict(params), "size": size, "pdi": pdi,
               "confidence": float(confidence), "loss": loss}
        self.history.append(rec)
        if self.best is None or loss < self.best["loss"]:
            self.best = rec
        self._check_stop(rec)
        return rec

    def _check_stop(self, rec):
        size, pdi, conf = rec["size"], rec["pdi"], rec["confidence"]
        hit = (size is not None and conf >= self.confidence_min
               and abs(size - self.target_size) <= self.tolerance
               and (pdi is None or pdi <= self.pdi_cap))
        if hit:
            self.status_str = "converged"
            self.converged_condition = rec
        elif len(self.history) >= self.budget:
            self.status_str = "exhausted"

    def _transform(self, y: np.ndarray) -> np.ndarray:
        """Optional monotone transform applied before the GP sees the loss.

        The raw loss spans ~0.3 to >200 across the search box, and one unsized
        profile contributes the 1e3 sentinel. A stationary GP with a single
        length scale cannot represent that dynamic range: the fit is dominated by
        the worst corner, so the posterior is flat exactly where the good recipes
        are. ``log1p`` compresses it. The transform is monotone, so the ranking
        EI produces is unchanged in spirit while the surrogate becomes fittable.

        Default is ``"none"`` — the historical behaviour. Nothing about a live
        campaign changes unless the operator opts in, because switching the
        objective scale mid-campaign would invalidate the history.
        """
        if self.loss_transform == "log1p":
            return np.log1p(np.clip(y, 0.0, None))
        return y

    def peek(self) -> dict | None:
        """The condition ``ask()`` would return next, WITHOUT consuming it.

        Diagnostics plots need to show where the loop is about to measure; they
        must not perturb the campaign to find out.
        """
        if self.status_str != "running":
            return None
        if self._n_asked < len(self._seeds):
            return dict(self._seeds[self._n_asked])
        return dict(self._suggest_bo())

    # ── BO proposal ────────────────────────────────────────────────────────────
    def fit_surrogate(self, upto: int | None = None):
        """The GP the proposal is actually made from — exposed so diagnostics
        plot the real surrogate rather than a lookalike rebuilt with different
        noise or length scale.

        ``upto`` fits on only the first N observations, which is how the
        retrospective uncertainty-decay trace is produced.

        Returns ``(gp, X, y)`` or None when there is nothing to fit.
        """
        hist = self.history if upto is None else self.history[:upto]
        if not hist:
            return None
        X = np.array([self.space.to_unit(h["params"]) for h in hist])
        y = self._transform(np.array([h["loss"] for h in hist], float))
        conf = np.array([max(h["confidence"], 0.05) for h in hist])
        base = 0.05 * max(np.var(y), 1e-6) + 1e-6
        noise = base / conf                                   # low confidence → high noise
        return GP(length_scale=0.3).fit(X, y, noise), X, y

    def candidate_pool(self, n: int = 256, seed_offset: int | None = None) -> list[dict]:
        """The constraint-valid Sobol pool the acquisition is maximised over."""
        off = self._n_asked + 1 if seed_offset is None else int(seed_offset)
        return self.space.sobol(int(n), seed=self.seed + off)

    def _suggest_bo(self) -> dict:
        # y here is already on the transformed scale, and so is gp — EI must be
        # evaluated against the incumbent on that same scale.
        gp, X, y = self.fit_surrogate()
        cand = self.candidate_pool(256)
        Xc = np.array([self.space.to_unit(c) for c in cand])
        mu, var = gp.predict(Xc)
        ei = expected_improvement(mu, var, float(np.min(y)))
        return cand[int(np.argmax(ei))]

    # ── status ─────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "status": self.status_str,
            "n_evaluations": len(self.history),
            "budget": self.budget,
            "n_init": self.n_init,
            "target_size": self.target_size,
            "tolerance": self.tolerance,
            "pdi_cap": self.pdi_cap,
            "best": self.best,
            "converged_condition": self.converged_condition,
        }
