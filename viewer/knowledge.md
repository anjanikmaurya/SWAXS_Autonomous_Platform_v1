# Viewer App — Knowledge Base

## Purpose
The Viewer app (port 5002) displays, selects, and averages 1D scattering curves
produced by the Reduction app.  It groups frames by keyword, lets users pick
which frames to include, and saves averaged curves as `.dat` files.

## Workflow

### 1. Load Reduced Files
- Reads all `.dat` files from `<project>/1D/SAXS/Reduction/` and
  `<project>/1D/WAXS/Reduction/`
- Groups files by the `keyword` metadata field in the `.dat` footer

### 2. Display Curves
- Plots all curves for a keyword on a shared log-log axis
- Colour-coded per frame with legend showing filename
- Sigma (error) bars rendered as shaded band

### 3. Frame Selection
Users select which frames to average by:
- Clicking individual frames to toggle include/exclude
- Using keyboard shortcuts: `a` = select all, `n` = select none
- Automatic outlier suggestions based on I₀ stability check

### 4. Averaging
Two modes supported:

**Simple average** (default):
```
I_avg(q) = mean(I_i(q))
σ_avg(q) = std(I_i(q)) / sqrt(N)
```
on a common q grid via linear interpolation.

**I₀-weighted average** (future):
```
I_avg(q) = sum(w_i * I_i(q)) / sum(w_i)   where w_i = I0_i
```

### 5. SAXS+WAXS Stitching
When both detectors are loaded for the same keyword:
1. Find q overlap region between SAXS and WAXS curves
2. Scale WAXS to match SAXS in the overlap (multiplicative scale factor)
3. Merge: use SAXS for q < q_merge, WAXS for q > q_merge
4. q_merge is the midpoint of the overlap region by default
5. Save merged curve as single `.dat` file in `Averaged/`

### 6. Output
Averaged files are saved to:
- `<project>/1D/SAXS/Averaged/<keyword>_avg.dat`
- `<project>/1D/WAXS/Averaged/<keyword>_avg.dat`
- `<project>/1D/Averaged/<keyword>_stitched.dat` (if stitched)

## Common Issues

### Frames Don't Overlay Well
Causes:
- Radiation damage — later frames drift upward at low q
- Beam glitch — single-frame I₀ spike
- Sample settling — first 1–2 frames differ before steady state

Action: exclude outlier frames; use I₀ stability view to identify glitches.

### Stitching Scale Factor Far from 1
If WAXS/SAXS scale factor > 1.3 or < 0.7:
- Check both detectors have correct PONI calibration
- Verify transmission corrections are consistent
- Overlap region may contain beamstop shadow — check q_min of WAXS

### Poor Statistics at High q
Symptom: averaged curve becomes noisy (σ/I > 0.3) before q_max of detector.
Action: truncate at a lower q_max before passing to analysis app.

### Negative Averaged Intensities
Happens if individual frames have negative I (noise floor).  Normal for high q
of well-background-subtracted data.  Background subtraction should be applied
AFTER averaging, not before.

## Data Format Reference

The viewer reads `.dat` files with this structure:
```
# q(nm-1)   I(a.u.)   sigma(a.u.)
0.01234      1.23e-2   5.6e-4
...
# keyword: BSA_10mg
# detector: SAXS
# energy_keV: 12.0
# transmission: 0.8542
```

The `read_dat_data_metadata` function in `src/utils/read_dat_metadata.py`
parses both the numeric columns and the footer metadata.

## src/ Imports
- `src.plot_reduction.read_folder(folder_path)` — returns `{keyword: [FileData]}` dict
- `src.plot_reduction.average_and_save(frames, output_path)` — compute and write averaged .dat
- `src.utils.read_dat_metadata.read_dat_data_metadata(path)` — parse a single .dat
