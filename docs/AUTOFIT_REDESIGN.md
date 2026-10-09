# Auto-Fit & Optimiser — full redesign (3-tab, pluggable methods)

Status: **design only — no code written yet.** This supersedes the earlier
`AUTOFIT_METHODS_REFACTOR.md` with a complete rethink: a three-tab app whose
**fitting** and **optimising** steps are selectable, comparable methods, backed
by a short literature survey of the best candidates. It is meant to double as a
*method-comparison study* platform (run several methods against the hidden
ground-truth simulator and report who wins).

---

## 1. The three tabs

A top tab-bar (`.toptab` / `showTab`, the analysis-app pattern), one `.page` each.

### Tab 1 — Setup
Everything you choose before a run:
- **Campaign**: target radius, tolerance, PDI cap, run budget (as now).
- **Fitting method** (dropdown): which fitter turns each profile into size/PDI/
  confidence.
- **Optimising method** (dropdown): which strategy proposes the next recipe.
- **Per-method options** render under each dropdown (e.g. UCB's κ, TuRBO's trust
  radius, ML model path, LLM model id).
- **Comparison-study mode** (toggle): run N methods on the *same* ground truth
  and collect their histories for the Report tab. (In-silico only — needs the
  simulator; greyed out against real hardware.)

### Tab 2 — Fitting
The per-profile view, as it is today: the **profiles table beside the fit plot**
(the side-by-side layout we just built), KPIs, scale buttons, residuals. It shows
the *active fitter's* result for the selected profile, and — in study mode — can
overlay two fitters' models on the same curve.

### Tab 3 — Report (optimisation)
The campaign/convergence view, as today: the surrogate / convergence / trajectory
figures from `src/optimizer/plots.py`, plus — in study mode — **method-vs-method**
convergence (best-loss vs iteration, size-error vs budget) so a study produces a
single comparison figure.

---

## 2. Fitting methods — survey and menu

The job: map a subtracted I(q) → {size, PDI, phase, confidence}. Three families,
increasingly data-driven.

| Method | What it is | Strengths | Caveats | Status |
|---|---|---|---|---|
| **Least-squares sphere** (current) | polydisperse-sphere form factor fit by scipy `least_squares`; Schulz/log-normal; Guinier fallback | physics-based, interpretable, no training, good baseline | slow per fit; struggles with multimodal / complex shapes | **ship (default)** |
| **ML regressor** | train a model (e.g. Random Forest or a feed-forward NN) on synthetic SAXS curves to predict R, PDI, background directly [1,2,6] | ~instant inference; uncertainty-aware variants exist [6]; good for a fixed model family | needs training data + retraining per model family; extrapolation risk | **build (phase 2)** |
| **ML model-selection + fit** | classifier picks the best scattering model, then fit [3,4] | removes the "which model?" bottleneck for mixed samples | heavier; more moving parts | later |
| **LLM-assisted** | an agent orchestrates tools (Guinier/Porod/p(r)/sasmodels), narrates, flags, picks a model — e.g. SasView-backed multi-agent systems [7,8,9] | great for interpretation, QC, model choice, non-expert guidance | not a precise quantitative fitter on its own; cost/latency; must stay auditable | **thin version now** (reuse the Guinier assistant), full agent later |

**Recommended study set (fitting):** `least_squares` (physics baseline) vs an
**ML RF/NN** regressor vs an **LLM-assisted** pipeline — compared on accuracy
against the simulator's ground-truth size/PDI, per-fit latency, and robustness to
noise. A nice synergy: the ML fitter can be trained on curves from the platform's
own `src/simulator`, so training data is free and in-distribution.

---

## 3. Optimising methods — survey and menu

The job: given the history of (recipe → loss), propose the next recipe. Loss is
the existing `((size − target)/tolerance)² + w·(PDI/PDI_cap)`.

| Method | What it is | Why include it | Status |
|---|---|---|---|
| **Random / grid** | sample the space with no model | the honest baseline every study needs — if a method can't beat random, it isn't earning its cost | **build first** (cheapest) |
| **GP + EI** (current) | GP surrogate, Expected-Improvement acquisition | balanced explore/exploit, analytic, strong default [10,11] | **ship (default)** |
| **GP + UCB** | same GP, upper-confidence-bound acquisition with explicit κ | one-line acquisition swap; lets a study tune explore/exploit directly [11,12] | **build (phase 2)** |
| **TuRBO** | trust-region local BO | scales better as the recipe space grows; avoids BO's over-exploration in higher dimensions [13] | phase 3 |
| **BAX** (Bayesian Algorithm eXecution) | goal-aware: targets the *subset* of recipe space meeting a condition (e.g. size = target ± tol, PDI ≤ cap), via information gain (InfoBAX) or its mean/switch variants [14] | **the best fit for our actual goal** — it maps the whole acceptance band, not just one optimum; the source paper demos exactly TiO₂ nanoparticle size + low-polydispersity targeting | **build (phase 2, flagship)** |
| **CMA-ES** | evolutionary strategy | derivative-free baseline from a different family | optional |

Note from the benchmarking literature: a GP with an **anisotropic** kernel (and
Random Forest surrogates) tend to beat the isotropic-kernel GP [10]; our current
GP uses a single length scale, so "GP-EI (anisotropic)" is a cheap, likely-worth-it
variant to include.

**Recommended study set (optimising):** `random` (baseline) · `GP-EI` (current) ·
`GP-UCB` · `TuRBO` · `BAX-Info`. That spans no-model, classic BO, explicit
explore/exploit, scalable-local, and goal-aware — a publishable comparison, and
BAX is the one most aligned with "hit a target size at low PDI".

---

## 4. Architecture (how the methods plug in)

Same pluggable-registry idea as before, now with the 3-tab UI on top:

- `src/fitting/` — `Fitter` protocol `fit(q,I,sigma,**opts)->result_dict`;
  `ls_sphere` wraps the current `analyze_profile` **unchanged**; `REGISTRY`,
  `get(id)`. New fitters: `ml_regressor`, `llm_assisted`.
- `src/optimizer/methods/` — `Optimiser` protocol
  `start(space,**cfg)/ask()/tell(...)/history/status_str/best/budget`;
  `CampaignController` registers as `gp_ei` **unchanged**. New optimisers:
  `random`, `gp_ucb`, `turbo`, `bax`. The loss is factored into a shared
  `objective(result,cfg)` so every method optimises the same scalar.
- **Study runner** — `src/optimizer/study.py`: runs several optimisers (and/or
  fitters) against `src/simulator/ground_truth` on the same seed, collects
  per-method histories, and hands them to the Report tab's comparison figure.
- Everything below the method layer is untouched: result-dict contract, the
  `.dat`/manifest/`fit.complete` outputs, watcher + `_startup_present` freeze,
  backfill, Fit-dir retention, run-tag scoping, the matplotlib lock.
- Defaults reproduce today exactly: fitting `ls_sphere`, optimising `gp_ei`,
  study mode off.

---

## 5. Build order (each step testable, behaviour-preserving first)

1. **3-tab shell**, panels reorganised, methods hardcoded to the current two —
   no behaviour change. (UI test.)
2. **Registries + wrap current code** as `ls_sphere` and `gp_ei`; Setup dropdowns
   list them (others `soon`). Existing analyzer/optimizer tests stay green.
3. **`random` optimiser** — proves the optimiser plug + gives the study baseline.
4. **`gp_ucb`** — proves acquisition swaps on the shared GP.
5. **Study runner + Report comparison figure** — in-silico, multi-method.
6. **BAX-Info** — flagship goal-aware optimiser; validate against ground truth.
7. **ML fitter** (train on simulator curves) and **LLM-assisted fitter** (reuse
   the Guinier assistant infra).
8. **TuRBO / CMA-ES / anisotropic-GP** as optional study arms.

---

## 6. Decisions to confirm before build

- Study mode: run methods **sequentially** on one simulator seed (simple,
  reproducible) to start; parallel later.
- Persist the chosen methods in `manifest.project_meta` so a resume restores them.
- Dependencies: ML (scikit-learn / a small torch model) and BAX add optional
  deps — keep them in `requirements-ai.txt`-style extras so the core app still
  runs if they're absent (graceful degradation, as elsewhere).
- Real-hardware runs use one method at a time; study mode is simulator-only.

---

## References (located via literature search, Oct 2026 — verify before citing in a paper)

1. Machine learning for accelerated prediction of size distributions of spherical nanoparticles from SAXS — *Phys. Chem. Chem. Phys.* (RSC), 2026.
2. Reconstruction of nanoparticle size distribution from SAXS via neural networks — *High Power Laser Sci. Eng.* (Cambridge), 2024.
3. Automated selection of nanoparticle models for SAXS using machine learning — *Acta Cryst. A* (IUCr), 2024.
4. Influence of device configuration and noise on an ML predictor for NP SAXS model selection — PMC, 2024.
5. Deep learning-assisted characterization of nanoparticle growth (SAXS evolution) — *Radiat. Detect. Technol. Methods* (Springer), 2024.
6. Uncertainty-Aware Machine Learning for SAXS Analysis in Autonomous Experimentation — *ACS Photon Science*, 2024/25.
7. SasAgent: Multi-Agent AI System for Small-Angle Scattering Data Analysis — arXiv:2509.05363.
8. SAXS Assistant: Automated SAXS analysis for biologics and polymeric nanoparticles — PubMed, 2025.
9. An agentic artificially intelligent X-ray scientist — *Nature Machine Intelligence*, 2026.
10. Benchmarking Bayesian optimization across materials science domains — *npj Comput. Mater.*, 2021 (s41524-021-00656-9).
11. Shahriari et al., Taking the human out of the loop: a review of Bayesian optimization — *Proc. IEEE* 104, 148 (2016); and acquisition-function references.
12. Randomised GP-UCB for Bayesian optimisation — IJCAI 2020 / arXiv:2006.04296.
13. Eriksson et al., Scalable Global Optimization via Local Bayesian Optimization (TuRBO) — arXiv:1910.01739.
14. Targeted materials discovery using Bayesian algorithm execution (InfoBAX/MeanBAX/SwitchBAX; TiO₂ size+PDI demo) — *npj Comput. Mater.*, 2024 (s41524-024-01326-2); arXiv:2312.16078.
