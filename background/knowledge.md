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

### Automatic — High-q Matching (implemented)
Choose `c` by a weighted least-squares match in a high-q window (default the top
25% of the overlapping q-range), where macromolecular signal is negligible and
only solvent/cell scattering remains:

```
c = Σ w·I_sample·I_bkg / Σ w·I_bkg²,    w = 1/σ_sample²
```

The result is clamped to [0.1, 5]. The window can be overridden with q_min/q_max.
This automates the standard validity check (sample ≳ buffer at high q; correct
scaling makes high-q overlay). Reference: SSRL/EMBL/BioXTAS subtraction guidance.

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
Background-subtracted files are saved alongside averaged files or in a
`Subtracted/` subfolder (convention depends on project setup).

Footer records (actual fields written):
```
# Sample     : <sample_path>
# Background : <background_path>
# Scale      : 1.0
# Method     : manual | auto_highq
# Detector   : saxs | waxs
# Mode       : individual | scan_matched
```

## Manifest Registration
After subtraction, `manifest.json` is updated under `background.<filename>`:
```json
{
  "sample_file":      "/path/sample_avg.dat",
  "background_file":  "/path/background_avg.dat",
  "scale_factor": 1.0,
  "scale_method": "manual",
  "subtracted_at": "...",
  "provenance": { ... }
}
```

## src/ Imports
- `src.manifest` — load/save manifest entries
- `src.utils.read_dat_metadata.read_dat_data_metadata` — load .dat files

## Scale Factor Quality Checks
A scale factor outside [0.5, 1.5] is flagged as suspicious.
The AI assistant will warn the user and suggest investigating:
1. Wrong background file selected
2. Concentration error during sample preparation
3. Instrument drift between sample and background measurements
