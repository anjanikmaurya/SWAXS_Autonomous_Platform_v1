# Analyser mockup v4: final audit

> **Status, 2026-10-24.** Audit of `analyser_mockup_v4.html` (built by
> `build_analyser_mockup_v4.py`) against the platform design system
> (`docs/DESIGN_SYSTEM.md`), the other apps' templates, and the science in
> `src/`. The mockup is layout only. Phase 1 of the build (goal column,
> Setup steps, held results, starting recipes, Pause, search range) is in
> `analyzer/templates/index.html` since 2026-10-24; knowledge sets, the Guide
> tab and the AI levels are not built yet.

## 1. What the mockup captures (agreed decisions)

| Decision | Where in v4 |
|---|---|
| Left column = one distinct choice per app; tabs the same for every choice | Left column "Goal": Hit a size · Map the in-spec region · Size vs PDI trade-off · Compare methods. Tabs: Setup · Fitting · Report · Knowledge · Guide |
| Methods grouped as Standard / ML / Agentic | Setup step 3, two lists side by side (Fitting, Next recipe); only the methods that suit the goal are listed (`GOAL_METHODS`) |
| AI levels Advisor / Co-pilot / Pilot | Setup step 3, Agentic group; detail box shows the optimiser it shortlists from or falls back to, and the progress review |
| Explanations as text links, no ⓘ marks | "Details in the Guide →" under each method; Guide tab has one page per method, goal and concept |
| Guinier stays general; design knowledge lives in the Analyser | Knowledge tab, one set per chemistry; footer says general SAXS knowledge stays shared with Guinier |
| Knowledge tab adds, Setup only chooses | Knowledge → Add knowledge (web search for a target, papers, notes), Cards, Your verdicts, Campaigns, Drafts, Evaluation. Setup step 4: set, Use ticks, For, Preview |
| Step 4 follows the chosen method | Past campaigns hidden for Random; verdicts and "every AI pick" only for AI next-recipe levels; Advisor reads "every progress review" |
| Harness (Meta-Harness style) | Knowledge frozen and versioned at Start; every AI pick cites its cards (Report → Decisions, "Based on"); drafts need approval; Evaluation replays past campaigns; "Did the knowledge help?" in Report; harness settings locked during a run |
| Safety | Cards only narrow the search; ranges clipped to the reactor limits; "Make it a reactor limit" for limit-like notes; Pilot cannot switch the review off and asks to confirm limits; paper text is never an AI instruction |
| Science accuracy | Expected shape and min confidence (other shapes and weak fits not scored); PDI = σ/R̄ from the fit; diameter everywhere; equivalent diameter for non-spheres marked * |

## 2. Layout against the other apps

| Rule (DESIGN_SYSTEM.md) | Other apps | Mockup v4 | Verdict |
|---|---|---|---|
| One top bar: logo + name, status, project, extras, theme, Port · ← Hub | all apps | taken from the real analyzer template | conforms |
| Multi-section apps: `body.swaxs-shell`, left column of sections only (`#sidebar`, `.nav-btn`), task tabs (`.toptabbar`) inside the page | analysis (SAXS/WAXS categories; Setup · Result · Batch), background, reactor (routes; Hardware test · Autonomous setup · Live plots) | `swaxs-shell`, Goal column, tabs inside the page | conforms; same concept as analysis and reactor |
| Single-workspace apps have the top bar only | today: analyzer, quality, assistant | analyzer becomes multi-section | **change**: update the list in DESIGN_SYSTEM.md when the page is built |
| Hints under the column | reactor, background use `.nav-hint` | goal line, current pairing | conforms |
| Forms: label beside its box (`.fg`, `.grid2`) | all apps | steps 1, 2, 4 use `.fg` / `.grid2`; Use/For rows a small grid of ticks | conforms |
| Icons from the sprite only, no emoji | all apps | sprite icons; ✓, →, ▾ are typography | conforms |
| Colours from `swaxs-tokens.css`, no hex | all apps | tokens only (`--warn-text`, `--err-text`, `--warn`) | conforms (fixed in v4) |
| `--fs-*` scale shared | analyzer today has its own values | mockup inherits them | fix at build: drop the analyzer's own `--fs-*` values |
| `:focus-visible` | missing in analyzer | missing | fix at build |
| Tab count | 3 to 4 elsewhere | 5 (adds Knowledge, Guide) | acceptable: Knowledge is the app's data; Guide is new to the platform. If it works, offer it to the other apps rather than keep it unique |

## 3. Science checks (against `src/`)

| Item | Mockup | Code | Verdict |
|---|---|---|---|
| Score | ((R − R_t)/tol)² + w·(PDI/cap) | `src/optimizer/campaign.py` same loss | conforms; Guide now says tol = diameter tolerance / 2, as `analyzer/templates/index.html` passes it (the optimiser works in radius) |
| Goal → methods | target-hit: GP + EI, GP + UCB, TuRBO, Random; level-set: BAX; pareto: EHVI | `GOAL_METHODS` same | conforms |
| Compare methods | Random, GP + EI, GP + UCB, TuRBO, plus AI Co-pilot and Pilot (AI login) | registry also lists BAX and EHVI for benchmark | **change**: race single-objective methods only (BAX and EHVI optimise other objectives); update `GOAL_METHODS["benchmark"]` when built |
| PDI | σ/R̄ of the Schulz distribution | `src/analysis/nanoparticle.py` (PDI = σ/R̄) | conforms; labelled so it is not read as DLS PDI |
| Equivalent size | R_eq = √(5/3)·Rg, shown as diameter | `catalogue_bic.apply_winner` | conforms |
| Example fit | sphere R 2.48 nm: first minimum q·R = 4.49 → q = 1.81 nm⁻¹; Kratky peak q·Rg 1.70 (√3 = 1.73) | sphere form factor | consistent |
| ML regressor range | radius 1 to 18 nm | `_R_LIB = linspace(1, 18)` (radius) | fixed: said "1 to 18 nm"; now "radius 1 to 18 nm" (also in `src/fitting/__init__.py`) |
| Catalogue | sphere, core-shell, ellipsoid, cylinder, mass fractal, structure factors | `saxs_models.yml` (also lists hayter_msa, not tried by Catalogue + BIC) | conforms |
| Search space in cards | T_reac, F_tot, x_ODE, x_TOP, x_oley; x each 0 to 0.3 | `src/optimizer/space.py` `NAMES`, `x_each=(0, 0.3)` | conforms; card L32 clipped to 0.30 |

## 4. New backend work the mockup implies

| Mockup feature | Exists today? |
|---|---|
| Start / Continue / Abort | yes: `/api/campaign/start`, `/continue`, `/abort` |
| Pause | **no**: needs `/api/campaign/pause` (stop issuing recipes, keep state) |
| Expected shape, min confidence, "not scored" | no: campaign must skip or down-weight those records |
| Knowledge sets, cards, drafts, verdicts, freezing and version stamp | no: `ai_knowledge/design/<set>/`, see `docs/design/DESIGN_HARNESS_PLAN.md` |
| Card ranges clipped to the reactor bounds; "Make it a reactor limit" | no: read `reactor/config.yml` `bounds` / `safety`; write via the reactor app with confirmation |
| AI Advisor / Co-pilot / Pilot | no (Guide says "Not built yet") |
| Decisions table, "Did the knowledge help?" replay | no |
| Real-code note: the benchmark summary line says "Target radius" (`analyzer/templates/index.html` line ~1178) | inconsistent with diameter everywhere; change when the page is rebuilt |

## 5. Verdict

The layout follows the platform concept (goal column, task tabs, shared chrome
and forms) and the science matches `src/`, with the fixes above applied in v4.
Ready to build, in this order: layout and Setup (no new backend), then the
not-scored rule and Pause, then knowledge sets, then the AI levels and harness.
