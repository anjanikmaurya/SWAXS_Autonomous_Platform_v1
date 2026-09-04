# SWAXS Platform

A local, AI-assisted toolkit for processing **small- and wide-angle X-ray scattering (SAXS/WAXS)** data — from raw 2D detector images all the way to structural analysis. Built around the SSRL Beamline 1-5 workflow, but configurable for other setups.

Everything runs on your own machine — the apps are local web servers and your data stays in the folder you point them at. The platform does make outbound connections, all of them optional and off by default: the Claude API (AI Assistant and AI quality grading), SFTP to a beamline host (Calibration's data sync), HTTP to the SPEC bServer (real beamline control), and Slack webhooks or SMTP (run notifications).

### 🚀 New machine? → **[Quick start](#quick-start)** — three steps, ~10 minutes.
Works on macOS, Windows (PowerShell, Anaconda Prompt or cmd) and Linux.

---

## What it does

The platform is organized as nine small web apps, launched from one central hub. You move through them roughly in order:

| # | App | Port | What it's for |
|---|-----|------|---------------|
| 🎯 | **Calibration & Raw Prep** | 5101 | Convert calibrant `.raw` → CBF and generate the pyFAI `.poni` files everything downstream needs; optional SFTP pull from the beamline host |
| 🌀 | **Reduction & Correction** | 5102 | Convert raw 2D detector images → 1D I(q) curves (PyFAI integration, transmission/normalization corrections) |
| 📊 | **Visualisation & Average** | 5103 | Visualise 2D & 1D data, average repeated scans, view SAXS and WAXS together |
| ➖ | **Background Subtraction** | 5104 | Subtract buffer/background by keyword, scan-matching, or manual selection |
| 🚦 | **Quality Gate** | 5105 | AI good/bad grading of subtracted profiles — scoring, auto-sort into Good/NeedsReview, frame selection |
| 📈 | **Data Analysis** | 5106 | Guinier, Porod, Kratky, pair-distance, peak fitting |
| 🧭 | **Auto-Fit & Optimiser** | 5107 | Fits nanoparticle size / PDI / phase from subtracted SAXS and proposes the next synthesis conditions (Bayesian optimization) — the brain of the autonomous loop |
| 🔁 | **Autonomous Synthesis (reactor)** | 5108 | 5-pump flow reactor **and** beamline control: sets temperature (SPEC `csettemp`) and triggers 2D collection (shutter + a configurable collect command, default `ct`) through the SPEC bServer, plus auto-flush |
| 🤖 | **Tassone Group** (AI assistant) | 5109 | Ask questions about your data, generate plots, get proactive quality hints |

A typical **data** session: **reduce → view & average → subtract background → quality-gate (optional) → analyze**, with the assistant available throughout.

### Autonomous closed loop (optional)

For self-driving nanoparticle synthesis at the beamline, the reactor and analyzer close a loop:

**Autonomous Synthesis** sets the temperature and flows for a recipe → triggers a SPEC 2D collection of the reacting sample (and a background during flush), tagged by `recipe_id` → the data pipeline reduces/averages/subtracts it → the **Analyzer** fits size/PDI/phase and the **optimizer** (`src/optimizer`) proposes the next conditions → the reactor runs them. Temperature and beamline actions go through the SPEC bServer; Stop/E-stop act on pumps only and never interrupt an in-progress X-ray collection. See the reactor doc set under `docs/` before a run.

---

## Quick start

You install once, in about **10 minutes**. It is the same three steps on every
platform:

**① get Python and git → ② download the code and install → ③ start the hub.**

If a command fails, the exact error is almost certainly in
[QUICKSTART.md](QUICKSTART.md#troubleshooting).

> **⚠ One thing that matters more than anything else on this page:**
> install from **`requirements-core.txt`**, never `requirements.txt`.
> `requirements.txt` is an old snapshot of one developer's Mac — it drags in ~1 GB
> the platform never uses and pins versions with no Windows build, so pip tries to
> *compile* numpy and dies with *"Microsoft Visual C++ 14.0 or greater is
> required"*. `requirements-core.txt` is 16 packages that install from prebuilt
> wheels everywhere, with no compiler. This is what broke the first Windows
> install, and it is the one line people get wrong.

### ① Get Python and git

You need **Python 3.10 or newer** (3.11 or 3.12 recommended) and **git**.
Check with `python --version` — on macOS and Linux, `python3 --version`.

| Your machine | How to get them |
|---|---|
| **macOS** | Python from [python.org](https://www.python.org/downloads/macos/). git comes with Xcode command-line tools — running `git --version` offers to install it. |
| **Windows** | Python from [python.org](https://www.python.org/downloads/windows/) — **tick "Add python.exe to PATH"** on the installer's first screen. It is easy to miss and nothing works without it. Then [Git for Windows](https://git-scm.com/download/win). |
| **Windows + Anaconda** | You already have both. Use the **Anaconda Prompt** from the Start menu, not PowerShell. If git is missing: `conda install -y git`. |
| **Linux** | `sudo apt install -y python3 python3-venv python3-pip git` (Debian/Ubuntu) · `sudo dnf install -y python3 python3-pip git` (Fedora/RHEL) |

### ② Download the code and install

Copy the whole block for your setup and paste it into one terminal window. The
`(venv)` or `(swaxs)` that appears in your prompt is how you know it worked.

<details open>
<summary><b>🍎 macOS &nbsp;·&nbsp; 🐧 Linux</b> — Terminal</summary>

```bash
cd ~/Desktop                                   # or wherever you keep projects
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

python3 -m venv venv
source venv/bin/activate                       # prompt now shows (venv)

pip install --upgrade pip
pip install -r requirements-core.txt           # 1-3 minutes
```

</details>

<details open>
<summary><b>🪟 Windows</b> — PowerShell</summary>

```powershell
cd $HOME\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

python -m venv venv
.\venv\Scripts\Activate.ps1                    # prompt now shows (venv)

python -m pip install --upgrade pip
python -m pip install -r requirements-core.txt
```

Blocked with *"running scripts is disabled on this system"*? Allow scripts for
your own account once, then re-run the activate line:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

</details>

<details open>
<summary><b>🐍 Windows + Anaconda</b> — Anaconda Prompt</summary>

The most reliable route on Windows: conda ships the science stack as prebuilt
binaries, and it needs no execution-policy change.

```bat
cd %USERPROFILE%\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

conda create -y -n swaxs python=3.12
conda activate swaxs                           REM prompt now shows (swaxs)

conda install -y -c conda-forge numpy scipy pandas matplotlib pyyaml h5py
python -m pip install -r requirements-core.txt
```

Create the `swaxs` environment as shown — don't install into conda `base`, where
a breakage would affect every other conda project on the machine.

</details>

### ③ Start the hub

In the same terminal, still with the environment active:

| | Run | Notes |
|---|---|---|
| **macOS / Linux** | `./start_platform.sh` | |
| **Windows PowerShell** | `.\start_platform.bat` | a `.bat` runs fine from PowerShell, and needs no execution-policy change |
| **Anaconda Prompt / cmd** | `start_platform.bat` | also works by **double-clicking** it in File Explorer |

Then open **http://localhost:5100** in your browser. That's the hub.

The launcher tells you what it found — interpreter, Python version, whether the
dependencies are installed — and stops with a clear message instead of a stack
trace if something is missing.

<details open>
<summary>Two things that occasionally get in the way</summary>

- **Port 5100 is busy.** The hub uses 5100 (apps 5101-5109) precisely to dodge
  macOS AirPlay Receiver, which owns 5000. If 5100 itself is taken, run
  `SWAXS_HUB_PORT=5200 ./start_platform.sh`, or switch AirPlay Receiver off in
  System Settings → General → AirDrop & Handoff.
- **Headless Linux server.** Plots render server-side, so no display is needed —
  forward the port: `ssh -L 5100:localhost:5100 you@server`.

</details>

### Starting it again tomorrow

Installing was a one-off. From then on it is two lines in a new terminal:

| | |
|---|---|
| **macOS / Linux** | `cd <repo>` → `source venv/bin/activate` → `./start_platform.sh` |
| **Windows PowerShell** | `cd <repo>` → `.\venv\Scripts\Activate.ps1` → `.\start_platform.bat` |
| **Anaconda Prompt** | `cd <repo>` → `conda activate swaxs` → `start_platform.bat` |

Forgetting to activate the environment is the most common day-two problem. It
looks like `ModuleNotFoundError: No module named 'flask'` — activate, then retry.

### Your first session

1. **Pick your project folder** — top-right of the hub page. Any folder with the
   `2D/SAXS` layout [shown below](#organizing-your-experiment-data) works; the
   apps create the `1D/` tree themselves.
2. **Start an app** with ▶, wait for the green dot, then **↗ Open**. Work left to
   right through the app table above.
3. **Stop** one app with ■, or press `Ctrl-C` in the terminal to close the hub —
   which closes every app with it.

To skip step 1 next time, pass the folder to the launcher:
`./start_platform.sh /path/to/experiment` or `start_platform.bat D:\data\Auto_Run`.

### Optional add-ons

Nothing here is needed to process data. Each one degrades gracefully — the app
says what is missing and keeps working.

| You want | Run |
|---|---|
| The real reactor (pumps + EPICS temperature) | `pip install -r requirements-hardware.txt` |
| AI Assistant chat | `pip install anthropic` + a token (below) |
| AI searchable knowledge base | `pip install -r requirements-ai.txt` — **pulls torch, ~2 GB** |
| Model fitting in the analysis app | `pip install sasmodels` |
| The test suite | `pip install pytest` then `pytest -q` |

**AI Assistant token.** The assistant reaches SLAC-managed AI services through the
enterprise gateway, so it needs a token (request via ServiceNow, SLAC IT
KB0015379) and the SLAC network or VPN. Put it in **`~/.claude/settings.json`** —
the sanctioned location, shared with the Claude Code CLI — and **never in `.env`
or anywhere inside the repo**. Steps: [SECURITY.md](SECURITY.md). Without a token
every data-processing app works normally; only the AI features are off.

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
│   │   ├── Subtracted/{Good,NeedsReview}/   # NeedsReview/Good sorted by Quality Gate
│   │   └── Results/           # everything kept after the fact
│   │       ├── Fit/               # per-fit PNG + .dat, every analyzed profile (cross-check after beamtime)
│   │       ├── QualityReports/    # Quality Gate CSV reports + accepted lists
│   │       └── campaign_<id>/     # Auto-Fit & Optimiser: final figures, written when a campaign ends
│   ├── WAXS/
│   │   ├── {Reduction,Averaged}/
│   │   └── Subtracted/{Good,NeedsReview}/
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

See [`CLAUDE.md`](CLAUDE.md) for the full config reference, including normalization terms, air-path transmission, dark/flat frames, and polarization.

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
| "Bus" badge in the hub stays grey | `flask-sock` is missing — it is in `requirements-core.txt`, so this only happens after a partial install. The apps still work; only live events are affected. |
| Reduction error: `'i0' not found in metadata` | `metadata_format` in `config.yml` doesn't match your files (`pdi` vs `csv`), or the metadata lacks an `i0`/`bstop` field. |
| Transmission > 1.0 warning | Check `i0_air`/`bstop_air` and the offset values in `config.yml`. |
| Negative intensities after reduction | Check `i0_offset` / `bstop_offset` (should be ≤ the dark-current reading with the shutter closed). |
| An app card shows "Starting…" forever, then "Not responding" | Read `logs/<app>.log` (the previous run is kept as `logs/<app>.log.1`). Usually a bad path in `config.yml` or a missing `.poni`. |
| An app card shows "⚠ CRASHED" | The card names the reason and the last log lines. Fix it, then press ▶ Start again. |
| `ModuleNotFoundError: No module named 'flask'` | The virtual environment isn't activated — your prompt should show `(venv)` or `(swaxs)`. See [Starting again later](#starting-again-later). |
| Red `Microsoft Visual C++ 14.0 or greater is required` on Windows | You used `requirements.txt`, or your Python is 3.9 or older. Use `requirements-core.txt` and Python 3.11/3.12. You do **not** need Visual Studio. |
| `... running scripts is disabled` (activating the venv) | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. The launcher itself is a `.bat` and never needs this. |
| Pipeline runs but no averaged files appear | Frames/batch is larger than the frames one acquisition delivers, so a batch can never complete. The Visualisation & Average log says so — set frames/batch to match `spec.frames`. |

More, with fuller explanations: **[QUICKSTART.md § Troubleshooting](QUICKSTART.md#troubleshooting)**.

---

## For developers

### Plugging your own program into the pipeline

You are not limited to the nine built-in apps. Every stage hands off through a
**folder of files** plus `manifest.json`, so a program of your own — in any
language — can read one stage's output and write the next stage's input,
without touching this codebase and with no plugin API to learn.

The common case is **your own ML model in place of Auto-Fit & Optimiser**: it
watches `1D/SAXS/Subtracted/Good/` for newly subtracted profiles, predicts the
structure, and drops the next conditions into `1D/SAXS/Conditions/`, which the
Autonomous Synthesis app already polls. Leave app 5107 stopped and the loop closes
through your model instead. Three folder paths and one small file format.

Other ways in: subscribe to the WebSocket event bus for push notifications
instead of polling, import `src/` directly as a Python library, write your
results into `manifest.json` for provenance, or register your program in
`apps.yml` so it gets its own hub card with start/stop.

**→ [docs/INTEGRATING_YOUR_OWN_CODE.md](docs/INTEGRATING_YOUR_OWN_CODE.md)** —
exact paths, the condition-file format and its required fields and bounds, a
working ~30-line skeleton, every integration surface ranked by how stable it
is, and the safety notes for anything that can drive the reactor.

### Reference

- **`CLAUDE.md`** — developer guide and full `config.yml` reference.
- **`docs/`** — extended documentation: `ARCHITECTURE.md` (system design), `DESIGN_SYSTEM.md`, app specs, and `docs/audits/` (point-in-time correctness/safety audits).
- **Reactor / beamtime docs** — `docs/REACTOR_SETUP.md` (software install/run), `docs/REACTOR_HARDWARE_SETUP.md` (fluidics + temperature + beamline wiring), `docs/REACTOR_MAP.md` (code map / troubleshooting), `tools/BEAMLINE_TESTING.md` (bench-test runbook), and `docs/audits/PRE_BEAMTIME_READINESS.md` + `BEAMLINE_SAFETY_AUDIT.md`.
- **`apps.yml`** — the app registry. Add an app here and the hub picks it up; no hub code changes needed.
- **`check_imports.py`** — `python check_imports.py` audits which `src/` modules each app uses.
- **Launchers** — `start_platform.sh` (macOS/Linux) and `start_platform.bat` (every Windows shell, including PowerShell, plus double-click). Both resolve your venv/conda interpreter, refuse a Python older than 3.10, probe the dependencies, and then start `hub/app.py` with that interpreter — the same one the hub uses to launch every sub-app.
- **Dependencies** — `requirements-core.txt` runs the platform; `requirements-hardware.txt` and `requirements-ai.txt` are opt-in extras. `tests/test_install_requirements.py` fails if a new import is added without being declared, or if an exact pin creeps back in.

**The one rule:** all science and data logic lives in `src/`. Each `app.py` is a thin Flask shell (routing only).

---

## License

See `LICENSE`.
