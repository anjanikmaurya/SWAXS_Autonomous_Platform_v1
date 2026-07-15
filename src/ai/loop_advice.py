"""
src/ai/loop_advice.py — embedded, headless LLM helpers for the autopilot loop.

DESIGN CONTRACT (read before using):
  • ADVISORY ONLY. The deterministic fitter (scipy/sasmodels) and the optimizer
    (BO) are the authority. Nothing here ever produces a value that reaches
    hardware directly — it produces notes, an enum choice among options the
    caller already computed, or a *narrowing* of an already-safe search space.
  • STRUCTURED OUT. Every call returns a small validated dict. The LLM can only
    return choices/fields we defined; anything else falls back to a neutral
    result. It cannot invent a model, a number, or a pump setpoint.
  • GRACEFUL DEGRADATION. If no AI credentials / no network / bad JSON, each
    function returns a safe neutral value and the loop runs unchanged. The LLM
    is never on the critical path.
  • REPRODUCIBLE. Callers store these results as *advisory metadata* next to the
    deterministic result — never as the recorded scientific value.

These are called inline by the analyzer and optimizer apps (not via the chat
Assistant). Specialization comes from RAG over ``ai_knowledge`` + prompts, NOT
from fine-tuning.
"""

from __future__ import annotations

import json
import os
from typing import Any

_MAX_TOKENS = 500


# ── shared client (unify with SWAXSAssistant._get_client in a refactor) ────────
def _get_client():
    """Return an Anthropic client, or None if no credentials are configured."""
    try:
        from .assistant import _load_claude_settings_into_env  # reuse SLAC-gateway loader
        _load_claude_settings_into_env()
        import anthropic
        if not (os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
            return None
        return anthropic.Anthropic()
    except Exception:
        return None


def _ask_json(system: str, user: str, max_tokens: int = _MAX_TOKENS) -> dict | None:
    """One-shot, JSON-only LLM call. Returns a parsed dict or None (never raises)."""
    client = _get_client()
    if client is None:
        return None
    try:
        model = os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-sonnet-5"
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=system + "\n\nRespond with ONLY a single valid JSON object, no prose, no code fences.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1]      # strip a stray fence
        return json.loads(text)
    except Exception:
        return None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


# ── 1. Narrate a fit (analyzer) — read-only operator guidance ──────────────────
def narrate_fit(fit: dict) -> dict:
    """Turn deterministic fit diagnostics into a plain-language note + flags.
    Does NOT touch the numeric confidence (that stays deterministic).

    Returns: {"summary": str, "flags": [str]}  (empty if LLM unavailable).
    """
    out = _ask_json(
        system=("You are a SAXS QC assistant. Given fit diagnostics, write a one-sentence "
                "plain-language summary for the operator and list any concerns as short flags "
                "from this set only: beamstop_shadow, aggregation_upturn, poor_high_q, "
                "sparse_data, ambiguous_model, wide_uncertainty. Keys: summary (string), "
                "flags (array of the allowed strings)."),
        user=json.dumps(fit, default=str),
    )
    if not isinstance(out, dict):
        return {"summary": "", "flags": []}
    allowed = {"beamstop_shadow", "aggregation_upturn", "poor_high_q",
               "sparse_data", "ambiguous_model", "wide_uncertainty"}
    flags = [f for f in out.get("flags", []) if f in allowed]
    return {"summary": str(out.get("summary", ""))[:400], "flags": flags}


# ── 2. Break a model-selection tie (analyzer) — only when stats are ambiguous ──
def triage_model(candidates: list[dict]) -> dict:
    """``candidates`` = [{"name": str, "aic": float, "chi2": float, "note": str}, ...],
    already computed by the caller. Called ONLY when |ΔAIC| is within the caller's
    ambiguity band. The pick MUST be one of the provided names; otherwise the caller
    keeps its deterministic best.

    Returns: {"pick": str|None, "reason": str}.
    """
    names = [c.get("name") for c in candidates]
    out = _ask_json(
        system=("You choose between already-fitted SAXS models when their statistics are "
                "too close to separate. Pick the physically most plausible one. Keys: "
                "pick (must be exactly one of the given names), reason (one sentence)."),
        user=json.dumps({"candidates": candidates}, default=str),
    )
    if not isinstance(out, dict) or out.get("pick") not in names:
        return {"pick": None, "reason": "no confident LLM choice; using deterministic best"}
    return {"pick": out["pick"], "reason": str(out.get("reason", ""))[:300]}


# ── 3. Literature-informed prior (optimizer) — narrows within safe bounds ──────
def suggest_prior(chemistry: str, bounds: dict, objective: str,
                  knowledge_base: Any = None) -> dict:
    """Propose a promising sub-region / seed points for cold-start, using RAG over
    the group literature. Every returned number is CLAMPED to ``bounds`` and any
    seed outside bounds is dropped — the LLM can only narrow a space that is
    already safe, never widen it.

    ``bounds`` = {param: [lo, hi], ...}.
    Returns: {"regions": {param:[lo,hi]}, "seeds": [ {param: value} ], "rationale": str}.
    """
    context = ""
    if knowledge_base is not None:
        try:
            hits = knowledge_base.search(f"{chemistry} nanoparticle size control SAXS synthesis", k=5)
            context = "\n".join(getattr(h, "text", str(h)) for h in hits)[:4000]
        except Exception:
            context = ""
    out = _ask_json(
        system=("You suggest a starting search region for Bayesian optimization of a "
                "nanoparticle synthesis, grounded in the provided literature excerpts. "
                "Stay within the given bounds. Keys: regions (object of param -> [lo,hi]), "
                "seeds (array of objects param->value), rationale (2 sentences)."),
        user=json.dumps({"chemistry": chemistry, "objective": objective,
                         "bounds": bounds, "literature": context}, default=str),
        max_tokens=700,
    )
    if not isinstance(out, dict):
        return {"regions": {}, "seeds": [], "rationale": ""}
    # clamp regions to bounds; drop unknown params
    regions = {}
    for p, rng in (out.get("regions") or {}).items():
        if p in bounds and isinstance(rng, (list, tuple)) and len(rng) == 2:
            lo, hi = bounds[p]
            a, b = _clamp(rng[0], lo, hi), _clamp(rng[1], lo, hi)
            regions[p] = [min(a, b), max(a, b)]
    # keep only fully in-bounds seeds
    seeds = []
    for s in (out.get("seeds") or []):
        if isinstance(s, dict) and all(
            p in bounds and bounds[p][0] <= v <= bounds[p][1] for p, v in s.items()
        ):
            seeds.append({p: float(v) for p, v in s.items()})
    return {"regions": regions, "seeds": seeds, "rationale": str(out.get("rationale", ""))[:400]}


# ── 4. Explain the optimizer's choice (optimizer) — pure narration ─────────────
def explain_decision(next_condition: dict, campaign_state: dict) -> str:
    """One-line, human-readable rationale for why BO picked this next condition.
    Narration only — returns '' if the LLM is unavailable."""
    out = _ask_json(
        system=("Explain in ONE sentence, for a chemist watching the run, why this next "
                "synthesis condition was chosen given the campaign so far. Key: note (string)."),
        user=json.dumps({"next": next_condition, "campaign": campaign_state}, default=str),
        max_tokens=200,
    )
    return str(out.get("note", ""))[:300] if isinstance(out, dict) else ""
