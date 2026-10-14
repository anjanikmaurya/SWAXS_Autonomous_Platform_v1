# Background Subtraction App: Knowledge Base

## 1. What it is for

The Background app (port 5104) subtracts a scaled background (buffer, solvent,
empty cell) from averaged sample curves:

```
I_sub(q) = I_sample(q) − c · I_background(q)
σ_sub(q) = sqrt( σ_sample² + c² · σ_background² )
```

It can be run by hand (three matching modes) or as a folder monitor
(**Auto-Subtract**) for unattended and autonomous runs.

## 2. Where it sits in the pipeline

Visualisation & Average (5103) → **Background (5104)** → Quality Gate (5105).

| Reads | Writes |
|---|---|
| `<project>/1D/<DET>/Averaged/*.dat` (samples AND backgrounds both live here) | `<project>/1D/<DET>/Subtracted/<sample stem>_sub.dat` |

- Manual modes: output goes to the **Output folder** if set, otherwise to a
  `Subtracted/` subfolder of the sample folder (Individual: of the first selected
  sample's folder).
- Auto-Subtract: output goes to a `Subtracted/` folder that is a SIBLING of the
  watched `Averaged/` folder, i.e. `1D/<DET>/Subtracted/`.
- The Quality Gate then copies each file into `Subtracted/Good/` or
  `Subtracted/NeedsReview/`.
- Each file gets a `background` manifest entry, a `files` entry with
  `stage: "subtracted"`, and a `file.subtracted` bus event
  (`file_path`, `keyword`, `scale`, `mode`).

## 3. How to use it

The left bar picks the mode: **Individual**, **Scan**, **Suggested**,
**Auto-Subtract**. Tabs: **Setup**, **BG-Sub 1D SAXS**, **BG-Sub 1D WAXS**,
**Averaged metadata**. Folder fields auto-fill from the hub project and each has
a **Browse…** button.

### Manual modes (Individual, Scan, Suggested)
1. **Detector**: SAXS or WAXS. Tags the output and picks the result tab.
2. **Folders**: Sample folder, Background folder (Scan and Suggested only),
   Output folder (blank = sample folder/Subtracted).
3. Pick the pairs:
   - **Individual**: **List files**, mark **one** file as **BG** and check the
     samples (**Select all samples**, **Clear**). **Background name filter** /
     **Sample name filter** (comma-separated, not case-sensitive) limit which
     files may take each role; **Reset filters** clears them.
   - **Scan** (Match by number & review): give **Sample keyword** (e.g. `Nylon6`),
     **Background keyword** (e.g. `Empty`) and **Match number after** (e.g.
     `ctr`). Files are paired when the number after that token is equal
     (`…ctr0…` ↔ `…ctr0…`), then **Match by number**.
   - **Suggested** (Match & review): optional **Match by filename token** hint
     (e.g. `buffer` or a shared run ID), then **Suggest backgrounds**. Each sample
     gets the background with the most shared filename tokens, boosted for
     background words (buffer, bkg, background, blank, empty, solvent, water, bg)
     and for the hint.
   - Scan and Suggested show an editable review table: change any row's
     background, then **Review** fills each row's scale and QC dot
     (green / amber / red, with % negative points).
4. **Subtraction method**: **Manual scale** (slider 0 to 2 or **Exact value**,
   default 1.0) or **Auto high-q** (optional **High-q window q-min / q-max**;
   default the top 25% of the sample's q-range).
5. **Output q-range & rebinning** (section 4).
6. **Preview & plot**, then **Subtract & save** (labelled **Subtract selected &
   save**, **Subtract matched & save** or **Apply suggested & save** per mode).

### Result tabs (BG-Sub 1D SAXS / WAXS)
- Views: **Overlay**, **Residual** (linear, with a zero line: dips below zero
  mean over-subtraction), **Ratio** (I_sample / (c·I_bkg), should flatten near
  1 at high q). Axis: Log-log, Semilog, Linear. Toggles **Raw + bkg**,
  **Subtracted**.
- The **Scale** slider re-previews the current pair instantly. It does not
  re-save; press **Subtract & save** again to write.
- **⇄ Compare methods** overlays manual vs auto high-q for the previewed pair.
- QC metrics and warnings are shown under the plot.

### Averaged metadata tab
**Refresh from sample folder** tabulates the metadata footer of the sample files.

### Auto-Subtract (the monitor)
1. **SAXS Averaged folder**, **WAXS Averaged folder** (blank = skip that detector).
2. **Sample keyword** (blank = every non-background file) and **Background
   keyword** (blank = detect by name: buffer, blank, empty, solvent, water, bkg,
   bg, background).
3. **Scale**: **Auto high-q** (default) or **Fixed value**.
4. **Schedule**: **Check interval (seconds)**, default 3.
5. **Output q-range & rebinning** (same setting as the manual modes).
6. **Start auto-subtraction** / **Stop**. The log streams each result:
   `out ← bkg · scale 0.983 · QC PASS`.

How the monitor pairs: a background from the SAME recipe (`{recipe_id}` in the
filename, `src/loop_naming.py`) is preferred; if other recipes have backgrounds
but this one does not yet, the sample waits ("waiting for the blank of recipe
…"). Without recipe ids, the nearest sequence index is used (the `_batchNNN`
number from Auto-Average, else the trailing scan index). A file still being
written is not read until its size and mtime are stable; a rewritten average
(new size/mtime) is subtracted again. Files that fail QC are still written and
counted as flagged.

On start, files whose `_sub.dat` already exists, or that are older than the
crash-gap window, are treated as done, so a new run does not redo the
back-catalogue. Progress is saved in `.swaxs_state/` and a restart resumes.

## 4. Settings, defaults and the maths

### Output q-range & rebinning
On by default, shared by all modes (`truncate_rebin` in
`src/background/core.py`). Each subtracted curve is trimmed to `[q min, q max]`
and resampled onto a fixed number of points before it is written.

| Setting | Default |
|---|---|
| q min / q max | 0.3 to 6.0 nm⁻¹ |
| Number of points | 549 |
| Spacing | linear (or log) |
| q unit | nm⁻¹ (pick Å⁻¹ to write Å⁻¹; values are then in Å⁻¹) |

- Never extrapolates: a window wider than the measured range is clipped to it
  (logged), so no plateau is invented.
- Sign-preserving: negative subtracted points are kept, so the Quality Gate can
  see over-subtraction.
- The setting persists across restarts; a banner says if it was restored or lost.
- If rebinning fails, the untruncated curve is written in nm⁻¹ with a warning.

### Scale, manual modes: Auto high-q (`_auto_scale`)
Weighted least squares in the high-q window (default top 25%,
`frac = 0.25`), where only solvent/cell scattering remains:

```
c = Σ w·I_sample·I_bkg / Σ w·I_bkg²,    w = 1/σ_sample²
```

Then one robust refit after a 3σ MAD clip of the residuals
(`σ_MAD = 1.4826 · median|r − median(r)|`, applied if ≥ 3 points survive), so
WAXS Bragg peaks or outliers cannot bias c. c is clamped to **[0.1, 5]**; fewer
than 3 usable points gives c = 1.0.

### Scale, Auto-Subtract: driven to zero high-q residual (`_auto_adjust_scale`)
1. `s0` = the weighted-LS scale above.
2. `s_zero` = mean(I_sample) / mean(I_bkg) in the same window (zero mean
   high-q residual).
3. `c = s_zero` clamped to `[0.5·s0, 1.5·s0]`, then to `[0.1, 5]`.

The footer and manifest record `ls_scale`, `zero_scale` and whether it was
`clamped`. So the same pair can get a slightly different c by hand and by the
monitor. **Fixed value** uses the given scale as is.

### QC metrics (`_qc_metrics`, per subtraction)
| Metric | Meaning | Warning |
|---|---|---|
| % negative points | over-subtraction | warning > 5%, error > 15% |
| high-q residual ratio = mean\|I_sub\| / mean\|I_sample\| in the top 25% | ≈ 0 good; large = under-subtraction or buffer mismatch | warning > 0.5 |
| low-q slope of ln I vs ln q (lowest 10% of positive points) | aggregation | warning if steeper than −3 |

Auto-Subtract collapses these to a verdict: any error → FAIL, any warning →
WARN, else PASS. The Quality Gate grades the written file separately.

This app does not flag unusual scale values. The AI Assistant warns when a
recorded scale is outside **[0.5, 1.5]** (`src/ai/hints.py`): check for a wrong
background file, a concentration error, or drift between the measurements.

### Uncertainty
σ_sub is the textbook form (Sedlak et al. 2017, eq. 6). Not included: the
uncertainty of the fitted c itself, and the correlation introduced if sample and
background had to be interpolated onto a common q grid (only when they were
reduced with different settings). Details: `docs/ERROR_PROPAGATION.md` §4.

### File format
```
# SAXS/WAXS background-subtracted data
# Columns: q_nm-1  I  sigma            (q_A-1 if Å⁻¹ output)
# Truncated/rebinned: 0.3–6.0 nm⁻¹, 549 pts (linear)
# Sample     : <sample path>
# Background : <background path>
# Scale      : 0.98
# Method     : manual | auto_highq
# Detector   : saxs | waxs
# Mode       : individual
# q_nm-1  I  sigma
<data>
```
Auto-Subtract writes instead `# Scale : 0.9832  (auto, high-q→0; LS=0.9910,
zero=0.9832)`, `# Mode : auto (auto scale)` and
`# QC : PASS  (neg=1.2%, highq_ratio=0.031)`. All UI manual modes write
`Mode: individual`; the `keyword` and `scan_matched` API routes write their own
mode names.

### Manifest entry (`background.<abs output path>`)
Keys: `sample_path`, `bkg_path`, `scale`, `scale_method` (`auto` | `manual`),
`scale_confidence` (null), `mode` (`individual` | `auto` | `keyword` |
`scan_matched`), `provenance`, `created_at`. There are no `sample_file`,
`background_file`, `scale_factor` or `subtracted_at` keys.

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Many negative points, Kratky dip | c too large: lower it slightly (e.g. 0.95) and check the Residual view. |
| Flat non-zero baseline at high q | c too small, or the background differs in composition. |
| Systematic negatives | Background from another day or radiation-damaged. Use a background measured the same day. |
| Low-q upturn warning | Possible aggregation; consider SEC-SAXS or re-centrifugation. |
| Auto-Subtract does nothing | Read the log: "no background file in the watched folder yet", "waiting for the blank of recipe …", or the sample/background count line. Check the keywords. |
| Monitor finds no backgrounds | Background names lack buffer/blank/empty/…; set **Background keyword**. |
| Scan mode pairs nothing | **Match number after** is not the token before the number, or the keywords match no files. |
| Sizes 10× off downstream | Check the output **q unit**; the column header says `q_nm-1` or `q_A-1`. |

## 6. API routes

| Route | Method | Purpose |
|---|---|---|
| `/api/health` | GET | Health check |
| `/api/set_project`, `/api/project` | POST / GET | Project root from the hub |
| `/api/browse` | GET | Folder/file browser |
| `/api/scan` | POST | List `.dat` files in a folder |
| `/api/preview` | POST | Subtract one pair without saving (curves, ratio, QC) |
| `/api/auto_scale` | POST | High-q scale for one pair |
| `/api/pair_qc` | POST | Scale + QC for one pair (Review) |
| `/api/match_token` | POST | Scan-mode pairing by number after a token |
| `/api/suggest` | POST | Suggested-mode pairing |
| `/api/subtract/individual` | POST | Subtract and save (used by all UI manual modes) |
| `/api/subtract/keyword`, `/api/subtract/scan_matched` | POST | Older API-only variants |
| `/api/metadata` | POST | Metadata footers for the metadata tab |
| `/api/truncation` | GET / POST | Output q-range & rebinning settings |
| `/api/restart_notice` | GET | Restored/lost settings banner |
| `/api/monitor/start`, `/api/monitor/stop` | POST | Auto-Subtract monitor |
| `/api/monitor/status`, `/api/monitor/stream` | GET | Monitor state, log (SSE) |
