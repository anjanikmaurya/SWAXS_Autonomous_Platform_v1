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
- Integration: `npt_radial` points (default 1000) along q axis
- Error model: `poisson` (σ = √N from photon counts)
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

### 5. Output Format
Each reduced frame is saved as a 3-column ASCII `.dat` file:

```
# q(nm-1)   I(a.u.)   sigma(a.u.)
0.01234      1.23e-2   5.6e-4
...
```

Footer metadata block (lines starting with `#`):
```
# keyword: BSA_10mg
# detector: SAXS
# energy_keV: 12.0
# transmission: 0.8542
# i0: 123456.7
# exposure: 1.0
# reduced_at: 2025-01-15T10:22:00Z
```

### 6. Manifest Registration
After reduction, each `.dat` file is registered in `manifest.json` under
`files.<path>` with `stage`, `keyword`, `detector`, `metadata` (the corrections),
and `provenance` — which records the **operator/user**, run_id, timestamp, input
files, and a config snapshot. Each run also stamps `project_meta` with the unique
`users` list plus `last_run_by`, `last_run_at`, `last_run_app`, and
`last_run_mode`. The operator is resolved as: UI Operator field →
`SWAXS_USER_ID` env → OS login → `unknown`.

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
mode: SWAXS          # SAXS | WAXS | SWAXS
metadata_format: csv # csv | pdi
energy_keV: 12
density_g_cm3: 0.92
thickness: null      # null = auto from transmission
poni_files:
  saxs: atT_SAXS.poni
  waxs: atT_WAXS.poni
mask_files:
  saxs: RT_SAXS_mask_03.edf
  waxs: null
i0_offset: 0.0
bstop_offset: 0.0
i0_air: 0.0
bstop_air: 0.0
npt_radial: 1000
error_model: poisson
normalization: [bstop]            # bstop | i0 | absolute  (one mode; default bstop)
unit: q_nm^-1                     # output q unit (default nm⁻¹)
absolute_calibration_factor: 1.0  # K for 'absolute' mode (water/GC standard)
polarization_factor: null         # ~0.95–0.99 for synchrotron; null = skip
```

The **operator/user** is captured automatically (UI Operator field →
`SWAXS_USER_ID` → OS login) and recorded in provenance — no config field needed.

## Dependencies
- `pyFAI` — azimuthal integration
- `fabio` — `.raw` and `.edf` file I/O
- `xraydb` — attenuation coefficients for thickness calculation
- `periodictable` (as `pt`) — molecular formula parsing for density/μ
