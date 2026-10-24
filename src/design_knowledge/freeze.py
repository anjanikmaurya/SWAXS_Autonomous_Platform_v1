"""
src/design_knowledge/freeze.py: what a campaign takes from a knowledge set at
Start, fixed for the whole run.

``for_campaign(req, space)`` reads the request's knowledge part

    {"set_id": "...", "use": {"literature": true, "notes": true, "campaigns": true},
     "n": 3}

and returns the frozen snapshot recorded with the campaign (set id, version,
the approved cards used, the campaigns used), the starting recipes from
knowledge (inside the range the cards allow) and the warm-start points from
past campaigns. Drafts and retired cards are never used.
"""
from __future__ import annotations

from . import store
from .campaigns import prior_points
from .cards import knowledge_seeds

MAX_KNOWLEDGE_SEEDS = 10


def for_campaign(req: dict | None, space) -> dict:
    out = {"frozen": None, "seeds": [], "cited": {}, "prior": [], "warnings": [], "errors": []}
    if not req or not req.get("set_id"):
        return out
    try:
        s = store.load_set(str(req["set_id"]))
    except (ValueError, OSError):
        out["errors"].append("the chosen knowledge set does not exist")
        return out
    use = req.get("use") or {}
    kinds = [k for k, flag in (("literature", use.get("literature", True)),
                               ("note", use.get("notes", True))) if flag]
    cards = [c for c in s.get("cards") or [] if c.get("status") == "approved" and c.get("kind") in kinds]
    try:
        n = int(req.get("n", 0) or 0)
    except (TypeError, ValueError):
        n, out["errors"] = 0, ["recipes from knowledge must be a whole number"]
    if n < 0 or n > MAX_KNOWLEDGE_SEEDS:
        out["errors"].append(f"recipes from knowledge must be 0 to {MAX_KNOWLEDGE_SEEDS}")
        n = 0
    seeds, cited, warns = knowledge_seeds(n, cards, space, seed=1) if n else ([], {}, [])
    out["seeds"], out["cited"] = seeds, cited
    out["warnings"] += warns
    campaigns = (s.get("campaigns") or []) if use.get("campaigns", True) else []
    prior, notes = prior_points(campaigns, space)
    out["prior"] = prior
    out["warnings"] += notes
    out["frozen"] = {"set_id": s["id"], "name": s.get("name"), "version": s.get("version"),
                     "use": {"literature": "literature" in kinds, "notes": "note" in kinds,
                             "campaigns": bool(use.get("campaigns", True))},
                     "cards": [{k: c.get(k) for k in ("id", "param", "effect", "range", "kind",
                                                       "source", "limit_like")} for c in cards],
                     "campaigns": [e.get("campaign_id") for e in campaigns],
                     "n_prior": len(prior), "n_knowledge_seeds": len(seeds), "cited": cited}
    return out
