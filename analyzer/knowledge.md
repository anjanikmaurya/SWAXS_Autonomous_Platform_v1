# Autonomous Analyser: knowledge

App id, folder and log name: `analyzer`. Port 5107. Shown in the hub as
**Autonomous Analyser**.

## 1. What the app is for

The Autonomous Analyser is the measurement half of the closed synthesis loop.
It watches the subtracted SAXS profiles and, as each new `.dat` appears, fits a
polydisperse-sphere model to get radius, diameter, polydispersity (PDI), Guinier
Rg, a relative Porod invariant, an ordered-phase label and a 0 to 1 confidence.
During a campaign each fit is scored against the operator's target and an
optimiser proposes the next synthesis recipe for the reactor.

All science lives in `src/analysis/nanoparticle.py`, `src/fitting/` and
`src/optimizer/`; `analyzer/app.py` holds the routes, the folder watcher, the
live stream and the manifest writes.

## 2. Where it sits in the pipeline

```
reactor (5108) -> reduction (5102) -> average (5103) -> background (5104)
      ^                                                       |
      |                                                       v
      +---- Conditions/ <---- analyzer (5107) <---- quality (5105)
```

| Direction | Folder (under the project root) | What |
|---|---|---|
| reads | `1D/SAXS/Subtracted/Good/` or `1D/SAXS/Subtracted/` | subtracted profiles (see Quality Gate below) |
| writes | `1D/SAXS/Conditions/<recipe_id>.txt` | the next recipe; the reactor polls this folder |
| writes | `1D/SAXS/Results/Fit/fit_<stem>.png` and `.dat` | a durable record of every fit |
| writes | `1D/SAXS/Results/campaign_<id>.json` | the campaign's target and outcome |
| writes | `1D/SAXS/Results/campaign_<id>/` | final figures and history when a campaign ends |
| writes | `manifest.json`, section `analyses` | one entry per fit |
| writes | `.swaxs_state/campaign.json` | resume state only (not a record) |

**Quality Gate.** The Quality Gate (5105) copies accepted profiles into
`Subtracted/Good/`. The gate mode (set through `POST /api/folder`, key `gate`;
there is no UI control) decides which folder is fitted:

- `auto` (default): fit `Good/` when it contains `.dat` files, otherwise the flat
  `Subtracted/` folder (with a warning in the Log). Re-checked on every poll.
- `good`: always fit `Good/` only, even if it is empty.
- `off`: always fit the flat folder; the gate is ignored.

When `Good/` is watched, profiles for a recipe the running campaign is waiting
for are still picked up from the flat folder, so an ungraded profile cannot stall
the loop.

**Watcher.** Non-recursive, polls every 3 s. A file is fitted only once its size
and mtime have stopped changing (`src/reactor/intake.py`, `decide_intake`), so a
half-written `.dat` is deferred. Profiles already present when the app starts (or
when the project or folder changes) are not auto-fitted; use **Analyse existing
profiles** for those. A profile that already has a `Results/Fit/` record is
skipped.

## 3. How to use it

The left column holds the campaign **Goal**: **Hit a size** (find one recipe in
spec; stops at the first confident hit), **Map the in-spec region** (every
in-spec recipe; BAX), **Size vs PDI trade-off** (the best PDI across a diameter
range; EHVI) and **Compare methods** (simulator only). The page has five tabs,
**Setup**, **Fitting**, **Report**, **Knowledge** and **Guide**, the same for every
goal, plus a **Log** panel visible on every tab. The project folder is pushed by the hub.

### Setup tab

Numbered steps, top to bottom:

1. **Data folders**: **Watched profiles** (default `1D/SAXS/Subtracted`; Browse…
   and Set) and **Conditions** (read-only: where each next recipe is written,
   checked against the folder the reactor reads).
2. **Target & limits**: **Target diameter (nm)** and **Tolerance ± (nm)**, or
   **Min / Max diameter (nm)** for the trade-off; **Max PDI (σ/R̄, from the SAXS
   fit)**, which is the spread of the fitted Schulz distribution, not the DLS
   polydispersity index; **Max experiments**; **Expected shape** (sphere
   (core-shell counts), sphere only, any shape); **Min fit confidence** (default
   0.5). **Search range** shows the bounds read-only from `reactor/config.yml`
   (with the live pump limits) and the hard safety caps; change them in the
   Reactor app.
3. **Knowledge base**: pick one by name, e.g. "Pd nanoparticles" (a base that
   matches `chemistry.yml` is preselected on first load; None is allowed). Two
   summary lines say what it gives: **Fitter** (Guinier's sources, notes and
   judged fits; used only by AI-assisted structure) and **Design loop** (literature
   and note cards, past campaigns, verdicts). What each part uses is ticked in the
   Knowledge tab and saved with the base, not here. Drafts waiting for approval are
   flagged and never used. The base's version is frozen at Start. The chosen base
   is also where your Right / Wrong-model calls on fits are kept.
4. **Fitting**: one list, Standard (Catalogue + BIC recommended, Least-squares
   sphere), then ML, then Agentic (AI-assisted structure, which says which base it
   reads).
5. **Design loop** (who picks the next recipe): one list, Standard (Random), then
   ML (GP + EI recommended, GP + UCB, TuRBO, BAX, Pareto, as suit the goal), then
   Agentic (AI Advisor, Co-pilot, Pilot). The chosen method's acquisition, or for
   an AI level the optimiser it uses and **Test AI connection**. **More options**
   (shown only when the method has any): optimiser tuning (kappa, trust region,
   tau), and for AI levels the progress review interval, apply mode (Advisor),
   Claude model and thinking.
6. **Starting recipes** (the cold start): **From knowledge** (recipes inside the
   range the base's approved cards allow; 3 by default when it has a card with a
   range, taken out of the spread so the total stays 10) and **Spread out** (Sobol
   points over the whole search range); **Total** is the cold start (`n_init`). It
   warns below 5, above a third of the budget, or when fewer than half are spread
   out, and Start refuses a total longer than the budget. **Preview starting
   recipes** lists the exact first recipes, in order, without starting anything.
   **More options: your own recipes**: up to 5 exact recipes (**Add recipe**),
   made first.

Case-by-case settings sit under **More options** in their step: Min fit
confidence (Target & limits, default 0.5), **Wait for more batches** (Target &
limits, default 3 min, 0 to 60), optimiser tuning and AI settings
(Design loop), your own recipes (Starting recipes).

The **Ready to start?** card on the right checks the folders, the search range,
the fitting method, the cold start and the expected shape. Press **Start
campaign** (for Compare methods: **Run comparison**). Goal, spec, methods and
starting recipes are locked while a campaign runs. The fitter belongs to the campaign: the
server refuses a change while one runs, and a restart or Continue fits with the
fitter the campaign started with (if it is not available here, a resumed
campaign comes back paused and Continue is refused, with the reason). If the latest run was
interrupted, **Continue RunN** appears beside Start. **Live progress** shows Best
diameter, Δ to target, Best PDI, Runs and a diameter-per-run chart.

**Safety**: your own recipes must have all five settings, be finite, lie inside
the search bounds (box, composition sum, each pump's flow window) and under the
reactor's hard caps (`T_max`, `F_tot_max`); otherwise Start and Preview refuse
with the reason, and nothing is cleared or started.

### Which batch trains the model

A recipe yields several batch profiles (the average app writes batch001,
batch002, ...). The Analyser fits each one as it arrives, but the model learns
from a recipe once: when the reactor has written `<recipe_id>.done.json` and no
new batch has come for **Wait for more batches** (default 3 min), it uses the
highest-confidence batch (the latest on a tie) and logs which one. Without a
done marker it learns after 20 min from the first batch
(`SWAXS_BATCH_MAX_WAIT_S`), so a missing marker never stalls the loop. A
condition that times out with batches is learned from them, not scored as
failed. The batches collected so far survive a restart, and Continue replays the
same choice. A batch that arrives after its recipe was learned is fitted and
recorded only. The status line shows each recipe being collected. 0 min keeps
the old rule (the first batch trains the model at once).

With **AI-assisted structure**, each live profile is fitted at once with
Catalogue + BIC (seconds), and the AI fit then runs in a background worker (one
at a time, queue of 200). The design loop learns from the **better of the two
fits**: the AI fit replaces the quick one only when the statistics prefer it
(lower BIC on the same data, the rule the fitters use between models); a losing
AI proposal is kept beside the result (`ai_refit`) and logged. The model does not
learn before the profile's AI fit has finished, but it waits at most 3 min from
the profile (`SWAXS_AI_REFIT_MAX_WAIT_S`); after that the quick fit is used. With
one profile per recipe the model therefore learns 3 min after the profile
arrives, from the AI fit if it was done and better, else from the quick fit. The status line
counts the AI fits still running. Analyse existing profiles still uses the AI fit
directly.

Knowledge tests (design loop and fitter Evaluation) are refused while a campaign
runs, so they never compete with live fitting.

### Safety: when the AI fitter is wrong, stuck or broken

The AI is never trusted with a number, and it can never stall the loop or run up
the token bill (`src/fitting/ai_guard.py`, `src/fitting/llm_structure.py`):

| Case | What happens |
|---|---|
| **Hallucination** (shapes not in the catalogue, unreadable or empty answer) | Off-catalogue shapes are dropped; an answer with nothing usable falls back to Catalogue + BIC (status `fallback_invalid_answer`). A plausible but wrong shape is fitted like any other and loses on BIC; the design loop always gets the lower-BIC fit. |
| **Stuck or slow** | Each AI call times out after 45 s, with no automatic retries; a second AI round is skipped once the fit has taken 90 s. The design loop waits at most 3 min for an AI fit, then uses the quick fit; a refit that would arrive after its recipe was learned is skipped (no tokens spent). A fit slower than 150 s counts as a failure. |
| **Broken** (no login, gateway down, errors) | Falls back to Catalogue + BIC at once. |
| **Repeated failures** | After 3 failed AI fits in a row (any case above), the AI fitter **pauses for 30 min**: every profile gets Catalogue + BIC only, no AI call, no token. The log says why (once) and the status line shows "AI FITTER PAUSED". After the pause one trial fit is made; another failure pauses again, a good fit clears it. |
| **Runaway use** | At most 60 AI calls per hour (`SWAXS_FITTER_AI_MAX_PER_HOUR`); above that the AI is skipped until the hour rolls over. Each call is capped at 900 output tokens, and an AI fit makes at most 2 calls. Cached answers cost nothing. |
| **Backlog** | At most 20 AI refits wait; beyond that a profile keeps its Catalogue + BIC fit. |

Settings: `SWAXS_FITTER_AI_MAX_FAILURES` (3), `SWAXS_FITTER_AI_COOLDOWN_S` (1800),
`SWAXS_FITTER_AI_SLOW_S` (150). The pause lives in memory: a restart allows one
trial fit again. The AI next-recipe levels have their own guards (validation,
the optimiser's pick as fallback, 45 s timeouts, every decision logged); see
"AI next-recipe levels".

### Fitting tab

**Analysed profiles** table (File, D (nm), PDI, Conf., Scored), sortable, with a
filter box, **Analyse existing profiles** (fits older profiles with no fit
record, oldest first, in the background; never drives the optimiser) and a
**CSV** download. D is the diameter; `*` marks the equivalent diameter
(2·√(5/3)·Rg) of a non-sphere. **Scored** says whether a result steers the
running campaign: **yes**; **weak** (below the minimum confidence: it informs the
model with a low weight but cannot be the answer or end the run); **held** (not
the expected shape: it counts against the budget but does not train the model
and cannot be the best or the hit). Select a row (or use the arrow keys) to see
Model, Diameter, Radius, PDI, Confidence, Rg (Guinier), Invariant and Phase, and
the data with the fitted model. A held fit shows **Accept: let it count**, after
a confirmation. Plot scales: **Log-log**, **Semilog Y**, **Semilog X**,
**Linear**, **Guinier**; toggles **Residuals** and **Error bars**. An optional AI
QC note appears under the plot when AI credentials are set. Confidence badges:
green at ≥ 0.6, amber at ≥ 0.3, otherwise low.

Under the plot, **Right model?** records your call on the fit: **Right** (the
fitted model is right), or the right model from the catalogue (optionally with a
structure factor), plus a note, then **Save: wrong model**. Calls are **judged
fits** in the Fitter part of the running campaign's knowledge base, else the base
chosen in Setup (the box says which; with no base it asks you to choose one); a
new call on the same file replaces the old one. Two or more calls saying the same
correction (X is really Y) draft a note that waits for your approval.

### Knowledge for the AI fitter (Knowledge tab, Fitter part of a base)

AI-assisted structure reads the **Fitter** part of the knowledge base chosen in
Setup; with no base, only Guinier's catalogue, corrections, SOPs and guidelines
(as before). In the Knowledge tab, **Fitter, What it uses** ticks, for that base:
**Guinier's knowledge** (shared with the assistant, `ai_knowledge/`): Model
catalogue (always on), Operator corrections, Group SOPs, SAXS guidelines,
Literature passages (the most relevant passages from the assistant's index, found
per curve; only when the index is built here) and Expert cases
(`ai_knowledge/cases/*.json`; none yet); and **this base's own knowledge**: Notes
and Judged fits. Settings: similar judged fits shown per curve (0 to 8, default
3) and literature passages per curve (1 to 6, default 3). For each curve the AI
is shown the most similar judged curves (rms difference of mean-centred log I on
a shared log-q grid, at most 0.6 decades), never the curve's own call. Order of
authority: corrections, then the base's notes, then SOPs, then guidelines. Notes,
passages and cases are passed as data, never as instructions; the AI still only
proposes, every candidate is fitted and BIC picks the winner. At Start the
Fitter part is frozen (`.swaxs_state/fit_knowledge/<hash>.json` in the project;
only at Start, pruned to those a campaign record names plus the newest 20)
and every fit records the version (`llm_structure.knowledge`: hash, base,
sources, judged fits and passages used). Between campaigns, fits use the base as
it is now. A project with an older `fit_knowledge.json` (before bases had a
Fitter part) offers **Copy them into this base**; the file is left as it was.
Logic: `src/fitting/fit_knowledge.py`.

### AI next-recipe levels (Setup, Design loop, Agentic)

| Level | Who picks each recipe | The AI's part |
|---|---|---|
| **AI Advisor** | the optimiser | every 5 or 10 runs it reviews progress and may suggest a narrower working range; **Apply suggestions**: automatically if inside the safety limits (undo in Report, Decisions), or **ask me first** |
| **AI Co-pilot** | the AI, from the optimiser's shortlist of 5 (the optimiser's own pick first, then the best by Expected Improvement) | picks one, gives a reason, cites cards or "data" |
| **AI Pilot** (experimental) | the AI writes the recipe | must be inside the search range, pump windows, hard caps, working range and a trust region (best ± a quarter of each span), new, and cited; one retry, then the optimiser's pick; reviews every 5 or 10 runs cannot be turned off; Start asks you to confirm the limits |

The cold start is never the AI's. The AI level drives the optimiser shown in its
detail box (Shortlist from / Fallback optimiser / Optimiser). **More options** sets the
**Claude model** (Default = `SWAXS_DESIGN_MODEL`, else `ANTHROPIC_MODEL`, else
`ANTHROPIC_DEFAULT_SONNET_MODEL` from `~/.claude/settings.json`, else
`us.anthropic.claude-sonnet-4-6`; or the same gateway ids the assistant offers:
Sonnet 4.6, Sonnet 5, Opus 4.8, Opus 5, Haiku 4.5) and
**Thinking** (off: temperature 0, the same answer for the same state; standard
about 2k and deep about 8k tokens of extended thinking, slower; answers are still
cached on the exact prompt). **Test AI connection** makes one tiny real call with
that model and shows "Connected: model, ms" or the error type (never a
credential); the Ready list repeats it. From a terminal: `python tools/ai_check.py
--all`. Both are recorded with every decision. Whether the AI reads the cards
and your verdicts at every pick or review (not only for the starting recipes) is
ticked with the base (Knowledge, Design loop, What it uses). Anything invalid,
no AI login, a timeout or a non-JSON answer: the optimiser's pick, recorded as a
fallback; the loop never waits on the AI (the call runs in the background, and
a pause or abort meanwhile drops its answer). Every decision is in **Report,
Decisions** (who chose, the shortlist position, the reason, the cards cited, the
knowledge version, the result) and traced in `logs/assistant_traces/` (kind
`design`). The same prompt gets the same (cached) answer, so a replay is
reproducible. Logic: `src/optimizer/ai_design.py`.

### Knowledge tab

One **knowledge base** per chemistry (e.g. Pd nanoparticles;
`ai_knowledge/design/<id>.json`, in git), with two parts, listed in this order:

- **Fitter**: **What it uses** (the sources above, and its settings), **Notes**
  (add, one line per note, used at once; drafted notes from your calls show
  **Approve** / **Discard**; **Retire**), **Judged fits** (your calls, with
  **Retire**) and **Evaluation** (below).
- **Design loop**: **What it uses** (Literature cards, Your note cards, Past
  campaigns as a warm start (not used by Random), Your verdicts (AI levels), and
  whether AI levels read them at every pick or review; plus the settings:
  starting recipes from knowledge, AI shortlist size, AI Pilot trust region), then
  the pages below.

Setup only picks the base by name. The assistant (Guinier) does not read a base.

- **Add knowledge**: **Search the web** (OpenAlex; only the query is sent; the
  query starts from `chemistry.yml` and the target; tick papers, then **Draft
  cards from the ticked abstracts**), **Add a paper** (a DOI, a PDF if a PDF
  reader is installed, or a pasted passage with its source) and **Your notes**
  (one line, one card; the setting and range are guessed).
- **Drafts**: every new card waits here. Each shows the passage it came from,
  how its range sits in the search range (clipped, or outside and not used) and
  whether it reads like a safety limit. Edit the setting, range, text or source,
  then **Approve** or **Reject**.
- **Cards**: the approved cards, filterable by Literature / Your notes;
  **Retire** stops using a card (it stays in the set's history).
- **Your verdicts**: the Good / Bad calls, with **Retire**.
- **Campaigns**: finished campaigns that used the set, with their measured
  runs, target, tolerance and PDI cap; they join automatically when a campaign
  ends (also on Abort).
- **Design loop, Evaluation**: **Run test** and **Propose improvement**.
  - **Run test** replays each finished campaign against an emulator fitted to
    its own measured runs (a GP for diameter and one for PDI), with each method
    running a fresh campaign with that campaign's target and budget, 3 times:
    Random, GP + EI, GP + EI + past campaigns (the other campaigns as a warm
    start), GP + EI + recipes from cards, and, if ticked, AI Co-pilot with cards
    and verdicts (calls the AI). Scores: runs to reach the spec (not reached =
    budget + 1) and the best score after 10. A campaign's own verdicts are left
    out of its replay. Indicative with one or two campaigns; an easy spec that
    every method meets in the cold start cannot separate them.
  - **Propose improvement** tries other settings on the older campaigns and
    proposes one only if it also does better on the most recent campaign, which
    it was not tuned on. **Approve** applies it (new set version); running
    campaigns keep the version they started with. Logic:
    `src/design_knowledge/replay.py`.
- **Export** downloads the set; **Import** makes a new set from an export, with
  every card as a draft to review.
- **Fitter, Evaluation**: **Run test** refits the base's judged curves that name
  the right model (at least 4, the latest 20) with Catalogue + BIC and with the
  AI fitter given Guinier's knowledge only, plus the notes, plus the notes and
  judged fits (each curve without its own call), and counts how often each finds
  the right model, on two halves (practice and held out). Without an AI login only
  the baseline runs. **Propose improvement** tries other settings on the practice
  half and proposes one only if it is also better on the held-out half;
  **Approve** applies it from the next campaign.

How the AI drafts cards: with an AI login the AI reads the text as data (never
as instructions) and must quote, for each card, a sentence that is really in
the text, name one of T_reac, F_tot, x_ODE, x_TOP, x_oley and give numbers only;
anything else is dropped. Without an AI login a sentence filter drafts cards
instead. Every change bumps the set's version; nothing is deleted.

### Guide tab

One page per goal, fitting method, next-recipe method and concept (score,
starting recipes, knowledge, report views): what it does, when to use it and
not, the maths, settings and how it scored. Every method in Setup and the goal
header link to their page (**Details in the Guide →**).

### Report tab

- **Decisions**: every run of the campaign (who chose it: cold start, your own,
  from knowledge, the optimiser or an AI level; what it was; why and what it was
  based on for AI picks; the result) and **Good call** / **Bad call** with one
  line why. A verdict is kept with the run and, when the campaign used a
  knowledge base, in that base at once (an AI level reads it next time). For AI
  levels also: AI picks, how many matched the optimiser's own top pick,
  fallbacks, the working range, the Advisor's reviews, a waiting suggestion with
  **Apply** / **Reject**, and **Undo range**.
- **Did the knowledge help?**: the last replay of the campaign's knowledge base
  (below), and **Run test**.
- **Campaign**: status, **Pause** / **Resume** (paused: no new recipe is issued;
  anything already queued still runs and its result is still recorded) and
  **Abort** (asks first; the reactor finishes the recipe it is running).
- **Parameter space & convergence**: views **Surrogate** (slice through the GP;
  choose x, y and "Through best recipe" or "Through box centre"),
  **Convergence**, **Trajectory**, **Level set**, **Pareto**. Read-only: viewing
  never changes the next recipe. In Simulation mode the simulator's hidden
  optimum is marked; with Hardware it is never shown. Guide:
  [PARAMETER_SPACE_AND_CONVERGENCE.md](../docs/PARAMETER_SPACE_AND_CONVERGENCE.md).
- **Method comparison (in silico)**: choose the Compare methods goal, tick the
  methods (Random, GP + EI, GP + UCB, TuRBO: the ones that optimise the same
  score; Random and GP + EI by default), set repeats and press **Run comparison**.
  It races them against the simulator's ground truth with the same loss and the
  Setup target. It never touches the live campaign, the reactor or real data.

## 4. Settings and defaults

### Fit model (Fitting method)

| Method | What it does |
|---|---|
| **Least-squares sphere** (`ls_sphere`, default) | the physics fit below |
| **ML regressor (k-NN)** (`ml_regressor`) | k-nearest-neighbour match against a library of Schulz sphere curves (R 1 to 18 nm, PDI 0.02 to 0.40); fast baseline. Its confidence is absolute: the log RMS of the predicted model against the data (scale and background fitted), times a signal check (a flat curve scores about 0), cut to a quarter when R or PDI sits on the library edge (`diagnostics.railed`; a 30 nm particle reads as about 18 nm). The library has no background term, so a curve with a strong flat background usually scores below 0.5 |
| **LLM-assisted** (`llm_assisted`) | the AI proposes, physics fits, statistics decide (`src/fitting/llm_structure.py`). The AI reads the curve's features, what the pipeline knows (recipe, `chemistry.yml`, normalisation, background and quality flags), a log-log and Kratky plot, and the knowledge bundle (model catalogue, corrections, SOPs, SAXS guidelines), and names up to 3 catalogue structures. Each, plus a sphere baseline, is fitted by the robust fitter; the lowest BIC wins, so a wrong AI guess is overruled. If the best fit still misses (reduced χ² > 2) the AI sees the residuals once and may add 2 more. Proposals are cached per curve; every decision goes to `logs/assistant_traces/` (kind "fitter"). With no AI login, an error or an invalid answer it falls back to Catalogue + BIC and says so (`llm_structure.status`). Optional `SWAXS_FITTER_MODEL` picks the AI model |
| **Catalogue + BIC** (`catalogue_bic`) | fits every structure in `ai_knowledge/rules/saxs_models.yml` (sphere, core-shell sphere, ellipsoid, cylinder, mass fractal), then the best one with hard-sphere and sticky hard-sphere structure factors, and keeps the lowest BIC (χ² plus a penalty per parameter). No AI. Robust fitter in `src/analysis/model_fit.py` (scale and background solved exactly, sizes on a log scale, several seeded starts). Reports the **equivalent radius** (√(5/3)·Rg of the fitted shape; the radius itself for a sphere) to the optimiser; the model, its parameters ± errors and every candidate's χ²/BIC are in the result, the Fit record and the manifest. About 3 to 20 s per curve |

The least-squares model (`src/analysis/nanoparticle.py`, `analyze_profile`):

```
I(q)   = scale · ∫ n(R) V(R)² P(q,R) dR + background
P(q,R) = [3(sin x − x cos x)/x³]²,  x = qR
V(R)   ∝ R³   (constants absorbed into scale)
n(R)   = Schulz or log-normal, mean R̄, PDI = σ/R̄
```

- The app fits with `dist="auto"`: both distributions are fitted and the one with
  the lower `rms_log10` is kept (`distribution`, `diagnostics.chosen_distribution`).
- Fit in log space, residual log₁₀(model) − log₁₀(I), weighted by 1/σ(log₁₀ I)
  when σ is available; `scipy.optimize.least_squares`, `trf`, max 4000
  evaluations. Four parameters: R̄, PDI, log₁₀(scale), background.
- Bounds: PDI in [0.01, 0.6]; R̄ in [1e-3, max(20·R̄₀, 3π/q_min)].
- Start radius R̄₀ = √(5/3)·Rg (Guinier, sphere relation), else 1/median(q).
- Uncertainties from a Gauss-Newton covariance on the Jacobian.
- numpy and scipy only (no sasmodels), so it runs unattended.

**Guinier**: the window is iterated up to 6 times toward q_max·Rg ≈ 1.3; it is
`valid` only when q_min·Rg < 1.0 and q_max·Rg < 1.5.

**Invariant is relative**: `invariant = {"Q_rel": ∫q²I dq, "absolute": false}`.
Intensity is in arbitrary units, so compare `Q_rel` only between profiles of the
same session and normalisation; never quote it as a volume fraction.

**Phase (report only, never affects size or confidence)**: Bragg peaks are found
on detrended log₁₀(I) (`detect_bragg_peaks`, `min_prominence=0.08`, needs ≥ 20
points) and indexed by q/q₁ ratios within 6%:

| Phase | q/q₁ |
|---|---|
| `lamellar` | 1, 2, 3, 4 |
| `hexagonal` | 1, 1.732, 2, 2.646, 3 |
| `BCC` | 1, 1.414, 1.732, 2, 2.236 |
| `FCC` | 1, 1.155, 1.633, 1.915, 2.309 |
| `simple_cubic` | 1, 1.414, 1.732, 2 |

Named when score ≥ 0.75 with ≥ 2 peaks, else `"disordered"`; `"none"` with fewer
than 2 peaks. `d_spacing = 2π/q₁`.

**Confidence** (0 to 1, product of factors):

```
f_fit  = 1 / (1 + (rms_log10 / 0.05)²)        0.05 log10 RMS = good
f_guin = 1.0 if Guinier valid else 0.6
f_unc  = 1 / (1 + (rel_err_radius / 0.10)²)   10% size error = borderline
× 1 / (1 + (|R_fit − R_Guinier| / R_Guinier / 0.30)²)   when Guinier is valid
× 0.25 if R or PDI hit a bound;  × 0.5 if the solver did not converge
```

**Result fields**: `n_points`, `distribution` (`schulz`, `lognormal`,
`guinier_only` or None), `size` `{radius, diameter, unit, source}`, `pdi`,
`guinier` `{Rg, I0, R_from_Rg, valid, n_points}`, `invariant`, `phase`
`{phase, n_peaks, ratios, d_spacing, match, score}`, `fit`
`{rms_log10, scale, background}`, `uncertainty` `{radius, pdi}`, `confidence`,
`diagnostics` `{rms_log10, guinier_valid, rel_err_radius, at_bounds, converged,
chosen_distribution}`.

**Degraded results**: fewer than 12 usable points gives confidence 0 and
`diagnostics.error = "too few points"`. If both form-factor fits fail, the size
falls back to √(5/3)·Rg (`distribution = "guinier_only"`, `size.source =
"guinier"`, no PDI), with a low confidence by construction.

**Units**: sizes are in the units of 1/q, nm for the platform default q in nm⁻¹.
If the `.dat` header marks q in Å⁻¹ (`q_A-1`, `a^-1` or `å`), q is multiplied by
10 before fitting.

### Parameter space (`src/optimizer/space.py`)

| Knob | Default bounds | Unit |
|---|---|---|
| `T_reac` | 180 to 300 | °C |
| `F_tot` | 40 to 120 | µL/min |
| `x_ODE`, `x_TOP`, `x_oley` | each 0 to 0.3 | fraction of F_tot |

Constraint `x_ODE + x_TOP + x_oley ≤ x_sum_max` (default 0.9); precursor is the
remainder. All come from the `bounds` block of the reactor config
(`ParameterSpace.from_config`), so the optimiser cannot propose a recipe the
reactor rejects. Sobol sampling with rejection keeps every proposal valid.

### Campaign parameters

The UI enters diameters; the optimiser works in **radius** (target and tolerance
are halved). For Pareto the target is the centre of the range and the tolerance
its half-width, both as radius.

| Parameter | UI default | API default (`POST /api/campaign/start`) |
|---|---|---|
| target | 5 nm diameter | `target_size` 5.0 (radius) |
| tolerance | ± 0.6 nm diameter | `tolerance` 0.3 (radius) |
| PDI cap | 0.1 | `pdi_cap` 0.15 |
| budget | 30 | `budget` 25 |
| cold start | 10 (Spread out + Your own) | `n_init` 10; must be 1 to `budget` |
| your own recipes | none | `seeds` [{T_reac, F_tot, x_ODE, x_TOP, x_oley}], at most 5 |
| expected shape | sphere (core-shell counts) | `expected_shape` `sphere_cs` (`sphere`, `any`) |
| min fit confidence | 0.5 | `confidence_min` 0.5 (0 to 1) |

`ai`: `{level: ai_advisor | ai_copilot | ai_pilot, review_every: 0 | 5 | 10, apply: auto | ask,
model, thinking: off | standard | deep, confirmed}` (Pilot needs `review_every` 5 or 10 and `confirmed: true`; not for
`benchmark`); `knowledge.use.every_pick` gives the AI the cards at every pick.
`knowledge`: `{set_id, use: {literature, notes, campaigns}, n}`; `n` (0 to 10) recipes
from knowledge go after your own and before the spread; an unknown set is refused.
The frozen snapshot (set id, version, cards and campaigns used) and the warm-start
points are recorded in the campaign meta, so resume and Continue rebuild the same run.

Other body keys: `goal` (`target-hit`, `level-set`, `pareto`, `benchmark`),
`size_range` [min, max] diameter (Pareto), `method_params`. A campaign recorded
before `expected_shape` existed keeps its old behaviour (any shape) when resumed
or continued.

**Search strategies** (`src/optimizer/methods/`):

| Strategy | Goal(s) | Extra input |
|---|---|---|
| Bayesian GP + EI (`gp_ei`, default) | Target-hit | none |
| Bayesian GP + UCB (`gp_ucb`) | Target-hit | UCB κ, default 2.0 |
| TuRBO (trust-region BO) (`turbo`) | Target-hit | trust-region size, default 0.5 |
| Random / Sobol search (`random`) | Target-hit | none |
| BAX (`bax`) | Level-set (forced) | band threshold τ (loss), default 2.0 |
| Pareto GP + EHVI (`pareto_ehvi`) | Pareto (forced) | none |

**How a campaign runs** (`CampaignController`, `src/optimizer/campaign.py`):

1. Cold start: `n_init` recipes: your own (`seeds`, in order) first, then space-filling Sobol points for the rest (seed 0, so Preview shows exactly these).
2. Then each proposal maximises the acquisition (EI for the default) over a pool
   of 256 Sobol candidates, under an RBF Gaussian process on unit-cube inputs.
3. Each proposal gets a `recipe_id` `RunN_rNNN` (for example `Run3_r001`) and is
   written as `Conditions/<recipe_id>.txt`, `key = value` lines: `recipe_id`,
   `T_reac`, `F_tot`, `x_ODE`, `x_TOP`, `x_oley`. N is one more than the highest
   `RunN` found on disk, so a new run never reuses old file names.
4. The reactor names acquisitions `{recipe_id}_{sample|bkg}...`; when a fitted
   profile carries a pending `recipe_id`, its radius, PDI and confidence are told
   to the optimiser and the next condition is written.

**Loss** (lower is better, radius units):

```
loss = ((R − R_target) / tol)² + weight_pdi · (PDI / pdi_cap)
```

`weight_pdi` = 1.0. No PDI counts as `weight_pdi · 1`. An unsized profile gets
1e3; it uses budget but is left out of the GP. `loss_transform` is `"none"`
(optional `"log1p"`).

**Confidence weights, it does not filter**: GP noise per point is
`(0.05·var(y) + 1e-6) / max(confidence, 0.05)`, so a weak fit is trusted less but
still counts. The reported best recipe prefers points with confidence ≥ 0.5.

**Held results** (`src/optimizer/intake_rules.py`): a fit whose structure is not
the expected shape (catalogue model without its structure factor: `sphere` and
`polydisperse_sphere` are spheres, `core_shell_sphere` counts for "sphere
(core-shell counts)") is recorded with `held: "shape"`. It uses its budget slot
but is left out of the GP (and the Pareto front, and TuRBO's trust region), and
cannot be the best or the hit, until `POST /api/campaign/accept`. Held and model
are in `history.csv`.

**Pause**: while paused no new condition is written; a result for an issued
condition is still told to the optimiser; Resume writes the next condition if
none is pending. The pause state survives a restart.

**Convergence**: status is `idle`, `running`, `converged`, `exhausted` or
`aborted`. A hit is a run with confidence ≥ 0.5 (`confidence_min`), |R − target|
≤ tolerance and PDI ≤ cap. Target-hit stops as `converged` at the first hit;
Level-set and Pareto keep measuring to the budget (outcome `mapped`). Reaching
the budget without a hit is `exhausted`.

**Timeouts and restarts**:
- A condition with no measurement after `SWAXS_PENDING_TIMEOUT_S` (default 3600 s)
  is recorded as failed and the next one is proposed, so the loop never idles.
  Its file is moved to `Conditions/done/` with a "NOT RUN" note, so the reactor
  drops it if it is still queued (one already running finishes). If its
  measurement arrives later anyway, it replaces the failure in the history
  (the budget count does not change) and can still converge the campaign.
- After a restart, the expiry clock of every pending condition starts again,
  and a profile for a pending condition that landed while the app was down is
  fitted (other profiles on disk at startup stay frozen).
- Continue and resume count the proposals already made from the highest
  `RunN_rNNN` issued (fit records, `Conditions/`, `Conditions/done/`, reactor
  feedback), so a seed already handed out is never proposed again.
- The search space applies each pump's `[sensor_min, max_flow]` (the reactor's
  live limits from `reactor_limits.json` over `reactor/config.yml`): a nonzero
  rate `x · F_tot` outside it is never proposed. The Pareto front uses only
  fits with confidence at least `confidence_min`.
- A running campaign is **not** auto-resumed after a restart (it drives the
  reactor); a banner says so. Use **Continue RunN** to rebuild it from the
  durable fit records, which also re-issues conditions still waiting in
  `Conditions/`. `SWAXS_RESUME=1` opts in to automatic resume.
- **Start** sets aside leftover condition files into `Conditions/done/` with a
  "NOT RUN" note (nothing is deleted). **Abort** does the same for its own
  queued conditions, saves the results, and clears the run so the next Start is
  a new Target Run.

### Records written

- **Every fit**: `Results/Fit/fit_<stem>.png` (log-log data and fit, annotated with
  R, D, PDI, phase, distribution, confidence) and `fit_<stem>.dat` (columns
  `q_nm-1  I_data  sigma  I_fit`, a `#` header with the fit values, source file
  and campaign target). `I_fit` is NaN when there was no form-factor fit. The
  newest 5,000 pairs are kept (`SWAXS_ANALYZER_FIT_RETENTION`, 0 = keep all).
- **Campaign target**, written at start in three places: `manifest.json`
  `project_meta.campaign`; each `analyses` entry's `params` (`campaign_id`,
  `target_size`, `tolerance`, `pdi_cap`); and `Results/campaign_<id>.json`,
  rewritten at the end with the outcome (`converged`, `mapped`, `exhausted` or
  `aborted`). `.swaxs_state/campaign.json` is resume state that expires after 7
  days; do not use it as a record.
- **Campaign end**: `Results/campaign_<id>/` with `campaign.json` (target,
  outcome, full history), `history.csv` (one row per evaluation),
  `convergence.png`, `trajectory.png`, `slice.png`.
- **Manifest entry per fit**: `analysis_type = "nanoparticle"`, `params` `{model:
  "polydisperse_sphere", distribution, ...}`, `results` = the table row,
  `quality_score` = confidence, `provenance` `{recipe_id, run_tag, q_unit, source}`
  where `source` is `simulated` or `measured`.
- **Bus event `fit.complete`** (live fits only, not backfill): `recipe_id`,
  `file`, `size` (radius), `pdi`, `confidence`, `distribution`, `phase`,
  `guinier_rg`, `suspect` (confidence ≤ 0.5), `plot_png`, `loss`. The Watchdog
  (5110) uses it for notifications. It is deliberately not `analysis.complete`,
  which belongs to the Data Analysis app (5106).

Never put other files in `Conditions/`: the reactor reads every `*.txt`, `*.dat`
and `*.json` there as a recipe.

## 5. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Nothing is fitted | Check the Log for the folder actually watched. Profiles present before startup are not auto-fitted: press **Analyse existing profiles**. In gate mode `good`, `Good/` may be empty. |
| Log warns "Good/ exists but holds no profiles" | The Quality Gate is not running. Start it, or set gate mode `off`. |
| Rejected profiles are being fitted | No `Good/` data yet, or gate mode `off`. Run the Quality Gate. |
| Campaign proposes one recipe then waits | The reactor has not run it yet, or the profile name does not carry the `recipe_id`. After 1 h it is recorded as failed and the loop moves on. Log warns about orphan profiles whose `recipe_id` is not pending. |
| Sizes 10× off | q unit not recognised. The header must say `q_A-1` for Å⁻¹; otherwise nm⁻¹ is assumed. |
| Low confidence everywhere | Too few points, no valid Guinier region, fit at a bound, or the sample is not sphere-like. Check the Guinier view and residuals. |
| "older profile(s) skipped" in the Log | Historical files with no fit record were not refitted, to keep the live loop fast. Use **Continue RunN** or **Analyse existing profiles**. |
| Campaign stopped after a restart | Intended: press **Continue RunN** (or Start a new one). |
| Cannot change the search strategy | A campaign is running; Abort first. |
| Start refused, "already running" | Abort the current campaign first. |

## 6. API routes

| Route | Purpose |
|---|---|
| `GET /api/health` | `{status, app: "analyzer", analyzed}` |
| `GET /api/project`, `POST /api/set_project` | project root (pushed by the hub) and watched path |
| `GET/POST /api/folder` | watched folder and `gate` (`auto`, `good`, `off`) |
| `GET /api/results` | summary row of every retained result (up to `SWAXS_ANALYZER_MAX_RESULTS`, default 600; the manifest holds all) |
| `GET /api/result/<name>` | summary, full result and downsampled plot data |
| `POST /api/analyze_existing` | backfill of unfitted profiles |
| `GET /api/stream` | SSE: first frame up to 200 newest rows, then only new rows, log lines and campaign status |
| `GET/POST /api/methods` | list and select fitting method and search strategy |
| `GET /api/campaign` | status, `pending` recipe ids, conditions folder, goal, `paused`, `held`, `expected_shape`, `confidence_min`, `n_held`, `runs` (the last 40, with who chose them and the verdict), `knowledge`, `ai` |
| `POST /api/campaign/start` | start a campaign (body keys above) |
| `POST /api/campaign/abort` | operator abort |
| `POST /api/campaign/pause` | `{paused: true|false}`; 409 when no campaign is running |
| `POST /api/campaign/accept` | `{recipe_id}` or `{name}`: let a held result count; 404 when nothing is held for it |
| `POST /api/campaign/preview` | the cold-start recipes a Start with this body would issue (`own` then `spread`), without starting; 400 with the reasons for a bad recipe |
| `GET /api/search_range` | search bounds, `x_sum_max`, hard safety caps, expected-shape choices (read-only) |
| `GET /api/guide` | the Guide pages |
| `GET /api/ai/status` | `{available, default_model, models, thinking}`: is an AI login configured (no call made) |
| `POST /api/ai/test` | `{model, thinking}`: one tiny real call; `{ok, model, ms, error}` |
| `POST /api/campaign/verdict` | `{recipe_id, call: good | bad, why}` on a measured run |
| `POST /api/knowledge/set/<id>/verdict/<vid>/retire` | stop using a verdict |
| `POST /api/knowledge/set/<id>/harness` | `{n_knowledge, shortlist_k, trust}` (clamped to their limits) |
| `POST /api/knowledge/set/<id>/evaluate`, `/propose`; `GET .../evaluation` | replay test and improvement proposal (background), and their state or last result |
| `POST /api/knowledge/set/<id>/proposal` | `{approve}`: apply or discard a proposed improvement |
| `POST /api/campaign/ai/suggestion` | `{accept}`: apply or reject the Advisor's waiting range |
| `POST /api/campaign/ai/undo` | undo the last applied working range |
| `GET/POST /api/knowledge/sets` | list sets (with `matches_chemistry`) and the project's `chemistry.yml`; create a set `{name, keywords}` |
| `GET /api/knowledge/set/<id>` | the set, each card with its check against the search range |
| `POST /api/knowledge/set/<id>/notes`, `/text`, `/doi`, `/pdf`, `/from_search` | draft cards from notes, a passage, a DOI's abstract, a PDF, ticked search results |
| `POST /api/knowledge/search` | OpenAlex search `{query}` |
| `POST /api/knowledge/set/<id>/review` | `{card_id, action: approve, reject, edit, retire, edit: {...}}` |
| `GET /api/knowledge/set/<id>/export`, `POST /api/knowledge/import` | share a set by file; imported cards are drafts |
| `GET /api/campaign/incomplete`, `POST /api/campaign/continue` | offer and rebuild the latest unfinished Target Run |
| `GET/POST /api/campaign/folder` | conditions folder. Until set here it follows the reactor's folder (`reactor_settings.json`, else `folders.recipes` in `reactor/config.yml`). Returns `reactor` and `mismatch`; a folder that differs from the reactor's is logged as an error and shown in the campaign status line (`conditions_folder_mismatch` in `GET /api/campaign`) |
| `GET /api/campaign/diagnostics` | read-only learning and convergence summary |
| `GET /api/campaign/plot/<view>.png` | `slice` (`x`, `y`, `anchor`), `convergence`, `trajectory`, `levelset`, `pareto` |
| `POST /api/study`, `GET /api/study/plot.png` | in-silico method comparison |
| `GET/POST /api/knowledge/active` | `{set_id}`: the base chosen in Setup (used by AI fits outside a campaign, and where calls on fits go) |
| `POST /api/knowledge/set/<id>/use` | what the design loop uses `{literature, notes, campaigns, verdicts, every_pick}`, saved with the base; a Start without `knowledge.use` takes it |
| `GET /api/fitknowledge` | Guinier's sources with what is there now, the catalogue's models, the active base, where calls go, the running campaign's frozen fitter knowledge, whether the project has an older `fit_knowledge.json` |
| `GET /api/knowledge/set/<id>/fitter` | the base's Fitter part: sources, notes, judged fits, settings, last evaluation, proposal, job |
| `POST /api/knowledge/set/<id>/fitter/sources`, `/settings` | `{sources}` (the catalogue is always added); `{n_fits, n_passages}` (clamped) |
| `POST /api/knowledge/set/<id>/fitter/note`, `/note/<nid>`, `/fit/<fid>/retire` | add notes `{text}` (one per line); `{action: approve, retire, discard}`; stop using a judged fit |
| `POST /api/knowledge/set/<id>/fitter/evaluate`, `/propose`, `/proposal`, `/import_project` | test and improvement (background; state in the part's `job`); `{approve}`; copy an older project `fit_knowledge.json` in |
| `POST /api/fitknowledge/verdict` | `{name, verdict: right | wrong, correct_model, note}` on a fit, saved in the running campaign's base, else the active one; returns the drafted notes |
| `GET /api/restart_notice` | whether an interrupted campaign needs a manual Continue |
