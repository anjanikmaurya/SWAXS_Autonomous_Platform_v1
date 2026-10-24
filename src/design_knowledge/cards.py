"""
src/design_knowledge/cards.py: typed design-knowledge cards.

A card says one thing about one recipe setting:

    {"id": "L3", "param": "T_reac", "effect": "higher -> larger particles",
     "range": [240, 290], "kind": "literature", "source": "Smith 2021, Fig. 3",
     "status": "approved", "limit_like": false, ...}

``param`` is one of the optimiser's own settings (src.optimizer.space.NAMES) or
"all"; ``range`` is [lo, hi] with either side None (one-sided); ``kind`` is
"literature" or "note". Drafts carry ``passage``: the text they came from, so
the operator can check them before approving.

Nothing here talks to the network or the reactor.
"""
from __future__ import annotations

import math
import re
import time

from src.optimizer.space import NAMES, ParameterSpace

KINDS = ("literature", "note")
STATUSES = ("approved", "draft", "retired")
MAX_EFFECT = 240
MAX_SOURCE = 200
MAX_PASSAGE = 600
PREFIX = {"literature": "L", "note": "N"}

UNITS = {"T_reac": "°C", "F_tot": "µL/min", "x_ODE": "", "x_TOP": "", "x_oley": ""}

#: words that make a statement read like a safety limit, not a trend
_LIMIT_WORDS = ("boil", "decompos", "unsafe", "never", "maximum", "minimum", "limit",
                "clog", "leak", "pressure", "burn", "explod", "ignite", "flash point",
                "melt", "freez", "do not exceed")
#: words that say the stated side is the BAD side ("above 300 °C it boils")
_BAD_WORDS = ("boil", "decompos", "unsafe", "aggregat", "clump", "unstable", "clog",
              "fail", "bad", "avoid", "worse", "precipitat", "broad", "poly", "leak")

_ALIASES = [
    ("T_reac", (r"\bt_reac\b", r"\btemperature\b", r"\btemp\b", r"°\s*c\b", r"\bdeg(?:rees)?\s*c\b")),
    ("F_tot", (r"\bf_tot\b", r"\bflow\b", r"[µu]l\s*/\s*min", r"\bresidence\b")),
    ("x_TOP", (r"\bx_top\b", r"\btop\b", r"trioctylphosphine")),
    ("x_oley", (r"\bx_oley\b", r"oleylamine", r"\boley\b")),
    ("x_ODE", (r"\bx_ode\b", r"\bode\b", r"octadecene")),
]
_NUM = r"(-?\d+(?:\.\d+)?)"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _f(v):
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"'{v}' is not a number")
    if not math.isfinite(x):
        raise ValueError("not a finite number")
    return x


def is_limit_like(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in _LIMIT_WORDS)


def validate_card(d: dict, *, status: str | None = None) -> tuple[dict | None, list[str]]:
    """Return (clean card, errors). Unknown keys are dropped; text is trimmed."""
    errs: list[str] = []
    if not isinstance(d, dict):
        return None, ["a card must be a set of fields"]
    param = str(d.get("param") or "").strip()
    if param not in NAMES and param != "all":
        errs.append(f"setting must be one of {', '.join(NAMES)} or all")
    effect = " ".join(str(d.get("effect") or "").split())[:MAX_EFFECT]
    if not effect:
        errs.append("say what the setting does")
    kind = str(d.get("kind") or "note")
    if kind not in KINDS:
        errs.append(f"kind must be one of {', '.join(KINDS)}")
    rng = d.get("range")
    lo = hi = None
    if rng not in (None, "", []):
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            errs.append("range must be [low, high] (either may be empty)")
        else:
            try:
                lo, hi = _f(rng[0]), _f(rng[1])
            except ValueError as exc:
                errs.append(f"range: {exc}")
            if lo is not None and hi is not None and lo > hi:
                errs.append("range low is above range high")
            if param == "all" and (lo is not None or hi is not None):
                errs.append("a range needs one setting, not all")
    st = status or str(d.get("status") or "draft")
    if st not in STATUSES:
        errs.append(f"status must be one of {', '.join(STATUSES)}")
    if errs:
        return None, errs
    card = {
        "id": str(d.get("id") or ""),
        "param": param,
        "effect": effect,
        "range": None if lo is None and hi is None else [lo, hi],
        "kind": kind,
        "source": " ".join(str(d.get("source") or "").split())[:MAX_SOURCE],
        "status": st,
        "limit_like": bool(d.get("limit_like")) or is_limit_like(effect),
        "added_at": str(d.get("added_at") or _now()),
        "added_by": str(d.get("added_by") or "")[:80],
    }
    if d.get("passage"):
        card["passage"] = " ".join(str(d["passage"]).split())[:MAX_PASSAGE]
    if d.get("doi"):
        card["doi"] = str(d["doi"])[:120]
    return card, []


def guess_param(text: str) -> str | None:
    """The recipe setting a sentence is about: explicit names first, then units, then words."""
    t = (text or "").lower()
    for name in NAMES:                         # explicit optimiser names win
        if name.lower() in t:
            return name
    if re.search(r"°\s*c\b|\bdeg(?:rees)?\s*c\b", t):
        return "T_reac"
    if re.search(r"[µu]l\s*/\s*min", t):
        return "F_tot"
    for name, pats in _ALIASES:
        if any(re.search(p, t) for p in pats):
            return name
    return None


def guess_range(text: str) -> list | None:
    """[lo, hi] from 'A to B', 'A-B', 'between A and B', 'above A', 'below B'.

    For a one-sided statement the BAD side is excluded when the sentence says it
    is bad ("above 300 °C the ODE boils" -> [None, 300]); otherwise the stated
    side is kept ("above 250 °C gives larger particles" -> [250, None])."""
    t = (text or "").lower().replace("–", "-").replace("—", "-")
    m = re.search(_NUM + r"\s*(?:°\s*c|[µu]l\s*/\s*min)?\s*(?:to|-|and)\s*" + _NUM, t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return [min(a, b), max(a, b)]
    bad = any(w in t for w in _BAD_WORDS)
    m = re.search(r"(?:above|over|more than|greater than|>\s*)\s*" + _NUM, t)
    if m:
        v = float(m.group(1))
        return [None, v] if bad else [v, None]
    m = re.search(r"(?:below|under|less than|<\s*)\s*" + _NUM, t)
    if m:
        v = float(m.group(1))
        return [v, None] if bad else [None, v]
    m = re.search(r"(?:max(?:imum)?|at most|up to)\s*" + _NUM, t)
    if m:
        return [None, float(m.group(1))]
    m = re.search(r"(?:min(?:imum)?|at least)\s*" + _NUM, t)
    if m:
        return [float(m.group(1)), None]
    return None


def notes_to_drafts(text: str, *, source: str = "", added_by: str = "") -> tuple[list[dict], list[str]]:
    """One note line -> one draft card (setting and range guessed, to be checked)."""
    drafts, skipped = [], []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split()).strip(" -*•")
        if len(line) < 4:
            continue
        param = guess_param(line)
        if param is None:
            skipped.append(f"no recipe setting found in: {line[:80]}")
            continue
        card, errs = validate_card({
            "param": param, "effect": line, "range": guess_range(line), "kind": "note",
            "source": source or "your note", "status": "draft", "passage": line,
            "added_by": added_by})
        if card:
            drafts.append(card)
        else:
            skipped.append("; ".join(errs))
    return drafts, skipped


# ── cards against the search range ───────────────────────────────────────────
def check_card(card: dict, space: ParameterSpace) -> dict:
    """How a card's range sits in the search bounds.

    Returns {"range": clipped [lo, hi] or None, "clipped": bool, "outside": bool,
    "note": text}. A card can only narrow: its range is intersected with the
    bounds, never extended past them."""
    out = {"range": None, "clipped": False, "outside": False, "note": ""}
    p, rng = card.get("param"), card.get("range")
    if p not in space.bounds or not rng:
        return out
    blo, bhi = space.bounds[p]
    lo = blo if rng[0] is None else float(rng[0])
    hi = bhi if rng[1] is None else float(rng[1])
    clo, chi = max(lo, blo), min(hi, bhi)
    if clo > chi:
        out.update(outside=True,
                   note=f"outside the search range {blo:g} to {bhi:g}: not used")
        return out
    out["range"] = [clo, chi]
    if (rng[0] is not None and float(rng[0]) < blo) or (rng[1] is not None and float(rng[1]) > bhi):
        out.update(clipped=True, note=f"clipped to {clo:g} to {chi:g} (search range)")
    return out


def narrowed_bounds(cards: list[dict], space: ParameterSpace) -> tuple[dict, dict, list[str]]:
    """Intersect the approved cards' ranges with the search bounds, per setting.

    Returns (bounds, cited, warnings): bounds for every setting (narrowed where
    cards say so), the card ids behind each narrowed setting, and warnings for
    cards that disagree (an empty intersection keeps the full range for that
    setting, so contradictory knowledge never shrinks the search to nothing)."""
    bounds = {k: tuple(v) for k, v in space.bounds.items()}
    cited: dict[str, list[str]] = {}
    warns: list[str] = []
    for p in space.bounds:
        rs = []
        for c in cards:
            if c.get("status") != "approved" or c.get("param") != p:
                continue
            chk = check_card(c, space)
            if chk["range"] is not None:
                rs.append((c.get("id", "?"), chk["range"]))
        if not rs:
            continue
        lo = max(r[1][0] for r in rs)
        hi = min(r[1][1] for r in rs)
        if lo > hi:
            warns.append(f"cards {', '.join(r[0] for r in rs)} disagree on {p}: "
                         f"its full search range is kept")
            continue
        bounds[p] = (lo, hi)
        cited[p] = [r[0] for r in rs]
    return bounds, cited, warns


def knowledge_seeds(n: int, cards: list[dict], space: ParameterSpace,
                    seed: int = 1) -> tuple[list[dict], dict, list[str]]:
    """``n`` starting recipes spread inside the range the approved cards allow.

    Same composition rule and pump windows as the full space, so every recipe is
    also valid in the full space. Returns (recipes, cited, warnings)."""
    if n <= 0:
        return [], {}, []
    b, cited, warns = narrowed_bounds(cards, space)
    if not cited:
        return [], {}, warns + ["no approved card with a range in the search range: "
                                "no recipes from knowledge"]
    sub = ParameterSpace(t_reac=b["T_reac"], f_tot=b["F_tot"], x_each=(0.0, 1.0),
                         x_sum_max=space.x_sum_max, pump_limits=dict(space.pump_limits))
    sub.bounds = {k: tuple(b[k]) for k in space.bounds}
    try:
        recipes = sub.sobol(n, seed=seed)
    except ValueError:
        return [], cited, warns + ["the range the cards allow holds no recipe the pumps "
                                   "can make: no recipes from knowledge"]
    recipes = [r for r in recipes if space.valid(r)]
    if len(recipes) < n:
        warns.append(f"only {len(recipes)} of {n} recipes fit inside the card range")
    return recipes, cited, warns
