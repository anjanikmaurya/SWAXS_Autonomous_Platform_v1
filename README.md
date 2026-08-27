# SWAXS Platform

A local, AI-assisted toolkit for processing **small- and wide-angle X-ray scattering (SAXS/WAXS)** data — from raw 2D detector images all the way to structural analysis. Built around the SSRL Beamline 1-5 workflow, but configurable for other setups.

Everything runs on your own machine. Your data never leaves your computer; the only outbound calls are to the Claude API, and only if you enable the AI Assistant.

### 🚀 New laptop? → [Quick start](#quick-start) · macOS · Windows (PowerShell **and** Anaconda Prompt) · Linux

---

## What it does

The platform is organized as nine small web apps, launched from one central hub. You move through them roughly in order:

| # | App | Port | What it's for |
|---|-----|------|---------------|
| ⚙️ | **Reduction & Correction** | 5001 | Convert raw 2D detector images → 1D I(q) curves (PyFAI integration, transmission/normalization corrections) |
| 📊 | **Data Viewer** | 5002 | Visualize 2D & 1D data, average repeated scans, stitch SAXS+WAXS |
| 🔬 | **Background Subtraction** | 5003 | Subtract buffer/background by keyword, scan-matching, or manual selection |
| ✅ | **Quality Gate** | 5006 | AI good/bad grading of subtracted profiles — scoring, auto-sort into Good/NeedsReview, frame selection |
| 📈 | **Data Analysis** | 5004 | Guinier, Porod, Kratky, pair-distance, peak fitting |
| 🧪 | **Flow Synthesis (reactor)** | 5007 | 5-pump flow reactor **and** beamline control: sets temperature (SPEC `csettemp`) and triggers 2D collection (shutter + `loopscan`) through the SPEC bServer, plus auto-flush |
| 🔁 | **Analyzer / Optimizer** | 5008 | Fits nanoparticle size / PDI / phase from subtracted SAXS and proposes the next synthesis conditions (Bayesian optimization) — the brain of the autonomous loop |
| 🤖 | **AI Assistant** | 5005 | Ask questions about your data, generate plots, get proactive quality hints |

A typical **data** session: **reduce → view & average → subtract background → quality-gate → analyze**, with the assistant available throughout.

### Autonomous closed loop (optional)

For self-driving nanoparticle synthesis at the beamline, the reactor and analyzer close a loop:

**Flow Synthesis** sets the temperature and flows for a recipe → triggers a SPEC 2D collection of the reacting sample (and a background during flush), tagged by `recipe_id` → the data pipeline reduces/averages/subtracts it → the **Analyzer** fits size/PDI/phase and the **optimizer** (`src/optimizer`) proposes the next conditions → the reactor runs them. Temperature and beamline actions go through the SPEC bServer; Stop/E-stop act on pumps only and never interrupt an in-progress X-ray collection. See the reactor doc set under `docs/` before a run.

---

## Quick start

About **10 minutes**, ~500 MB of downloads. Needs **Python 3.10 or newer** and
**git**. Pick your platform — each block is self-contained.

> ### ⚠ Install from `requirements-core.txt`, not `requirements.txt`
>
> `requirements.txt` is an old `pip freeze` of one developer's Mac. It carries
> ~1 GB the platform never imports (PyQt6, silx, pyopencl, torch) and pins exact
> versions with **no Windows build for Python 3.9** — pip then tries to compile
> numpy from source and the install dies with *"Microsoft Visual C++ 14.0 or
> greater is required"*. `requirements-core.txt` is 16 packages, verified to
> install from prebuilt wheels on Windows, macOS and Linux with no compiler.

<details open>
<summary><b>🍎 macOS</b></summary>

Open **Terminal** (⌘-Space → `Terminal`):

```bash
python3 --version          # need 3.10+; get it from python.org if missing

cd ~/Desktop
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

python3 -m venv venv
source venv/bin/activate               # prompt should now show (venv)

pip install --upgrade pip
pip install -r requirements-core.txt   # 1-3 minutes

./start_platform.sh
```

Open **http://localhost:5000**.

*Port 5000 busy?* macOS AirPlay Receiver uses it too. Usually harmless; if the
hub can't bind, run `SWAXS_HUB_PORT=5100 ./start_platform.sh` or turn AirPlay
Receiver off in System Settings → General → AirDrop & Handoff.

</details>

<details open>
<summary><b>🪟 Windows — PowerShell</b></summary>

First install [Python 3.11/3.12](https://www.python.org/downloads/windows/) —
**tick "Add python.exe to PATH"** on the installer's first screen, it is easy to
miss and nothing works without it — and [Git for Windows](https://git-scm.com/download/win).

Then in **PowerShell**:

```powershell
python --version           # must be 3.10+. 3.9 has no science wheels on Windows.

cd $HOME\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

python -m venv venv
.\venv\Scripts\Activate.ps1           # prompt should now show (venv)

python -m pip install --upgrade pip
python -m pip install -r requirements-core.txt

.\start_platform.ps1
```

Open **http://localhost:5000**.

*"running scripts is disabled on this system"?* Allow scripts for your own
account, once — then repeat the activate:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Or skip the policy entirely and use `start_platform.bat`.

</details>

<details open>
<summary><b>🐍 Windows — Anaconda Prompt</b></summary>

Open **Anaconda Prompt** from the Start menu (not PowerShell). Conda gives you
the science stack as prebuilt binaries — the most reliable route on Windows.

```bat
cd %USERPROFILE%\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

conda create -y -n swaxs python=3.12
conda activate swaxs                   REM prompt should now show (swaxs)

conda install -y -c conda-forge numpy scipy pandas matplotlib pyyaml h5py
python -m pip install -r requirements-core.txt

start_platform.bat
```

Open **http://localhost:5000**.

`start_platform.bat` also works by **double-clicking it** in File Explorer, and
needs no execution-policy change. Don't install into conda `base` — a broken
`base` breaks every other conda project you have.

</details>

<details open>
<summary><b>🐧 Linux</b></summary>

```bash
sudo apt install -y python3 python3-venv python3-pip git   # Debian/Ubuntu

cd ~
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements-core.txt

./start_platform.sh
```

Open **http://localhost:5000**. On a headless server the plots render
server-side, so no display is needed — forward the port with
`ssh -L 5000:localhost:5000 you@server`.

</details>

### Optional extras

Install only what you need. Everything degrades gracefully without them: the app
says what is missing and keeps working.

| You want | Run |
|---|---|
| The real reactor (pumps + EPICS temperature) | `pip install -r requirements-hardware.txt` |
| AI Assistant chat | `pip install anthropic` + a token (see below) |
| AI searchable knowledge base | `pip install -r requirements-ai.txt` — **pulls torch, ~2 GB** |
| Model fitting in the analysis app | `pip install sasmodels` |
| The test suite | `pip install pytest` then `pytest -q` |

### Enabling the AI Assistant (optional)

The assistant authenticates to **SLAC-managed AI services** via the enterprise
gateway. Request a token (ServiceNow, SLAC IT KB0015379) and put it in
**`~/.claude/settings.json`** — the SLAC-sanctioned location, shared with the
Claude Code CLI. The platform reads the token, endpoint and model from that one
file. **Never commit it, and never put it in `.env`.** Full steps in
`SECURITY.md`; in short:

```bash
mkdir -p ~/.claude && chmod 700 ~/.claude
nano ~/.claude/settings.json      # paste the KB0015379 JSON, insert your token
chmod og-rwx ~/.claude/settings.json
```

You must be on the SLAC network or VPN. Without a token every data-processing app
works normally — only the AI features are disabled.

### First run

1. **Pick your project folder** — top-right of the hub page (layout below). No
   data yet? Point it at the bundled `Demo_Data/` folder to click around safely.
2. **Start an app** with ▶, wait for the green dot, then **↗ Open**. Work left to
   right through the table above.
3. **Stop** with ■ on a card, or `Ctrl-C` in the terminal to close the hub — which
   closes every app with it.

You can pre-select a project folder: `./start_platform.sh /path/to/experiment`
(or `.\start_platform.ps1 D:\data\Auto_Run`).

### Starting again later

You install once. After that, in a new terminal:

| | |
|---|---|
| **macOS / Linux** | `cd <repo>` → `source venv/bin/activate` → `./start_platform.sh` |
| **Windows PowerShell** | `cd <repo>` → `.\venv\Scripts\Activate.ps1` → `.\start_platform.ps1` |
| **Anaconda Prompt** | `cd <repo>` → `conda activate swaxs` → `start_platform.bat` |

Forgetting to activate the environment is the most common day-two problem — it
shows up as `ModuleNotFoundError: No module named 'flask'`.

**Something went wrong?** [QUICKSTART.md](QUICKSTART.md) has the same steps with
more explanation plus a troubleshooting section ordered by how often each problem
actually happens.

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
│   ├── SAXS/
│   │   ├── {Reduction,Averaged}/
│   │   └── Subtracted/{Good,NeedsReview}/   # NeedsReview/Good sorted by Quality Gate
│   ├── WAXS/
│   │   ├── {Reduction,Averaged}/
│   │   └── Subtracted/{Good,NeedsReview}/
│   └── QualityReports/       # Quality Gate CSV reports + accepted lists
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
| Assistant/Quality Gate say the AI token isn't set | Add your token to `~/.claude/settings.json` (`ANTHROPIC_AUTH_TOKEN`, endpoint, model — see `SECURITY.md`) and restart. Do **not** put it in `.env`. You must also be on the SLAC network/VPN. |
| "Bus" badge in the hub stays grey | `flask-sock` not installed (`pip install flask-sock`), or the hub was started a different way. The apps still work; only live events are affected. |
| Reduction error: `'i0' not found in metadata` | `metadata_format` in `config.yml` doesn't match your files (`pdi` vs `csv`), or the metadata lacks an `i0`/`bstop` field. |
| Transmission > 1.0 warning | Check `i0_air`/`bstop_air` and the offset values in `config.yml`. |
| Negative intensities after reduction | Check `i0_offset` / `bstop_offset` (should be ≤ the dark-current reading with the shutter closed). |
| An app card shows "Starting…" forever, then "Not responding" | Read `logs/<app>.log` (the previous run is kept as `logs/<app>.log.1`). Usually a bad path in `config.yml` or a missing `.poni`. |
| An app card shows "⚠ CRASHED" | The card names the reason and the last log lines. Fix it, then press ▶ Start again. |
| `ModuleNotFoundError: No module named 'flask'` | The virtual environment isn't activated — your prompt should show `(venv)` or `(swaxs)`. See [Starting again later](#starting-again-later). |
| Red `Microsoft Visual C++ 14.0 or greater is required` on Windows | You used `requirements.txt`, or your Python is 3.9 or older. Use `requirements-core.txt` and Python 3.11/3.12. You do **not** need Visual Studio. |
| `.\start_platform.ps1 ... running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `start_platform.bat`. |
| Pipeline runs but no averaged files appear | Frames/batch is larger than the frames one acquisition delivers, so a batch can never complete. The viewer log says so — set frames/batch to match `spec.frames`. |

More, with fuller explanations: **[QUICKSTART.md § Troubleshooting](QUICKSTART.md#troubleshooting)**.

---

## For developers

- **`CLAUDE.md`** — developer guide and full `config.yml` reference.
- **`docs/`** — extended documentation: `ARCHITECTURE.md` (system design), `DESIGN_SYSTEM.md`, app specs, and `docs/audits/` (point-in-time correctness/safety audits).
- **Reactor / beamtime docs** — `docs/REACTOR_SETUP.md` (software install/run), `docs/REACTOR_HARDWARE_SETUP.md` (fluidics + temperature + beamline wiring), `docs/REACTOR_MAP.md` (code map / troubleshooting), `tools/BEAMLINE_TESTING.md` (bench-test runbook), and `docs/audits/PRE_BEAMTIME_READINESS.md` + `BEAMLINE_SAFETY_AUDIT.md`.
- **`apps.yml`** — the app registry. Add an app here and the hub picks it up; no hub code changes needed.
- **`check_imports.py`** — `python check_imports.py` audits which `src/` modules each app uses.
- **Launchers** — `start_platform.sh` (macOS/Linux), `start_platform.ps1` (PowerShell), `start_platform.bat` (Anaconda Prompt / double-click). All three start `hub/app.py`; the Windows two also check the Python version and that the dependencies are installed.
- **Dependencies** — `requirements-core.txt` runs the platform; `requirements-hardware.txt` and `requirements-ai.txt` are opt-in extras. `tests/test_install_requirements.py` fails if a new import is added without being declared, or if an exact pin creeps back in.

**The one rule:** all science and data logic lives in `src/`. Each `app.py` is a thin Flask shell (routing only).

---

## License

See `LICENSE`.
