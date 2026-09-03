# Analysis App — Knowledge Base

## Purpose
The Analysis app (port 5106) provides structural analysis for
background-subtracted 1D scattering curves. It covers the classical SAXS
analyses (Guinier, Kratky, dimensionless Kratky, power-law "Porod", p(r),
classical invariants), WAXS peak fitting, `sasmodels`/SasView model fitting, and
optional ATSAS command-line integration. Every analysis can be run on one file or
batched over a folder, and each result is saved to disk and registered in
`manifest.json`.

Input files are usually `1D/<DET>/Subtracted/` (or `Subtracted/Good/` — the
Quality Gate's accepted set). Addressable stages are `Reduction`, `Averaged`,
`Subtracted`, `Good` (= `Subtracted/Good`), `NeedsReview`
(= `Subtracted/NeedsReview`) and `Analysed`.

## Guinier Analysis

### Theory
At very low q the scattering intensity follows:

```
I(q) ≈ I(0) · exp(−q²Rg²/3)
```

Linearised (Guinier plot): **ln I(q) vs q²** gives a straight line with:
- Slope = −Rg²/3  →  Rg = sqrt(−3 · slope)
- Intercept = ln I(0)  →  I(0) = exp(intercept)

### What `guinier_fit` returns
`src/analysis/core.py::guinier_fit(q, I, sigma, q_min=None, q_max=None,
auto_range=True)` returns:

```
{ Rg, I0, slope, intercept, R2, q_range: [q_lo, q_hi], qRg_max, plot }
```
or `{"error": "..."}`. There is **no `chi2`** key and no `qRg_lo`/`n_points` key.
`R2` is capitalised.

Error returns: fewer than 5 points in the range →
"Insufficient data points in Guinier range"; a non-negative slope →
"Positive slope in Guinier plot — not a valid Guinier region".

### Auto-range Selection (`auto_range=True`)
One refinement pass, not four:
1. Fit `ln I` vs `q²` over the supplied `[q_min, q_max]` (either may be None).
2. From that Rg, compute `q_max_ok = 1.3 / Rg` and re-fit with
   `q ≤ q_max_ok` — but only if at least 5 points survive and the refit slope is
   still negative.

There is no q_min refinement, no `q ≥ 0.005 nm⁻¹` floor, and no iteration beyond
this single pass. `1.3` is hardcoded in the refinement regardless of particle
shape.

### Validity Criterion (`guinier_quality`)
`guinier_quality(result, shape="globular")` is the QC gate. The upper qRg bound is
**shape-dependent**:

| `shape` | upper qRg limit |
|---|---|
| `globular` (default) | 1.3 |
| `rod` | 1.0 |
| `disc` | 1.7 |

Warnings raised:
- `q_max·Rg` above the shape's limit → "exceeds ~<limit> for a <shape> particle"
- `q_min·Rg > 0.65` → "q_min·Rg is high — extend to lower q if possible"
  (the gate is **0.65**, not 0.3)
- `R2 < 0.99` → "fit may be poor"

Verdict is `PASS` when there are no warnings, otherwise `WARN` (or `FAIL` if the
fit itself errored). There is **no χ² > 2 rule** anywhere in this app — the fit
does not compute a χ².

Textbook context: qRg below ≈0.3 risks beamstop artefacts and beam-divergence
effects; above the shape limit, higher-order terms make Rg underestimated. The
code enforces the numbers in the table above, not a flat [0.3, 1.3].

### Rg Interpretation
Rg is reported in the same length unit as 1/q — nm when reduction wrote q in
nm⁻¹ (the platform default).

| Rg (nm) | Approximate particle |
|---------|-------------------------------|
| 1–2     | small globular protein (~20 kDa) |
| 2–4     | medium protein (50–150 kDa)   |
| 4–8     | large protein / small complex  |
| > 10    | large complex, nanoparticle    |

### Common Errors
- **Upturn in Guinier plot**: aggregation or repulsion (check low q)
- **Curvature downward**: polydispersity, multiple species
- **R² < 0.99**: poor fit — adjust the range or discard the frame

## Kratky Analysis

### Standard Kratky
`kratky_plot` returns display data only — `{q, Iq2}` with `Iq2 = I·q²`. No fit,
no derived numbers.

Plot: **q²·I(q) vs q**
- Folded globular protein: bell-shaped peak, returns to zero
- Partially unfolded: broad, elevated at high q
- Fully unstructured (IDP): monotonically increasing plateau

### Dimensionless Kratky
`dimensionless_kratky(q, I, Rg, I0)` — requires Rg and I0 from a Guinier fit.
Plot: **(qRg)²·I(q)/I(0) vs qRg**. Returns
`{qRg, y, peak_qRg, peak_y, ideal_peak_qRg, ideal_peak_y}`.

The universal reference point for an ideal compact globule is
**(√3, 3/e) = (1.732, 1.104)** — the code reports `ideal_peak_qRg = 1.732` and
`ideal_peak_y = 1.104`.

- Peak above the reference: flatter than an ideal globule (possible unfolding)
- Peak at the reference with a sharp descent: compact, folded
- No peak: highly flexible / disordered

## Porod / Power-law Analysis — what this app actually computes

`porod_fit(q, I, sigma, q_min, q_max)` is a **LOG-LOG POWER-LAW FIT**, not a q⁴I
plateau. It fits

```
ln I = a + n·ln q
```

by linear regression and returns `{n, K, R2, interpretation, q_range, plot}`, with
`K = exp(a)`. The plot data is `ln I vs ln q` (`lnq_data`, `lnI_data`,
`lnq_fit`, `lnI_fit`). Nothing plateaus, and there is no `K_p` in this result.

`interpretation` is assigned from the exponent:

| exponent n | `interpretation` | meaning |
|---|---|---|
| within 0.3 of −4 | `smooth interface` | sharp-surface Porod scattering |
| within 0.5 of −2 | `polymer / Gaussian chain` | Gaussian chain in solution |
| within 0.4 of −1 | `rigid rod` | rod-like scatterer |
| otherwise | `other` | mass/surface fractal, mixed, or bad range |

Fewer than 4 points in the range → "Insufficient data points for power-law fit".

### Background theory: the q⁴I plateau (a DIFFERENT construction)
Separately from what `porod_fit` computes, the classical Porod law says that at
high q a particle with a sharp surface gives `I(q) → K_p / q⁴`, so a plot of
**q⁴·I(q) vs q⁴** should plateau at the Porod constant K_p. Deviations:
oscillating plateau ⇒ monodisperse particles; continuously rising ⇒ diffuse
interface; never plateauing ⇒ need higher q (WAXS).

That q⁴I plateau is what the **Assistant's** plotting helper
(`src/ai/plots.py`, the `porod` plot type) draws, and it is what
`classical_invariants` uses internally to get `porod_constant`. This app's
`/api/porod` route does the log-log exponent fit instead. When someone says
"Porod" on this platform, check which of the two they mean.

## Classical Invariants (`/api/classical`, `/api/classical/batch`)

`classical_invariants(q, I, Rg, I0)` needs Rg and I0 from a Guinier fit and ≥10
positive points. It integrates over the measured range and extrapolates both
ends — low q with the Guinier form `I0·exp(−Rg²q²/3)` from 0 to q_min, high q
with a Porod tail `I ≈ Kp/q⁴` above q_max (giving analytic tails
`Kp/q_max` and `Kp/(2q_max²)`). `Kp` is the median of `q⁴I` over the top 15% of q.

Returns:
| Key | Meaning |
|---|---|
| `Q` | Porod invariant ∫q²I dq |
| `porod_volume` | Vp = 2π²·I0/Q [nm³] |
| `mw_porod_kda` | Vp / 1.66 (approximate protein rule) |
| `Vc` | volume of correlation I0/∫qI dq [nm²] |
| `Qr` | Vc²/Rg (Rambo–Tainer) |
| `mw_vc_kda` | protein MW from Qr (Rambo–Tainer; constants applied in Å, converted internally) |
| `porod_constant` | Kp = ⟨q⁴I⟩ at high q |
| `surface_area` | specific surface S/V = π·Kp/Q [nm⁻¹] |
| `porod_tail_reached` | bool — false means the Porod region was not reached and the estimates are unreliable |

All of these are scale-independent ratios, so they are safe without absolute
calibration, but they assume a globular two-phase particle.

The classical route accepts these `analysis` values: `guinier`, `kratky`,
`porod`, `pair_distance`, `dimensionless_kratky`, `invariant`.

## Pair Distance Distribution p(r) — this app has its own IFT

### Theory
p(r) is the probability of finding two scattering centres a distance r apart, and
is related to I(q) by

```
I(q) = 4π ∫₀^Dmax p(r) · sin(qr)/(qr) dr
```

### What `pair_distance_ift` does
`pair_distance_ift(q, I, sigma=None, dmax=None, n_r=120, alpha=None)` solves that
equation directly with a **Tikhonov-regularized indirect Fourier transform**
implemented in numpy — no external software required:

- Design matrix `A[i,j] = 4π · sin(q_i r_j)/(q_i r_j) · dr` on `r ∈ [0, Dmax]`
  with `n_r` points (default 120).
- Rows inverse-variance weighted by `1/σ` (σ = 1 if not supplied).
- Smoothness penalty from a second-difference operator L, weight `alpha`; when
  `alpha` is None it is set automatically to `1e-2·tr(AᵗA)/tr(LᵗL)`.
- Boundary conditions `p(0) = p(Dmax) = 0` imposed by a strong penalty term.
- Solved by `np.linalg.solve` (falling back to `lstsq`), then clipped to `p ≥ 0`.

Derived quantities: `I0 = 4π∫p dr`, `Rg² = ∫p·r² dr / (2∫p dr)`, and
`chi2 = Σ((I − I_fit)/σ)² / (N−1)`.

Returns `{r, pr, Rg, I0, Dmax, chi2, q_fit, I_fit}`, or an error if there are
fewer than 10 positive-q points, or if the solution comes out non-positive
("try a different Dmax").

### Dmax
`Dmax` does NOT have to be supplied. If `dmax` is None or ≤ 0 it is estimated as
**π / q_min**. Supplying it explicitly is better when you know the particle, since
Dmax shapes the whole solution.

Shape reading:
- Globular: bell-shaped, symmetric
- Elongated: asymmetric, long tail to high r
- Hollow: two-peak distribution
- Rule of thumb for spheres: Rg ≈ 0.77 × (Dmax/2)

ATSAS `datgnom` is available as an alternative through the ATSAS routes below,
but it is optional — the built-in IFT is the default path.

## WAXS / Bragg Peak Fitting (`/api/peak`, `/api/waxs_peaks`, `/api/waxs_peaks/batch`)

`peak_fit(q, I, sigma, q_min, q_max, n_peaks=None, shape="gaussian")` fits peaks
on a **linear background** (`bg_a`, `bg_b`).

- `shape`: `gaussian` | `lorentzian` | `voigt` (pseudo-Voigt, mixing parameter η).
  All components are parameterised by FWHM `f` and height `A` at centre `q0`.
- `n_peaks`: if None, peaks are auto-detected (numpy local maxima above a crude
  linear baseline, prominence ≥ 4% of the max, at most 6). If given, the
  strongest `n_peaks` maxima are used as seeds.
- Fewer than 6 points in the range → error.

Per peak it returns `q0`, `fwhm`, `area`, `height`, `d_nm` and `d_A`
(d-spacing from the peak position), plus η for pseudo-Voigt. Globally: `shape`,
`n_peaks`, `bg_a`, `bg_b`, `chi2`, and plot data with per-peak components.

`/api/waxs_peaks` adds a QC block: reduced χ² > 5 raises
"check peak count/shape", and no fitted peaks raises "No peaks fitted".
`/api/waxs_peaks/batch` fits every file matching a keyword in
`1D/<DET>/<stage>/` and writes a combined summary table to
`1D/<DET>/Analysed/Peaks/`. This app reports d-spacings; superlattice/phase
indexing from a peak set is done by the Analyzer app.

## Model Fitting with sasmodels / SasView

Routes: `/api/model`, `/api/sasview`, `/api/sasview/batch`,
`/api/sasview/compare`, `/api/sasmodels/list`, `/api/sasmodels/params`.

`sasmodels_fit(q, I, sigma, model_name, params, free=None, bounds=None,
q_unit="nm^-1")`:
- `model_name` is any `sasmodels` model name. Product models are supported with
  the `form@structure` syntax, e.g. `sphere@hardsphere`. Polydispersity is set
  through the usual `*_pd`, `*_pd_n`, `*_pd_type` parameters.
- `params` is `{name: value}` starting values; `free` names the parameters to
  optimise (everything else is held fixed). `bounds` is `{param: [low, high]}`;
  when bounds are given a bounded optimiser (L-BFGS-B) is used and any parameter
  pinned at a bound is reported.
- **Unit handling matters**: sasmodels is native in Å⁻¹ with lengths in Å and SLD
  in 1e-6 Å⁻². Incoming q is converted from `q_unit` (default `nm^-1`) to Å⁻¹
  before fitting, so **fitted lengths come out in Å**, and the returned fit-curve
  q is converted back so it overlays the input data. `length_unit` is in the
  result.
- Returns `{model, params, chi2, plot, converged, at_bounds, length_unit}`, or
  `{"error": ...}` if `sasmodels` is not installed
  (`pip install sasmodels`) or the model name cannot be loaded.

`converged` and `at_bounds` are persisted — a railed parameter is distinguishable
from a good fit.

`/api/sasmodels/list` returns the available model names;
`/api/sasmodels/params?model=<name>` returns each parameter's name, default,
units and limits (used to build the UI).
`/api/sasview/compare` fits several candidate models to one curve and ranks them
by reduced χ².

## ATSAS Integration (`/api/atsas`, `/api/atsas/batch`, `/api/atsas/available`)

Optional. `src/analysis/atsas.py` shells out to ATSAS binaries if they are on
PATH; `GET /api/atsas/available` reports which are present (`{tools: {...}, any:
bool}`) so the UI can disable the rest. Each tool returns
`{"error": "<tool> not found in PATH..."}` when missing.

| `tool` | Binary | Output |
|---|---|---|
| `autorg` | `autorg` | Rg, I(0), qRg range, quality |
| `datgnom` / `gnom` | `datgnom` | GNOM p(r), Dmax, real-space Rg/I0, plus a `.out` file |
| `datporod` | `datgnom` → `datporod` | Porod volume (runs datgnom first for the `.out`) |
| `datvc` | `datvc` | volume of correlation + MW |
| `datmw` | `datmw` | molecular weight (`method` defaults to `vc`) |
| `dammif` | `datgnom` → `dammif` | ab-initio bead model (slow; `mode` defaults to `fast`) |

Default tool is `autorg`. Results are saved under
`1D/<DET>/Analysed/ATSAS/`; `dammif` gets its own subfolder
`<stem>_dammif/`.

## Output Files — the `Analysed/` tree

Every saved analysis goes through `src/analysis/io.py::save_analysis`, which
writes into a folder that is a SIBLING of `Subtracted/`/`Averaged/`:

```
1D/<DET>/Analysed/<Type>/<source stem>_<type>.json      params + results + created_at + user
                        /<source stem>_<type>_fit.dat   the fit curve (when there is one)
                        /<source stem>_<type>.png       the figure (when one is supplied)
```

`<Type>` folder names: `Guinier`, `Kratky`, `Porod`, `PairDistance`,
`Invariant`, `Model` (both `model` and `sasview`), `Peaks`, `ATSAS`.

The `_fit.dat` file has the header `# q_nm-1  I` and two columns.

`save_analysis` also:
- Appends the scalar results back into the SOURCE `.dat` footer under a
  `# ANALYSIS INFORMATION` block as `# <type>.<key>: <value>` lines. Re-running
  the same analysis type replaces its previous block, so this is idempotent.
- Registers the analysis in `manifest.json`.

Only finite scalars, strings and booleans are kept in the persisted results;
arrays and plot data are dropped. Booleans are deliberately kept so `converged`
and `at_bounds` survive.

Batch routes additionally write a combined summary table via
`write_batch_summary` — a CSV, plus an XLSX when `openpyxl` is available.

## Manifest Registration
Analysis results are saved in `manifest.json` under the top-level `analyses`
section, keyed by a generated uuid (one entry per analysis run):
```json
"analyses": {
  "f4c2…uuid": {
    "id": "f4c2…uuid",
    "type": "guinier",
    "file_path": "/path/to/subtracted.dat",
    "params": { "q_min": 0.012, "q_max": 0.045 },
    "results": { "Rg": 3.14, "I0": 0.0142, "R2": 0.998 },
    "fit_range": [0.012, 0.045],
    "quality_score": 0.92,
    "ai_assessment": "Rg = 3.14 nm, qRg range valid …",
    "provenance": { "app": "analysis", "run_id": "…" },
    "created_at": "2026-01-15T12:00:00Z"
  }
}
```
`fit_range` is taken from the result's `q_range`.

## Other routes
- `GET  /api/health` — `{"status": "ok", "app": "analysis"}`
- `POST /api/set_project`, `GET /api/project` — project root (pushed by the hub)
- `GET  /api/browse` — directory browser
- `POST /api/load_dat` — load one file and return (q, I, sigma) for display
- `GET  /api/list_subtracted` — list `.dat` files from an explicit `dir` or from
  `1D/<detector>/<stage>/`, with an optional keyword filter
- `POST /api/save` — persist a previously-run result to `Analysed/`. Fitting and
  saving are separate steps: the UI calls this only once the user accepts the fit.

Each analysis route also emits an `analysis.complete` bus event with
`analysis_type`, `file_path` and `results`.

## src/ Imports
- `src.analysis.core` — `guinier_fit`, `guinier_quality`, `porod_fit`,
  `kratky_plot`, `dimensionless_kratky`, `pair_distance_ift`,
  `classical_invariants`, `peak_fit`, `sasmodels_fit`, `sasmodels_params`
- `src.analysis.io` — `save_analysis`, `analysed_dir_for_source`,
  `annotate_source_dat`, `write_batch_summary`
- `src.analysis.atsas` — `available`, `run_autorg`, `run_datgnom`,
  `run_datporod`, `run_datvc`, `run_datmw`, `run_dammif`
- `src.manifest` — register analysis results
- `src.utils.read_dat_metadata.read_dat_data_metadata` — load `.dat` files
