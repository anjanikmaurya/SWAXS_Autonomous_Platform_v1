# Viewer App — Knowledge Base

## Purpose
The Viewer app (port 5002) displays, selects, and averages 1D scattering curves
produced by the Reduction app. It groups frames by keyword, lets users pick which
frames to include, and writes averaged curves as `.dat` files. It also previews
raw 2D detector images and runs an auto-averaging monitor for unattended runs.

## Workflow

### 1. Load Reduced Files
- Reads `.dat` files from `<project>/1D/SAXS/Reduction/` and
  `<project>/1D/WAXS/Reduction/` (any folder can be browsed).
- Groups files by a keyword derived from the **FILENAME**, not from any metadata
  field. `read_folder` (`src/plot_reduction.py`) takes the part of the name before
  the trailing `_NNNN` scan index (with an optional `_DET` suffix), then strips any
  `_<N>files` / `_Average` / `_Avg` label. Examples:
  - `BSA_10mg_0007_SAXS.dat` → keyword `BSA_10mg`, scan_idx 7
  - `BSA_10mg_12files_Average.dat` → keyword `BSA_10mg`, scan_idx 0
  There is no `keyword` field in the `.dat` footer; grouping is purely filename-based.
- When explicit keywords are supplied to `average_and_save`, files are grouped by
  the LONGEST supplied keyword that appears in the filename, so a partial keyword
  collects all its files into one group instead of one group per file.

### 2. Display Curves
- Plots all curves for a keyword on a shared log-log axis (axis mode selectable).
- Colour-coded per frame, legend shows the filename.
- Sigma (error) bars rendered as a shaded band, toggleable.

### 3. Frame Selection
Users select which frames to average by clicking individual frames to toggle
include/exclude. There are no `a` / `n` (select all / select none) keyboard
shortcuts — the only keydown handlers in the viewer UI are Enter in the
keyword-entry box and Escape to close a popup.

### 4. Averaging
Frames in a group are interpolated onto a common **log-spaced** q grid
(`np.geomspace` over the overlap of all files, `n_pts` points, default 1000) and
the interpolation itself is done in LOG space in both q and I:

```
I_i(q_grid) = exp( interp( ln q_grid, ln q_i, ln I_i ) )
```

Points with q ≤ 0 or I ≤ 0 are dropped per file; a file with fewer than 3 valid
points is skipped entirely. If two files have no overlapping q range at all, the
group is skipped with a warning.

The average and its uncertainty are:

```
I_avg(q) = mean(I_i(q))                       over the n files that CONTRIBUTED
σ_avg(q) = sqrt( Σ σ_i(q)² ) / n
```

σ is the **propagation of the per-point errors** already present in each file, not
the sample standard deviation of the frames. It does not measure frame-to-frame
scatter, so it will not grow if the frames disagree. `n` counts only the files
that actually contributed, so skipped frames never inflate the denominator (which
would bias I low and σ small).

There is no I₀-**weighted** averaging — every contributing frame gets equal
weight.

### 4b. I₀ Outlier Rejection
What does exist is I₀-based frame REJECTION, controlled by `i0_filter_pct`
(0 = off). Before averaging, the `i0` value is read from each frame's metadata,
the group median is taken, and any frame deviating from that median by more than
`i0_filter_pct` percent is discarded. Frames with no usable `i0` value are kept.

In the UI this is the "🔬 I0 Frame Filter" card: a "Filter frames by I0
stability" checkbox plus a percent threshold (default 20). It applies to both
plotting and averaging.

### 5. Stitch SAXS+WAXS checkbox
The "Stitch SAXS+WAXS" checkbox on the Averaged tab is a **DISPLAY toggle only**:
checked draws SAXS and WAXS averaged curves on one shared Plotly axis, unchecked
draws them in two side-by-side panels. There is no overlap detection, no scale
factor is computed, nothing is rescaled, and no merged file is written.

### 6. Output
Averaged files go to `output_dir`, defaulting to a sibling `Averaged/` folder next
to the input folder (so `1D/SAXS/Reduction/` → `1D/SAXS/Averaged/`). The name is:

```
<keyword>_<N>files_<label_suffix>.dat
```

with `label_suffix` defaulting to `Average` and `N` the number of frames that
actually contributed. Examples:
- `<project>/1D/SAXS/Averaged/BSA_10mg_12files_Average.dat`
- `<project>/1D/WAXS/Averaged/BSA_10mg_12files_Average.dat`

The auto-averaging monitor uses a batch name instead:
`<keyword>_batch<NNN>_<N>files_<label_suffix>.dat`.

Writes are atomic — the file is written as `<name>.dat.part` and renamed — so a
downstream watcher never consumes a truncated average.

### 7. Auto-averaging monitor
`/api/monitor/start` runs a background thread that polls the Reduction folders and
averages rolling batches of `frames_per_average` consecutive frames per group via
`average_batch`. It emits a `file.averaged` bus event
(`file_path`, `keyword`, `n_files`, `detector`) and writes a
`stage: "averaged"` entry to `manifest.json` for each batch. Progress and
"waiting for N more frames" messages stream over `/api/monitor/stream` (SSE).
`/api/monitor/status` reports the state of the THREAD, not just a flag, so a dead
worker cannot read as healthy.

## Common Issues

### Frames Don't Overlay Well
Causes:
- Radiation damage — later frames drift upward at low q
- Beam glitch — single-frame I₀ spike
- Sample settling — first 1–2 frames differ before steady state

Action: exclude outlier frames, or turn on the I₀ Frame Filter to reject frames
whose I₀ deviates from the group median.

### "No overlapping q range across files"
Raised by `_common_q_grid` when the largest q_min is at or above the smallest
q_max — usually a SAXS file mixed into a WAXS group, or a frame integrated with a
different `radial_range`. The group is skipped rather than averaged.

### Poor Statistics at High q
Symptom: averaged curve becomes noisy (σ/I > 0.3) before the detector q_max.
Action: truncate at a lower q_max before passing to the analysis app. The
"✂ Truncate saved q-range" card applies `[q_min, q_max]` (nm⁻¹) to the saved file;
if the range would keep fewer than 2 points it is ignored with a warning.

### Negative Averaged Intensities
Happens if individual frames have negative I (noise floor). Normal at high q for
well-background-subtracted data. Background subtraction is applied AFTER
averaging, not before.

## Data Format Reference

### Input (reduction output)
Reduction `.dat` files carry pyFAI's own multi-line `#` header, three numeric
columns (q in the configured unit — nm⁻¹ by default, I, σ), and a
`# METADATA INFORMATION (YML FORMAT)` footer holding the raw beamline metadata.
There is no `keyword`, `detector` or `transmission` line in that footer;
computed corrections live in `manifest.json`, not in the file.

### Output (averaged files)
Averaged files have a LEADING header, then the data, then a metadata footer:
```
# Averaged SAXS/WAXS data — keyword: BSA_10mg
# Files averaged: 12
# Columns: q_nm-1  I  sigma
1.23400000e-02  1.23000000e-02  5.60000000e-04
...
# METADATA INFORMATION
# i0: 123456.7
# ...
```
The footer metadata is **aggregated across the contributing frames**: each
numeric key is the MEDIAN of that key over those frames; non-numeric (string)
values carry the first value seen. So a single `i0` line in an averaged file is
the median I₀ of the batch, not any one frame's reading.

`read_dat_data_metadata` in `src/utils/read_dat_metadata.py` parses both the
numeric columns and the metadata block of either format.

## src/ Imports
- `src.plot_reduction.read_folder(folder, keywords=None) -> list[dict]` — a FLAT
  list of frame dicts, each `{filename, keyword, scan_idx, q, I, sigma, metadata}`.
  It is not a `{keyword: [...]}` mapping; group it yourself if you need groups.
- `src.plot_reduction.average_and_save(folder, keywords, *, n_pts=1000,
  label_suffix="Average", output_dir=None, i0_filter_pct=0.0, q_min=None,
  q_max=None) -> list[(keyword, Path)]` — groups a whole FOLDER by keyword and
  writes one averaged `.dat` per keyword.
- `src.plot_reduction.average_batch(frames, keyword, out_path, *,
  i0_filter_pct=0.0, n_pts=1000, q_min=None, q_max=None) -> Path | None` — a
  DIFFERENT function that averages exactly the explicit list of frame dicts passed
  in and writes one file. This is what the auto-averaging monitor uses.
- `src.utils.read_dat_metadata.read_dat_data_metadata(path)` — parse a single `.dat`.
