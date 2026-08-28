# Nanoparticle Analyzer — knowledge

## Purpose and place in the closed loop
The Nanoparticle Analyzer (port 5008) is the **measurement half of the closed
synthesis loop**. It watches the SAXS `Subtracted` folder and, as each new
profile appears, auto-fits a polydisperse-sphere model to extract size,
polydispersity, the (relative) Porod invariant, an ordered-phase classification
and a 0–1 confidence. Its output drives the Bayesian optimizer, which writes the
next synthesis condition into `1D/SAXS/Conditions` — the folder the REACTOR app
watches.

The full loop:

```
reactor (5007)  →  reduction (5001)  →  viewer (5002)  →  background (5003)
       ↑                                                        ↓
       └──── optimizer ←── analyzer (5008) ←──── quality (5006) ─┘
             (writes 1D/SAXS/Conditions)
```

The reactor synthesises a condition, the beamline writes 2D frames, reduction
makes 1D profiles, the viewer averages them, the background app subtracts, the
Quality Gate sorts them into `Good/`, the analyzer fits the accepted profiles, the
optimizer turns the fit into a loss and proposes the next condition, and the
reactor picks it up.

All science lives in `src/analysis/nanoparticle.py` and `src/optimizer/`;
`analyzer/app.py` is a thin shell holding the routes, the folder watcher, SSE and
manifest writing.

## Watched folder and the Quality Gate
Default watched folder is `1D/SAXS/Subtracted` (`_sub_folder`, changeable through
`GET/POST /api/folder`, relative to the project root or absolute).

`_gate_mode` decides how the Quality Gate is honoured:
- **auto** (default) — if a `Good/` subfolder exists under the Subtracted folder,
  analyse THAT, so a profile the gate rejected can never reach the fit or the
  optimizer.
- **good** — always require `Good/`.
- **off** — legacy: analyse the flat folder, gate advisory only.

The watcher is non-recursive and polls every 3 s. It uses
`src.reactor.intake.decide_intake` on (size, mtime) so a file is only analysed
once it has stopped changing — a half-written `.dat` is deferred, not fitted.

## The model
Polydisperse sphere form factor with a size distribution:

```
I(q) = scale · ∫ n(R) V(R)² P(q,R) dR + background
P(q,R) = [3(sin x − x cos x)/x³]²,   x = qR
V(R)  ∝ R³      (constants absorbed into `scale`)
n(R)  = Schulz (gamma) or log-normal, mean R̄, PDI = σ/R̄
```

`dist` may be `"schulz"`, `"lognormal"`, or **`"auto"` (what the app uses)**:
with `auto` BOTH distributions are fitted and the one with the lower
`rms_log10` residual is kept. The chosen one is reported as `distribution` and in
`diagnostics.chosen_distribution`.

The fit is done in **log space** (residual = log₁₀(model) − log₁₀(I)) with
`scipy.optimize.least_squares` (`trf`, bounded, max 4000 evaluations) over four
parameters: R̄, PDI, log₁₀(scale), background. PDI is bounded to [0.01, 0.6] and
R̄ to [1e-3, 20·R̄₀]. Parameter uncertainties come from a Gauss–Newton covariance
approximation on the Jacobian.

The starting radius R̄₀ is the Guinier estimate `R = √(5/3)·Rg` (the sphere
relation), falling back to `1/median(q)`.

Implementation is numpy + scipy only — no sasmodels — so it runs unattended.

## What is returned per profile
`analyze_profile(q, I, sigma=None, dist="auto")` **never raises**. It returns:

| Key | Contents |
|---|---|
| `n_points` | usable point count (finite, q > 0, I > 0) |
| `distribution` | `"schulz"` \| `"lognormal"` \| `"guinier_only"` \| None |
| `size` | `{radius, diameter, unit, source}` — `source` is `"form_factor"` or `"guinier"` |
| `pdi` | fitted polydispersity index σ/R̄ (None in the fallback path) |
| `guinier` | `{Rg, I0, R_from_Rg, valid, n_points}` |
| `invariant` | `{Q_rel, absolute: false}` |
| `phase` | `{phase, n_peaks, ratios, d_spacing, match, score}` |
| `fit` | `{rms_log10, scale, background}` |
| `uncertainty` | `{radius, pdi}` (None where the covariance was not finite) |
| `confidence` | 0–1 |
| `diagnostics` | `{rms_log10, guinier_valid, rel_err_radius, chosen_distribution}` |

### The invariant is RELATIVE
`invariant = {"Q_rel": ∫q²I dq, "absolute": false}`. Intensity is in arbitrary
units — there is no absolute calibration in this path — so `Q_rel` is only
comparable BETWEEN profiles from the same session with the same normalization.
Do not quote it as a volume fraction or a cross-section.

### Confidence
`_confidence(rms_log, guinier_valid, rel_err_R)` is a product of three factors:

```
f_fit  = 1 / (1 + (rms_log10 / 0.05)²)      ~0.05 log10 RMS = good
f_guin = 1.0 if the Guinier window was valid else 0.6
f_unc  = 1 / (1 + (rel_err_radius / 0.10)²) 10% size error = borderline
confidence = clamp(f_fit · f_guin · f_unc, 0, 1)
```

The UI colours a result `ok` at ≥ 0.6, `warn` at ≥ 0.3 and `info` below that, and
`QC_CONF_THRESHOLD = 0.5` — at or below it, a small log-log QC PNG of the profile
plus fit is rendered into `1D/QualityReports/qc_<stem>.png` and attached to the
`analysis.complete` event so the notifier can show the curve rather than a number.

**Always check `confidence` before trusting a size.**

## Units caveat
Sizes are in the same units as 1/q — nm when q is in nm⁻¹, which is the platform
default set by the reduction `unit` key (`q_nm^-1`). The `size.unit` field says so
explicitly: `"same as 1/q (nm if q in nm^-1)"`.

The analyzer additionally detects an Å⁻¹ q column from the `.dat` header (a header
mentioning `q_A-1`, `a^-1` or `å`, as written by the background app's
ML-truncated files) and multiplies q by 10 before fitting, so sizes are not 10×
wrong and the campaign optimises toward the right target.

## Graceful degradation
- Fewer than **12** usable points → an empty result with
  `diagnostics.error = "too few points"` and confidence 0.
- If BOTH form-factor fits fail, the code falls back to a **Guinier-only size**:
  `distribution = "guinier_only"`, `size.source = "guinier"`,
  `size.radius = √(5/3)·Rg`, no `pdi`, no `fit`, `diagnostics.note =
  "form-factor fit failed; Guinier-only size"`, and the confidence is computed
  with `rms_log = 1.0` (so it is low by construction).

The Guinier estimate itself iterates its window up to 6 times so that
`q_max·Rg ≈ 1.3`, and marks itself `valid` only when `q_min·Rg < 1.0` and
`q_max·Rg < 1.5`.

## Bragg-peak detection and phase indexing
`detect_bragg_peaks(q, I, min_prominence=0.08)` finds structure-factor peaks
sitting ABOVE the smooth form-factor decay: it detrends log₁₀(I) with a moving
average and runs `scipy.signal.find_peaks` on the residual. Needs ≥ 20 points.

`index_phase(peak_q)` classifies an ordered mesophase from the peak-position
ratios q/q₁ against known sequences:

| Phase | q/q₁ ratios |
|---|---|
| `lamellar` | 1, 2, 3, 4 |
| `hexagonal` | 1, 1.732, 2, 2.646, 3 |
| `BCC` | 1, 1.414, 1.732, 2, 2.236 |
| `FCC` | 1, 1.155, 1.633, 1.915, 2.309 |
| `simple_cubic` | 1, 1.414, 1.732, 2 |

A ratio counts as matched within 6% of the nearest expected value. The phase is
named only when the match score is ≥ 0.75 with ≥ 2 peaks; otherwise it reports
`"disordered"`, and `"none"` with fewer than 2 peaks. `d_spacing = 2π/q₁`.

This is **report-only** — it never affects size, PDI or confidence.

## The optimizer — parameter space
`src/optimizer/space.py` — five knobs, `NAMES = ["T_reac", "F_tot", "x_ODE",
"x_TOP", "x_oley"]`, with the reactor's own bounds so the optimizer can never
propose a recipe the reactor would reject:

| Knob | Default bounds | Unit |
|---|---|---|
| `T_reac` | [180, 300] | °C |
| `F_tot` | [40, 120] | µL/min |
| `x_ODE`, `x_TOP`, `x_oley` | each [0, 0.3] | fraction of F_tot |

Plus the composition constraint `x_ODE + x_TOP + x_oley ≤ x_sum_max` (default
**0.9**); the precursor fraction is the remainder `1 − Σx`. All of these come from
the `bounds` block of the reactor config (`ParameterSpace.from_config`).

Space-filling samples are drawn with **Sobol** sampling plus rejection, so every
proposal already satisfies the constraint.

## The optimizer — campaign lifecycle
`CampaignController` (`src/optimizer/campaign.py`).

`POST /api/campaign/start` accepts `target_size` (default 5.0), `tolerance`
(0.3), `pdi_cap` (0.15), `budget` (25) and `n_init` (10).

1. **Cold start** — optional literature seeds, then Sobol fills the rest of the
   `n_init` budget.
2. **GP suggestion** — after the seeds, each proposal is the Sobol candidate
   (pool of 256) that maximises **Expected Improvement** under a Gaussian-process
   surrogate fitted on the unit-cube parameters.
3. Every proposal gets a generated `recipe_id`
   (`auto_YYYYmmdd_HHMMSS_<4 hex>`) and is written as
   `<conditions folder>/<recipe_id>.txt` in the reactor's `key = value` param
   format (`recipe_id`, `T_reac`, `F_tot`, `x_ODE`, `x_TOP`, `x_oley`).
4. When the matching measurement arrives, `tell()` records it and the next
   condition is proposed.

Status is one of `idle`, `running`, `converged`, `exhausted`, `aborted`.
It **converges** when a CONFIDENT run (confidence ≥ `confidence_min`, default
0.5) lands within `tolerance` of `target_size` with PDI ≤ `pdi_cap`, and becomes
**exhausted** at the run budget.

### The loss
```
loss = ((size − target_size) / tolerance)²  +  weight_pdi · (PDI / pdi_cap)
```
with `weight_pdi` default 1.0. A profile that could not be sized gets the
sentinel `_FAIL_LOSS = 1e3`; a sized profile with no PDI contributes
`weight_pdi · 1.0` for the PDI term.

An optional monotone `loss_transform: "log1p"` can be applied before the GP sees
the loss (the raw loss spans ~0.3 to >200 and one sentinel dominates a stationary
GP). The default is `"none"` — switching it mid-campaign would invalidate the
history.

### Confidence is a WEIGHT, not a gate
A low-confidence profile is **not dropped**. `fit_surrogate` sets the GP
observation noise per point to

```
noise_i = (0.05 · var(y) + 1e-6) / max(confidence_i, 0.05)
```

so low confidence → high noise → the point is trusted less but still informs the
surrogate. (`confidence_min` is used only for the convergence test, not for
admission.)

### Self-healing and restart
- A proposal whose measurement never arrives times out after
  `SWAXS_PENDING_TIMEOUT_S` (default 3600 s) and is recorded as a FAILED
  measurement (`tell(params, None, None, 0.0)`), then the next condition is
  proposed — so the loop cannot idle until morning.
- Campaign state (config, status, history, pending set, handled files) is
  snapshotted to a run-state file, NOT the manifest, and a `running` campaign is
  rebuilt on startup by replaying its history through `tell()`. The replay
  deliberately skips the proposal step so it does not re-emit condition files or
  notifications; the pending set is restored verbatim. Restoring `handled` is what
  stops a restart re-analysing every existing profile (which would append
  duplicate manifest entries and duplicate notifications).

### Campaign endpoints
- `GET  /api/campaign` — status plus `pending` recipe ids and the resolved
  conditions folder
- `POST /api/campaign/start` — start a campaign and emit the first condition
- `POST /api/campaign/abort` — operator abort
- `GET/POST /api/campaign/folder` — the conditions folder the proposals are
  written to (default `1D/SAXS/Conditions`)
- `GET  /api/campaign/diagnostics` — is the loop still learning, and is it still
  roaming? Read-only: it uses `peek()`, never `ask()`, so opening the panel
  mid-run cannot change which recipe the reactor is told to make next.
- `GET  /api/campaign/plot/<view>.png` — server-rendered figure. Views:
  **`slice`** (takes `x`, `y`, `anchor`), **`convergence`**, **`trajectory`**.
  An unknown view returns a placeholder image rather than an error.

## Manifest and event bus
Each fitted profile is written to `manifest.json` via `add_analysis_entry` with
`analysis_type = "nanoparticle"`,
`params = {model: "polydisperse_sphere", distribution: <chosen>}`,
`results` = the summary row, and `quality_score` = the confidence. Entries land
under the top-level `analyses` key, one uuid per fit.

An `analysis.complete` bus event is published with `recipe_id`, `file`, `size`,
`pdi`, `confidence`, `distribution`, `phase`, `guinier_rg`, `suspect`,
`plot_png` and `loss`. The reactor's notifier consumes it to report the fit
against the recipe that produced it.

## Recipe correlation — tying a profile back to its conditions
`src/optimizer/io.py`:
- `recipe_id_from_filename(filename, tags=("sample","bkg"))` — the reactor names
  every acquisition `{recipe_id}_{sample|bkg}`, so the id is everything before the
  role tag. Returns `""` when the filename does not follow the convention. Used
  for notifications and provenance, where there is no pending list to match on.
- `match_recipe_id(filename, pending_ids)` — returns the pending recipe_id
  carried in this filename, or None. Longest id first, so a longer id cannot be
  shadowed by a shorter one that is its prefix.

This join is what makes "which conditions gave the smallest particles?"
answerable: the fitted size comes from the analyzer, the conditions from the
recipe file, and the filename carries the `recipe_id` that links them.

## Other endpoints
- `GET  /api/health` — `{"status": "ok", "app": "analyzer", "analyzed": <count>}`
- `GET  /api/project`, `POST /api/set_project` — project root (pushed by the hub);
  setting it clears the handled set so the new project is rescanned
- `GET/POST /api/folder` — watched folder and `gate` mode (`auto`/`good`/`off`)
- `GET  /api/results` — the summary row of every retained result
- `GET  /api/result/<name>` — summary + full result + downsampled plot data
  (q, I, model, sigma)
- `GET  /api/stream` — incremental SSE. The first frame carries a bounded
  snapshot (`_SNAPSHOT = 200`, newest first); after that each frame is normally
  empty or a single row.

Retained results are capped at `SWAXS_ANALYZER_MAX_RESULTS` (default 600) — an
overnight campaign produces thousands and the full record lives in the manifest,
so keeping every fit in RAM only slows the app down.
