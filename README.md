# SWAXS Platform

A local, AI-assisted toolkit for processing **small- and wide-angle X-ray scattering (SAXS/WAXS)** data — from raw 2D detector images all the way to structural analysis. Built around the SSRL Beamline 1-5 workflow, but configurable for other setups.

Everything runs on your own machine. Your data never leaves your computer; the only outbound calls are to the Claude API, and only if you enable the AI Assistant.

---

## What it does

The platform is organized as five small web apps, launched from one central hub. You move through them roughly in order:

| # | App | Port | What it's for |
|---|-----|------|---------------|
| ⚙️ | **Reduction & Correction** | 5001 | Convert raw 2D detector images → 1D I(q) curves (PyFAI integration, transmission/normalization corrections) |
| 📊 | **Data Viewer** | 5002 | Visualize 2D & 1D data, average repeated scans, stitch SAXS+WAXS |
| 🔬 | **Background Subtraction** | 5003 | Subtract buffer/background by keyword, scan-matching, or manual selection |
| 📈 | **Data Analysis** | 5004 | Guinier, Porod, Kratky, pair-distance, peak fitting |
| 🤖 | **AI Assistant** | 5005 | Ask questions about your data, generate plots, get proactive quality hints |

A typical session: **reduce → view & average → subtract background → analyze**, with the assistant available throughout.

---

## Quick start

### 1. Requirements

- Python 3.12 (3.9+ may work with minor changes)
- [`uv`](https://docs.astral.sh/uv/) recommended (the platform uses `uv run`); plain `python` also works
- Git

### 2. Get the code and install dependencies

```bash
git clone https://github.com/anjanikmaurya/SWAXS_data_correction_reduction_averaging
cd SWAXS_data_correction_reduction_averaging

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# Install everything (this takes a few minutes)
pip install -r requirements.txt
```

### 3. (Optional) Enable the AI Assistant

The AI Assistant authenticates to **SLAC-managed AI services** via the enterprise
gateway. Request a token (ServiceNow, see SLAC IT KB0015379) and put it in
**`~/.claude/settings.json`** — the SLAC-sanctioned location, shared with the
Claude Code CLI. The SWAXS app reads the token, endpoint, and model from that one
file automatically. **Never commit the token or put it in `.env`.** Full steps
are in **`SECURITY.md`**; in short:

```bash
mkdir -p ~/.claude && chmod 700 ~/.claude
nano ~/.claude/settings.json      # paste the KB0015379 JSON, insert your token
chmod og-rwx ~/.claude/settings.json
```

You must be **on the SLAC network or VPN**. Without a token the other four apps
work normally — only the assistant is disabled.

### 4. Start the platform

```bash
./start_platform.sh
```

Then open **http://localhost:5000** in your browser. The hub lets you:

1. **Pick your project folder** (the folder holding your experiment data — see layout below).
2. **Start any app** with its ▶ button, then **Open** it.

You can also pre-select a project folder: `./start_platform.sh /path/to/experiment`.

---

## Organizing your experiment data

Apps read and write inside a single project folder. The expected layout:

```
<project_root>/
├── 2D/
│   ├── SAXS/                 # *.raw detector images + *.raw.pdi metadata
│   └── WAXS/                 # *.raw detector images + *.raw.pdi metadata
├── poni/                     # PyFAI calibration (*.poni) + detector masks (*.edf)
├── config.yml                # Reduction settings (see below)
├── 1D/                       # Created by the apps:
│   ├── SAXS/{Reduction,Averaged}/
│   └── WAXS/{Reduction,Averaged}/
└── manifest.json             # Auto-managed shared state across apps
```

CSV metadata (one `*.csv` per scan at the `2D/` level) is also supported instead of `.raw.pdi` — set `metadata_format` accordingly in `config.yml`.

### config.yml essentials

```yaml
data_directory: "/path/to/2D"
poni_directory: "/path/to/poni"

compound: "C2H4"          # sample formula (for absorption / thickness)
energy_keV: 12
density_g_cm3: 0.92
thickness: null           # null = auto-derive from transmission

mode: "SWAXS"             # "SAXS", "WAXS", or "SWAXS"
metadata_format: "pdi"    # "pdi" or "csv"

detector_shapes:
  saxs: [1043, 981]
  waxs: [195, 487]

poni_files: { saxs: "atT_SAXS.poni", waxs: "atT_WAXS.poni" }
mask_files: { saxs: "RT_SAXS_mask_03.edf", waxs: null }

npt_radial: 1000
error_model: "poisson"
```

See `CLAUDE.md` for the full config reference, including normalization terms, air-path transmission, dark/flat frames, and polarization.

---

## How the apps talk to each other

- **`manifest.json`** at the project root is the shared record of every file produced and every analysis run. Each app writes only its own section.
- The **hub runs an event bus** (WebSocket). When one app finishes something (e.g. reduces a file), it announces it; the others — and the AI Assistant — can react. The hub UI shows these events live.

You don't need to manage any of this; it happens automatically.

---

## The AI Assistant

When enabled, the assistant can:

- Answer questions about what's been processed (it reads `manifest.json`).
- Run analyses and generate plots inline (Guinier, Kratky, Porod, etc.).
- Surface **proactive hints** — e.g. a Guinier range outside the valid qRg window, a possible aggregation upturn, an I₀ outlier frame, or an unusual background scale factor.
- Learn from corrections you make, and remember per-user, per-project, and per-beamline context across sessions.

Its domain knowledge lives in `ai_knowledge/` and the per-app `knowledge.md` files, which are indexed into a local vector database on first run.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Assistant says "ANTHROPIC_API_KEY is not set" | Add the key to `.env` in the project root and restart. |
| "Bus" badge in the hub stays grey | `flask-sock` not installed (`pip install flask-sock`), or the hub was started a different way. The apps still work; only live events are affected. |
| Reduction error: `'i0' not found in metadata` | `metadata_format` in `config.yml` doesn't match your files (`pdi` vs `csv`), or the metadata lacks an `i0`/`bstop` field. |
| Transmission > 1.0 warning | Check `i0_air`/`bstop_air` and the offset values in `config.yml`. |
| Negative intensities after reduction | Check `i0_offset` / `bstop_offset` (should be ≤ the dark-current reading with the shutter closed). |
| An app card shows "Starting…" forever | Open the app's terminal output, or start it directly for full logs: `uv run reduction/app.py`. |

---

## For developers

- **`CLAUDE.md`** — developer guide and full `config.yml` reference.
- **`docs/`** — extended documentation: `ARCHITECTURE.md` (system design), `GETTING_STARTED.md`, `DESIGN_SYSTEM.md`, app specs, and `docs/audits/` (point-in-time correctness/UX audits).
- **`apps.yml`** — the app registry. Add an app here and the hub picks it up; no hub code changes needed.
- **`check_imports.py`** — `uv run check_imports.py` audits which `src/` modules each app uses.

**The one rule:** all science and data logic lives in `src/`. Each `app.py` is a thin Flask shell (routing only).

---

## License

See `LICENSE`.
