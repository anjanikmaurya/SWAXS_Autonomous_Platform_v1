# Visualisation & Average App: Knowledge Base

## 1. What it is for

The Visualisation & Average app (port 5103, folder and app id `average`) lets you:

- view raw 2D detector images (`.raw`), optionally on q axes;
- plot reduced 1D curves, grouped by sample keyword, and their per-scan metadata;
- average the 1D frames of each sample into one `.dat` per sample;
- run **Auto-Average**, a folder monitor that averages rolling batches of N frames
  for unattended and autonomous runs.

Older logs and `.swaxs_state` files may still call this app `viewer`.

## 2. Where it sits in the pipeline

Reduction (5102) → **Visualisation & Average (5103)** → Background (5104).

| Reads | Writes |
|---|---|
| `<project>/1D/SAXS/Reduction/*.dat`, `<project>/1D/WAXS/Reduction/*.dat` | `<project>/1D/<DET>/Averaged/*.dat` (sibling `Averaged/` of the input folder by default) |
| `<project>/2D/SAXS/*.raw`, `<project>/2D/WAXS/*.raw` (2D Viewer) | `<raw folder>/Saved_2D/*.png` + `.svg` (2D image saves) |

Every averaged file gets a `files` entry with `stage: "averaged"` in the manifest
and a `file.averaged` bus event (`file_path`, `keyword`, `n_files`, `detector`).
Folder fields auto-fill from the hub project and every one has a **Browse…** button.

## 3. How to use it

The left bar has three modes: **2D Viewer**, **1D Viewer**, **Auto-Average**.

### 2D Viewer (tabs: Setup, SAXS Image, WAXS Image)
1. **Setup**: set the SAXS/WAXS raw folders, then **Load SAXS files**,
   **Load WAXS files** or **Load both**. Check **Detector size (pixels)**
   (defaults SAXS 1043 × 981, WAXS 195 × 487; files are read as int32).
2. Optional **q-space calibration**: give a `.poni` per detector to show the image
   on qx/qy axes (nm⁻¹); blank = pixel axes.
3. **Display options**: Colormap (Viridis default), Intensity scale (Log₁₀ default
   or Linear), Contrast clip % (default 2%, clips that % of darkest and brightest
   pixels).
4. On the image tabs: filter filenames, step with **Prev** / **Next**, then
   **Save: Current image** or **Average all N filtered** (pixel-by-pixel mean of
   the filtered frames). Both write PNG + SVG to `<raw folder>/Saved_2D/`.

### 1D Viewer (tabs: Setup, SAXS 1D, WAXS 1D, Metadata, Averaged)
1. **Setup → Reduced data folders**: SAXS Reduction folder, WAXS Reduction folder
   (leave one blank to skip that detector), Output folder (blank = sibling
   `Averaged/` of each input folder).
2. **Keywords (sample IDs)**: type one and **+ Add**, or **Paste list…**
   (one per line). A keyword is any text in the filename. Reactor loop files are
   `<recipe_id>_sample` / `<recipe_id>_bkg`; add a `…_sample` keyword to average
   only samples. **Discover sample IDs** helps: **Trim start** / **Trim end**
   drop tokens (default trim end 1, separator `_`), **Contains** filters, then
   **Scan folders** and add the prefixes you want.
3. **Display options**: q min / q max and unit (nm⁻¹ or Å⁻¹) for the plot,
   Axis scale (Log-log, Semilog, Linear), Max scans per keyword (50),
   Display points (300 per curve), Error bands.
4. Optional **I0 frame filter** and **Truncate saved q-range** (section 4).
5. **Actions**: **Scan files** (counts matching files), **Load & plot**, then
   **∑ Average & save**. The app switches to the **Averaged** tab.
6. **SAXS 1D / WAXS 1D**: individual scans per keyword. Clicking a curve offers
   **Hide this curve**; **Restore all** brings hidden curves back. Hiding is
   display only: hidden curves are still included in **Average & save**. To
   exclude frames from an average, use the I0 filter or a narrower keyword.
7. **Metadata**: one card per numeric metadata field vs scan index (or a time
   field, chosen in **X-axis**).
8. **Averaged**: the saved averages. **Combine SAXS + WAXS** is a display toggle
   only: checked draws SAXS and WAXS on one shared axis, unchecked draws them
   side by side. Nothing is fitted, rescaled or merged, and no file is written.

### Auto-Average (the monitor)
1. Set the SAXS/WAXS **Reduction folders** and optionally an **Output folder**
   (blank = each detector's `Averaged/`; prefer blank, because one shared folder
   gets SAXS and WAXS batches with the same filename).
2. **Averaging options**: **Frames per average (N)** (default 30), **Check
   interval (seconds)** (default 3), **Keywords** (comma-separated, blank = all
   samples), **I0 filter %** (0 = off).
3. Optional **Truncate saved q-range**.
4. **Start auto-averaging** / **Stop**. The log below streams progress, e.g.
   `kw [saxs]: 12/30 frames, waiting for 18 more`.

Each poll groups frames by keyword and averages every full block of N
not-yet-used frames (ordered by scan index); each frame is used in exactly one
batch. Reactor loop files are grouped by `{recipe_id}_{role}`
(`src/loop_naming.py`), so sample and background of a condition are averaged
separately and need no keywords. **Set N to at most the frames per acquisition**
(`spec.frames` in the reactor config): a group never larger than N never
completes. The app warns at start if N is larger.

Settings and progress persist in `.swaxs_state/`; after a restart the last
settings are shown (with a banner) and, if the resume policy allows, the
monitor resumes without re-averaging frames already used.

## 4. Settings, defaults and the maths

### Grouping (filename only)
The keyword comes from the filename, not the footer (`keyword_and_index` in
`src/plot_reduction.py`): the trailing `_NNNN` scan index (optional `_DET`
suffix) is removed, then any `_<N>files_Average` / `_Average` / `_Avg` label.
`BSA_10mg_0007_SAXS.dat` → keyword `BSA_10mg`, scan_idx 7. With explicit keywords,
each file joins the LONGEST keyword it contains, so a partial keyword collects
all its files into one group.

### Averaging
- Frames are interpolated onto a common log-spaced grid (`np.geomspace` over the
  q overlap of all frames, 1000 points), in log space for both q and I:
  `I_i(q) = exp(interp(ln q, ln q_i, ln I_i))`.
- Points with q ≤ 0, I ≤ 0 or non-finite I/σ are dropped per frame; a frame with
  fewer than 3 valid points is skipped. A one-frame group is written as is.
- `I_avg = mean(I_i)`, `σ_avg = sqrt(Σ σ_i²) / n`, with n = frames that actually
  contributed. Equal weight per frame (no I0 or inverse-variance weighting, see
  **D4** in `docs/audits/OPEN_DEFECTS.md`). σ propagates the per-point errors; it
  is not the frame-to-frame scatter.

### I0 frame filter
`i0_filter_pct` (UI default 20% when enabled, 0 = off): frames whose `i0`
deviates from the group (or batch) median by more than that percent are dropped
before averaging, and from the plots. Frames without a usable `i0` are kept.

### Truncate saved q-range
Cuts the saved average to `[q-min, q-max]` (nm⁻¹, either bound optional). If
fewer than 2 points would remain, the cut is ignored with a warning.

### Output files
- Manual: `<keyword>_<N>files_Average.dat`, N = frames that contributed.
- Monitor: `<keyword>_batch<NNN>_<N>files_Average.dat`, N = batch size (the header
  `# Files averaged:` gives the frames actually used).
- Written atomically (`.part`, then rename), so watchers never read half a file.
- Layout: header lines `# Averaged SAXS/WAXS data, keyword: …`,
  `# Files averaged: N`, `# Columns: q_nm-1  I  sigma`, then data, then
  `# METADATA INFORMATION` with each numeric key the MEDIAN over the contributing
  frames (strings: first value). So `i0` in an average is the batch median.
- Input reduction files: pyFAI header, q (configured unit, nm⁻¹ default), I, σ,
  and a `# METADATA INFORMATION (YML FORMAT)` footer with raw beamline metadata.
  Both formats are read by `src/utils/read_dat_metadata.py`.

### Uncertainty caveats
σ is log-interpolated onto the grid (an approximation), and interpolation
correlates neighbouring q points, so downstream fit errors are probably slightly
too small. Details: `docs/ERROR_PROPAGATION.md` §3.

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Frames do not overlay | Radiation damage (low-q drift), beam glitch, sample settling. Enable the I0 frame filter or use a narrower keyword. |
| "No overlapping q range across files" | A SAXS file in a WAXS group, or frames reduced with different `radial_range`. The group is skipped. |
| Auto-Average log stays at "x/N frames, waiting" | N is larger than the frames one acquisition produces. Lower N. |
| `batch N dropped, …` in the log | The batch had no usable frames. Reasons: `I ≤ 0 at every q` (blank/dark input or bad normalization), `sigma is NaN/inf` (check `error_model`, mask), `the file has no data rows`, `no overlapping q range`. The batch is consumed and not retried, so that condition never gets an average (or a subtracted profile). An `average.skipped` bus event lets Autonomous Watch report the reason. |
| One bad frame in a batch | It is excluded, not averaged as zero: 9 good + 1 dark gives the mean of 9 (`Files averaged: 9`). |
| Noisy average at high q (σ/I > 0.3) | Use **Truncate saved q-range** with a lower q-max. |
| Missing points at high q | Points with I ≤ 0 are dropped before averaging. Normal near the noise floor. |
| Hidden curve still in the average | Hiding is display only (see 3.6). |

## 6. API routes

| Route | Method | Purpose |
|---|---|---|
| `/api/health` | GET | Health check |
| `/api/set_project`, `/api/project` | POST / GET | Project root from the hub |
| `/api/browse` | GET | Folder browser |
| `/api/discover-keywords` | POST | Unique filename prefixes with counts |
| `/api/scan` | POST | Count files per keyword |
| `/api/load` | POST | Load curves for plotting (I0 filter applied) |
| `/api/average` | POST | Average and save per keyword |
| `/api/monitor/start`, `/api/monitor/stop` | POST | Auto-Average monitor |
| `/api/monitor/status` | GET | Thread state, batches, waiting groups (`gate`) |
| `/api/monitor/stream` | GET | Monitor log (SSE) |
| `/api/restart_notice` | GET | Settings-restored banner |
| `/api/list-raw`, `/api/raw-image` | GET | List and render `.raw` images |
| `/api/save-image`, `/api/save-average` | POST | Save current / pixel-averaged 2D image |
