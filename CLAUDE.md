# SWAXS Platform — Developer Guide

## Critical Rules

IMPORTANT: Do not test the code after making changes unless explicitly told to do so.
IMPORTANT: Always use `uv run` to execute Python — this ensures the virtual environment is active.
IMPORTANT: VSCode may show false diagnostic warnings for the `periodictable` (pt) library — ignore them, they are not real errors.

---

## Architecture Overview

The platform is a hub-and-spoke system: one central hub (port 5000) launches and monitors five independent Flask apps as subprocesses. Every app follows the same pattern — `app.py` handles routing, `templates/index.html` is the UI, and all science/data logic lives in the shared `src/` package.

```
SWAXS_data_correction_reduction_averaging/
│
├── hub/                        # Central launcher (port 5000)
│   ├── app.py
│   └── templates/index.html
│
├── reduction/                  # 2D→1D reduction & correction (port 5001)
│   ├── app.py
│   └── templates/index.html
│
├── viewer/                     # Data viewer & averaging (port 5002)
│   ├── app.py
│   └── templates/index.html
│
├── background/                 # Background subtraction (port 5003)
│   ├── app.py
│   └── templates/index.html
│
├── quality/                    # AI good/bad grading of subtracted profiles (port 5006)
│   ├── app.py
│   └── templates/index.html
│
├── analysis/                   # Guinier, Porod, Kratky, peak fitting (port 5004)
│   ├── app.py
│   └── templates/index.html
│
├── assistant/                  # AI assistant (port 5005)
│   ├── app.py
│   └── templates/index.html
│
├── reactor/                    # Flow-synthesis reactor + SPEC/beamline control (port 5007)
│   ├── app.py
│   └── templates/index.html
│
├── analyzer/                   # Bayesian optimizer + nanoparticle analysis (port 5008)
│   ├── app.py
│   └── templates/index.html
│
├── src/                        # Shared science/data logic — all apps import from here
│   ├── __init__.py
│   ├── manifest.py             # Shared experiment manifest (cross-app data contract)
│   ├── plot_reduction.py       # Data loading, averaging, plotting utilities
│   ├── reduction/              # Reduction-specific modules
│   │   ├── __init__.py
│   │   ├── core.py             # Experiment class, PyFAI integration, correction pipeline
│   │   ├── process_metadata.py # CSV/PDI beamline metadata extraction
│   │   └── read_raw_file.py    # Binary .raw detector file reader
│   ├── analysis/              # Guinier/Porod/Kratky/p(r), model & nanoparticle fits
│   │   └── nanoparticle.py    # Nanoparticle size/shape analysis (optimizer feedback)
│   ├── beamline/              # SPEC bServer HTTP driver (shutter, counters, 2D collection)
│   ├── optimizer/             # Bayesian optimization campaign (recipe suggestion)
│   └── utils/
│       ├── __init__.py
│       └── read_dat_metadata.py  # .dat file parser (q, I, sigma + metadata footer)
│
├── start_platform.sh           # One-command startup script
├── requirements.txt            # Python dependencies
└── CLAUDE.md                   # This file
```

---

## The One Rule

**All logic lives in `src/`. All apps are thin Flask shells.**

Every `app.py` does exactly three things:
1. Adds the project root to `sys.path` so `src.*` imports resolve
2. Imports what it needs from `src/`
3. Defines Flask routes

If you find yourself writing science or data logic directly in `app.py`, move it to `src/` first.

---

## What Each App Imports from src/

| App | Imports |
|---|---|
| `reduction` | `src.reduction.core` (Experiment, run_pipeline, find_new_raw_files) |
| `viewer` | `src.plot_reduction` (read_folder, average_and_save), `src.utils.read_dat_metadata` |
| `background` | `src.manifest`, `src.utils.read_dat_metadata` |
| `analysis` | `src.manifest`, `src.utils.read_dat_metadata` |
| `quality` | `src.quality` (grade_profile, score_metrics), `src.manifest`, `src.utils.read_dat_metadata` |
| `reactor` | `src.reactor` (ReactorController, Recipe, load_config), `src.beamline` (SPEC driver), `src.manifest` |
| `analyzer` | `src.manifest`, `src.utils.read_dat_metadata`, `src.optimizer` (Bayesian campaign), `src.analysis.nanoparticle` |
| `assistant` | `src.manifest`, `src.utils.read_dat_metadata` |
| `hub` | `src.manifest` (project-root state) — otherwise only stdlib and Flask |

---

## Running the Platform

```bash
# Start everything (recommended)
./start_platform.sh

# Or start the hub manually
uv run hub/app.py

# Start a single app directly (for development)
uv run reduction/app.py
uv run viewer/app.py
uv run background/app.py
uv run analysis/app.py
uv run assistant/app.py
```

Ports: hub=5000, reduction=5001, viewer=5002, background=5003, analysis=5004, quality=5006, reactor=5007, assistant=5005.

---

## Experiment Data Structure

Apps read and write data from a user-selected project folder. The expected layout:

```
<project_root>/
├── 2D/
│   ├── SAXS/
│   │   ├── *.raw          # Raw SAXS detector images
│   │   └── *.raw.pdi      # PDI metadata (or *.csv alongside)
│   └── WAXS/
│       ├── *.raw          # Raw WAXS detector images
│       └── *.raw.pdi
├── *.csv                  # Experiment-level CSV metadata (if CSV mode)
├── poni/
│   ├── *.poni             # PyFAI calibration files
│   └── *.edf              # Detector mask files
├── 1D/
│   ├── SAXS/
│   │   ├── Reduction/     # Output from reduction app (*.dat)
│   │   ├── Averaged/      # Output from viewer app averaging (*.dat)
│   │   └── Subtracted/    # Output from background app (*.dat)
│   │       ├── Good/         # Quality Gate: accepted (analysis-ready) profiles
│   │       └── NeedsReview/  # Quality Gate: flagged / bad profiles
│   ├── WAXS/
│   │   ├── Reduction/
│   │   ├── Averaged/
│   │   └── Subtracted/{Good,NeedsReview}/
│   └── QualityReports/   # Quality Gate CSV reports + accepted lists
└── config.yml             # Reduction configuration (loaded by reduction app)
```

---

## config.yml Reference

The reduction app reads a YAML config file. Key fields:

```yaml
data_directory: "/path/to/2D"      # Path to 2D raw data folder
poni_directory: "/path/to/poni"    # Calibration and mask files

compound: "C2H4"                   # Sample molecular formula
energy_keV: 12                     # X-ray energy
density_g_cm3: 0.92                # Sample density
thickness: null                    # Sample thickness in METRES (0.001 = 1 mm!); null = auto from transmission

mode: "SWAXS"                      # "SAXS", "WAXS", or "SWAXS"
metadata_format: "csv"             # "csv" or "pdi"

detector_shapes:
  saxs: [1043, 981]
  waxs: [195, 487]

poni_files:
  saxs: "atT_SAXS.poni"
  waxs: "atT_WAXS.poni"

mask_files:
  saxs: "RT_SAXS_mask_03.edf"
  waxs: null                       # null = no mask

# Detector offsets (set to 0 if not needed)
i0_offset: 0.0
bstop_offset: 0.0

# Air path measurements (set to 0 if not measuring air transmission)
i0_air: 0.0
bstop_air: 0.0

# Integration
npt_radial: 1000
error_model: "poisson"
unit: "q_nm^-1"                    # output q unit; DEFAULT nm⁻¹ (matches viewer/analysis)

# Normalization — choose ONE mode (terms overlap; combos are collapsed w/ a warning)
normalization: ["bstop"]           # "bstop" (default) | "i0" | "absolute"
absolute_calibration_factor: 1.0   # K for "absolute" mode (from water/GC standard)
polarization_factor: null          # ~0.95–0.99 for synchrotron; null = skip

beamline:
  type: "1-5"
  data_format: "raw"
```

### Normalization modes

PyFAI divides each pixel by one scalar `normalization_factor` before integration:

- **bstop** (default): `NF = bstop_corr` → `I = counts/(I0·T)` (transmission-corrected, semi-absolute).
- **i0**: `NF = i0_corr` → `I = counts/I0` (incident-flux only; no absorption correction).
- **absolute**: `NF = (bstop·d_cm)/K` → `I = K·counts/(I0·T·d)` = dΣ/dΩ in cm⁻¹.

There is no exposure-time division — normalize by `i0`/`bstop` (which scale with flux × time).
Frames with a non-positive corrected `i0`/`bstop` are skipped (no `.dat` written).
The **operator/user** is captured automatically (UI Operator field → `SWAXS_USER_ID`
→ OS login) and stored in each file's provenance plus `project_meta`.

### Transmission formula

When `i0_air` and `bstop_air` are provided (non-zero):

```
T_sample = (bstop_corr / i0_corr) / (bstop_air_corr / i0_air_corr)
```

where `*_corr = raw_value − offset`. When they are zero the simpler ratio `bstop_corr / i0_corr` is used.

---

## Adding a New App

1. Create a new folder: `myapp/`
2. Create `myapp/app.py` — copy the sys.path block from any existing app, add your Flask routes
3. Create `myapp/templates/index.html` — the UI
4. If the app needs new logic, add it to `src/` (new file or subfolder)
5. Register the app in `hub/app.py` — add an entry to the `APPS` list with a unique `id`, `port`, and `entry` path
6. Add the port to `start_platform.sh` banner

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `flask` | Web framework for all apps |
| `pyFAI` | Detector calibration and azimuthal integration |
| `fabio` | Scientific image I/O (.raw, .edf files) |
| `xraydb` | X-ray absorption coefficients |
| `numpy`, `pandas` | Numerical processing |
| `matplotlib` | Plot generation (server-side, Agg backend) |
| `scipy` | Curve fitting (analysis app) |
| `pyyaml` | Config file parsing |

---

## manifest.json — Cross-App Data Contract

`src/manifest.py` manages a `manifest.json` file at the project root. This is how apps share state about processed files.

- The **reduction app** writes entries when it produces `.dat` files
- The **viewer app** updates entries when it averages scans
- The **background app** writes subtraction records
- The **analysis app** writes analysis results
- The **assistant app** reads the manifest to answer questions about the experiment

Each app reads/writes only its own section. Apps must never overwrite other apps' keys.
