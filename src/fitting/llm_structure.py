"""src/fitting/llm_structure.py: the AI proposes the structure, physics fits it.

The LLM-assisted fitting method (docs/design/LLM_FITTER_PLAN.md, Phase 4):

  1. CONTEXT    what the pipeline knows (src/analysis/fit_context.py)
  2. FEATURES   model-free numbers (src/analysis/features.py)
  3. PROPOSE    the AI reads features + context + a log-log / Kratky plot + the
                knowledge bundle and names up to 3 catalogue models with
                starting values, as JSON. It may ONLY name catalogue entries.
  4. FIT        each candidate, plus the sphere as a baseline, with the robust
                fitter (src/analysis/model_fit.py)
  5. SELECT     lowest BIC: statistics decide, not the AI
  6. CHECK      if the winner still fits poorly (reduced χ² > 2), the AI sees the
                residual pattern once and may propose up to 2 more
  7. REPORT     the standard fitter result, with the AI's reasoning attached

The knowledge bundle (model catalogue, operator corrections, group SOPs, SAXS
guidelines) is fixed and versioned by content hash; every fit records the
version. The Analyser can pass a frozen snapshot instead (``fit_knowledge=``,
src/fitting/fit_knowledge.py): which of Guinier's sources to use, plus this
project's notes and its most similar judged fits as precedent. Proposals are cached by (curve, context, bundle, prompt version, AI
model), so re-analysing a file costs nothing. Every decision is written to the
assistant traces (kind "fitter"). With no AI login, a timeout or an invalid
answer, it falls back to Catalogue + BIC (statistics only) and says so.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PROMPT_VERSION = "1"
MAX_CANDIDATES = 3
MAX_EXTRA = 2
CHECK_CHI2 = 2.0
TIMEOUT_S = 60.0
ALLOWED_FLAGS = ("aggregation_upturn", "interparticle_interference", "polydisperse",
                 "possible_mixture", "subtraction_issue", "low_q_missing",
                 "high_q_missing", "low_signal", "ambiguous_model")

BUNDLE_FILES = ("ai_knowledge/rules/saxs_models.yml", "ai_knowledge/rules/corrections.json",
                "ai_knowledge/rules/sops.json", "analysis/saxs_knowledge.yaml")

SYSTEM = """\
You are an expert in small-angle X-ray scattering (SAXS) of nanoparticles.
Your job is ONE step of a fitting pipeline: from a curve's model-free features,
its plot, and what the pipeline knows about the sample, name the structure
models worth fitting. You do NOT fit numbers; a physics fitter fits every model
you name and statistics (BIC) choose the winner, so propose what is plausible,
ranked, and let the data decide.

Rules:
- Name ONLY models from the catalogue below, optionally with ONE structure factor
  from its structure_factors list. Anything else is discarded.
- Up to 3 candidates. Starting values are optional, in nm, only for the model's
  `fit` parameters, within its bounds.
- Read the features like an expert: evenly spaced oscillation minima point to
  spheres (first minimum q·R = 4.49); a q^-1 region to rods; a lowered Kratky
  peak and smeared minima to anisotropy or polydispersity; a steep low-q slope
  to aggregation (consider mass_fractal or stickyhardsphere); a low-q dip or a
  correlation peak to repulsive interactions (hardsphere). A missing low-q or
  high-q range limits what can be told apart; say so with a flag.
- Use the chemistry and expected shape as a prior, but the data win. Follow the
  operator corrections and group SOPs below; they outrank general knowledge.
- Reply with ONE JSON object, nothing else:
  {"candidates": [{"model": "...", "structure_factor": null | "...",
                   "start": {"param": value}, "reason": "one sentence"}],
   "summary": "one plain-language sentence for the operator",
   "flags": [zero or more of: %s]}
""" % ", ".join(ALLOWED_FLAGS)


# ── knowledge bundle ─────────────────────────────────────────────────────────

def knowledge_bundle(include_guidelines: bool = True) -> tuple[str, str]:
    """(text, version): the files the AI reads, and a short hash of their content."""
    parts, h = [], hashlib.sha256(PROMPT_VERSION.encode())
    for rel in BUNDLE_FILES:
        if rel.endswith("saxs_knowledge.yaml") and not include_guidelines:
            continue
        p = ROOT / rel
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        h.update(rel.encode()); h.update(txt.encode())
        parts.append(f"## {rel}\n{txt}")
    return "\n\n".join(parts), h.hexdigest()[:12]


# ── plot for the AI ──────────────────────────────────────────────────────────

def curve_png(q, I, sigma=None) -> str | None:
    """Base64 PNG: log-log I(q) and Kratky q²I(q), small."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        try:
            from src.plot_lock import MPL_LOCK as _lock
        except Exception:                                   # noqa: BLE001
            import threading
            _lock = threading.Lock()
        with _lock:
            fig, ax = plt.subplots(1, 2, figsize=(7, 2.8), dpi=80)
            try:
                ax[0].loglog(q, I, ".", ms=2)
                ax[0].set_xlabel("q (nm$^{-1}$)"); ax[0].set_ylabel("I(q)")
                ax[0].set_title("log-log", fontsize=9)
                ax[1].plot(q, np.asarray(q) ** 2 * np.asarray(I), ".", ms=2)
                ax[1].set_xlabel("q (nm$^{-1}$)"); ax[1].set_ylabel("q$^2$ I(q)")
                ax[1].set_title("Kratky", fontsize=9)
                fig.tight_layout()
                buf = io.BytesIO()
                fig.savefig(buf, format="png")
            finally:
                plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:                                       # noqa: BLE001
        return None


# ── the AI call ──────────────────────────────────────────────────────────────

def _client():
    try:
        from src.ai.loop_advice import _get_client
        return _get_client()
    except Exception:                                       # noqa: BLE001
        return None


def _ai_model() -> str:
    from src.ai.models import default_model
    return default_model("SWAXS_FITTER_MODEL")


def _ask(client, bundle: str, user_text: str, png: str | None) -> tuple[dict | None, dict]:
    """One JSON-only call. Returns (parsed or None, usage)."""
    content = []
    if png:
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": png}})
    content.append({"type": "text", "text": user_text})
    system = [{"type": "text", "text": SYSTEM},
              {"type": "text", "text": "# Knowledge bundle\n\n" + bundle,
               "cache_control": {"type": "ephemeral"}}]
    resp = client.messages.create(model=_ai_model(), max_tokens=900, system=system,
                                  messages=[{"role": "user", "content": content}],
                                  timeout=TIMEOUT_S)
    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    u = getattr(resp, "usage", None)
    usage = {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens",
                                              "cache_read_input_tokens",
                                              "cache_creation_input_tokens")} if u else {}
    m = re.search(r"\{.*\}", text, flags=re.S)
    try:
        return (json.loads(m.group(0)) if m else None), usage
    except ValueError:
        return None, usage


def validate(proposal, catalogue: dict, limit: int = MAX_CANDIDATES) -> list[dict]:
    """Keep only catalogue models / structure factors; clip starts to bounds."""
    out, seen = [], set()
    if not isinstance(proposal, dict):
        return out
    models, sfs = catalogue["models"], catalogue.get("structure_factors", {})
    for c in proposal.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        m, sf = c.get("model"), c.get("structure_factor") or None
        if m not in models or (sf is not None and sf not in sfs):
            continue
        if sf is not None and m == "mass_fractal":
            continue
        if (m, sf) in seen:
            continue
        b = dict(models[m].get("bounds") or {})
        start = {}
        for k, v in (c.get("start") or {}).items():
            if k in (models[m].get("fit") or []) and isinstance(v, (int, float)) and k in b:
                lo, hi = b[k]
                start[k] = float(min(max(float(v), lo), hi))
        seen.add((m, sf))
        out.append({"model": m, "structure_factor": sf, "start": start,
                    "reason": str(c.get("reason", ""))[:300]})
        if len(out) >= limit:
            break
    return out


def _residual_summary(win: dict, I, s) -> dict:
    """Where the winner misses: mean signed residual in 5 q-bands."""
    r = (np.asarray(I) - np.asarray(win["I_fit"])) / np.asarray(s)
    q = np.asarray(win["q"])
    bands = np.array_split(np.arange(len(q)), 5)
    return {"model": win["model"], "reduced_chi2": win["chi2"],
            "bands": [{"q_from": round(float(q[b[0]]), 4), "q_to": round(float(q[b[-1]]), 4),
                       "mean_residual_sigma": round(float(np.mean(r[b])), 2)} for b in bands if len(b)]}


# ── cache ────────────────────────────────────────────────────────────────────

def _cache_dir(project_root) -> Path:
    if project_root:
        return Path(project_root) / ".swaxs_state" / "llm_fit_cache"
    return Path.home() / ".swaxs" / "cache" / "llm_fit"


def _cache_key(q, I, sigma, context, bundle_version) -> str:
    h = hashlib.sha256()
    for a in (q, I, sigma):
        if a is not None:
            h.update(np.round(np.asarray(a, float), 8).tobytes())
    h.update(json.dumps(context, sort_keys=True, default=str).encode())
    h.update(f"{bundle_version}|{PROMPT_VERSION}|{_ai_model()}".encode())
    return h.hexdigest()[:24]


def _cache_get(d: Path, key: str):
    try:
        return json.loads((d / f"{key}.json").read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return None


def _cache_put(d: Path, key: str, value: dict) -> None:
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{key}.json.tmp"
        tmp.write_text(json.dumps(value, default=str), encoding="utf-8")
        os.replace(tmp, d / f"{key}.json")
    except Exception:                                       # noqa: BLE001
        pass


# ── the fitter ───────────────────────────────────────────────────────────────

class LLMStructure:
    id = "llm_assisted"
    label = "LLM-assisted"
    ready = True

    def __init__(self, client_factory=None):
        # looked up at call time, so the module's _client can be replaced
        self._client_factory = client_factory or (lambda: _client())

    def fit(self, q, I, sigma=None, **opts) -> dict:
        from src.analysis.features import compute_features
        from src.analysis.model_fit import fit_model, load_catalogue, seeds_from_features
        from src.analysis.nanoparticle import analyze_profile
        from src.fitting.catalogue_bic import apply_winner, select
        from src.ai import traces

        t0 = time.perf_counter()
        res = analyze_profile(q, I, sigma, dist=opts.get("dist", "auto"), fit_form_factor=False)
        q = np.asarray(q, float); I = np.asarray(I, float)
        s = np.asarray(sigma, float) if sigma is not None else np.abs(I) * 0.05
        cat = load_catalogue()
        feats = compute_features(q, I, s, with_pr=False)
        ctx = {}
        if opts.get("path"):
            try:
                from src.analysis.fit_context import collect
                ctx = collect(opts["path"], opts.get("project_root"))
            except Exception:                               # noqa: BLE001
                ctx = {}
        # Knowledge: a snapshot frozen by the Analyser (src/fitting/fit_knowledge.py)
        # chooses Guinier's sources and adds this project's notes and judged
        # fits; without one, the original four files exactly as before.
        snap = opts.get("fit_knowledge")
        extra_text, used = "", {}
        if isinstance(snap, dict) and snap.get("sources"):
            from src.fitting import fit_knowledge as fk
            bundle, version = fk.system_bundle(snap)
            version = f"{fk.KNOWLEDGE_PROMPT_VERSION}-{version}"
            excl = opts.get("exclude_file") or (Path(opts["path"]).name if opts.get("path") else None)
            extra_text, used = fk.per_curve(snap, q, I, feats, ctx, exclude_file=excl)
        else:
            bundle, version = knowledge_bundle()
        info = {"status": "ai", "bundle_version": version, "prompt_version": PROMPT_VERSION,
                "ai_model": _ai_model(), "cached": False, "rounds": 0, "usage": []}
        if isinstance(snap, dict) and snap.get("sources"):
            info["knowledge"] = {"hash": snap.get("hash"), "sources": snap.get("sources"),
                                 "project_version": snap.get("project_version"),
                                 "n_notes": len(snap.get("notes") or []) if "notes" in snap["sources"] else 0,
                                 "fits": used.get("fits") or [], "passages": used.get("passages") or []}
        key = _cache_key(q, I, s, dict(ctx, _knowledge=extra_text) if extra_text else ctx, version)
        cdir = _cache_dir(opts.get("project_root"))
        cached = _cache_get(cdir, key)

        proposal, extra = None, None
        if cached:
            proposal, extra = cached.get("proposal"), cached.get("extra")
            info["cached"] = True
        else:
            client = self._client_factory()
            if client is None:
                info["status"] = "fallback_no_ai"
            else:
                user = json.dumps({"features": feats, "context": ctx,
                                   "data": {"q_unit": "nm^-1", "intensity":
                                            "absolute cm^-1" if (ctx.get("reduction") or {})
                                            .get("absolute_units") else "relative"}},
                                  default=str)
                try:
                    proposal, usage = _ask(client, bundle, "Curve to classify:\n" + user
                                           + ("\n\n" + extra_text if extra_text else ""),
                                           curve_png(q, I, s))
                    info["usage"].append(usage)
                    info["rounds"] = 1
                except Exception as exc:                    # noqa: BLE001
                    info["status"] = "fallback_ai_error"
                    info["error"] = f"{type(exc).__name__}: {exc}"[:300]

        cands = validate(proposal, cat)
        if not cands and info["status"] == "ai":        # unreadable, empty or off-catalogue
            info["status"] = "fallback_invalid_answer"

        if info["status"] != "ai":
            # Statistics only: the same result as Catalogue + BIC, clearly marked.
            try:
                sel = select(q, I, s, catalogue=cat, features=feats)
                if sel["winner"] and sel["winner"].get("equivalent_radius"):
                    res = apply_winner(res, sel["winner"], sel["ranked"], feats, method=self.id)
                else:
                    res = analyze_profile(q, I, s, dist=opts.get("dist", "auto"))
            except Exception as exc:                        # noqa: BLE001
                info["fallback_error"] = str(exc)
                res = analyze_profile(q, I, s, dist=opts.get("dist", "auto"))
            res["llm_structure"] = info
            res["llm_assist"] = {"summary": "", "flags": []}
            if opts.get("trace", True):
                self._trace(traces, info, [], res, t0)
            return res

        seeds = seeds_from_features(feats)
        if not any(c["model"] == "sphere" and not c["structure_factor"] for c in cands):
            cands.append({"model": "sphere", "structure_factor": None, "start": {},
                          "reason": "baseline"})
        fits = [fit_model(q, I, s, c["model"], cat, structure_factor=c["structure_factor"],
                          seeds=seeds, starts=[c["start"]] if c["start"] else None, n_extra=2)
                for c in cands]
        ranked = sorted((f for f in fits if "error" not in f), key=lambda f: f["bic"])

        if ranked and ranked[0]["chi2"] > CHECK_CHI2 and extra is None and not info["cached"]:
            client = self._client_factory()
            if client is not None:
                try:
                    tried = [f"{c['model']}@{c['structure_factor']}" if c["structure_factor"]
                             else c["model"] for c in cands]
                    msg = json.dumps({"already_fitted": tried,
                                      "best_fit_residuals": _residual_summary(ranked[0], I, s)})
                    extra, usage = _ask(client, bundle,
                                        "The best fit still misses the data. Propose up to "
                                        f"{MAX_EXTRA} DIFFERENT candidates, same JSON format:\n" + msg,
                                        None)
                    info["usage"].append(usage)
                    info["rounds"] = 2
                except Exception as exc:                    # noqa: BLE001
                    info["check_error"] = str(exc)[:200]
        more = [c for c in validate(extra, cat, MAX_EXTRA)
                if (c["model"], c["structure_factor"]) not in
                {(x["model"], x["structure_factor"]) for x in cands}]
        if more:
            fits += [fit_model(q, I, s, c["model"], cat, structure_factor=c["structure_factor"],
                               seeds=seeds, starts=[c["start"]] if c["start"] else None, n_extra=2)
                     for c in more]
            cands += more
            ranked = sorted((f for f in fits if "error" not in f), key=lambda f: f["bic"])

        if not info["cached"]:
            _cache_put(cdir, key, {"proposal": proposal, "extra": extra,
                                   "bundle_version": version, "ts": time.time()})
        if ranked and ranked[0].get("equivalent_radius"):
            res = apply_winner(res, ranked[0], ranked, feats, method=self.id)
        else:                                       # nothing fitted: the sphere fit stands
            res = analyze_profile(q, I, s, dist=opts.get("dist", "auto"))
            info["status"] = "fallback_no_fit"
        info["proposed"] = cands
        res["llm_structure"] = info
        res["llm_assist"] = {"summary": str((proposal or {}).get("summary", ""))[:400],
                             "flags": [f for f in (proposal or {}).get("flags", [])
                                       if f in ALLOWED_FLAGS]}
        if opts.get("trace", True):
            self._trace(traces, info, cands, res, t0)
        return res

    @staticmethod
    def _trace(traces, info, cands, res, t0):
        traces.record("fitter", method="llm_assisted", status=info.get("status"),
                      cached=info.get("cached"), bundle_version=info.get("bundle_version"),
                      knowledge=info.get("knowledge"),
                      proposed=[f"{c['model']}@{c['structure_factor']}" if c.get("structure_factor")
                                else c["model"] for c in cands],
                      winner=res.get("model"), confidence=res.get("confidence"),
                      equivalent_radius=(res.get("size") or {}).get("radius"),
                      summary=(res.get("llm_assist") or {}).get("summary"),
                      usage=info.get("usage"), seconds=round(time.perf_counter() - t0, 2))
