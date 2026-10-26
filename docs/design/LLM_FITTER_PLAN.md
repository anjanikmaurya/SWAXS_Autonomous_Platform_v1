# LLM-assisted structure fitting: plan

> **Status, 2026-10-22.** Phase 1 is built: `src/analysis/features.py` (curve
> features incl. oscillation minima), `src/analysis/fit_context.py` (pipeline
> context, `chemistry.yml` template), BIC/AIC and parameter errors in
> `sasmodels_fit`, the catalogue `ai_knowledge/rules/saxs_models.yml`, and the
> normalisation mode recorded with every reduced file. Finding: the existing
> `sasmodels_fit` stalls near its starting point (scale and size fitted together
> in raw units), so Phase 2 starts with a robust fitter: scale and background
> solved exactly, sizes on a log scale, least squares, several seeded starts.
> Phase 2 is built: `src/analysis/model_fit.py` (robust fitter) and the
> **Catalogue + BIC** method `src/fitting/catalogue_bic.py`, which picked the
> true shape for 5 of 5 sasmodels test curves with parameters within 2 %; the
> Analyser records, draws and reports any model.
> Phase 3 is built: `src/fitting/benchmark.py` and `tools/fitter_benchmark.py`;
> results in `docs/benchmarks/FITTER_BENCHMARK_2026-10.md` (Catalogue + BIC:
> 40/40 shapes on held-out curves, median size error 0.3 %). Next for Phase 3:
> a hard set with real-world effects, without which the AI cannot be judged.
> Phase 3 also has hard sets (`hard`, `hard_heldout`) with real-data problems:
> Catalogue + BIC there 55 % of shapes, 52 % of sizes within 10 %.
> Phase 4 is built: `src/fitting/llm_structure.py`, behind the existing
> **LLM-assisted** menu entry (propose, fit, select by BIC, one residual check,
> cache, traces, fallback to Catalogue + BIC). Its benchmark score needs the AI
> login, so it is run on the operator's machine (command in
> `docs/benchmarks/FITTER_BENCHMARK_2026-10.md`).
> Phase 5 is NEVER BUILT yet.
> Knowledge, 2026-10-25: the bundle is now chosen per campaign in the Analyser
> (`src/fitting/fit_knowledge.py`): Guinier's sources part by part, plus this
> project's notes and judged fits, frozen at Start with a hash, with a
> with/without evaluation on the project's judged curves. Without a snapshot the
> fixed bundle below is used unchanged.

## Goal

When **LLM-assisted** is selected as the fitting method in the Autonomous
Analyser (5107), the fitter decides *which structure* a SAXS curve shows
(polydisperse sphere, core-shell sphere, cylinder, ellipsoid, mass fractal,
each optionally with a structure factor) and fits it properly. It runs on every
curve while that method is selected; the other methods are unchanged.

## Principle: the AI proposes, physics fits, statistics decide

AI models are poor at reading numbers off a curve and good at the expert step
of recognising what a curve's features mean. So:

| Step | Who | What |
|---|---|---|
| 1 Context | code | everything the pipeline already knows about this sample |
| 2 Features | code | Guinier Rg/I₀, Porod exponent, Kratky shape, p(r) Dmax, oscillation minima, low-q upturn, invariants |
| 3 Propose | AI | 1 to 3 models from a closed catalogue, with starting values and bounds, as JSON |
| 4 Fit | sasmodels | σ-weighted fit of each candidate |
| 5 Select | statistics | BIC, reduced χ², residual pattern, sanity of values |
| 6 Check | AI (optional) | sees the winner's residuals; may propose one alternative |
| 7 Report | code | model, parameters ± errors, equivalent radius, PDI, confidence, reasoning |

Any failure (no AI login, network, invalid answer, no fit converges) falls back
to the least-squares sphere fit and says so. Results are cached by the curve's
fingerprint and every decision is written to the assistant traces.

## What each pipeline step contributes (the gap)

Today the fitter receives only q, I and σ. The subtracted `.dat` carries no
metadata footer, so everything below stays in the manifest unless collected:

| Step | Contributes | Where it is |
|---|---|---|
| Reactor | recipe (T, flows, fractions) | `reactor/feedback/<rid>.done.json` |
| Chemistry | reagents, solvent, expected product | `<project>/chemistry.yml`, written by the operator |
| Reduction | normalisation mode (absolute cm⁻¹ or relative), transmission, thickness | manifest `files` (mode recorded from Phase 1) |
| Average | frames used | manifest provenance |
| Background | scale, negative points, high-q ratio, low-q slope | manifest `background` |
| Quality Gate | score, flags (aggregation, low_snr, narrow_q, featureless …) | manifest `quality`, keyed by the flat path |
| WAXS | crystal peaks for the same recipe | not paired yet (Phase 5) |

## Knowledge given to the AI

A fixed, versioned bundle, recorded with every fit: the model catalogue
(`ai_knowledge/rules/saxs_models.yml`), the SAXS guidelines, the operator
corrections and SOPs, and later the expert cases. No personal chat memory or
chat history, so the same curve always gets the same treatment. The benchmark
runs with and without the bundle; it stays on only if it measurably helps.

## In the autonomous loop

The optimiser keeps working on **equivalent radius** and PDI whatever the shape
(for a sphere the radius; otherwise the radius of a sphere with the same Rg).
The fitted model and its own parameters are recorded beside it. The Analyser
must record the real model name (not always `polydisperse_sphere`), draw any
model's overlay, and call the AI comment once per curve, not twice.

## Baseline the AI must beat

**Fit every catalogue model and keep the lowest BIC** (no AI). It is also a
useful method in its own right. The benchmark compares sphere fit, ML
regressor, catalogue + BIC and the AI fitter on simulated curves of every
catalogue shape with known truth: shape accuracy, size error, cost per curve,
on a held-out split.

## Phases

| Phase | What | AI? |
|---|---|---|
| 1 Context and features | collect metadata, record normalisation mode, feature set incl. oscillation minima, BIC and errors in `sasmodels_fit`, model catalogue | no |
| 2 Baseline | catalogue + BIC method; Analyser handles any model | no |
| 3 Benchmark | simulator shapes via sasmodels, scoring script | no |
| 4 AI proposer | propose, fit, select, check, with the knowledge bundle | yes |
| 5 Later | WAXS pairing, Guinier reads fit decisions, expert cases | partly |

Prior art: SasAgent (Ding and Do, ORNL; arXiv 2509.05363, IUCr AI for Science
2026) uses the same split, with SasView documentation for model choice and
SasView fitting for the numbers.
