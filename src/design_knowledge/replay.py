"""
src/design_knowledge/replay.py: did the knowledge help? Replay past real
campaigns, and keep a harness change only if it does better on a campaign it
was not tuned on.

The simulator alone cannot judge literature knowledge (its chemistry is
invented), so the test uses the set's own finished campaigns:

1. For a past campaign, an EMULATOR is fitted to its measured runs (a Gaussian
   process for diameter and one for PDI over the five settings). It answers
   "what would this recipe have given?" close to what was really measured, and
   regresses to the average far from it.
2. Each ARM (a method, with or without a source of knowledge) runs a fresh
   campaign against that emulator, with the campaign's own target, for the
   same budget, several times with different seeds.
3. Scores: runs to reach the spec (a confident run within tolerance and under
   the PDI cap; not reached = budget + 1), and the best score after 10 runs.

Arms:
    random              the reference every method must beat
    gp_ei               GP + EI, no knowledge
    gp_ei_warm          + the set's OTHER campaigns as a warm start
    gp_ei_knowledge     + starting recipes from the approved cards
    ai_copilot          AI Co-pilot with cards and verdicts (only when asked:
                        it calls the AI; without a login it runs as GP + EI)

Split: the most recent campaign is HELD OUT; the others are practice.
``propose_improvement`` tries alternative harness settings on the practice
campaigns and proposes one only if it also beats the current settings on the
held-out campaign. The operator approves it (store.set_harness); nothing here
changes the set.

Honest limits, stated in every result: an emulator is only as good as the runs
it was fitted on; with one or two campaigns the result is indicative; cards
written after a campaign may already contain what that campaign taught.
"""
from __future__ import annotations

import time

import numpy as np

from src.optimizer import methods as _methods
from src.optimizer.gp import GP

from .campaigns import prior_points
from .cards import knowledge_seeds
from .store import HARNESS_DEFAULTS, harness

ARMS = ("random", "gp_ei", "gp_ei_warm", "gp_ei_knowledge")
AI_ARMS = ("ai_copilot",)
LABELS = {"random": "Random", "gp_ei": "GP + EI", "gp_ei_warm": "GP + EI + past campaigns",
          "gp_ei_knowledge": "GP + EI + recipes from cards", "ai_copilot": "AI Co-pilot + cards"}
MIN_RUNS = 5
NOISE_FRAC = 0.02
CONF = 0.9


class Emulator:
    """What a past campaign's recipes gave, interpolated by two GPs."""

    def __init__(self, entry: dict, space):
        runs = [r for r in entry.get("runs") or []
                if r.get("size") is not None and space.valid(r.get("params") or {})]
        if len(runs) < MIN_RUNS:
            raise ValueError(f"{entry.get('run_tag') or entry.get('campaign_id')}: only "
                             f"{len(runs)} measured run(s) in this search range (need {MIN_RUNS})")
        self.space = space
        X = np.array([space.to_unit(r["params"]) for r in runs])
        R = np.array([float(r["size"]) for r in runs])
        P = np.array([float(r["pdi"]) if r.get("pdi") is not None else 0.3 for r in runs])
        self.gR = GP(length_scale=0.3).fit(X, R, 0.02 * max(np.var(R), 1e-6) + 1e-6)
        self.gP = GP(length_scale=0.3).fit(X, P, 0.02 * max(np.var(P), 1e-6) + 1e-6)
        self.n = len(runs)

    def __call__(self, params: dict, rng) -> tuple[float, float]:
        u = self.space.to_unit(params)[None, :]
        R = float(self.gR.predict(u)[0][0])
        P = float(self.gP.predict(u)[0][0])
        R = max(R * (1.0 + NOISE_FRAC * rng.standard_normal()), 0.1)
        P = min(max(P + 0.005 * rng.standard_normal(), 0.005), 1.0)
        return R, P


def _target(entry: dict) -> dict | None:
    try:
        t = float(entry["target_size"])
        tol = float(entry["tolerance"])
        cap = float(entry["pdi_cap"])
    except (KeyError, TypeError, ValueError):
        return None
    if t <= 0 or tol <= 0 or cap <= 0:
        return None
    return {"target_size": t, "tolerance": tol, "pdi_cap": cap}


def _hit(size, pdi, tgt) -> bool:
    return abs(size - tgt["target_size"]) <= tgt["tolerance"] and pdi <= tgt["pdi_cap"]


def run_arm(arm: str, held: dict, others: list[dict], cards: list[dict], verdicts: list[dict],
            space, *, h: dict, repeats: int = 3, budget: int | None = None,
            client_factory=None) -> dict:
    """One arm on one held-out campaign; returns its scores over the repeats."""
    tgt = _target(held)
    emu = Emulator(held, space)
    B = int(budget or min(max(int(held.get("n") or 0), 12), 30))
    n_init = min(10, max(2, B // 2))
    to_spec, best10, fallbacks = [], [], 0
    for r in range(max(1, int(repeats))):
        cfg = dict(tgt, budget=B, n_init=n_init, seed=r, stop_on_hit=False)
        inner = _methods.make("random" if arm == "random" else "gp_ei", space, **cfg)
        seeds, prior = [], []
        if arm == "gp_ei_warm":
            prior, _ = prior_points(others, space)
        if arm in ("gp_ei_knowledge", "ai_copilot"):
            seeds, _, _ = knowledge_seeds(min(int(h["n_knowledge"]), n_init), cards, space, seed=1 + r)
        ctl = inner
        if arm == "ai_copilot":
            from src.optimizer.ai_design import AIDesign            # noqa: PLC0415
            ctl = AIDesign(inner, "ai_copilot", k=int(h["shortlist_k"]), trust=float(h["trust"]),
                           knowledge={"cards": cards, "verdicts": verdicts}, every_pick=True,
                           **({"client_factory": client_factory} if client_factory else {}))
        ctl.start(seeds=seeds, prior=prior)
        rng = np.random.default_rng(1000 + r)
        first, scores = None, []
        for n in range(B):
            p = ctl.ask()
            if p is None:
                break
            R, P = emu(p, rng)
            ctl.tell(p, R, P, CONF, recipe_id=f"replay_{arm}_{r}_{n}")
            scores.append(ctl.history[-1]["loss"])
            if first is None and _hit(R, P, tgt):
                first = n + 1
        to_spec.append(first if first is not None else B + 1)
        best10.append(min(scores[:10]) if scores else None)
        fallbacks += int(getattr(ctl, "n_ai_fallbacks", 0) or 0)
    b10 = [x for x in best10 if x is not None]
    return {"arm": arm, "label": LABELS[arm], "budget": B,
            "runs_to_spec": float(np.median(to_spec)),
            "reached": sum(1 for x in to_spec if x <= B), "repeats": len(to_spec),
            "best_after_10": float(np.mean(b10)) if b10 else None, "fallbacks": fallbacks}


def _usable(entries: list[dict], space) -> tuple[list[dict], list[str]]:
    ok, notes = [], []
    for e in entries:
        name = e.get("run_tag") or e.get("campaign_id")
        if _target(e) is None:
            notes.append(f"{name}: no target, tolerance or PDI cap recorded: left out")
            continue
        try:
            Emulator(e, space)
        except ValueError as exc:
            notes.append(f"{exc}: left out")
            continue
        ok.append(e)
    ok.sort(key=lambda e: e.get("ended") or "")
    return ok, notes


def evaluate(s: dict, space, *, arms=ARMS, repeats: int = 3, h: dict | None = None,
             held_ids=None, client_factory=None, budget: int | None = None) -> dict:
    """Replay every usable campaign of a set (or ``held_ids``) for each arm."""
    h = h or harness(s)
    camps, notes = _usable(s.get("campaigns") or [], space)
    cards = [c for c in s.get("cards") or [] if c.get("status") == "approved"]
    verdicts = [v for v in s.get("verdicts") or [] if v.get("status") != "retired"]
    held = [c for c in camps if held_ids is None or c.get("campaign_id") in set(held_ids)]
    per = []
    for c in held:
        others = [o for o in camps if o is not c]
        vs = [v for v in verdicts if v.get("campaign_id") != c.get("campaign_id")]   # no leakage
        rows = [run_arm(a, c, others, cards, vs, space, h=h, repeats=repeats,
                        client_factory=client_factory, budget=budget) for a in arms]
        per.append({"campaign": c.get("run_tag") or c.get("campaign_id"),
                    "campaign_id": c.get("campaign_id"), "rows": rows})
    summary = []
    for a in arms:
        rs = [r for p in per for r in p["rows"] if r["arm"] == a]
        if rs:
            b10 = [r["best_after_10"] for r in rs if r["best_after_10"] is not None]
            summary.append({"arm": a, "label": LABELS[a],
                            "runs_to_spec": float(np.mean([r["runs_to_spec"] for r in rs])),
                            "reached": sum(r["reached"] for r in rs),
                            "trials": sum(r["repeats"] for r in rs),
                            "best_after_10": float(np.mean(b10)) if b10 else None,
                            "fallbacks": sum(r["fallbacks"] for r in rs)})
    caveat = ("No usable past campaign yet: finish a campaign with this set first." if not camps else
              ("Indicative only: one past campaign, no held-out split." if len(camps) == 1 else
               f"Indicative: {len(camps)} past campaigns; each is replayed with an emulator fitted "
               f"to its own measured runs."))
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "set_version": s.get("version"),
            "harness": h, "repeats": repeats, "campaigns": [p["campaign"] for p in per],
            "per_campaign": per, "summary": summary, "notes": notes, "caveat": caveat}


def _score(ev: dict, arm: str) -> tuple:
    row = next((r for r in ev["summary"] if r["arm"] == arm), None)
    if row is None:
        return (float("inf"), float("inf"))
    return (row["runs_to_spec"], row["best_after_10"] if row["best_after_10"] is not None else float("inf"))


def propose_improvement(s: dict, space, *, repeats: int = 3, include_ai: bool = False,
                        client_factory=None) -> dict:
    """Try alternative settings on the PRACTICE campaigns; propose the best only
    if it also beats the current settings on the HELD-OUT campaign."""
    camps, notes = _usable(s.get("campaigns") or [], space)
    if len(camps) < 2:
        return {"ok": False, "why": "needs at least 2 usable past campaigns (one to tune on, "
                                    "one to judge on)", "notes": notes}
    held_id, practice_ids = camps[-1]["campaign_id"], [c["campaign_id"] for c in camps[:-1]]
    cur = harness(s)
    cands = [{**cur, "n_knowledge": n} for n in (0, 3, 5) if n != cur["n_knowledge"]]
    arm = "gp_ei_knowledge"
    if include_ai:
        arm = "ai_copilot"
        cands += [{**cur, "shortlist_k": k} for k in (3, 5, 8) if k != cur["shortlist_k"]]
    arms = (arm,)

    def ev(h, ids):
        return evaluate(s, space, arms=arms, repeats=repeats, h=h, held_ids=ids,
                        client_factory=client_factory)

    base_p = ev(cur, practice_ids)
    best, best_p = None, base_p
    for h in cands:
        e = ev(h, practice_ids)
        if _score(e, arm) < _score(best_p, arm):
            best, best_p = h, e
    out = {"ok": True, "arm": arm, "current": cur, "held_out": camps[-1].get("run_tag") or held_id,
           "practice": [c.get("run_tag") or c["campaign_id"] for c in camps[:-1]],
           "practice_score": {"current": _score(base_p, arm)[0]}, "notes": notes,
           "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "set_version": s.get("version")}
    if best is None:
        out.update(proposal=None, why="no alternative did better on the practice campaigns")
        return out
    out["practice_score"]["proposed"] = _score(best_p, arm)[0]
    base_h = ev(cur, [held_id])
    new_h = ev(best, [held_id])
    out["heldout_score"] = {"current": _score(base_h, arm)[0], "proposed": _score(new_h, arm)[0]}
    if _score(new_h, arm) < _score(base_h, arm):
        out.update(proposal={k: best[k] for k in HARNESS_DEFAULTS},
                   why="better on the practice campaigns and on the held-out campaign")
    else:
        out.update(proposal=None, why="better on practice but not on the held-out campaign: not proposed")
    return out
