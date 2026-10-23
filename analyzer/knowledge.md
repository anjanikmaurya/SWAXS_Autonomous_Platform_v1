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

The page has three tabs: **Setup**, **Fitting**, **Report**, plus a **Log** panel
visible on every tab. The project folder is pushed by the hub.

### Setup tab

1. **Watched folder** card: the default `1D/SAXS/Subtracted` is usually right.
   Change it with **Browse…** and **Set** (project-relative or absolute). For data
   collected earlier, press **Analyse existing profiles**: it fits profiles that
   have no fit record, oldest first, in the background, and never drives the
   optimiser.
2. **Closed-loop campaign** card, three steps:
   - **1 Campaign goal**: **Target-hit** (find one recipe in spec; stops at the
     first confident hit), **Level-set** (map every in-spec recipe; uses BAX),
     **Size-PDI Pareto** (map the size vs PDI trade-off across a diameter range;
     uses EHVI), **BAX vs BO** (benchmark; run the comparison in Report).
   - **2 Target & limits**: **Target diameter (nm)** and **Tolerance ± (nm)**, or
     **Min diameter / Max diameter (nm)** for Pareto; **Max PDI (spread)**;
     **Max experiments**.
   - **3 How it fits & searches**: **Fitting method** and **Search strategy**
     (the list is filtered to the goal), plus any strategy-specific inputs.
3. Press **Start campaign**. The first condition file is written immediately.
   Spec, goal and methods are locked while it runs. **Abort** stops the
   campaign (the reactor finishes the recipe it is running). If the latest run
   was interrupted, a **Continue RunN: X of Y used** button appears instead.
4. **Live progress** shows the status, Best radius, Δ to target, Best PDI, Runs
   and a small radius-per-run chart. **How the score works** explains the loss.

### Fitting tab

**Analysed profiles** table (File, R (nm), PDI, Conf., Phase), sortable, with a
filter box and a **CSV** download. Select a row (or use the arrow keys) to see
Radius, Diameter, PDI, Confidence, Rg (Guinier), Invariant (rel.) and Phase, and
the data with the fitted model. Plot scales: **Log-log**, **Semilog Y**,
**Semilog X**, **Linear**, **Guinier**; toggles **Residuals** and **Error bars**.
An optional AI QC note appears under the plot when AI credentials are set.
Confidence badges: green at ≥ 0.6, amber at ≥ 0.3, otherwise low.

### Report tab

- **Parameter space & convergence**: views **Surrogate** (slice through the GP;
  choose x, y and "Through best recipe" or "Through box centre"),
  **Convergence**, **Trajectory**, **Level set**, **Pareto**. Read-only: viewing
  never changes the next recipe. In Simulation mode the simulator's hidden
  optimum is marked; with Hardware it is never shown. Guide:
  [PARAMETER_SPACE_AND_CONVERGENCE.md](../docs/PARAMETER_SPACE_AND_CONVERGENCE.md).
- **Method comparison (in silico)**: tick optimisers and press **Run
  comparison**. It races them against the simulator's ground truth with the same
  loss. It never touches the live campaign, the reactor or real data.

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
| seed points | not in UI | `n_init` 10 |

Other body keys: `goal` (`target-hit`, `level-set`, `pareto`, `benchmark`),
`size_range` [min, max] diameter (Pareto), `method_params`.

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

1. Cold start: `n_init` space-filling Sobol points.
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
| `GET /api/campaign` | status, `pending` recipe ids, conditions folder, goal |
| `POST /api/campaign/start` | start a campaign (body keys above) |
| `POST /api/campaign/abort` | operator abort |
| `GET /api/campaign/incomplete`, `POST /api/campaign/continue` | offer and rebuild the latest unfinished Target Run |
| `GET/POST /api/campaign/folder` | conditions folder. Until set here it follows the reactor's folder (`reactor_settings.json`, else `folders.recipes` in `reactor/config.yml`). Returns `reactor` and `mismatch`; a folder that differs from the reactor's is logged as an error and shown in the campaign status line (`conditions_folder_mismatch` in `GET /api/campaign`) |
| `GET /api/campaign/diagnostics` | read-only learning and convergence summary |
| `GET /api/campaign/plot/<view>.png` | `slice` (`x`, `y`, `anchor`), `convergence`, `trajectory`, `levelset`, `pareto` |
| `POST /api/study`, `GET /api/study/plot.png` | in-silico method comparison |
| `GET /api/restart_notice` | whether an interrupted campaign needs a manual Continue |
