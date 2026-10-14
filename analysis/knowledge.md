# Data Analysis App: Knowledge Base

## What it is for
The Data Analysis app (port 5106) does structural analysis of 1D scattering
curves, usually background-subtracted ones. It covers:

- **Classical SAXS**: Guinier (Rg, I0), Kratky, dimensionless Kratky, power-law
  ("Porod") exponent, pair distance distribution p(r) with Dmax, and the
  invariant-based quantities (Q, Vp, Vc, MW, surface area).
- **SASView**: model fitting with the `sasmodels` library (any catalog model,
  optional structure factor and polydispersity).
- **ATSAS**: the ATSAS command-line tools (autorg, datgnom, datporod, datvc,
  datmw, dammif), if they are installed.
- **WAXS peak fitting**: automatic peak detection and Gaussian, Lorentzian or
  pseudo-Voigt fits, with d-spacings.

Each analysis can be run on one file (review, then save) or as a batch over all
files whose name contains a keyword. Saved results go to `Analysed/` and are
registered in the manifest.

This app is for interactive analysis. The closed-loop nanoparticle size fit that
feeds the optimiser is done by the Autonomous Analyser (port 5107), not here.

## Where it sits in the pipeline
- **Reads from:** `1D/<DET>/Subtracted/` by default. Peak fitting can also read
  `1D/WAXS/Averaged/`. You can type or browse to any other folder in the
  **Folder** box (for example `Subtracted/Good`, the Quality Gate's accepted set).
- **Writes to:** `1D/<DET>/Analysed/<Type>/` (see Output files), a
  `# ANALYSIS INFORMATION` block appended to the source `.dat`, and the
  manifest's `analyses` section.
- **Emits:** `analysis.complete` on the event bus after each single-file
  Classical, SASView or peak fit (`analysis_type`, `file_path`, `results`).
  Batch runs and ATSAS runs do not emit it.

Points with q ≤ 0, I ≤ 0 or σ ≤ 0 are dropped when a file is loaded, so the
negative points a subtraction can produce never enter a fit.

## How to use it

### Layout
- **Left nav** picks the analysis category. SAXS group: **Classical**,
  **SASView**, **ATSAS**. WAXS group: **Peak fitting**.
- **Top tabs:** **Setup** (choose the file and settings, run), **Result** (plot,
  results table, QC badge, save), **Batch** (run over many files).
- The **Detector** badge follows the category: SAXS for Classical, SASView and
  ATSAS; WAXS for Peak fitting. It is not a separate control.

### Single file
1. Set the project folder in the Hub. The **Folder** box then shows the project's
   Subtracted folder for the current detector. Leave it blank for that default,
   or type or **Browse…** to another folder. Clearing the box restores the default.
2. Pick a category in the left nav. Optionally type a **Filter** keyword to
   narrow the file list; the ↻ button refreshes it.
3. Click a file in the list to select it.
4. Fill in card **2** for the category and click **Run fit** (Classical,
   SASView), **Run** (ATSAS) or **Fit peaks** (Peak fitting).
5. The app switches to **Result**: plot, results table and a PASS / WARN badge
   with any QC warnings.
6. If the fit is good, click **Save to Analysed**. Nothing is written until you
   do; fitting and saving are separate steps.

### Batch
1. Choose the category in the left nav and set its card on **Setup** (model and
   parameters for SASView, tool for ATSAS, shape and peak mode for Peak fitting).
2. Open **Batch**, enter a **Keyword** (for example `12CE`) and click
   **Run batch**. For Classical, choose the analysis in the Batch tab's **Type**
   menu; for the other categories the Setup card's settings are used.
3. Every matching file is fitted independently with the same settings and
   **saved automatically**. A summary table is shown and written as CSV (plus
   XLSX when `openpyxl` is installed) to `1D/<DET>/Analysed/<Type>/`.

Batch notes:
- Batch always reads the project's `1D/<DET>/Subtracted/` folder (Peak fitting:
  the selected **Source**, Subtracted or Averaged). It ignores the Folder box,
  so a batch over `Subtracted/Good` is not possible from the UI.
- DAMMIF cannot be run in batch (too slow).
- Classical batch uses auto q-range unless q limits are sent through the API.

## Classical analyses

Card **2 · Classical analysis**: **Type** menu (Guinier, Kratky, Dimensionless
Kratky, Porod (exponent), p(r) & Dmax, Invariant), **Auto q-range** checkbox
(on by default; untick to type **q_min** / **q_max**), and **Dmax** (nm) for
p(r). q is in the unit of the data, nm⁻¹ by default.

Dimensionless Kratky and Invariant need Rg and I0. If they are not supplied the
app runs an automatic Guinier fit first; if that fails the analysis reports
"Need Rg/I0 (Guinier failed: ...)".

### Guinier
At low q, `I(q) ≈ I0 · exp(−q²Rg²/3)`. A straight-line fit of **ln I vs q²**
gives slope = −Rg²/3 (Rg = sqrt(−3·slope)) and intercept = ln I0.

- The fit is weighted by 1/σ(ln I)², with σ(ln I) ≈ σ(I)/I, so precise low-q
  points count more than noisy ones. It falls back to an unweighted fit if σ is
  unusable.
- **Auto q-range** (one refinement pass): fit over the given range, then refit
  with q ≤ 1.3/Rg if at least 5 points remain and the slope stays negative. There
  is no q_min refinement. The 1.3 is used for every shape.
- Returns `Rg`, `I0`, `slope`, `intercept`, `R2`, `q_range`, `qRg_max`. There is
  no χ² for a Guinier fit.
- Errors: fewer than 5 points ("Insufficient data points in Guinier range"), or
  a non-negative slope ("Positive slope in Guinier plot").

**QC** (`guinier_quality`, shape `globular` by default):

| shape | upper q·Rg limit |
|---|---|
| globular | 1.3 |
| rod | 1.0 |
| disc | 1.7 |

Warnings: q_max·Rg above the limit; q_min·Rg > 0.65 ("extend to lower q");
R² < 0.99. No warnings gives PASS, otherwise WARN; a failed fit is FAIL.

Rg is in the inverse of the q unit (nm for nm⁻¹ data). Rough guide: 1 to 2 nm
small protein (~20 kDa), 2 to 4 nm medium protein, 4 to 8 nm large protein or
complex, above 10 nm large complex or nanoparticle.

Reading the Guinier plot: an upturn at the lowest q means aggregation;
downward curvature means interparticle repulsion or polydispersity.

### Kratky
Plot of **q²·I(q) vs q**. Display only, no fitted numbers. A folded globular
particle gives a bell-shaped peak that returns toward zero; a partly unfolded
one stays elevated at high q; a disordered chain rises to a plateau.

### Dimensionless Kratky
Plot of **(q·Rg)²·I(q)/I0 vs q·Rg**. Returns the observed peak (`peak_qRg`,
`peak_y`) and the ideal compact-globule reference **(√3, 3/e) = (1.732, 1.104)**,
drawn as a red cross. A peak near the reference means compact and folded; a peak
shifted up and to the right means elongated or partly unfolded; no peak means
flexible or disordered.

### Porod (power-law exponent)
This is a **log-log power-law fit**, `ln I = a + n·ln q`, weighted like Guinier.
It returns `n`, `K = exp(a)`, `R2`, `q_range` and an `interpretation`:

| exponent n | interpretation |
|---|---|
| within 0.3 of −4 | smooth interface (Porod) |
| within 0.5 of −2 | polymer / Gaussian chain |
| within 0.4 of −1 | rigid rod |
| otherwise | other (fractal, mixed, or a poor range) |

Choose the high-q range by unticking Auto q-range; the default fits the whole
curve. Fewer than 4 points is an error.

This is not the q⁴·I plateau construction (Porod constant Kp). That plateau is
what the Invariant analysis uses internally and what the assistant's `porod`
plot draws. When someone says "Porod", check which one they mean.

### p(r) and Dmax (built-in IFT)
`I(q) = 4π ∫₀^Dmax p(r) · sin(qr)/(qr) dr`, solved by a Tikhonov-regularised
indirect Fourier transform in numpy (no ATSAS needed):

- 120 r points on [0, Dmax], rows weighted by 1/σ, second-difference smoothness
  penalty (weight set automatically from the data), p(0) = p(Dmax) = 0 enforced,
  p ≥ 0 after the solve.
- Derived: `I0 = 4π∫p dr`, `Rg² = ∫p·r² dr / (2∫p dr)`, reduced
  `chi2`. Returns `r`, `pr`, `Rg`, `I0`, `Dmax`, `chi2`.
- **Dmax given** (the Dmax box, in nm): used exactly.
- **Dmax blank**: the app scans Dmax (40 values from max(2π/q_max, (π/q_min)/60)
  up to π/q_min, then 25 finer values around the best) and keeps the smallest
  Dmax whose χ² is within max(1.03·χ²min, χ²min + 1) and whose solution is not
  dominated by negative lobes. This is "the most compact p(r) that still fits".
- Errors: fewer than 10 positive-q points; a non-positive solution ("try a
  different Dmax").

Reading p(r): bell-shaped and symmetric means globular; a long tail to high r
means elongated; two peaks can mean a hollow or two-domain particle. For a solid
sphere, Rg ≈ 0.77 × (Dmax/2). Compare the p(r) Rg with the Guinier Rg; a large
mismatch points at a bad Dmax or a bad Guinier range.

### Invariant (Q, Vp, MW, Vc)
Needs Rg and I0 and at least 10 positive points. The measured range is
integrated and both ends are extrapolated: Guinier form `I0·exp(−Rg²q²/3)` from
0 to q_min, and a Porod tail `I ≈ Kp/q⁴` above q_max (tails Kp/q_max for Q and
Kp/(2·q_max²) for ∫qI). Kp is the median of q⁴·I over the top 15% of q.

| key | meaning |
|---|---|
| `Q` | Porod invariant ∫q²I dq |
| `porod_volume` | Vp = 2π²·I0/Q (nm³) |
| `mw_porod_kda` | Vp/1.66 (protein rule of thumb) |
| `Vc` | volume of correlation I0/∫qI dq (nm²) |
| `Qr` | Vc²/Rg (Rambo and Tainer) |
| `mw_vc_kda` | protein MW from Qr (Rambo and Tainer; constants applied in Å) |
| `porod_constant` | Kp |
| `surface_area` | specific surface S/V = π·Kp/Q (nm⁻¹) |
| `porod_tail_reached` | false means the Porod region was not reached; treat the numbers as unreliable |

These are ratios, so absolute calibration is not needed, but they assume a
globular two-phase particle and the MW values are protein estimates only. The
Guinier fit itself does not give MW.

## SASView (sasmodels) model fitting

Card **2 · SASView model fit**:
- **Model**: "Common" models first (sphere, core_shell_sphere, ellipsoid,
  cylinder, core_shell_cylinder, flexible_cylinder, lamellar,
  lamellar_stack_paracrystal, broad_peak, correlation_length, power_law,
  guinier_porod, mass_fractal), then the full catalog.
- **S(q)**: None, hardsphere, hayter_msa, stickyhardsphere, squarewell. The
  model becomes `form@structure`, for example `sphere@hardsphere`.
- **q_min / q_max** (optional) and **q unit** (nm⁻¹, the platform default, or Å⁻¹).
- Parameter table: tick **Fit** to free a parameter (scale, background, radius,
  length, thickness, sld and rg are ticked by default), set the start **Value**
  and optional **Lower** / **Upper** bounds. Polydispersity parameters such as
  `radius_pd`, `radius_pd_n`, `radius_pd_type` are in the same table.

How it fits:
- Minimises χ² = Σ((I − I_model)/σ)². With any bounds set it uses L-BFGS-B and
  reports parameters pinned at a bound (`at_bounds`); without bounds it uses
  Nelder-Mead.
- **Units:** sasmodels works in Å⁻¹, with lengths in Å and SLD in 1e-6 Å⁻².
  nm⁻¹ data is converted (q_Å = q_nm/10) before fitting, so **fitted lengths
  are in Å** (a radius of 50 is 5 nm). The fit curve is converted back to the
  data's q unit for plotting.
- Returns `model`, `params`, reduced `chi2`, `converged`, `at_bounds`,
  `length_unit`, `sld_unit`, `q_unit`. `converged` and `at_bounds` are saved,
  so a railed parameter can be told apart from a good fit.
- The Result plot shows data, fit and a residual panel.

QC warnings: optimiser did not converge; parameter(s) pinned at a bound;
reduced χ² > 5.

Model comparison (`POST /api/sasview/compare`, API only) fits several models to
one curve and ranks them by reduced χ².

If `sasmodels` is not installed (it is in `requirements-core.txt`), the Model
menu says so and the rest of the app still works.

## ATSAS

Card **2 · ATSAS**. On opening, the app checks which binaries are on `PATH`
and marks missing tools "(missing)". If none are found it says "ATSAS not found
on PATH. Install or activate it, then restart this app."

| Tool (menu) | Binary | Output | Extra input |
|---|---|---|---|
| AUTORG | `autorg` | Rg, I0, q·Rg range, quality | |
| GNOM p(r) & Dmax | `datgnom` | p(r), Dmax, real-space Rg and I0, `.out` file | Rg hint (nm) |
| DATPOROD | `datgnom` then `datporod` | Porod volume (plus Dmax, real-space Rg) | Rg hint |
| DATVC | `datvc` | volume of correlation, MW | |
| DATMW | `datmw` | MW | Method: vc (default), vp, bayes, shape |
| DAMMIF | `datgnom` then `dammif` | ab-initio bead model(s) | Rg hint |

- Timeouts: 60 s per tool, 90 s for datgnom, 900 s for dammif (`fast` mode).
- DAMMIF takes minutes, writes its models to
  `1D/<DET>/Analysed/ATSAS/<stem>_dammif/`, and is single-file only. Its result
  is not saved through **Save to Analysed**.
- The raw tool output is shown under the results table when parsing found
  nothing.

The built-in p(r) in Classical is the default; ATSAS is optional.

## WAXS peak fitting

Card **2 · WAXS peak fitting**:
- **Shape**: Gaussian (default), Lorentzian, Voigt (pseudo-Voigt,
  η·Lorentzian + (1−η)·Gaussian, 0 ≤ η ≤ 1).
- **Peaks**: Auto-detect, or Fixed N (1 to 12, default 2).
- **Source**: Subtracted (default) or Averaged.
- **q_min / q_max** (optional).

How it fits:
- All peaks sit on a linear background `bg_a + bg_b·q` and are parameterised by
  height, centre q0 and FWHM. The fit is σ-weighted.
- Auto-detect finds local maxima above a straight line drawn between the end
  points, at least 4% of the largest one, at most 6 peaks. Fixed N seeds the N
  strongest maxima.
- Per peak: `q0` (and `q0_err`), `fwhm`, `height`, `area`, `d_nm` and `d_A`
  (d = 2π/q0), plus `eta` for Voigt. Overall: `shape`, `n_peaks`, `bg_a`, `bg_b`,
  reduced `chi2`. The plot shows each peak as a dotted component.
- Errors: fewer than 6 points; "No peaks detected, adjust the q-range".

QC warnings: reduced χ² > 5 ("check peak count/shape"); no peaks fitted; a peak
as wide as the fit range ("may be background").

This app reports d-spacings only. Phase or superlattice indexing from a set of
peaks is done in the Autonomous Analyser.

## Output files: the `Analysed/` tree

`Analysed/` is a sibling of `Subtracted/` and `Averaged/` under the detector
folder:

```
1D/<DET>/Analysed/<Type>/<source stem>_<type>.json      params, results, source, detector, created_at, user
                        /<source stem>_<type>_fit.dat   fit curve (when there is one)
                        /<type>_batch_<timestamp>.csv   batch summary (+ .xlsx with openpyxl)
```

- `<Type>` folders: `Guinier`, `Kratky`, `Porod`, `PairDistance`, `Invariant`,
  `Model` (SASView), `Peaks`, `ATSAS`. Dimensionless Kratky is saved under its
  own type name.
- If the source is not inside a `Subtracted/`, `Averaged/` or `Reduction/`
  folder, `Analysed/<Type>/` is created next to the source file.
- `_fit.dat` has the header `# q_nm-1  I` and two columns (for p(r) results the
  columns are r and p(r)).
- The analysis app does not write PNG figures; the plots are interactive in the
  browser (Plotly, bundled for offline use).
- Only scalars, strings and booleans go into the saved results; arrays and plot
  data are dropped. Short lists such as `at_bounds` are kept as text.
- Saving with the same file and analysis type again overwrites the JSON and fit.

**Source `.dat` annotation:** the scalar results are appended to the fitted
file's footer under `# ANALYSIS INFORMATION`, one `# <type>.<key>: <value>`
line each. Re-running the same type replaces its previous lines.

## Manifest record
Saved analyses go into the manifest's `analyses` section (only when a project is
set). One record per (analysis type, file): re-saving the same type for the same
file updates the record instead of adding a new one. The section is capped at
the newest 3000 records (`SWAXS_MANIFEST_ANALYSES_CAP`, 0 disables the cap).

```json
"analyses": {
  "<uuid>": {
    "id": "<uuid>",
    "type": "guinier",
    "file_path": "/abs/path/to/sample.dat",
    "params": {"q_min": null, "q_max": null, "auto_range": true},
    "results": {"Rg": 3.14, "I0": 0.0142, "R2": 0.998},
    "fit_range": [0.012, 0.041],
    "quality_score": null,
    "ai_assessment": "",
    "provenance": {"app": "analysis", "...": "..."},
    "created_at": "...",
    "updated_at": "..."
  }
}
```
`fit_range` is the result's `q_range`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| File list says "No Subtracted SAXS files (set the project in the Hub)" | No project set, or the folder is empty. Set the project in the Hub, or type a folder in **Folder**. |
| Peak fitting shows no files | It reads `1D/WAXS/<Source>/`. Switch **Source** to Averaged if there is no WAXS subtraction. |
| "Select a file first." | Click a file in the list before running. |
| Guinier "Positive slope" or WARN on q·Rg | Range is outside the Guinier region. Untick Auto q-range and choose a lower q_max, or check for aggregation (upturn at low q). |
| Guinier R² < 0.99 | Noisy or curved data. Narrow the range or check the subtraction. |
| p(r) Dmax looks too large or too small | Type a Dmax you trust; the auto scan picks the most compact solution that fits. |
| Invariant `porod_tail_reached` false or odd volumes | The curve does not reach the q⁻⁴ region. Use WAXS-extended data or treat the numbers as rough. |
| SASView radius looks 10 times too large | Lengths are in Å. 50 Å is 5 nm. |
| SASView WARN "pinned at a bound" | Widen the bounds or improve the start value. |
| "sasmodels not installed" | `pip install -r requirements-core.txt` in the platform's environment, then restart the app. |
| ATSAS tools marked missing | Put the ATSAS `bin` folder on `PATH` in the shell that starts the Hub, then restart this app. |
| DAMMIF timed out | 900 s limit in `fast` mode. Run heavier modes outside the platform. |
| Batch "No subtracted ... files match" | Batch reads only the project Subtracted folder (or the peak Source); check the keyword. |

## API routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | `{"status": "ok", "app": "analysis"}` |
| POST | `/api/set_project` | set project root (pushed by the Hub), body `{"path": ...}` |
| GET | `/api/project` | current project root |
| GET | `/api/browse` | folder listing for Browse… |
| GET | `/api/list_subtracted` | list `.dat` files: `dir=` or `detector=` + `stage=` (Reduction, Averaged, Subtracted, Good, NeedsReview, Analysed), optional `keyword=` |
| POST | `/api/load_dat` | load one file, returns q, I, σ (downsampled to 600 points) |
| POST | `/api/classical` | one classical analysis: `path`, `analysis` (guinier, kratky, porod, pair_distance, dimensionless_kratky, invariant), `auto_range`, `q_min`, `q_max`, `dmax`, `Rg`, `I0`, `shape`, `save` |
| POST | `/api/classical/batch` | same over Subtracted files matching `keyword` (always saves) |
| POST | `/api/sasview` | one model fit: `path`, `model_name`, `structure_factor`, `params`, `free`, `bounds`, `q_min`, `q_max`, `q_unit`, `save` |
| POST | `/api/sasview/batch` | same model over Subtracted files matching `keyword` |
| POST | `/api/sasview/compare` | rank `models` (list) on one file by reduced χ² |
| GET | `/api/sasmodels/list` | available model names |
| GET | `/api/sasmodels/params?model=` | parameter names, defaults, units, limits |
| GET | `/api/atsas/available` | `{tools: {...}, any: bool}` |
| POST | `/api/atsas` | one ATSAS run: `path`, `tool`, `rg`, `method`, `mode`, `save` |
| POST | `/api/atsas/batch` | ATSAS tool over Subtracted files matching `keyword` (not dammif) |
| POST | `/api/waxs_peaks` | peak fit: `path`, `shape`, `n_peaks`, `q_min`, `q_max`, `save` |
| POST | `/api/waxs_peaks/batch` | peak fit over `stage` files matching `keyword` |
| POST | `/api/save` | save a reviewed result: `path`, `analysis_type`, `params`, `results`, `fit_curve` |

Older single-purpose routes kept for scripts: `/api/guinier`, `/api/porod`,
`/api/kratky`, `/api/peak`, `/api/model`. They write no `Analysed/` files
(all but `/api/kratky` register the result in the manifest directly); the UI
does not use them.

Logic lives in `src/analysis/core.py` (fits), `src/analysis/io.py` (saving,
annotation, manifest, batch tables) and `src/analysis/atsas.py` (ATSAS
wrappers); `analysis/app.py` is routing only.
