# Quick Start

This page takes you from a fresh machine (laptop, lab desktop or beamline
control PC) to the platform running in your browser. Every command is written
out in full, and each step says what you should see if it worked.

Pick your section:

- [macOS](#macos) · [Windows — PowerShell](#windows--powershell) ·
  [Windows — Anaconda Prompt](#windows--anaconda-prompt) · [Linux](#linux)
- Then: [First run](#first-run-all-platforms) →
  [Try it without beam](#try-the-whole-pipeline-without-beam-simulation) →
  [Troubleshooting](#troubleshooting)

**Time:** about 10 minutes, most of it download. **Disk:** about 500 MB to
install. **Internet:** needed for the install, not to run.

> **Install from `requirements-core.txt`.** There is no requirements.txt in
> this repo. If an old clone still has one, delete it and do not install it: it
> was a snapshot of one Mac that makes pip try to compile numpy on Windows.

---

## macOS

### 1. Check what you have

Open **Terminal** (⌘-Space, type `Terminal`, Enter) and run:

```bash
python3 --version
git --version
```

You need **Python 3.10 or newer** (3.11 or 3.12 recommended). If `python3` is
missing or older, install a current one from
[python.org/downloads/macos](https://www.python.org/downloads/macos/) and reopen
Terminal. If `git` is missing, macOS offers to install the developer tools:
accept.

### 2. Get the code

```bash
cd ~/Desktop
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1
```

Already have the folder? `cd` into it and run `git pull` instead.

### 3. Make an isolated environment

```bash
python3 -m venv venv
source venv/bin/activate
```

Your prompt now starts with `(venv)`. You need the `source` line every time you
open a new Terminal (see [Every time after](#every-time-after)).

### 4. Install

```bash
pip install --upgrade pip
pip install -r requirements-core.txt
```

Expect 1 to 3 minutes and a list of `Successfully installed ...`. Yellow
warnings are normal; red `ERROR:` lines are not (see
[Troubleshooting](#troubleshooting)).

### 5. Start

```bash
./start_platform.sh
```

Then open **http://localhost:5100** in your browser.

---

## Windows — PowerShell

### 1. Install Python and git

Download **Python 3.11 or 3.12** from
[python.org/downloads/windows](https://www.python.org/downloads/windows/).

> ⚠ **In the installer, tick "Add python.exe to PATH"** on the first screen.
> Everything below fails without it.

Install **Git for Windows** from [git-scm.com/download/win](https://git-scm.com/download/win)
(the defaults are fine). Then open **PowerShell** and check:

```powershell
python --version
git --version
```

Both should print a version. If `python` opens the Microsoft Store, the PATH box
was not ticked: reinstall Python with it ticked. Do not use Python 3.9 or older
(the launcher refuses anything below 3.10).

### 2. Get the code

```powershell
cd $HOME\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1
```

### 3. Make an isolated environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Your prompt now starts with `(venv)`.

> **"running scripts is disabled on this system"?** Allow scripts for your own
> account once, then repeat the activate line:
>
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

### 4. Install

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements-core.txt
```

### 5. Start

```powershell
.\start_platform.bat
```

Then open **http://localhost:5100**. `start_platform.bat` is the only Windows
launcher; it runs from PowerShell, cmd, the Anaconda Prompt or a double-click,
and needs no execution-policy change. It checks your Python version and the
installed packages, and tells you what to run if something is missing.

---

## Windows — Anaconda Prompt

Use this if you already have Anaconda or Miniconda. Open **Anaconda Prompt**
from the Start menu (not PowerShell).

### 1. Get the code

```bat
cd %USERPROFILE%\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1
```

No git? Run `conda install -y git` first.

### 2. Make a conda environment

```bat
conda create -y -n swaxs python=3.12
conda activate swaxs
```

Your prompt now starts with `(swaxs)`. Do not install into `base`.

### 3. Install

```bat
conda install -y -c conda-forge numpy scipy pandas matplotlib pyyaml h5py
python -m pip install -r requirements-core.txt
```

The second command finds numpy, scipy and the others already installed and only
fetches the rest (flask, pyFAI, fabio, xraydb, ...).

### 4. Start

```bat
start_platform.bat
```

Then open **http://localhost:5100**. You can also double-click
`start_platform.bat` in File Explorer.

---

## Linux

```bash
# Debian / Ubuntu (Fedora / RHEL: sudo dnf install -y python3 python3-pip git)
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version          # must be 3.10 or newer

cd ~
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements-core.txt

./start_platform.sh
```

Then open **http://localhost:5100**. On a headless server, plots are rendered
server-side so no display is needed; reach the UI from your laptop with
`ssh -L 5100:localhost:5100 you@server`.

---

## First run (all platforms)

The terminal prints a banner and the hub at **http://localhost:5100** shows ten
app cards.

**1. Choose your project folder.** Click the folder box in the hub's top bar
("No project selected. Click to choose."). The hub passes this folder to every
app. It should look like this:

```
<your project folder>/
├── 2D/
│   ├── SAXS/          *.raw detector images (+ *.raw.pdi, or a *.csv in 2D/)
│   └── WAXS/
├── poni/              *.poni calibration + *.edf masks
└── config.yml         reduction settings
```

The apps create `1D/` and the manifest (`manifest.db`, plus a `manifest.json`
copy) themselves. You can also pass the folder to the launcher to skip this
step: `./start_platform.sh /path/to/experiment` or
`start_platform.bat D:\data\Auto_Run`.

Type your name or initials in the **Operator** box in the same top bar. It is
entered only there, and every app records it with its results.

**2. Start an app.** Press **Start** on a card, wait for the dot to turn green,
then press **Open**. Work through them in pipeline order:

| # | | App | Port | Does |
|---|---|-----|------|------|
| 1 | <img src="assets/icons/docs/swaxs-calibration.svg" width="20" height="20" alt=""> | Geometry Calibration | 5101 | copy data off the beamline, make the pyFAI `.poni` files |
| 2 | <img src="assets/icons/docs/swaxs-reduction.svg" width="20" height="20" alt=""> | Reduction & Correction | 5102 | 2D images → 1D curves |
| 3 | <img src="assets/icons/docs/swaxs-visualisation.svg" width="20" height="20" alt=""> | Visualisation & Average | 5103 | view 2D and 1D data, average the curves |
| 4 | <img src="assets/icons/docs/swaxs-subtraction.svg" width="20" height="20" alt=""> | Background Subtraction | 5104 | subtract the blank |
| 5 | <img src="assets/icons/docs/swaxs-quality.svg" width="20" height="20" alt=""> | Quality Gate | 5105 | sort subtracted curves into Good and NeedsReview (optional) |
| 6 | <img src="assets/icons/docs/swaxs-analysis.svg" width="20" height="20" alt=""> | Data Analysis | 5106 | Guinier, Porod, Kratky, p(r), model fits |
| 7 | <img src="assets/icons/docs/swaxs-autofit.svg" width="20" height="20" alt=""> | Autonomous Analyser | 5107 | automatic size and PDI fits, closed-loop optimiser |
| 8 | <img src="assets/icons/docs/swaxs-synthesis.svg" width="20" height="20" alt=""> | Autonomous Synthesis | 5108 | the 5-pump flow reactor and SPEC control (starts in Simulation) |
| 9 | <img src="assets/icons/docs/swaxs-guinier-ai.svg" width="20" height="20" alt=""> | Guinier | 5109 | the AI assistant: answers questions about the experiment |
| 10 | <img src="assets/icons/docs/swaxs-watch.svg" width="20" height="20" alt=""> | Autonomous Watch | 5110 | loop monitoring and all platform notifications (Slack) |

**3. Stop.** Press **Stop** on a card, **Stop all** above the cards, or
`Ctrl-C` in the terminal to close the hub and every app with it.

### Every time after

You install once. To start again, open a terminal and:

**macOS / Linux**
```bash
cd ~/Desktop/SWAXS_Autonomous_Platform_v1     # wherever you cloned it
source venv/bin/activate
./start_platform.sh
```

**Windows PowerShell**
```powershell
cd $HOME\Documents\SWAXS_Autonomous_Platform_v1
.\venv\Scripts\Activate.ps1
.\start_platform.bat
```

**Windows Anaconda Prompt**
```bat
cd %USERPROFILE%\Documents\SWAXS_Autonomous_Platform_v1
conda activate swaxs
start_platform.bat
```

Forgetting to activate the environment is the most common day-two problem: you
get `ModuleNotFoundError: No module named 'flask'`.

---

## Try the whole pipeline without beam (Simulation)

The Autonomous Synthesis app starts in **Simulation** (the Simulation | Hardware
toggle in its top bar). In Simulation every triggered collection writes real
`.raw` detector frames with known particle sizes, so you can run reduction →
averaging → subtraction → analyser → optimiser end to end on a laptop and check
that the loop finds the right answer. No pumps, SPEC or beam needed.

How to set it up, which folder to use, and how to check the result:
**[docs/SIMULATED_2D_DATA.md](docs/SIMULATED_2D_DATA.md)**.

---

## Optional extras

Install these only if you need them. Without them the app says what is missing
and keeps working.

| You want | Run |
|---|---|
| The real reactor (pumps + EPICS temperature) | `pip install -r requirements-hardware.txt` |
| AI assistant chat | `pip install anthropic`, plus a token (see `SECURITY.md`) |
| AI searchable knowledge base | `pip install -r requirements-ai.txt` (pulls torch, about 2 GB) |
| Slack alerts (Autonomous Watch app) | nothing to install: set `SWAXS_SLACK_WEBHOOK_URL` in `.env`, see `docs/NOTIFICATIONS.md` |
| The test suite | `pip install pytest`, then `python -m pytest -q` |

Model fitting (`sasmodels`) and the pyFAI-calib2 calibration GUI (`PySide6`) are
already in `requirements-core.txt`.

---

## Recommended compute spec

There is no GPU anywhere; integration runs on the CPU. What matters is how long
and how unattended the run is.

| | Minimum (install, short tests) | Recommended (multi-day autonomous run) |
|---|---|---|
| CPU | 4 cores | 8+ cores (up to 10 apps run as separate processes) |
| RAM | 8 GB | 16 GB (headroom for the AI extras and plotting) |
| Disk | 10 GB free | 50 to 100 GB free on an **SSD**: raw frames and every derived stage accumulate for the whole campaign |
| OS | macOS 12+, Windows 10/11, Ubuntu 20.04+ | same |
| Network | none to run | only for the optional outbound calls: Claude API, SFTP, SPEC bServer, Slack |

Several apps poll a folder every few seconds, so a fast disk matters more than a
fast CPU. To check your machine against this table (and see whether any of ports
5100 to 5110 are already taken) in about 15 seconds:

```bash
python tools/check_system_spec.py
```

---

## Plugging in your own program

Each stage hands off through a folder of files, so your own program (for
example your own ML model in place of the Autonomous Analyser) can read
`1D/SAXS/Subtracted/Good/` and write the next conditions into
`1D/SAXS/Conditions/`, which the Autonomous Synthesis app already watches. Folder
paths, the condition-file format and a working skeleton:
**[docs/INTEGRATING_YOUR_OWN_CODE.md](docs/INTEGRATING_YOUR_OWN_CODE.md)**.

---

## Upgrading from a version before September 2026

Your data, project folders, `config.yml` and manifest are unaffected. `git pull`,
then start as usual; no reinstall or config edit is needed.

| Was | Now |
|---|---|
| Hub on 5000, apps 5001 to 5009 (used until September 2026) | Hub on **5100**, apps **5101 to 5110** (update your bookmarks) |
| Data Viewer (`viewer/`) | **Visualisation & Average** (`average/`) |
| Nanoparticle Analyzer | **Autonomous Analyser** |
| Flow Synthesis | **Autonomous Synthesis** |
| Reactor sent its own Slack messages | **Autonomous Watch** (port 5110) sends every notification |
| `start_platform.ps1` (removed) | `start_platform.bat` only, also from PowerShell |
| requirements.txt (removed) | `requirements-core.txt` |

If a Windows launcher misbehaves after a `git pull` on an old working copy, its
line endings may be wrong (`.bat` must be CRLF, now pinned by `.gitattributes`):
run `git rm --cached start_platform.bat` then `git checkout -- start_platform.bat`.

---

## Troubleshooting

Ordered by how often it happens.

### `ModuleNotFoundError: No module named 'flask'` (or numpy, yaml, ...)
The environment is not activated. Your prompt should start with `(venv)` or
`(swaxs)`. Activate it (see [Every time after](#every-time-after)) and try again.

### A long red error mentioning "Microsoft Visual C++ 14.0 or greater is required"
pip is trying to compile a package because no prebuilt version matched. Either
you installed a leftover requirements.txt from an old clone (delete it, use
`requirements-core.txt`), or your Python is 3.9 or older (install 3.11 or 3.12,
delete the `venv` folder, redo steps 3 and 4). You do not need Visual Studio.

### `File ... cannot be loaded because running scripts is disabled`
Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or skip activation
and run `start_platform.bat`, which finds `venv\` itself.

### `'python' is not recognized as an internal or external command`
Python is not on your PATH. Reinstall it with "Add python.exe to PATH" ticked,
or try `py`. In the Anaconda Prompt, run `conda activate swaxs` first.

### `./start_platform.sh: Permission denied` (macOS / Linux)
```bash
chmod +x start_platform.sh
```

### "Port 5100 is already in use" / the page will not load
The platform uses 5100 (hub) and 5101 to 5110 (apps), which stays clear of
macOS AirPlay Receiver on port 5000. Press **Ports** in the hub to see what holds
each app's port.

- **Use another hub port:** `SWAXS_HUB_PORT=5200 ./start_platform.sh` (Windows
  PowerShell: `$env:SWAXS_HUB_PORT="5200"` then `.\start_platform.bat`), and open
  `localhost:5200`.
- **macOS:** `lsof -i :5100` names the process.
- **Linux:** `ss -ltnp | grep 510` names the process.
- **Windows:** Hyper-V and WSL2 reserve blocks of about 100 ports, so a port can
  be blocked with nothing running on it. Check with
  `netsh int ipv4 show excludedportrange protocol=tcp`. If 5100 to 5110 is in
  there, use another hub port, or reserve ours as Administrator right after a
  reboot:
  `netsh int ipv4 add excludedportrange protocol=tcp startport=5100 numberofports=11`.
- **A previous hub is still running:** the new hub detects it and takes the port
  back automatically; if a foreign process holds it, the message names it.

### An app card says "CRASHED"
The card shows the reason and the last log lines. The full log is
`logs/<app>.log` (the previous run is `logs/<app>.log.1`). Fix the cause, then
press **Start** again.

### An app card is stuck on "Starting…", then "Not responding"
Check `logs/<app>.log`. Usually a bad path in `config.yml` or a missing `.poni`.

### The pipeline runs but produces nothing
Check in order:
1. **Averaging:** if the log says the frames per average is larger than the
   frames the reactor collects per acquisition (for example 30 vs 10), set
   **Frames per average (N)** to match. Until then no average is written.
2. **Subtraction:** the log names any sample still waiting for its blank.
3. **Folders:** each app must watch the previous stage's output folder.

### numpy / chromadb conflict
Seen only when installing a leftover requirements.txt: old `chromadb` caps
`numpy<2`. `chromadb` is an optional AI extra and is not in
`requirements-core.txt`. Install the core first, then, if you want the AI
knowledge base, `pip install -r requirements-ai.txt` on its own. If the
assistant later complains about its vector database, delete
`ai_knowledge/vector_db/`; it is rebuilt locally.

### pyopencl fails to build
Nothing imports it and it is not in `requirements-core.txt`. Do not install it;
pyFAI integration runs fine on the CPU.

### `git clone` asks for a password
The repository is private. Ask for access, then use a
[personal access token](https://github.com/settings/tokens) as the password, or
set up SSH keys.

### Still stuck
Send the output of:

```bash
python -c "import sys, platform; print(sys.version); print(platform.platform())"
python -m pip list
```

---

## What not to do

- Do not install from a leftover requirements.txt (see the top of this page).
- Do not install into conda `base`; make a named environment.
- Do not commit your `.env` file. It is git-ignored because it can hold secrets.
- Do not run two hubs against the same project folder; both would write the
  manifest.

---

## Where to go next

| Document | For |
|---|---|
| `README.md` | what the platform is and how the apps fit together |
| `docs/SIMULATED_2D_DATA.md` | a full closed-loop run on a laptop, no beam |
| `docs/AUTONOMOUS_RUN_STEPS.md` | operator steps for an autonomous campaign at the beamline |
| `docs/REACTOR_SETUP.md` | configuring and running the reactor app |
| `docs/NOTIFICATIONS.md` | Slack alerts from the Autonomous Watch app |
| `SECURITY.md` | the AI token: where it goes, what never goes in git |
| `SYNC.md` | working on two computers with git |
