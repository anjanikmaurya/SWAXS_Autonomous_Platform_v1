# SWAXS Platform — Developer Guide

## Critical Rules

IMPORTANT: Do not test the code after making changes unless explicitly told to do so.
IMPORTANT: Run Python from the activated virtual environment (`venv/`), with plain
`python`. Do **not** use `uv run` — there is no `pyproject.toml` or `uv.lock` here,
and the hub launches every sub-app with `sys.executable`, so the interpreter that
starts the hub is the one that must have the dependencies installed.
IMPORTANT: Install with `pip install -r requirements-core.txt`, never
`requirements.txt` (a `pip freeze` of one Mac; it fails to install on Windows).
IMPORTANT: VSCode may show false diagnostic warnings for the `periodictable` (pt) library — ignore them, they are not real errors.

---

## The One Rule

**All logic lives in `src/`. All apps are thin Flask shells.**

Every `app.py` does exactly three things:

1. Adds the project root to `sys.path` so `src.*` imports resolve
2. Imports what it needs from `src/`
3. Defines Flask routes

If you find yourself writing science or data logic directly in `app.py`, move it to `src/` first.

Known standing violation: the subtraction science still lives in
`background/app.py` (`_subtract`, `_interpolate_onto`, `_auto_scale`,
`_qc_metrics`, `truncate_rebin`, `_write_dat`). Tracked as **D3** in
`docs/audits/OPEN_DEFECTS.md`; the write-up is
`docs/design/AUTOPILOT_PIPELINE_DESIGN.md` §4.

---

## Architecture Overview

Hub-and-spoke: one central hub (port 5000) launches and monitors **nine**
independent Flask apps as subprocesses. Every app follows the same pattern —
`app.py` handles routing, `templates/index.html` is the UI, `knowledge.md` is
indexed by the AI assistant, and all science/data logic lives in the shared
`src/` package.

```
SWAXS_Autonomous_Platform_v1/
│
├── hub/                    # Central launcher (5000) — also knowledge.md, no app entry
├── calibration/            # .raw → CBF, pyFAI .poni generation, SFTP pull (5009)
├── reduction/              # 2D→1D reduction & correction (5001)
├── viewer/                 # Data viewer & averaging (5002)
├── background/             # Background subtraction (5003)
├── quality/                # AI good/bad grading of subtracted profiles (5006)
├── analysis/               # Guinier, Porod, Kratky, p(r), sasmodels, ATSAS, peaks (5004)
├── analyzer/               # Nanoparticle fit + Bayesian optimizer (5008)
├── reactor/                # Flow-synthesis reactor + SPEC/beamline control (5007)
├── assistant/              # AI assistant (5005)
│       └── each of the above: app.py · templates/index.html · knowledge.md
│
├── src/                    # Shared logic — all apps import from here
│   ├── manifest.py             # Cross-app data contract (manifest.json)
│   ├── events.py               # WebSocket event bus client + emit_* helpers
│   ├── runstate.py             # Atomic monitor-state persistence; monitor_alive()
│   ├── proc_lifecycle.py       # Port binding, process trees, log rotation (hub)
│   ├── loop_naming.py          # {recipe_id}_{role} condition keywords
│   ├── plot_reduction.py       # Data loading, averaging, plotting utilities
│   ├── preprocess/             # calib.py · raw_convert.py · sftp_sync.py
│   ├── reduction/              # core.py (Experiment, pyFAI) · process_metadata.py
│   │                           #   · read_raw_file.py
│   ├── utils/read_dat_metadata.py   # .dat parser (q, I, sigma + metadata footer)
│   ├── quality/core.py         # grade_profile, score_metrics
│   ├── analysis/               # core.py · io.py · atsas.py · nanoparticle.py
│   ├── optimizer/              # campaign.py · space.py · gp.py · io.py
│   │                           #   · diagnostics.py · plots.py
│   ├── reactor/                # controller.py · hardware.py · config.py
│   │                           #   · recipe.py · intake.py · drivers/Py_P_Pump.py
│   ├── beamline/driver.py      # SPEC bServer HTTP + EPICS reads
│   ├── simulator/              # Mock-only synthetic 2D data: ground_truth · pattern
│   │                           #   · writer · collector
│   ├── notify/                 # slack.py · email_notify.py · multi.py
│   └── ai/                     # assistant.py · knowledge.py · memory.py · hints.py
│                               #   · plots.py · code_exec.py · loop_advice.py
│
├── apps.yml                # THE app registry — ports, entries, knowledge, manifest keys
├── start_platform.sh       # macOS / Linux launcher
├── start_platform.ps1      # Windows PowerShell launcher
├── start_platform.bat      # Windows Anaconda Prompt / double-click launcher
├── requirements-core.txt   # What you install (16 packages, all prebuilt wheels)
├── requirements-hardware.txt   # pyserial, pyepics — real rig only
├── requirements-ai.txt     # anthropic, chromadb, … — AI assistant extras
├── requirements.txt        # DEPRECATED pip freeze; do not install
├── check_imports.py · conftest.py · pytest.ini
├── tests/ · tools/ · docs/ · ai_knowledge/ · logs/
└── CLAUDE.md               # This file
```

Ports: hub 5000 · reduction 5001 · viewer 5002 · background 5003 · analysis 5004 ·
assistant 5005 · quality 5006 · reactor 5007 · analyzer 5008 · calibration 5009.

---

## What Each App Imports from src/

Every app also imports `src.events` (bus) and, if it runs a monitor loop,
`src.runstate`.

| App | Imports |
|---|---|
| `calibration` | `src.preprocess` (calib, raw_convert, sftp_sync) |
| `reduction` | `src.reduction.core` (Experiment, run_pipeline, find_new_raw_files), `src.manifest` |
| `viewer` | `src.plot_reduction` (read_folder, average_and_save, average_batch), `src.utils.read_dat_metadata`, `src.loop_naming`, `src.manifest`, `src.reactor.load_config` |
| `background` | `src.manifest`, `src.utils.read_dat_metadata`, `src.reactor.intake` (decide_intake), `src.loop_naming` |
| `quality` | `src.quality` (grade_profile, score_metrics), `src.manifest`, `src.utils.read_dat_metadata` |
| `analysis` | `src.analysis.core`, `src.analysis.io`, `src.analysis.atsas`, `src.manifest`, `src.utils.read_dat_metadata` |
| `analyzer` | `src.analysis.nanoparticle`, `src.optimizer` (campaign, io, diagnostics, plots), `src.ai.loop_advice`, `src.reactor.intake`, `src.simulator.ground_truth`, `src.manifest` |
| `reactor` | `src.reactor` (ReactorController, Recipe, load_config, intake), `src.notify`, `src.manifest` — `src.beamline` only indirectly, via `src/reactor/controller.py` |
| `assistant` | `src.ai.assistant`, `src.ai.hints` |
| `hub` | `src.proc_lifecycle`, `src.manifest`, `yaml`, Flask, optional `flask_sock` |

---

## Running the Platform

```bash
# Start everything (recommended — resolves the interpreter, checks deps)
./start_platform.sh                 # macOS / Linux
.\start_platform.ps1                # Windows PowerShell
start_platform.bat                  # Windows Anaconda Prompt

# Or start the hub manually (venv activated)
python hub/app.py

# Start a single app directly (for development)
python reduction/app.py             # or viewer / background / quality / analysis
                                    #  / analyzer / reactor / assistant / calibration
```

Full install instructions for all platforms: [QUICKSTART.md](QUICKSTART.md).

---

## Experiment Data Structure

Apps read and write data from a user-selected project folder:

```
<project_root>/
├── 2D/
│   ├── SAXS/{*.raw, *.raw.pdi}     # raw detector images + PDI metadata
│   ├── WAXS/{*.raw, *.raw.pdi}
│   └── *.csv                       # experiment-level CSV metadata (CSV mode)
├── poni/{*.poni, *.edf}            # pyFAI calibration + detector masks
├── 1D/
│   ├── SAXS/
│   │   ├── Reduction/              # reduction app output (*.dat)
│   │   ├── Averaged/               # viewer app averaging
│   │   ├── Subtracted/             # background app
│   │   │   ├── Good/               # Quality Gate: accepted
│   │   │   └── NeedsReview/        # Quality Gate: flagged
│   │   ├── Analysed/<Type>/        # analysis app: JSON + _fit.dat + PNG
│   │   └── Conditions/             # optimizer → reactor handoff
│   ├── WAXS/                       # same tree
│   └── QualityReports/             # Quality Gate CSV reports
├── config.yml                      # reduction configuration
├── reactor_limits.json             # persisted pump limits / calibration
├── manifest.json                   # cross-app provenance
└── .swaxs_state/                   # monitor state (src/runstate.py)
```

Note the CSV lives **inside `2D/`**, not at the project root:
`src/reduction/process_metadata.py` globs `raw_file.parent.parent`.

---

## config.yml Reference

```yaml
data_directory: "/path/to/2D"      # Path to 2D raw data folder
poni_directory: "/path/to/poni"    # Calibration and mask files

compound: "C2H4"                   # Sample molecular formula
energy_keV: 12                     # X-ray energy
density_g_cm3: 0.92                # Sample density
thickness: null                    # METRES (0.001 = 1 mm!); null = auto from transmission

mode: "SWAXS"                      # "SAXS", "WAXS", or "SWAXS"
metadata_format: "csv"             # "csv" or "pdi"

detector_shapes:
  saxs: [1043, 981]
  waxs: [195, 487]

poni_files: {saxs: "atT_SAXS.poni", waxs: "atT_WAXS.poni"}
mask_files: {saxs: "RT_SAXS_mask_03.edf", waxs: null}   # null = no mask

# Detector offsets and air-path measurements (0 = not measured)
i0_offset: 0.0
bstop_offset: 0.0
i0_air: 0.0
bstop_air: 0.0

# Integration — npt_radial and error_model are REQUIRED (KeyError if absent)
npt_radial: 1000
error_model: "poisson"
unit: "q_nm^-1"                    # DEFAULT nm⁻¹ (matches viewer/analysis)

# Optional integration controls
correct_solid_angle: true
radial_range_min: null             # both bounds needed, or neither
radial_range_max: null
azimuth_range_min: null
azimuth_range_max: null
dummy: null                        # masked-pixel value
delta_dummy: null
dark_files: null                   # dark-frame correction
flat_files: null                   # flat-field correction
output_directory: null             # default: <project>/1D
saxs_filename_prefix: ""
waxs_filename_prefix: ""

# Normalization — choose ONE mode (terms overlap; combos collapse w/ a warning)
normalization: ["bstop"]           # "bstop" (default) | "i0" | "absolute"
absolute_calibration_factor: 1.0   # K for "absolute" mode (water/GC standard)
polarization_factor: null          # ~0.95–0.99 for synchrotron; null = skip

beamline: {type: "1-5", data_format: "raw"}
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

Combining terms is a physics error, not a preference:
`["bstop","absolute"]` gives `NF = bstop²·d/K`, i.e. `I ∝ counts/(I0²T²·d)` —
meaningless. The full derivation is in `reduction/knowledge.md`.

### Transmission formula

When `i0_air` and `bstop_air` are provided (non-zero):

```
T_sample = (bstop_corr / i0_corr) / (bstop_air_corr / i0_air_corr)
```

where `*_corr = raw_value − offset`. When they are zero the simpler ratio `bstop_corr / i0_corr` is used.

Note: the computed corrections (transmission, thickness, normalization_factor) go
to `manifest.json` **only**. The `.dat` footer carries the raw beamline metadata
under `# METADATA INFORMATION (YML FORMAT)` and nothing else.

---

## Adding a New App

1. Create `myapp/app.py` — copy the `sys.path` block from any existing app, add your Flask routes
2. Create `myapp/templates/index.html` — the UI
3. Create `myapp/knowledge.md` — the AI assistant indexes it (see any existing one)
4. If the app needs new logic, add it to `src/`
5. **Register it in `apps.yml`** — a new entry with a unique `id`, `port`, `entry`, and
   optionally `knowledge`, `manifest_key`, `description`, `icon`, `color`.
   No `hub/app.py` change is needed; `POST /api/apps/reload` re-reads the registry live.
6. Add the port to all three launcher banners (`start_platform.sh`, `.ps1`, `.bat`)

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `flask` | Web framework for the hub and all nine apps |
| `flask-sock`, `simple-websocket`, `websocket-client` | WebSocket event bus (hub `:5000/ws`) |
| `pyFAI` | Detector calibration and azimuthal integration |
| `fabio` | Scientific image I/O (.raw, .edf, .cbf) |
| `xraydb` | X-ray absorption coefficients |
| `numpy`, `pandas` | Numerical processing, beamline CSV metadata |
| `matplotlib` | Plot generation (server-side, Agg backend) |
| `scipy` | Curve fitting (analysis, nanoparticle sizing) |
| `pyyaml` | `config.yml`, `apps.yml` parsing |
| `psutil` | Hub process lifecycle (start/stop/reclaim ports) |
| `paramiko` | SFTP data copy in the calibration app |
| `requests` | SPEC/bServer HTTP driver |
| `openpyxl` | .xlsx export from the analysis app |

Optional: `requirements-hardware.txt` (pyserial, pyepics) for the real rig;
`requirements-ai.txt` (anthropic, chromadb, …) for the assistant; `sasmodels` for
the analysis app's model tab. Everything optional degrades gracefully — the app
says what is missing and keeps working.

---

## manifest.json — Cross-App Data Contract

`src/manifest.py` manages a `manifest.json` at the project root. Each app reads
and writes only its own section, declared as `manifest_key` in `apps.yml`, and
must never overwrite another app's keys.

| App | Section |
|---|---|
| reduction, viewer | `files` |
| background | `background` |
| quality | `quality` |
| analysis, analyzer | `analyses` |
| reactor | `reactor.runs` |
| assistant | `ai_memory` |

`events` holds a rolling window of the last 100 bus events. The assistant reads
the whole manifest to answer questions about the experiment.

---

## Event Bus

The hub runs a WebSocket bus at `ws://localhost:5000/ws`. Envelope:

```json
{"type": "file.reduced", "source_app": "reduction",
 "timestamp": "...", "data": {...}, "ai_triggered": false}
```

Note `type` and `timestamp` — not `event_type`/`ts`. Emit via the `emit_*`
helpers in `src/events.py`; subscribe with `EventBusClient("myapp").on_event(cb).connect(retry=True)`.
Encoding failures drop the single event, not the connection.

---

## Where the docs are

| Doc | What |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | Install and first run, all platforms |
| [README.md](README.md) | What the platform does, app-by-app |
| [SECURITY.md](SECURITY.md) | AI token handling |
| [SYNC.md](SYNC.md) | Working across two laptops |
| `docs/ARCHITECTURE.md` | System design, event bus, manifest v2 |
| `docs/AUTONOMOUS_RUN_STEPS.md` | Operator runbook for a closed-loop run |
| `docs/audits/OPEN_DEFECTS.md` | **The register of known open defects** |
| `docs/audits/PRE_BEAMTIME_READINESS.md` | Go/no-go checklist |
| `docs/audits/BEAMLINE_SAFETY_AUDIT.md` | Every SPEC command the platform issues |
| `docs/REACTOR_SETUP.md`, `_HARDWARE_SETUP.md`, `_MAP.md` | Reactor software, rig, code map |
| `tools/BEAMLINE_TESTING.md` | Bench-test the beamline before a run |
| `docs/NOTIFICATIONS.md` | Slack + email run notifications |
| `docs/DESIGN_SYSTEM.md` | Shared UI tokens and per-app conformance |
| `docs/PARAMETER_SPACE_AND_CONVERGENCE.md` | Optimizer parameter space and convergence |
