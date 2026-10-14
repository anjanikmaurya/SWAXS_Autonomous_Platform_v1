# Reduction & Correction: knowledge

## What this app is for
Reduction & Correction (port 5102) turns raw 2D detector frames (`.raw`) into
calibrated, normalized 1D profiles (`.dat`: q, I, σ) with pyFAI azimuthal
integration. It handles SAXS, WAXS, or both detectors (SWAXS). It can reduce a
folder once (**Run now**) or watch it and reduce frames as they land
(**Watch for new files**), which is how it runs during an autonomous loop.

## Where it sits in the pipeline
- **Upstream:** Geometry Calibration (5101) provides the `.poni` files; the
  beamline (or the calibration app's SFTP copy) provides the raw data.
- **Reads:**
  - frames: every `*.raw` under `<data_directory>/**/SAXS/**` and `**/WAXS/**`
    (`._*` files and `.raw.pdi` are ignored);
  - metadata: a CSV in `2D/` (CSV mode) or the `.raw.pdi` beside each frame (PDI mode);
  - geometry: `poni_directory` + `poni_files`, optional masks, dark and flat frames.
- **Writes:** `<output_dir>/<DET>/Reduction/<stem>_<DET>.dat`, e.g.
  `1D/SAXS/Reduction/scan_0007_SAXS.dat`. `<output_dir>` is `output_directory`,
  or by default a `1D/` folder beside the `2D/` data folder. `<stem>` is the raw
  filename without the `.raw` suffix and without any configured
  `saxs_filename_prefix` / `waxs_filename_prefix`.
- **Manifest:** section `files` (shared with the average app).
- **Events:** `watch.new_raw` when the watcher sees a frame, `file.reduced` per
  `.dat`, `file.skipped` when a frame is given up on.
- **Downstream:** Visualisation & Average (5103) reads `1D/<DET>/Reduction/`.

## How to use it
The sidebar has two tabs: **Setup** and **Run & monitor**.

### Setup tab
When the hub sets a project, the folder fields fill in automatically
(`<project>/2D`, `<project>/poni`, `<project>/1D`, `<project>/config.yml`). The
cards, top to bottom:

1. **Directories**: **Data directory (2D)** (contains `SAXS/` and `WAXS/`),
   **Output directory (1D)** (blank = `1D/` beside `2D/`), **PONI directory**.
2. **Calibration & mask files**: filenames only, all inside the PONI directory:
   SAXS/WAXS PONI, mask (`.edf`, blank = no mask), dark frame (blank = none),
   flat-field (blank = none).
3. **Detector size (pixels)**: SAXS 1043 × 981, WAXS 195 × 487 by default.
4. **Sample & beam**: **Compound formula** (C2H4), **X-ray energy (keV)** (12),
   **Density (g/cm³)** (0.92), **Thickness (m)** (blank = from transmission).
5. **Processing options**: **Mode** (SWAXS / SAXS only / WAXS only), **Metadata
   format** (CSV / PDI), **Radial bins (npt)** (1000), **Error model** (Poisson /
   Azimuthal), **Normalise 1D by** (pills: None, Bstop, I₀, Absolute; pick one).
   Absolute shows the **Absolute calibration constant K** field.
6. **pyFAI integration**: **Output unit** (q nm⁻¹, q Å⁻¹, 2θ deg, 2θ rad),
   **Solid angle correction** (on), **Polarization factor p** (the form starts at
   0.99; **Clear (off)** skips it), **q range (min / max)**, **Azimuthal range χ
   (min / max)**, **Dummy value** ± tolerance.
7. **Advanced (keep defaults if unsure)**: I₀ and Bstop dark-current offsets, I₀
   and Bstop air measurements, SAXS/WAXS prefix to strip.
8. **Save or load config (YAML)**: **Config file path**, **Save as YAML**,
   **Load from YAML**. The form is not loaded from `config.yml` automatically:
   click **Load from YAML** to use the project's file.

The form is the config: every Run or Watch sends the current form values, so
edits apply without saving (save them to keep them).

### Run & monitor tab
- **Run now**: reduces, one at a time, every frame not yet reduced in this app
  session (SAXS first, then WAXS). It does not check for an existing `.dat`, so
  on a freshly started app it re-reduces the whole folder. **Stop** halts after
  the current frame. Refused while the watcher is running.
- **Watch for new files** (toggle) **every N seconds** (default 10): starts a
  *new run*. At the first poll everything already in the folder is treated as
  done (see below), and only new frames are reduced. A frame is reduced once its
  file has been unchanged for 2 s. A frame counts as already reduced only when
  its OWN detector folder holds exactly `{stem}_SAXS.dat` (or `_WAXS.dat`),
  newer than the frame; a WAXS `.dat` never marks the SAXS frame done.
  Start is refused (409) while a Run now is in progress, and also when the
  previous watcher is still finishing its current frame after Stop (try again
  a few seconds later); two watchers never run side by side.
- **Reprocess existing frames** (checkbox, ticked before starting the watcher):
  reduces every frame in the folder again, oldest first, overwriting existing
  output; new frames queue behind it, so do not use it during a beamtime. It is
  not saved with the config and is forced off when the watcher auto-resumes
  after a restart.
- **↻ Retry failed**: a frame that fails 3 times is skipped for the rest of the
  session (and `file.skipped` tells Autonomous Watch to stop waiting for it).
  This button clears those failure counts only; it does not re-reduce frames
  that worked.
- **Clear log**; the **Live log** streams progress and per-frame results.

What the watcher counts as already done at the first poll (shared rule in
`src/backlog.py`):

| Frame | Verdict |
|---|---|
| has a `.dat` at least as new as the `.raw` | done, skipped |
| no `.dat`, older than 10 min | history, skipped |
| no `.dat`, newer than 10 min | reduced (it may have landed just before a crash) |

The log says so once, as a line starting `↪ N existing frames already
processed` ("this is a new run, not a re-run of the folder"). After
that, a `.raw` that is newer than its `.dat` (a re-acquisition) is reduced again.

The operator name is entered once, in the hub top bar (this app has no field of
its own).

## Processing steps

### 1. Metadata
- **CSV mode** (`metadata_format: csv`): the CSV lives in the folder one level
  above `SAXS/`/`WAXS/` (i.e. in `2D/`). The CSV whose filename stem appears in
  the raw name (with its `_NNNN` index removed) as whole `_`-separated words is
  used; the longest match wins. `run_scan1.csv` matches `run_scan1_0003.raw` and
  `sone_run_scan1_0003.raw`, but never `run_scan10_0003.raw`.
  The row read is the frame index from the filename (`run_0009.raw` → row 9;
  4 or more digits).
- **PDI mode** (`metadata_format: pdi`): reads `<frame>.raw.pdi`. If a WAXS PDI
  has no counters, the matching SAXS PDI is used.
- Required counters: an I₀ column (`i0`, `I0`, `Ion0`, `ion0`, `I0_diode`) and a
  beamstop column (`bstop`, `Bstop`, `bstop_diode`, `Bstop_diode`, `bs`).
- During a watch, a frame whose CSV has not appeared yet waits (no failure is
  counted) while new frames with the same filename prefix are still arriving
  (within 3 poll intervals), because the CSV is written when the acquisition ends.

### 2. Corrections and normalization
`*_corr = raw value − offset` (`i0_offset`, `bstop_offset`).

Transmission:
```
T = bstop_corr / i0_corr                                    (no air values)
T = (bstop_corr / i0_corr) / (bstop_air_corr / i0_air_corr) (both air values > 0)
```
T is clipped to [1e-6, 1]. With air values set, the air-corrected transmitted
flux (`I0·T_sample`) replaces `bstop_corr` in the bstop and absolute modes.

PyFAI divides every pixel by one scalar `normalization_factor` (NF) inside
`integrate1d`, so Poisson variances scale correctly (by 1/NF²). Exactly one mode:

| Mode | NF | Result |
|---|---|---|
| `bstop` (default) | `bstop_corr` | `I = counts/(I0·T)`: flux and transmission corrected (semi-absolute), standard at SSRL |
| `i0` | `i0_corr` | `I = counts/I0`: incident flux only, no absorption correction (the log warns) |
| `absolute` | `(bstop·d_cm)/K` | `I = K·counts/(I0·T·d)` = dΣ/dΩ in cm⁻¹; K = `absolute_calibration_factor` from a water or glassy-carbon standard (K = 1 is semi-absolute) |
| none (`[]`) | 1 | raw integrated counts |

There is no exposure-time division: `i0` and `bstop` already scale with flux ×
time.

Thickness `d`: `thickness` in metres if set; otherwise Beer-Lambert
`d = −ln(T)/μ`, with `μ_cm = xraydb.material_mu(compound, energy_eV, density)`
in cm⁻¹ (`d_m = −ln(T)/(100·μ_cm)`). This assumes `compound`/`density` describe
the bulk absorber in the beam (solvent or buffer); for dilute samples it is an
approximation. Thickness only affects the absolute mode.

Other pyFAI corrections: solid angle (default on), polarization
(`polarization_factor`, ≈0.95 to 0.99 for synchrotron; when unset the log warns),
2D dark and flat frames, mask, dummy pixels.

Frames are **skipped** (no `.dat` written) when: the corrected i0 or bstop is ≤ 0;
i0 or bstop is blank/NaN; or the integrated profile has fewer than 3 positive
points (a blank frame).

### 3. Output file
- Written atomically: pyFAI writes `<name>.dat.part`, the footer is appended,
  then it is renamed to `.dat`, so a watcher never sees a half-written file.
- Header: pyFAI's own `#` block (version, integration parameters, geometry).
  There is no `# q(nm-1) I(a.u.) sigma(a.u.)` column line; do not parse for one.
- Body: three columns, q (in `unit`, nm⁻¹ by default), I, σ.
- Footer: `# METADATA INFORMATION (YML FORMAT)` followed by `# <key>: <value>`
  lines holding only the raw beamline metadata (I0, Bstop, CTEMP, ...).
- The computed corrections (`transmission`, `thickness_m`,
  `normalization_factor`, `i0_corrected`, `bstop_corrected`,
  `calibration_factor`) are **not** in the `.dat`. They are only in the manifest
  under `files.<path>.metadata`.

### 4. Manifest
Each `.dat` is registered under `files.<path>` with `stage: reduced`,
`detector`, `keyword`, `scan_idx`, `metadata` (the corrections) and
`provenance` (operator, run id, timestamp, input file, and a config snapshot of
`npt_radial`, `error_model`, `mode`, `compound`). Each run also stamps
`project_meta` (`users`, `last_run_by`, `last_run_at`, `last_run_app`,
`last_run_mode`). The operator is resolved by `src/operator_id.py`: hub operator
(`project_meta.current_operator`), then `SWAXS_USER_ID`, then the OS login.

## Why the normalization is correct
The detector records
```
I_meas(q) = I0 · T · t · ΔΩ · ε · (dΣ/dΩ)(q)        (+ background)
```
(`I0` incident flux, `T` transmission, `t` thickness, `ΔΩ` pixel solid angle,
`ε` detector efficiency), so `dΣ/dΩ = I_meas / (I0 · T · t · ΔΩ · ε)`. The
beamstop diode measures the transmitted flux, so `bstop ∝ I0·T` and `i0 ∝ I0`,
which gives `T = bstop / i0`.

The modes overlap, so they are never multiplied. `["bstop", "absolute"]` would
give `NF = bstop²·d/K`, i.e. `I ∝ counts/(I0²·T²·d)`, and `["i0", "bstop"]` would
give `I ∝ counts/(I0²·T)`: both physically meaningless. `resolve_normalization`
collapses combinations with a warning: `absolute` + anything → `absolute`;
`i0` + `bstop` → `bstop`. Unknown terms are ignored with a warning.

References:
- Pauw, "Everything SAXS", J. Phys. Condens. Matter 25, 383201 (2013),
  https://doi.org/10.1088/0953-8984/25/38/383201
- BSRF absolute-intensity calibration, Nucl. Instrum. Methods A (2018),
  https://www.sciencedirect.com/science/article/abs/pii/S0168900218306260
- EMBL BioSAXS data-reduction notes (Kikhney),
  https://www.embl-hamburg.de/biosaxs/courses/embo2012/slides/data-reduction-processing-kikhney.pdf

## Uncertainty σ(q)
σ comes from Poisson counting statistics: with `error_model: poisson` pyFAI uses
`σᵢ² = max(1, signalᵢ)` per pixel and propagates it through dark, flat, mask and
the normalization factor inside the same `integrate1d` call
(`src/reduction/core.py`), so σ is divided by the same scalar as I.

Limits worth knowing before quoting error bars:
1. Nothing checks that `.raw` holds unprocessed photon counts. If the detector
   firmware applies gain, flat or dead-time corrections first, Poisson σ is wrong.
2. With no WAXS mask (`mask_files.waxs: null`), dead pixels feed into I and σ.
3. `dummy`/`delta_dummy` are unset by default, so a firmware sentinel value for
   bad pixels would be integrated as signal.
4. No azimuthal cross-check or blank-frame χ² check is run.

Full treatment: `docs/ERROR_PROPAGATION.md` §2. Further reading: Sedlak, Bruetzel
& Lipfert 2017, J. Appl. Cryst. 50, 621-630
(https://doi.org/10.1107/S1600576717003077); Kieffer et al. 2025, J. Appl.
Cryst. 58, 138-153, pyFAI's error-model citation
(https://doi.org/10.1107/S1600576724011038).

## config.yml reference
```yaml
# Required (missing keys raise KeyError)
data_directory: /path/to/2D
poni_directory: /path/to/poni
poni_files:      {saxs: atT_SAXS.poni, waxs: atT_WAXS.poni}
mask_files:      {saxs: RT_SAXS_mask_03.edf, waxs: null}   # null = no mask
detector_shapes: {saxs: [1043, 981], waxs: [195, 487]}     # [rows, cols]
compound: C2H4
energy_keV: 12
density_g_cm3: 0.92
mode: SWAXS                 # SAXS | WAXS | SWAXS
metadata_format: csv        # csv | pdi
npt_radial: 1000            # no default
error_model: poisson        # no default; poisson | azimuthal

# Optional (defaults shown)
thickness: null             # METRES (0.001 = 1 mm); null = from transmission
normalization: [bstop]      # bstop | i0 | absolute; one mode; [] = raw counts
absolute_calibration_factor: 1.0   # K for absolute mode
unit: q_nm^-1               # q_nm^-1 | q_A^-1 | 2th_deg | 2th_rad
correct_solid_angle: true
polarization_factor: null   # null = skip; ~0.95-0.99 for synchrotron
radial_range_min: null      # in `unit`; applied only if BOTH are set
radial_range_max: null
azimuth_range_min: null     # χ in degrees; applied only if BOTH are set
azimuth_range_max: null
dummy: null                 # pixel value treated as masked
delta_dummy: null           # tolerance around dummy
dark_files: null            # {saxs: file, waxs: file}, 2D dark frames
flat_files: null            # {saxs: file, waxs: file}, 2D flat fields
i0_offset: 0.0              # scalar diode dark-current offsets
bstop_offset: 0.0
i0_air: 0.0                 # empty-beam readings; 0 = not measured
bstop_air: 0.0
output_directory: null      # null or "" = 1D/ beside data_directory
saxs_filename_prefix: ""    # stripped from output names
waxs_filename_prefix: ""
beamline: {type: "1-5", data_format: raw}   # data_format = frame file extension
```
`dark_files`/`flat_files` are 2D frames applied pixel by pixel; they are
unrelated to the scalar `i0_offset`/`bstop_offset` diode corrections.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| q range looks wrong | PONI does not match the detector (SAXS vs WAXS), `poni_files` points at the wrong file, or the distance in the PONI is off. Recalibrate in Geometry Calibration. |
| Transmission > 1 (warned when thickness is auto) | Without air values T is only the raw bstop/I0 diode ratio, so > 1 can be normal; it is clipped to 1 and the derived thickness becomes 0. With air values: wrong offset sign, beam drift, or bad `i0_air`/`bstop_air`. Set an explicit `thickness` for absolute work. |
| Very low transmission (< 0.02) | Sample too thick, beam off-centre, or sample dried on the window. |
| Negative I at high q | Not expected here: reduction does no background subtraction. Negative values appear after subtraction of noisy data; cut q_max before analysis. |
| Hot pixels or streaks | Check the mask `.edf` covers bad pixels and the beamstop; set `dummy` if the detector flags dead pixels with a value. |
| "No CSV matching ..." | CSV mode: no CSV in `2D/` whose name appears in the raw name as whole `_`-separated words. During a watch this waits while the acquisition is still producing frames. Check `metadata_format`. |
| "'i0' (or 'bstop') not found in metadata" | Wrong `metadata_format`, or the CSV/PDI lacks an I0/Bstop column. |
| "Could not parse frame index" | The raw name has no `_NNNN.` index (4+ digits), needed to pick the CSV row. |
| "I0_corrected ≤ 0" / "Bstop_corrected ≤ 0" | The offset is larger than the reading; the frame is skipped. Offsets should be the shutter-closed reading. |
| "integrates to no signal" | Blank frame (zero exposure, shutter closed, or a mask covering everything); skipped. |
| "failed 3×, skipping until restart or Retry failed" | Fix the cause, then **↻ Retry failed**. |
| Run now refused: "The file monitor is running" | Only one of Run now / Watch can run; the watcher is already reducing new files (or is still stopping). |
| Watch refused: "A one-shot run (Run now) is in progress" | Only one of Run now / Watch can run. Wait for the run to finish, or Stop it, then start the watcher. |
| Watch refused: "The previous monitor is still finishing" | Stop was pressed and the old watcher is still on its current frame. Press Start again in a few seconds. |
| Restart re-reduced old frames, or did not | Run now redoes frames not seen this session; use Watch (which skips existing output) unless you want a full redo. |
| KeyError `npt_radial` / `error_model` | Both are required in `config.yml`. |

## API routes

| Route | Purpose |
|---|---|
| `GET /api/health` | `{"status": "ok", "app": "reduction"}` |
| `POST /api/set_project` | `{path}` from the hub; sets `SWAXS_PROJECT` |
| `GET /api/project` | `{project_root}` |
| `GET /api/browse?path=` | Folder browser |
| `POST /api/run` | One-shot run with the config in the body (409 if the watcher runs) |
| `POST /api/run/stop` | Stop the one-shot run after the current frame |
| `POST /api/monitor/start` | `{config, interval, reprocess_existing}` start the watcher (409 while Run now is active or the previous watcher is still stopping) |
| `POST /api/monitor/stop` | Stop the watcher |
| `GET /api/monitor/status` | `{monitoring}` |
| `POST /api/retry-failed` | Clear failure counts (old name `/api/reset` still works) |
| `GET /api/stream` | Server-sent log stream |
| `POST /api/save-config` / `POST /api/load-config` | Write / read a YAML config at `path` |
| `GET /api/list-dat?dir=` / `GET /api/dat-data?file=` | List `.dat` files / read one |
| `GET /api/list-raw?dir=` / `GET /api/raw-image?file=` | List `.raw` files / render one as an image |
