# Reduction App — Knowledge Base

## Purpose
The Reduction app (port 5001) converts raw 2D detector images (.raw files) into
calibrated, corrected 1D scattering curves (.dat files).  It covers SAXS, WAXS,
and simultaneous SWAXS modes.

## Pipeline Steps

### 1. Raw File Discovery
- Scans `<project>/2D/SAXS/` and/or `<project>/2D/WAXS/` for `.raw` files
- Reads paired `.raw.pdi` files (PDI metadata format) or a single CSV
- Extracts per-frame: I₀, beamstop transmission, exposure time, timestamp

### 2. Metadata Extraction
Two formats supported:
- **PDI mode** (`metadata_format: pdi`): reads `*.raw.pdi` files alongside each
  raw frame.  Each PDI file contains tab-separated key-value pairs.
- **CSV mode** (`metadata_format: csv`): a single CSV at the project root maps
  filename → metadata columns (I0, BStop, exposure, etc.)

Key metadata fields: `i0`, `bstop`, `exposure`, `transmission`, `keyword`,
`filename`, `timestamp`.

### 3. Azimuthal Integration (PyFAI)
- Uses PONI calibration file (`.poni`) per detector
- Optional mask (`.edf` EDF format) to exclude bad pixels / beamstop shadow
- Integration: `npt_radial` points along the q axis (a REQUIRED config key —
  there is no default; 1000 is the value used in the example configs)
- Error model: `error_model`, also REQUIRED; `poisson` (σ = √N from photon counts)
  is the usual choice
- Output: q (nm⁻¹), I(q), σ(q)

### 4. Corrections & Normalization
The beamstop diode measures the **transmitted** flux, so `bstop ∝ I0·T` and
`i0 ∝ I0`; hence transmission `T = bstop_corr / i0_corr` (dark-current offsets
subtracted from both). PyFAI divides every pixel by a single scalar
`normalization_factor` *before* azimuthal averaging (correct Poisson error
propagation). Exactly **one** normalization mode is used at a time — the modes
overlap, so combinations are collapsed automatically with a warning:

- **bstop** (default): `NF = bstop_corr` → `I = counts/(I0·T)` — flux- and
  transmission-corrected (semi-absolute). Standard SSRL scheme.
- **i0**: `NF = i0_corr` → `I = counts/I0` — incident-flux only; does **not**
  correct for sample absorption/transmission.
- **absolute**: `NF = (bstop·d_cm)/K` → `I = K·counts/(I0·T·d)` = dΣ/dΩ in cm⁻¹,
  where `K` (`absolute_calibration_factor`) comes from a water / glassy-carbon
  standard.

Note: there is **no exposure-time division** in the current pipeline; normalize
by `i0`/`bstop` (which scale with flux × time) instead.

Air-path (empty-beam) correction: if `i0_air`/`bstop_air` are set, the true
sample transmission `T = (bstop/i0)/(bstop_air/i0_air)` is used, and the
air-corrected transmitted flux (`I0·T_sample`) is used for both the bstop and
absolute modes.

Thickness `d`: taken from `thickness` (metres) if set, otherwise derived from
transmission via Beer–Lambert `d = −ln(T)/μ` (μ from xraydb for the configured
compound/density — assumes that compound is the **bulk** absorber). Thickness
only affects the **absolute** mode.

Other PyFAI corrections: solid-angle (on by default) and polarization
(`polarization_factor` ≈ 0.95–0.99 recommended for synchrotron; off by default).

Robustness: frames whose corrected i0 or bstop is ≤ 0 are **skipped** (no `.dat`
written) so corrupt normalization can't enter averaging/analysis.

Output q unit defaults to **nm⁻¹** (`unit: q_nm^-1`) to match the rest of the
platform (averaging, analysis, Rg/Dmax). Override with `unit` if needed;
dimensionless qRg checks are unaffected by the choice.

### 5. Output Format and Path
Each reduced frame is written to:

```
<output_dir>/<DET>/Reduction/<stem>_<DET>.dat
```

where `<DET>` is `SAXS` or `WAXS`, `<stem>` is the raw filename with the data-format
suffix (and any configured `saxs_filename_prefix`/`waxs_filename_prefix`) stripped,
and `<output_dir>` is `output_directory` from the config or, by default, a `1D`
folder alongside `data_directory`. Example:
`1D/SAXS/Reduction/scan_0007_SAXS.dat`.

The file is written atomically: pyFAI writes to `<path>.dat.part`, the metadata
footer is appended to that same `.part`, and only then is it renamed to the final
`.dat`. A folder watcher therefore never sees a footer-less half-written file.

Body: three columns — q (in the configured `unit`, nm⁻¹ by default), I, and σ.
The HEADER is written by pyFAI's own `integrate1d(filename=...)` writer: a
multi-line `#` block listing the pyFAI version, the integration parameters and the
detector geometry. There is **no** `# q(nm-1) I(a.u.) sigma(a.u.)` column line —
do not parse for one.

Footer: `_append_metadata_to_dat` appends
```
# METADATA INFORMATION (YML FORMAT)
# <key>: <value>        ← one line per raw beamline metadata key
```
This block contains ONLY the raw PDI/CSV beamline metadata dict as read from the
instrument (e.g. `I0`, `Bstop`, `CTEMP`, exposure, timestamps — whatever the
beamline wrote).

The COMPUTED corrections are **not** in the `.dat` file. `transmission`,
`thickness_m`, `normalization_factor`, `i0_corrected`, `bstop_corrected` and
`calibration_factor` are returned by `_compute_corrections` and stored in
`manifest.json` under `files.<path>.metadata` — that is the only place to read
them. Reading transmission out of a `.dat` footer will not work unless the
beamline itself recorded a transmission column.

### 6. Manifest Registration
After reduction, each `.dat` file is registered in `manifest.json` under
`files.<path>` with `stage`, `keyword`, `detector`, `metadata` (the corrections),
and `provenance` — which records the **operator/user**, run_id, timestamp, input
files, and a config snapshot. Each run also stamps `project_meta` with the unique
`users` list plus `last_run_by`, `last_run_at`, `last_run_app`, and
`last_run_mode`. The operator is resolved as: UI Operator field →
`SWAXS_USER_ID` env → OS login → `unknown`.

## Why the Normalization Math Is Correct (canonical formula + references)

The intensity recorded on the detector is

```
I_meas(q) = I0 · T · t · ΔΩ · ε · (dΣ/dΩ)(q)        (+ background)
```

with `I0` the incident flux, `T` the sample transmission, `t` the sample
thickness, `ΔΩ` the pixel solid angle, `ε` the detector efficiency, and `dΣ/dΩ`
the absolute differential cross-section per unit volume (cm⁻¹·sr⁻¹). Hence

```
dΣ/dΩ (q) = I_meas / (I0 · T · t · ΔΩ · ε)
```

A beamstop pin-diode measures the **transmitted** flux, so `bstop ∝ I0·T` and
`i0 ∝ I0`, giving the identity `T = bstop / i0` that the pipeline uses.

References:
- Pauw, "Everything SAXS", J. Phys. Condens. Matter 25, 383201 (2013) —
  https://iopscience.iop.org/article/10.1088/0953-8984/25/38/383201
- BSRF absolute-intensity calibration, Nucl. Instrum. Methods A (2018) —
  https://www.sciencedirect.com/science/article/abs/pii/S0168900218306260
- EMBL BioSAXS data-reduction notes (Kikhney) —
  https://www.embl-hamburg.de/biosaxs/courses/embo2012/slides/data-reduction-processing-kikhney.pdf
- USP SAXS/SANS normalization notes —
  https://portal.if.usp.br/cristal/sites/portal.if.usp.br.cristal/files/Treatment_SAXS_crislpo.pdf

Error propagation is correct because the factor is passed into
`integrate1d(..., normalization_factor=NF, error_model="poisson")` rather than
being divided out afterwards, so Poisson variances scale by 1/NF².

Beer–Lambert thickness units check out: `μ_cm = xraydb.material_mu(energy_eV,
density)` in cm⁻¹, `μ_m = μ_cm·100`, `d_m = −ln(T)/μ_m`, `d_cm = −ln(T)/μ_cm`.

### Why normalization is a single mode, not a list
The three terms are not independent — `absolute` already contains `bstop`. If the
terms were multiplied, `normalization: ["bstop", "absolute"]` would give

```
NF = bstop · (bstop·d/K) = bstop²·d/K
  ⟹  I ∝ counts/(I0²·T²·d)          — physically meaningless (divides by flux²)
```

and `["i0", "bstop"]` would give `NF = i0·bstop ⟹ I ∝ counts/(I0²·T)`, also
wrong. Only `["bstop"]`, `["i0"]` or `["absolute"]` are meaningful, so
`resolve_normalization` collapses any overlapping combination to a single mode and
logs a warning. Unrecognised terms are ignored with a warning rather than silently
dropped.

### Known approximations
- Thickness-from-transmission `d = −ln(T)/μ(compound, density)` is exact only if
  `compound`/`density` describe the **bulk** absorber in the beam (solvent or
  buffer), not a dilute solute. For dilute samples it is an approximation.
- Polarization is off by default (`polarization_factor: null`). Synchrotron beams
  are strongly horizontally polarized; the app logs a warning and ≈0.95–0.99 is
  recommended for quantitative work.
- `i0` mode normalizes to incident flux only and does not correct absorption; the
  app logs a warning when that mode is selected alone.

## Common Issues

### Wrong q Range
If q values look off, check:
1. PONI file matches detector (SAXS ↔ SAXS, WAXS ↔ WAXS)
2. `poni_files` keys in `config.yml` point to correct files
3. Sample-to-detector distance in PONI is accurate

### Transmission > 1
Causes: `i0_offset` or `bstop_offset` wrong sign; beam drift between sample and
air measurement; incorrect `i0_air`/`bstop_air` values.

### Very Low Transmission (< 0.02)
Sample too thick, beam not centred, or concentrating sample dried on window.

### I(q) Negative at High q
Normal after background subtraction of noisy data.  Apply q_max cut before
analysis.  Do NOT apply background here — reduction output is raw sample.

### Hot Pixels / Rings in 2D
- Verify mask `.edf` file covers bad pixel regions
- Check detector for persistent hot pixels using a flat-field or empty beam
- PyFAI's `azimuthal_integrator.integrate1d` accepts a `mask` kwarg

## Config Reference (key fields)
```yaml
mode: SWAXS          # SAXS | WAXS | SWAXS      (REQUIRED)
metadata_format: csv # csv | pdi                (REQUIRED)
energy_keV: 12                                # REQUIRED
density_g_cm3: 0.92                           # REQUIRED
compound: C2H4                                # REQUIRED
data_directory: /path/to/2D                   # REQUIRED
poni_directory: /path/to/poni                 # REQUIRED
detector_shapes:                              # REQUIRED
  saxs: [1043, 981]
  waxs: [195, 487]
thickness: null      # metres (0.001 = 1 mm); null = auto from transmission
poni_files:          # REQUIRED
  saxs: atT_SAXS.poni
  waxs: atT_WAXS.poni
mask_files:          # REQUIRED (values may be null)
  saxs: RT_SAXS_mask_03.edf
  waxs: null
npt_radial: 1000     # REQUIRED — no default; a missing key raises KeyError
error_model: poisson # REQUIRED — no default; a missing key raises KeyError

# ── Optional, with defaults ──────────────────────────────────────────────
i0_offset: 0.0
bstop_offset: 0.0
i0_air: 0.0
bstop_air: 0.0
normalization: [bstop]            # bstop | i0 | absolute  (one mode; default bstop)
unit: q_nm^-1                     # output q unit (default nm⁻¹)
absolute_calibration_factor: 1.0  # K for 'absolute' mode (water/GC standard)
polarization_factor: null         # ~0.95–0.99 for synchrotron; null = skip
correct_solid_angle: true         # cos³θ factor; default true
radial_range_min: null            # q/2θ integration range in the chosen unit;
radial_range_max: null            #   BOTH must be set or the range is ignored
azimuth_range_min: null           # χ sector in degrees; BOTH must be set,
azimuth_range_max: null           #   otherwise the full circle is integrated
dummy: null                       # pixel value treated as masked
delta_dummy: null                 # tolerance around `dummy`
dark_files: {}                    # per-detector 2D dark frames (saxs:/waxs:)
flat_files: {}                    # per-detector 2D flat fields (saxs:/waxs:)
output_directory: ""              # "" = <data_directory>/../1D
saxs_filename_prefix: ""          # stripped from output stems
waxs_filename_prefix: ""
beamline:
  data_format: raw                # file extension of the 2D frames
```

Note that `npt_radial` and `error_model` are read with `config["..."]`, not
`config.get(...)` — they are REQUIRED and there is no fallback value.
`dark_files`/`flat_files` are 2D frames subtracted/divided pixel-by-pixel and are
a different thing from the scalar `i0_offset`/`bstop_offset` diode corrections.

The **operator/user** is captured automatically (UI Operator field →
`SWAXS_USER_ID` → OS login) and recorded in provenance — no config field needed.

## Dependencies
- `pyFAI` — azimuthal integration
- `fabio` — `.raw` and `.edf` file I/O
- `xraydb` — attenuation coefficients for thickness calculation
- `periodictable` (as `pt`) — molecular formula parsing for density/μ
