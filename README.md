# <img src="assets/icons/docs/swaxs-ui-mark.svg" width="32" height="32" alt=""> SWAXS Platform

A local, AI-assisted toolkit for **small- and wide-angle X-ray scattering
(SAXS/WAXS)**: from raw 2D detector images to 1D curves, background subtraction,
structural analysis and, optionally, a closed autonomous loop that runs a flow
reactor at the beamline. Built around the SSRL Beamline 1-5 workflow, configurable
for other setups.

Everything runs on your own machine as local web pages, and your data stays in the
project folder you choose. Outbound connections are all optional: the Claude API
(AI assistant and AI quality grading), SFTP to a beamline host (Calibration's data
copy), HTTP to the SPEC bServer (real beamline control) and Slack (notifications).

**New machine?** Go to [Quick start](#quick-start) below (about 10 minutes, macOS,
Windows or Linux). Full guide with troubleshooting: [QUICKSTART.md](QUICKSTART.md).

---

## The apps

One hub (**http://localhost:5100**) starts, stops and monitors ten apps. You
normally move through them in this order:

| | App | Port | What it is for |
|---|---|---|---|
| <img src="assets/icons/docs/swaxs-calibration.svg" width="20" height="20" alt=""> | **Geometry Calibration** | 5101 | Convert calibrant `.raw` → CBF, make the pyFAI `.poni` geometry files, optional SFTP copy from the beamline host |
| <img src="assets/icons/docs/swaxs-reduction.svg" width="20" height="20" alt=""> | **Reduction & Correction** | 5102 | 2D detector images → 1D I(q) (pyFAI integration, transmission and normalization corrections) |
| <img src="assets/icons/docs/swaxs-visualisation.svg" width="20" height="20" alt=""> | **Visualisation & Average** | 5103 | View 2D and 1D data, average repeated frames, show SAXS and WAXS on one axis |
| <img src="assets/icons/docs/swaxs-subtraction.svg" width="20" height="20" alt=""> | **Background Subtraction** | 5104 | Subtract buffer/background by keyword, scan matching or manual choice |
| <img src="assets/icons/docs/swaxs-quality.svg" width="20" height="20" alt=""> | **Quality Gate** | 5105 | AI good/bad grading of subtracted curves, sorted into `Good/` and `NeedsReview/` |
| <img src="assets/icons/docs/swaxs-analysis.svg" width="20" height="20" alt=""> | **Data Analysis** | 5106 | Guinier, Porod, Kratky, p(r), model fitting (sasmodels), peaks |
| <img src="assets/icons/docs/swaxs-autofit.svg" width="20" height="20" alt=""> | **Autonomous Analyser** | 5107 | Fits nanoparticle size, PDI and phase, and proposes the next synthesis conditions (Bayesian optimisation) |
| <img src="assets/icons/docs/swaxs-synthesis.svg" width="20" height="20" alt=""> | **Autonomous Synthesis** | 5108 | Five-pump flow reactor plus SPEC beamline control (temperature, 2D collection, auto flush); starts in Simulation |
| <img src="assets/icons/docs/swaxs-guinier-ai.svg" width="20" height="20" alt=""> | **Guinier** | 5109 | AI assistant: ask about your data, get plots and hints |
| <img src="assets/icons/docs/swaxs-watch.svg" width="20" height="20" alt=""> | **Autonomous Watch** | 5110 | Watches the autonomous loop for stalls and sends every platform notification (Slack) |

A typical **data** session: reduce → average → subtract → (quality gate) →
analyse, with the assistant available throughout.

### The autonomous loop (optional)

Autonomous Synthesis runs a recipe (temperature and flows) and triggers a SPEC 2D
collection of the sample and its background, both tagged with the `recipe_id` →
the data apps reduce, average and subtract it → the Autonomous Analyser fits size
and PDI and writes the next condition → the reactor runs it. Stop and E-stop act on
the pumps only and never interrupt an X-ray collection. Turning **Run
autonomously** off pauses after the current condition (the queue is kept); that
paused window is when beamline settings can be changed.

To rehearse the whole loop on a laptop with synthetic data, see
[docs/SIMULATED_2D_DATA.md](docs/SIMULATED_2D_DATA.md). For a real run, follow
[docs/AUTONOMOUS_RUN_STEPS.md](docs/AUTONOMOUS_RUN_STEPS.md).

---

## Quick start

Install once, then start the hub. If a command fails, the fix is almost certainly
in [QUICKSTART.md § Troubleshooting](QUICKSTART.md#troubleshooting).

> **Install from `requirements-core.txt`.** There is no `requirements.txt`. If an
> old clone still has one, delete it: it makes pip try to compile numpy on Windows
> ("Microsoft Visual C++ 14.0 or greater is required").

### 1. Get Python and git

You need **Python 3.10 or newer** (3.11 or 3.12 recommended) and **git**.

| Your machine | How to get them |
|---|---|
| **macOS** | Python from [python.org](https://www.python.org/downloads/macos/). `git --version` offers to install git if it is missing. |
| **Windows** | Python from [python.org](https://www.python.org/downloads/windows/), **tick "Add python.exe to PATH"** on the first installer screen. Then [Git for Windows](https://git-scm.com/download/win). |
| **Windows + Anaconda** | Use the **Anaconda Prompt** (not PowerShell). If git is missing: `conda install -y git`. |
| **Linux** | `sudo apt install -y python3 python3-venv python3-pip git` (Debian/Ubuntu) or `sudo dnf install -y python3 python3-pip git` (Fedora/RHEL) |

### 2. Download the code and install

Paste the block for your setup into one terminal. The `(venv)` or `(swaxs)` in your
prompt shows it worked.

<details open>
<summary><b>macOS / Linux</b> (Terminal)</summary>

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
<summary><b>Windows</b> (PowerShell)</summary>

```powershell
cd $HOME\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

python -m venv venv
.\venv\Scripts\Activate.ps1                    # prompt now shows (venv)

python -m pip install --upgrade pip
python -m pip install -r requirements-core.txt
```

Blocked with "running scripts is disabled on this system"? Run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then the activate line
again.

</details>

<details open>
<summary><b>Windows + Anaconda</b> (Anaconda Prompt)</summary>

```bat
cd %USERPROFILE%\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

conda create -y -n swaxs python=3.12
conda activate swaxs                           REM prompt now shows (swaxs)

conda install -y -c conda-forge numpy scipy pandas matplotlib pyyaml h5py
python -m pip install -r requirements-core.txt
```

Use the named `swaxs` environment, not conda `base`.

</details>

### 3. Start the hub

| | Run |
|---|---|
| **macOS / Linux** | `./start_platform.sh` |
| **Windows PowerShell** | `.\start_platform.bat` |
| **Anaconda Prompt / cmd** | `start_platform.bat` (or double-click it) |

Then open **http://localhost:5100**. The launcher reports the interpreter and
Python version it found and stops with a clear message if a package is missing.
Next time, it is: `cd` into the folder, activate (`source venv/bin/activate`,
`.\venv\Scripts\Activate.ps1` or `conda activate swaxs`), run the launcher.

### 4. First session

1. Click the project folder box in the hub's top bar and choose your experiment
   folder (layout [below](#organizing-your-experiment-data)). Type your name in the
   **Operator** box next to it; every app records it with its results.
2. Press **Start** on an app card, wait for the green dot, press **Open**.
3. Press **Stop** on a card, or `Ctrl-C` in the terminal to close the hub and all
   apps.

Optional extras (real reactor hardware, AI assistant, Slack alerts) are listed in
[QUICKSTART.md § Optional extras](QUICKSTART.md#optional-extras). The AI token goes
in `~/.claude/settings.json`, never in the repo: see [SECURITY.md](SECURITY.md).

---

## Organizing your experiment data

All apps read and write inside one project folder:

```
<project_root>/
├── 2D/
│   ├── SAXS/                 # *.raw detector images (+ *.raw.pdi metadata)
│   ├── WAXS/
│   └── *.csv                 # beamline metadata, if metadata_format: "csv"
├── poni/                     # pyFAI calibration (*.poni) + detector masks (*.edf)
├── config.yml                # reduction settings
├── 1D/                       # created by the apps
│   ├── SAXS/
│   │   ├── Reduction/  Averaged/
│   │   ├── Subtracted/{Good,NeedsReview}/   # Good/NeedsReview sorted by the Quality Gate
│   │   ├── Analysed/<Type>/                 # Data Analysis results
│   │   ├── Conditions/                      # next recipes, optimiser → reactor
│   │   └── Results/                         # Fit/, QualityReports/, campaign_<id>/
│   └── WAXS/                 # same tree
├── manifest.db               # shared record of every file and analysis (SQLite)
└── manifest.json             # JSON copy of the same, rewritten on every save
```

### config.yml essentials

```yaml
data_directory: "/path/to/2D"
poni_directory: "/path/to/poni"

compound: "C2H4"          # sample formula (absorption / thickness)
energy_keV: 12
density_g_cm3: 0.92
thickness: null           # metres; null = derive from transmission

mode: "SWAXS"             # "SAXS", "WAXS" or "SWAXS"
metadata_format: "csv"    # "csv" or "pdi"

detector_shapes:
  saxs: [1043, 981]
  waxs: [195, 487]

poni_files: { saxs: "atT_SAXS.poni", waxs: "atT_WAXS.poni" }
mask_files: { saxs: "RT_SAXS_mask_03.edf", waxs: null }

npt_radial: 1000          # required
error_model: "poisson"    # required
normalization: ["bstop"]  # "bstop" (default), "i0" or "absolute": choose one
```

You can also edit and save all of this from the Reduction app's **Setup** tab. The
full reference (normalization modes, air-path transmission, dark/flat frames,
polarization) is in [CLAUDE.md](CLAUDE.md#configyml-reference) and
`reduction/knowledge.md`.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `ModuleNotFoundError: No module named 'flask'` | The environment is not activated; the prompt should show `(venv)` or `(swaxs)`. |
| An app card shows "Starting…", then "Not responding" | Read `logs/<app>.log`. Usually a bad path in `config.yml` or a missing `.poni`. |
| An app card shows "CRASHED" | The card shows the reason and the last log lines. Fix it, press **Start** again. |
| Reduction error: `'i0' not found in metadata` | `metadata_format` does not match your files (`pdi` vs `csv`), or the metadata has no `i0`/`bstop` field. |
| Transmission > 1.0 warning | Check `i0_air` / `bstop_air` and the offsets in `config.yml`. |
| Pipeline runs but no averaged files appear | **Frames per average (N)** in Visualisation & Average is larger than the frames one acquisition delivers. The app's log says so; set N to match. |
| AI features say the token is not set | Put the token in `~/.claude/settings.json` (see [SECURITY.md](SECURITY.md)), be on the SLAC network or VPN, restart. |
| The "Bus" badge in the hub stays grey | Partial install (`flask-sock` missing). Apps still work; only live events are affected. |

More: [QUICKSTART.md § Troubleshooting](QUICKSTART.md#troubleshooting).

---

## Where to go next

| You want to | Read |
|---|---|
| Install, first run, fix an install problem | [QUICKSTART.md](QUICKSTART.md) |
| Rehearse the closed loop without beam | [docs/SIMULATED_2D_DATA.md](docs/SIMULATED_2D_DATA.md) |
| Run an autonomous campaign at the beamline | [docs/AUTONOMOUS_RUN_STEPS.md](docs/AUTONOMOUS_RUN_STEPS.md), then [docs/audits/PRE_BEAMTIME_READINESS.md](docs/audits/PRE_BEAMTIME_READINESS.md) |
| Set up the reactor software and rig | [docs/REACTOR_SETUP.md](docs/REACTOR_SETUP.md), [docs/REACTOR_HARDWARE_SETUP.md](docs/REACTOR_HARDWARE_SETUP.md), [docs/PUMP_FLOW_CALIBRATION.md](docs/PUMP_FLOW_CALIBRATION.md) |
| Bench-test the SPEC/beamline link | [tools/BEAMLINE_TESTING.md](tools/BEAMLINE_TESTING.md) |
| Get Slack alerts | [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md) |
| Set up the AI token | [SECURITY.md](SECURITY.md) |
| Plug in your own program (e.g. your own ML model) | [docs/INTEGRATING_YOUR_OWN_CODE.md](docs/INTEGRATING_YOUR_OWN_CODE.md) |
| See what changed recently | [docs/CHANGELOG.md](docs/CHANGELOG.md) |
| Develop on the code | [CLAUDE.md](CLAUDE.md) (developer guide), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Work on two computers | [SYNC.md](SYNC.md) |

---

## License

See `LICENSE`.
