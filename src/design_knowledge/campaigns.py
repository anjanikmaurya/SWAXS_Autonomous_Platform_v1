"""
src/design_knowledge/campaigns.py: finished campaigns as knowledge.

A finished campaign joins its knowledge set as an entry that carries its own
measured runs (so the set travels without the project folder):

    {"campaign_id": "...", "run_tag": "Run3", "project": "<path>", "goal": "...",
     "target_size": 2.5, "n": 24, "best": {"size": .., "pdi": ..},
     "runs": [{"params": {...}, "size": .., "pdi": .., "confidence": ..}, ...]}

``prior_points`` turns ticked entries into warm-start points for the optimiser:
measured size and PDI per recipe, which the controller scores against the NEW
campaign's target (CampaignController(prior=...)). Failed, held and
out-of-range runs are left out.
"""
from __future__ import annotations

MAX_RUNS = 400


def entry_from_campaign(record: dict, history: list[dict], project: str = "") -> dict:
    runs = []
    for h in history or []:
        if h.get("size") is None or h.get("held") or h.get("expired"):
            continue
        p = h.get("params") or {}
        runs.append({"params": {k: float(v) for k, v in p.items()},
                     "size": float(h["size"]),
                     "pdi": None if h.get("pdi") is None else float(h["pdi"]),
                     "confidence": float(h.get("confidence") or 0.0)})
    best = record.get("best") or (record.get("outcome") or {}).get("best") or {}
    return {"campaign_id": str(record.get("campaign_id") or ""),
            "run_tag": str(record.get("run_tag") or ""),
            "project": str(project or ""),
            "goal": str(record.get("goal") or ""),
            "target_size": record.get("target_size"),
            "ended": str(record.get("ended_at") or ""),
            "n": len(history or []),
            "best": {"size": best.get("size"), "pdi": best.get("pdi")},
            "runs": runs[-MAX_RUNS:]}


def prior_points(entries: list[dict], space, campaign_ids=None) -> tuple[list[dict], list[str]]:
    """Warm-start points from the chosen campaigns that lie in this search range."""
    pts, notes = [], []
    want = set(campaign_ids) if campaign_ids is not None else None
    for e in entries or []:
        if want is not None and e.get("campaign_id") not in want:
            continue
        kept = 0
        for r in e.get("runs") or []:
            p = r.get("params") or {}
            try:
                ok = space.valid(p)
            except Exception:                                  # noqa: BLE001
                ok = False
            if not ok or r.get("size") is None:
                continue
            pts.append({"params": dict(p), "size": float(r["size"]), "pdi": r.get("pdi"),
                        "confidence": float(r.get("confidence") or 0.0),
                        "from": e.get("run_tag") or e.get("campaign_id")})
            kept += 1
        skipped = len(e.get("runs") or []) - kept
        if skipped:
            notes.append(f"{e.get('run_tag') or e.get('campaign_id')}: {skipped} run(s) outside "
                         f"this search range left out")
    return pts, notes
