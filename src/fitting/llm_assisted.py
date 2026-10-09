"""LLM-assisted fitter — physics fit with an AI interpretation layer.

The quantitative numbers (size / PDI / confidence) come from the same
least-squares sphere fit as `ls_sphere` — an LLM is not a reliable quantitative
fitter on its own (see docs/AUTOFIT_REDESIGN.md). What this method adds is an AI
reading of the fit: a plain-language summary and quality flags, via the same
narrate helper the Guinier assistant uses. It degrades gracefully: with no AI
backend configured, narrate returns an empty note and this behaves exactly like
the physics fit.

A fuller agentic fitter (SasView-tool-driven model selection, e.g. SasAgent) can
later register under this same interface.
"""
from __future__ import annotations

from src.analysis.nanoparticle import analyze_profile
from . import register

try:
    from src.ai.loop_advice import narrate_fit
except Exception:                       # AI extras absent → interpretation is a no-op
    def narrate_fit(fit):               # type: ignore
        return {"summary": "", "flags": []}


class LLMAssisted:
    id = "llm_assisted"
    label = "LLM-assisted"
    ready = True

    def fit(self, q, I, sigma=None, **opts) -> dict:
        res = analyze_profile(q, I, sigma, dist=opts.get("dist", "auto"))
        # Attach the AI interpretation under a dedicated key. (_analyze_file also
        # sets res["llm"] for the standard QC note; keeping ours separate means
        # this method's reading survives and is clearly attributable.)
        try:
            res["llm_assist"] = narrate_fit(res.get("diagnostics", {}))
        except Exception:
            res["llm_assist"] = {"summary": "", "flags": []}
        res.setdefault("diagnostics", {})["method"] = "llm_assisted"
        return res


register(LLMAssisted())
