"""LLM-assisted fitter: the AI proposes the structure, physics fits it.

The implementation is src/fitting/llm_structure.py (Phase 4 of
docs/design/LLM_FITTER_PLAN.md). Registered under the same id as before, so an
Analyser that had "LLM-assisted" selected keeps it. With no AI login it falls
back to Catalogue + BIC (statistics only) and says so in the result.
"""
from __future__ import annotations

from . import register
from .llm_structure import LLMStructure

# kept for callers that patch it (tests); the new fitter writes its own summary
try:
    from src.ai.loop_advice import narrate_fit  # noqa: F401
except Exception:                                   # noqa: BLE001
    def narrate_fit(fit):                           # type: ignore
        return {"summary": "", "flags": []}

LLMAssisted = LLMStructure

register(LLMStructure())
