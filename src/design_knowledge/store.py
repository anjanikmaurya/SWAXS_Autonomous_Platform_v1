"""
src/design_knowledge/store.py: knowledge sets on disk.

One JSON file per set, ``ai_knowledge/design/<set_id>.json`` (in git, so a set
travels with the code; ``SWAXS_DESIGN_KNOWLEDGE_DIR`` overrides the folder):

    {"id": "pd_ode_oleylamine", "name": "Pd · ODE · oleylamine",
     "chemistry": {"keywords": ["Pd", "ODE", "oleylamine"]},
     "version": 4, "updated_at": "...",
     "cards": [...],        # approved, draft and retired cards (see cards.py)
     "verdicts": [...],     # the operator's Good / Bad calls on past picks
     "campaigns": [...],    # finished campaigns, with their measured runs
     "harness": {...},      # settings the campaigns use (HARNESS_DEFAULTS)
     "design_use": {...},   # which design parts campaigns use (DESIGN_USE_DEFAULTS)
     "fitter": {...},       # the AI fitter's part: sources, notes, judged fits,
                            #   settings, evaluations (src/fitting/fit_knowledge.py)
     "evaluations": [...],  # replay results (src/design_knowledge/replay.py)
     "proposal": {...},     # an improvement waiting for the operator's approval
     "log": [...]}          # what changed, newest last (version, what, who)

Every change bumps ``version`` and is written atomically (temp file, then
rename) under a lock, so a crash never leaves half a set. Nothing is deleted:
a removed card is marked "retired" and stays in the file.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .cards import PREFIX, validate_card

_ROOT = Path(__file__).resolve().parents[2]

#: Settings a campaign takes from its set. Changed only by the operator, or by
#: an improvement the operator approves (replay.propose_improvement).
HARNESS_DEFAULTS = {"n_knowledge": 3, "shortlist_k": 5, "trust": 0.25}
HARNESS_LIMITS = {"n_knowledge": (0, 10), "shortlist_k": (2, 8), "trust": (0.05, 0.6)}
#: What the design loop takes from the base, ticked in the Knowledge tab (Setup
#: only picks the base by name). every_pick: an AI level also reads the cards
#: and verdicts at every pick or review, not only for the starting recipes.
DESIGN_USE_DEFAULTS = {"literature": True, "notes": True, "campaigns": True,
                       "verdicts": True, "every_pick": False}
MAX_VERDICTS = 500
MAX_EVALS = 30
_LOCK = threading.RLock()
MAX_LOG = 200


def design_dir() -> Path:
    d = os.environ.get("SWAXS_DESIGN_KNOWLEDGE_DIR")
    return Path(d) if d else _ROOT / "ai_knowledge" / "design"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s[:60] or "set"


def _path(set_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_]{1,60}", set_id or ""):
        raise ValueError("unknown knowledge set")
    return design_dir() / f"{set_id}.json"


def _write(s: dict) -> None:
    p = _path(s["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.part")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _bump(s: dict, what: str, who: str = "") -> dict:
    s["version"] = int(s.get("version") or 0) + 1
    s["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    s.setdefault("log", []).append({"version": s["version"], "what": what[:200],
                                    "who": who[:80], "at": s["updated_at"]})
    s["log"] = s["log"][-MAX_LOG:]
    return s


def _keywords(chemistry) -> list[str]:
    if isinstance(chemistry, dict):
        kw = chemistry.get("keywords") or []
    else:
        kw = chemistry or []
    return [str(k).strip() for k in kw if str(k).strip()][:12]


def list_sets() -> list[dict]:
    out = []
    d = design_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cards = s.get("cards") or []
        out.append({"id": s.get("id"), "name": s.get("name"), "version": s.get("version", 0),
                    "keywords": _keywords(s.get("chemistry")),
                    "n_cards": sum(1 for c in cards if c.get("status") == "approved"),
                    "n_drafts": sum(1 for c in cards if c.get("status") == "draft"),
                    "n_verdicts": sum(1 for v in s.get("verdicts") or [] if v.get("status") != "retired"),
                    "n_campaigns": len(s.get("campaigns") or []),
                    "harness": harness(s), "design_use": design_use(s),
                    "fitter": _fitter_summary(s)})
    return out


def design_use(s: dict) -> dict:
    u = dict(DESIGN_USE_DEFAULTS)
    for k in u:
        if k in (s.get("design_use") or {}):
            u[k] = bool(s["design_use"][k])
    return u


def set_design_use(set_id: str, use: dict, who: str = "") -> dict:
    with _LOCK:
        s = load_set(set_id)
        u = design_use({"design_use": {k: v for k, v in (use or {}).items() if k in DESIGN_USE_DEFAULTS}})
        s["design_use"] = u
        _bump(s, "design loop uses " + ", ".join(k for k, v in u.items() if v), who)
        _write(s)
        return u


def _fitter_summary(s: dict) -> dict:
    f = s.get("fitter") or {}
    fits = [x for x in f.get("fits") or [] if x.get("status") != "retired"]
    notes = f.get("notes") or []
    return {"sources": f.get("sources"),
            "n_notes": sum(1 for n in notes if n.get("status") == "active"),
            "n_drafts": sum(1 for n in notes if n.get("status") == "draft"),
            "n_fits": len(fits), "n_labelled": sum(1 for x in fits if x.get("label"))}


def load_set(set_id: str) -> dict:
    p = _path(set_id)
    if not p.is_file():
        raise ValueError("unknown knowledge set")
    return json.loads(p.read_text(encoding="utf-8"))


def create_set(name: str, keywords=None, who: str = "") -> dict:
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise ValueError("give the set a name")
    with _LOCK:
        base = _slug(name)
        sid, i = base, 2
        while _path(sid).exists():
            sid = f"{base[:56]}_{i}"
            i += 1
        s = {"id": sid, "name": name, "chemistry": {"keywords": _keywords(keywords)},
             "version": 0, "cards": [], "campaigns": [], "log": []}
        _bump(s, "created", who)
        _write(s)
        return s


def _next_id(s: dict, kind: str) -> str:
    pre = PREFIX.get(kind, "N")
    nums = [int(c["id"][1:]) for c in s.get("cards") or []
            if str(c.get("id", "")).startswith(pre) and c["id"][1:].isdigit()]
    return f"{pre}{(max(nums) + 1) if nums else 1}"


def add_drafts(set_id: str, drafts: list[dict], who: str = "", what: str = "drafts added") -> list[dict]:
    """Add cards as DRAFTS (never approved on the way in). Returns the stored drafts."""
    added = []
    with _LOCK:
        s = load_set(set_id)
        for d in drafts:
            card, errs = validate_card({**d, "added_by": d.get("added_by") or who}, status="draft")
            if not card:
                continue
            card["id"] = _next_id(s, card["kind"])
            s.setdefault("cards", []).append(card)
            added.append(card)
        if added:
            _bump(s, f"{what}: {len(added)}", who)
            _write(s)
    return added


def review(set_id: str, card_id: str, action: str, edit: dict | None = None,
           who: str = "") -> dict:
    """approve | reject | edit | retire. Edit keeps the card's status unless it is
    a draft being approved with the edit (action "approve" with ``edit``)."""
    if action not in ("approve", "reject", "edit", "retire"):
        raise ValueError("unknown action")
    with _LOCK:
        s = load_set(set_id)
        for i, c in enumerate(s.get("cards") or []):
            if c.get("id") == card_id:
                break
        else:
            raise ValueError("unknown card")
        if action in ("edit", "approve") and edit:
            merged = {**c, **{k: v for k, v in edit.items()
                              if k in ("param", "effect", "range", "source", "kind")}}
            card, errs = validate_card(merged, status=c["status"])
            if not card:
                raise ValueError("; ".join(errs))
            card["id"] = c["id"]
            if card["kind"] != c["kind"]:
                card["kind"] = c["kind"]            # the id prefix follows the kind it was born with
            c = card
        if action == "approve":
            c["status"] = "approved"
        elif action == "reject":
            c["status"] = "retired"
            c["retired_why"] = "rejected draft"
        elif action == "retire":
            c["status"] = "retired"
        c["reviewed_by"] = who[:80]
        c["reviewed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        s["cards"][i] = c
        _bump(s, f"{action} {card_id}", who)
        _write(s)
        return c


def add_campaign(set_id: str, entry: dict, who: str = "") -> bool:
    """Join a finished campaign to the set (once per campaign_id)."""
    with _LOCK:
        s = load_set(set_id)
        if any(e.get("campaign_id") == entry.get("campaign_id") for e in s.get("campaigns") or []):
            return False
        s.setdefault("campaigns", []).append(entry)
        _bump(s, f"campaign {entry.get('run_tag') or entry.get('campaign_id')} joined", who)
        _write(s)
        return True


def harness(s: dict) -> dict:
    """The set's harness settings, filled with the defaults and clamped to the limits."""
    h = {**HARNESS_DEFAULTS, **(s.get("harness") or {})}
    out = {}
    for k, d in HARNESS_DEFAULTS.items():
        lo, hi = HARNESS_LIMITS[k]
        try:
            v = type(d)(h.get(k, d))
        except (TypeError, ValueError):
            v = d
        out[k] = min(max(v, lo), hi)
    return out


def set_harness(set_id: str, settings: dict, who: str = "", why: str = "") -> dict:
    with _LOCK:
        s = load_set(set_id)
        new = harness({"harness": {**harness(s), **{k: v for k, v in (settings or {}).items()
                                                      if k in HARNESS_DEFAULTS}}})
        if new == harness(s):
            return new
        s["harness"] = new
        _bump(s, "harness " + ", ".join(f"{k}={v}" for k, v in new.items()) + (f" ({why})" if why else ""), who)
        _write(s)
        return new


def add_verdict(set_id: str, verdict: dict, who: str = "") -> dict:
    """The operator's Good / Bad call on a pick. Counts straight away (it is the
    operator's own judgement, not a draft). One per recipe: a new call replaces
    the old one, which is kept as retired."""
    call = str(verdict.get("call") or "")
    if call not in ("good", "bad"):
        raise ValueError("the call must be good or bad")
    why = " ".join(str(verdict.get("why") or "").split())[:240]
    with _LOCK:
        s = load_set(set_id)
        vs = s.setdefault("verdicts", [])
        rid = str(verdict.get("recipe_id") or "")
        for v in vs:
            if rid and v.get("recipe_id") == rid and v.get("status") != "retired":
                v["status"] = "retired"
                v["retired_why"] = "replaced by a new call"
        nums = [int(v["id"][1:]) for v in vs if str(v.get("id", "")).startswith("V") and v["id"][1:].isdigit()]
        rec = {"id": f"V{(max(nums) + 1) if nums else 1}", "call": call, "why": why,
               "recipe_id": rid, "campaign_id": str(verdict.get("campaign_id") or ""),
               "run_tag": str(verdict.get("run_tag") or ""),
               "chosen_by": str(verdict.get("chosen_by") or "")[:60],
               "params": {k: float(v) for k, v in (verdict.get("params") or {}).items()},
               "result": verdict.get("result") or {}, "reason": str(verdict.get("reason") or "")[:300],
               "status": "approved", "added_by": who[:80],
               "added_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        vs.append(rec)
        s["verdicts"] = vs[-MAX_VERDICTS:]
        _bump(s, f"verdict {rec['id']} ({call}) on {rid}", who)
        _write(s)
        return rec


def retire_verdict(set_id: str, vid: str, who: str = "") -> bool:
    with _LOCK:
        s = load_set(set_id)
        for v in s.get("verdicts") or []:
            if v.get("id") == vid and v.get("status") != "retired":
                v["status"] = "retired"
                _bump(s, f"retire {vid}", who)
                _write(s)
                return True
    return False


def add_evaluation(set_id: str, ev: dict) -> None:
    with _LOCK:
        s = load_set(set_id)
        s.setdefault("evaluations", []).append(ev)
        s["evaluations"] = s["evaluations"][-MAX_EVALS:]
        _write(s)                          # a result is not a knowledge change: no version bump


def set_proposal(set_id: str, proposal: dict | None, who: str = "") -> None:
    with _LOCK:
        s = load_set(set_id)
        s["proposal"] = proposal
        _write(s)


def export_set(set_id: str) -> dict:
    s = load_set(set_id)
    return {"format": "swaxs-design-knowledge", "format_version": 1, "set": s}


def import_set(payload: dict, who: str = "") -> dict:
    """A new set from an export. Every card arrives as a DRAFT: knowledge from
    another lab is reviewed here before it can steer a campaign. Campaign
    entries are kept (they are measurements, not claims)."""
    if not isinstance(payload, dict) or payload.get("format") != "swaxs-design-knowledge":
        raise ValueError("not a design-knowledge export")
    src = payload.get("set") or {}
    s = create_set(f"{src.get('name') or 'Imported set'} (imported)",
                   _keywords(src.get("chemistry")), who)
    add_drafts(s["id"], [{**c, "status": "draft"} for c in src.get("cards") or []
                         if c.get("status") != "retired"],
               who, what=f"imported from {src.get('id') or '?'} v{src.get('version', '?')}")
    for e in (src.get("campaigns") or [])[:50]:
        if isinstance(e, dict) and e.get("campaign_id"):
            add_campaign(s["id"], e, who)
    fit = src.get("fitter") or {}
    if isinstance(fit, dict) and (fit.get("notes") or fit.get("fits") or fit.get("sources")):
        with _LOCK:
            s = load_set(s["id"])
            s["fitter"] = {"sources": fit.get("sources"),
                           # notes from elsewhere are reviewed first, like cards
                           "notes": [dict(n, status="draft") for n in fit.get("notes") or []
                                     if isinstance(n, dict) and n.get("status") != "retired"][:200],
                           "fits": [x for x in fit.get("fits") or []
                                    if isinstance(x, dict) and x.get("status") != "retired"][:1000],
                           "settings": fit.get("settings") or {}}
            _bump(s, "fitter part imported (notes as drafts)", who)
            _write(s)
    if isinstance(src.get("design_use"), dict):
        set_design_use(s["id"], src["design_use"], who)
    return load_set(s["id"])


def matches(set_summary_or_set: dict, chemistry: dict | None) -> bool | None:
    """Does a set fit this project's chemistry.yml? None when either says nothing."""
    kws = [k.lower() for k in (set_summary_or_set.get("keywords")
                               or _keywords(set_summary_or_set.get("chemistry")))]
    if not kws or not chemistry:
        return None
    blob = json.dumps(chemistry, ensure_ascii=False).lower()
    return all(k in blob for k in kws)
