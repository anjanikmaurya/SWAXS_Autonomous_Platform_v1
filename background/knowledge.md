# Background Subtraction App — Knowledge Base

## Purpose
The Background app (port 5003) subtracts a background or solvent scattering from
sample scattering curves.  Input is averaged/reduced `.dat` files.
Output is background-subtracted curves ready for analysis.

## UI Workflow (v2)
Four tabs (left nav): **Setup**, **BG-Sub 1D SAXS**, **BG-Sub 1D WAXS**,
**Average Metadata**. An active-detector toggle (SAXS/WAXS) tags output and
routes results to the matching tab; folder defaults auto-fill from the hub
project root.

Setup offers three matching modes:
- **Individual** — list the Averaged folder, mark **one file as BG (background)**
  and check the rest as **samples** (roles shown with badges/colors).
- **Scan** — pair sample and background files by the **number that follows a
  token** in each filename: the user gives a token for the sample name and one
  for the background name (e.g. both `ctr`), and files with the same number are
  paired (`…ctr0…` ↔ `…ctr0…`). Matches appear in an editable review table.
- **Auto-suggest** — each sample is matched to a background by filename/keyword
  token overlap (preferring buffer/blank/empty/solvent tokens); shown in the same
  editable review table.

Background data is averaged and lives in the **Averaged** folder, so the
background-folder default is the same `1D/<det>/Averaged` as the samples. Scan and
Auto-suggest share one review table (editable per-row background dropdown +
**Review** for per-row scale/QC) and the same grouped run path.

The result tabs overlay raw sample + background + subtracted curves, show QC
metrics/warnings, and have a dynamic scale slider that re-previews instantly.
They also offer an **Overlay (log)** vs **Residual (linear, with a zero line)**
view to spot over-subtraction (dips below zero), and a **Compare methods**
button that overlays manual vs auto high-q subtraction for the current pair.
In Auto-suggest, a **Review** action fills per-row **scale + QC status** (green/
amber/red) via `/api/pair_qc` so batch pairs can be validated before applying.

## Theory

The background-subtracted scattering intensity is:

```
I_sub(q) = I_sample(q) − c · I_background(q)
```

where `c` is a scale factor that accounts for differences in:
- Sample concentration (dilution effect — solvent contribution)
- Measurement conditions (exposure time differences already corrected)
- Matching volumes (for in-line flow cells)

Error propagation:
```
σ_sub(q) = sqrt(σ_sample² + c² · σ_background²)
```

## Scale Factor Determination

### Manual
User enters `c` directly (default 1.0 for matched background).

### Automatic — High-q Matching (`_auto_scale`, the manual/batch tabs)
Choose `c` by a weighted least-squares match in a high-q window (default the top
25% of the sample's q-range, `frac = 0.25`), where macromolecular signal is
negligible and only solvent/cell scattering remains:

```
c = Σ w·I_sample·I_bkg / Σ w·I_bkg²,    w = 1/σ_sample²
```

The fit is then repeated ONCE after a robust **3σ MAD sigma-clip** on the
residuals `r = I_sample − c·I_bkg` (`σ_MAD = 1.4826 · median|r − median(r)|`), so
sharp WAXS Bragg peaks or single outliers inside the window cannot bias the scale.
The clip is applied only if at least 3 points survive; the number of clipped
points is reported as `n_clipped`.

The result is clamped to **[0.1, 5]**. The window can be overridden with
q_min/q_max. Fewer than 3 usable points in the window → `c = 1.0`.
This automates the standard validity check (sample ≳ buffer at high q; correct
scaling makes high-q overlay). Reference: SSRL/EMBL/BioXTAS subtraction guidance.

### Automatic — the MONITOR uses a different estimator (`_auto_adjust_scale`)
The automated-subtraction monitor does **not** use the plain weighted LS scale. In
`scale_mode: "auto"` it:
1. computes `s0` = the high-q weighted-LS scale above (with its sigma-clip);
2. computes `s_zero = mean(I_sample) / mean(I_bkg)` over the same high-q window —
   the scale at which the MEAN high-q residual is exactly zero;
3. takes `c = s_zero` clamped to `[0.5·s0, 1.5·s0]`, then clamped again to the
   global sane range `[0.1, 5]`.

It reports `scale`, `ls_scale` (= s0), `zero_scale` (= s_zero) and a `clamped`
flag saying whether step 3 had to pull `s_zero` back. `scale_mode: "fixed"` uses
the supplied `fixed_scale` verbatim with `scale_method: "manual"`.

So the same profile can get two slightly different scale factors depending on
whether it was subtracted interactively or by the monitor; the footer and the
manifest record which estimator ran.

### Quality-control metrics (computed per subtraction)
- **% negative points** — over-subtraction indicator (negatives → sharp upturns
  in log). Warns above ~5%, error above ~15%.
- **high-q residual ratio** = mean|I_sub|/mean(I_sample) in the high-q window
  (≈0 good; large ⇒ under-subtraction / buffer mismatch).
- **low-q slope** of ln I vs ln q (steeper than ≈ −3 ⇒ possible aggregation).

## Common Issues

### Negative Intensities After Subtraction
- Scale factor `c` too large — reduce slightly
- Check background is from same measurement day and conditions
- If systematic, check for radiation damage in background (background was damaged first)

### Over-subtracted at Low q
Symptom: I_sub(q) has an upturn at low q pointing negative (Kratky shows dip).
Cause: c too large.  Try c = 0.95 and inspect.

### Under-subtracted at High q
Symptom: flat non-zero baseline at high q.
Cause: c too small, or background has different composition / additive concentration.

### Background from Different Day
Use a water/background standard measured the same day to cross-normalize backgrounds
from different sessions using their I₀ ratios.

## Output Format

### Output path (deterministic)
```
<sample folder>/Subtracted/<sample stem>_sub.dat
```
Every writer follows this rule. If `output_folder` is supplied in the request it
is used verbatim instead; otherwise the folder is a `Subtracted/` subfolder of the
sample folder (for the `individual` mode: of the first selected sample's parent
folder). The automated monitor defaults to a `Subtracted/` folder that is a
SIBLING of the watched `Averaged/` folder — i.e. `1D/<DET>/Subtracted/`, not
nested inside `Averaged/`.

The Quality Gate then sorts these files into `Subtracted/Good/` and
`Subtracted/NeedsReview/`.

### Footer fields — they differ by mode
There are FOUR modes: `keyword`, `scan_matched`, `individual`, `auto` (the
monitor).

`individual`:
```
# Sample     : <sample_path>
# Background : <background_path>
# Scale      : 1.0
# Method     : manual | auto_highq
# Detector   : saxs | waxs
# Mode       : individual
```

`scan_matched` — the same six lines with `# Mode : scan_matched`, plus:
```
# scan_idx   : <index>
```

`keyword` — a REDUCED set; no `Method`, no `Detector`, and no scale-method detail:
```
# Sample     : <sample_path>
# Background : <background_path>
# Scale      : 1.0
# Mode       : keyword
```

`auto` (monitor) — a DIFFERENT set, including a QC verdict line:
```
# Sample       : <sample_path>
# Background   : <background_path>
# Scale        : 0.9832  (auto, high-q→0; LS=0.9910, zero=0.9832)
# Mode         : auto (auto scale)
# QC           : PASS  (neg=1.2%, highq_ratio=0.031)
```
`QC` is `PASS` / `WARN` / `FAIL`, collapsed from the QC warning severities
(any `error` → FAIL, any `warning` → WARN, else PASS).

## Manifest Registration
After subtraction, `manifest.json` is updated under `background.<abs output path>`
by `src.manifest.add_background_entry`. The keys are exactly:
```json
{
  "sample_path":      "/path/BSA_10mg_12files_Average.dat",
  "bkg_path":         "/path/buffer_12files_Average.dat",
  "scale":            1.0,
  "scale_method":     "manual",
  "scale_confidence": null,
  "mode":             "individual",
  "provenance":       { },
  "created_at":       "2025-01-15T10:22:00+00:00"
}
```
- `scale_method` — `"auto"` | `"manual"` | `"concentration"`
- `mode` — `"keyword"` | `"scan_matched"` | `"individual"` | `"auto"`
- there is no `sample_file`, `background_file`, `scale_factor` or `subtracted_at`
  key; querying those returns nothing.

A `files.<abs output path>` entry with `stage: "subtracted"` is written at the same
time, and a `file.subtracted` bus event is emitted with
`file_path`, `keyword`, `scale`, `mode`.

## src/ Imports
- `src.manifest` — load/save manifest entries
- `src.utils.read_dat_metadata.read_dat_data_metadata` — load .dat files

## Scale Factor Quality Checks — done by the ASSISTANT, not this app
This app itself does not flag scale factors: it clamps `c` to **[0.1, 5]** and has
no notion of 0.5 or 1.5 as limits.

The `[0.5, 1.5]` suspicion band belongs to the **AI Assistant's** HintChecker
(`src/ai/hints.py`). When the assistant sees a recorded scale outside that band it
warns the user and suggests investigating:
1. Wrong background file selected
2. Concentration error during sample preparation
3. Instrument drift between sample and background measurements

So a scale of 1.8 will be applied by the Background app without complaint and
flagged separately by the assistant.
