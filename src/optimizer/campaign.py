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
  • A HELD result (the fit is not the expected shape, see
    src/optimizer/intake_rules.py) counts against the budget but does not train
    the surrogate, cannot be the best and cannot end the run, until accept().

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
                 loss_transform: str = "none", stop_on_hit: bool = True):
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
        # Target-hit wants ONE good recipe, so it stops at the first confident hit.
        # Goals that MAP a set (level-set: every in-spec recipe; Pareto: the
        # size vs PDI front) must keep measuring to the budget: stopping at the
        # first hit ended a Pareto run during its Sobol seeding (Run19: 5 of 10
        # seeds) before the acquisition chose a single recipe.
        self.stop_on_hit = bool(stop_on_hit)

        self.status_str = "idle"          # idle | running | converged | exhausted | aborted
        self.history: list[dict] = []     # {params, size, pdi, confidence, loss}
        self.best: dict | None = None
        self.converged_condition: dict | None = None
        self._seeds: list[dict] = []
        #: Warm start: measured points from earlier campaigns (params, size, pdi,
        #: confidence). They train the surrogate, scored against THIS campaign's
        #: target, but are not history: no budget, never the best or the hit.
        self.prior: list[dict] = []
        #: Optional narrower working box for NEW proposals (the AI Advisor's
        #: accepted suggestion). The surrogate still lives in the full space.
        self.work_bounds: dict | None = None
        self._n_asked = 0
        self.last_error: str | None = None   # set when ask() had to fall back
        self.n_fallbacks = 0

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def start(self, seeds: list[dict] | None = None, prior: list[dict] | None = None):
        """Begin a campaign. Optional ``seeds`` (e.g. a literature-informed prior)
        are used first, then Sobol fills the rest of the cold-start budget.
        Optional ``prior`` points (earlier campaigns) warm-start the surrogate."""
        self.prior = [dict(pp) for pp in (prior or [])
                      if pp.get("size") is not None and self.space.valid(pp.get("params") or {})]
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
        self.last_error = None
        if self._n_asked < len(self._seeds):
            cond = self._seeds[self._n_asked]
        else:
            try:
                cond = self._suggest_bo()
            except Exception as exc:
                # An acquisition failure (e.g. a singular GP covariance) must not
                # stall a multi-day loop: the caller has already consumed the
                # measurement, so with no new proposal nothing would ever be
                # pending or time out. Fall back to a fresh constraint-valid
                # space-filling point and record why, so it can be surfaced.
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.n_fallbacks += 1
                pool = self.candidate_pool(1)
                cond = pool[0] if pool else self._seeds[0]
        self._n_asked += 1
        return dict(cond)

    def tell(self, params: dict, size, pdi, confidence: float,
             recipe_id: str = "", held: str | None = None,
             model: str | None = None) -> dict:
        """Record a measured result and update stop state.

        ``recipe_id`` is stored as a TOP-LEVEL field (not inside ``params``, which
        is replayed into the GP) so the loss can be joined back to the reactor's
        run/condition records and reported in notifications. Without it the loss
        sent to Slack and written to the durable history.csv was always
        None/blank."""
        loss = self.loss(size, pdi)
        rec = {"params": dict(params), "size": size, "pdi": pdi,
               "confidence": float(confidence), "loss": loss,
               "recipe_id": str(recipe_id or "")}
        if held:
            rec["held"] = str(held)
        if model:
            rec["model"] = str(model)
        self.history.append(rec)
        self._consider_best(rec)
        self._check_stop(rec)
        return rec

    def _consider_best(self, rec: dict) -> None:
        """Fold one history record into ``best``."""
        size, loss = rec["size"], rec["loss"]
        # Tiers, best first: trusted (sized, confident, not held) > untrusted
        # (low confidence) > held (wrong shape, waiting for the operator). A
        # held point is kept only so `best` is never None once there is data.
        # Track `best` among CONFIDENCE-GATED points. `best` is reported as the
        # recommended recipe on exhaustion, so a low-confidence lucky hit (a
        # railed/noisy fit whose size happens to land on target → lowest loss)
        # must not become the answer. A trusted point always beats an untrusted
        # one; within a tier, lowest loss wins; an all-failed campaign still keeps
        # its least-bad point so `best` is never None once there's data.
        held = bool(rec.get("held"))
        trusted = (not held and size is not None
                   and float(rec["confidence"]) >= self.confidence_min)
        tier = 0 if trusted else (2 if held else 1)
        cur = self.best
        cur_tier = None if cur is None else cur.get("_tier", 0 if cur.get("_trusted") else 1)
        if cur is None or tier < cur_tier or (tier == cur_tier and loss < cur["loss"]):
            self.best = {**rec, "_trusted": trusted, "_tier": tier}

    def replace_failed(self, recipe_id: str, size, pdi, confidence: float,
                       held: str | None = None, model: str | None = None) -> dict | None:
        """Put a LATE measurement in place of the failure recorded for it.

        A condition whose data did not arrive in time is told as a failure so the
        loop keeps moving (analyzer ``_expire_pending``). If its real result then
        lands, discarding it wastes a budget slot that was actually measured, so
        the failed record is overwritten in place: the budget count is unchanged,
        ``best`` is recomputed, and a confident in-spec late result can still
        converge the campaign. Only a record marked ``expired`` (a timeout, not
        a measured-but-unsized profile) is replaced, and only once. Returns the
        new record, or None when this recipe_id has nothing to replace."""
        rid = str(recipe_id or "")
        if not rid:
            return None
        for i, h in enumerate(self.history):
            if (h.get("recipe_id") == rid and h.get("size") is None
                    and h.get("expired")):
                break
        else:
            return None
        rec = {"params": dict(h["params"]), "size": size, "pdi": pdi,
               "confidence": float(confidence), "loss": self.loss(size, pdi),
               "recipe_id": rid, "late": True}
        if held:
            rec["held"] = str(held)
        if model:
            rec["model"] = str(model)
        self.history[i] = rec
        self.best = None
        for r in self.history:
            self._consider_best(r)
        if self.status_str in ("running", "exhausted"):
            self._check_stop(rec)
        return rec

    def accept(self, recipe_id: str) -> dict | None:
        """The operator accepts a HELD result: it now trains the surrogate and may
        be the best or the hit. Returns the record, or None if nothing was held
        under this recipe_id."""
        rid = str(recipe_id or "")
        for rec in self.history:
            if rid and rec.get("recipe_id") == rid and rec.get("held"):
                rec.pop("held", None)
                rec["accepted"] = True
                break
        else:
            return None
        self.best = None
        for r in self.history:
            self._consider_best(r)
        if self.status_str == "running":
            self._check_stop(rec)
        return rec

    def held(self) -> list[dict]:
        """Results waiting for the operator: recipe_id, reason, model, size."""
        return [{"recipe_id": h.get("recipe_id", ""), "reason": h.get("held"),
                 "model": h.get("model"), "size": h.get("size"), "pdi": h.get("pdi")}
                for h in self.history if h.get("held")]

    def _check_stop(self, rec):
        size, pdi, conf = rec["size"], rec["pdi"], rec["confidence"]
        hit = (size is not None and not rec.get("held") and conf >= self.confidence_min
               and abs(size - self.target_size) <= self.tolerance
               and (pdi is None or pdi <= self.pdi_cap))
        if hit and self.converged_condition is None:
            self.converged_condition = rec        # first confident in-spec recipe
        if hit and self.stop_on_hit:
            self.status_str = "converged"
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
        # Exclude FAILED measurements (unsized profiles / the _FAIL_LOSS sentinel):
        # a single 1e3 outlier dominates np.var(y) and the base noise floor, so the
        # GP posterior goes flat everywhere and EI can't discriminate. Failures
        # still count against the budget; they just don't train the surrogate.
        # HELD results (not the expected shape) wait for the operator: training on
        # an equivalent size of the wrong structure would steer the search.
        hist = [h for h in hist if h.get("size") is not None and h["loss"] < _FAIL_LOSS
                and not h.get("held")]
        # warm start: earlier campaigns' measurements, scored against THIS target
        hist = hist + [{"params": pp["params"], "loss": self.loss(pp["size"], pp.get("pdi")),
                        "confidence": float(pp.get("confidence") or 0.0)}
                       for pp in self.prior]
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
        if self.work_bounds:
            try:
                pool = self.space.sub(self.work_bounds).sobol(int(n), seed=self.seed + off)
                if pool:
                    return pool
            except ValueError:
                pass                     # an empty working box never stops the loop
        return self.space.sobol(int(n), seed=self.seed + off)

    def _suggest_bo(self) -> dict:
        # y here is already on the transformed scale, and so is gp — EI must be
        # evaluated against the incumbent on that same scale.
        fit = self.fit_surrogate()
        if fit is None:
            # Every result so far failed (no sized fit) → no surrogate to fit.
            # Fall back to a fresh constraint-valid random point rather than
            # crashing on a None unpack; keep exploring until something sizes.
            pool = self.candidate_pool(1)
            return dict(pool[0]) if pool else dict(self._seeds[0])
        gp, X, y = fit
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
            "confidence_min": self.confidence_min,
            "n_held": sum(1 for h in self.history if h.get("held")),
            "n_prior": len(self.prior),
        }
