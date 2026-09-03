# Quick Start

Getting the SWAXS platform running on a fresh laptop. **No prior Python
experience assumed** — every command is written out, and every step says what
you should see if it worked.

Pick your section:

- [macOS](#macos) · [Windows — PowerShell](#windows--powershell) ·
  [Windows — Anaconda Prompt](#windows--anaconda-prompt) · [Linux](#linux)
- Then: [First run](#first-run-all-platforms) →
  [Troubleshooting](#troubleshooting)

**Time:** about 10 minutes, most of it waiting for the download.
**Disk:** ~500 MB. **Internet:** needed for the install, not to run.

> **One rule before you start.** Install from `requirements-core.txt`, **not**
> `requirements.txt`. The latter is an old snapshot of the original developer's
> Mac: it carries ~1 GB of packages this platform never uses (PyQt6, silx,
> pyopencl, torch) and pins exact versions that have no Windows build for older
> Pythons — pip then tries to compile numpy from source and fails. That is the
> single most common reason an install goes wrong.

---

## macOS

### 1. Check what you have

Open **Terminal** (⌘-Space, type `Terminal`, Enter) and run:

```bash
python3 --version
git --version
```

You need **Python 3.10 or newer**. If `python3` is missing or older, install a
current one from [python.org/downloads/macos](https://www.python.org/downloads/macos/)
and reopen Terminal. If `git` is missing, macOS will offer to install the
developer tools — accept.

### 2. Get the code

```bash
cd ~/Desktop
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1
```

*Already have the folder?* Skip the clone and just `cd` into it, then
`git pull`.

### 3. Make an isolated environment

This keeps the platform's packages away from the rest of your system, so
nothing you install here can break another project.

```bash
python3 -m venv venv
source venv/bin/activate
```

Your prompt should now start with `(venv)`. **You need this line every time you
open a new Terminal** — see [Every time after](#every-time-after).

### 4. Install

```bash
pip install --upgrade pip
pip install -r requirements-core.txt
```

Expect 1–3 minutes and a wall of `Successfully installed ...`. Warnings in
yellow are normal; red `ERROR:` lines are not — see
[Troubleshooting](#troubleshooting).

### 5. Start

```bash
./start_platform.sh
```

Then open **http://localhost:5000** in your browser.

> **If port 5000 is taken:** macOS uses it for AirPlay Receiver. The hub usually
> shares it fine. If it genuinely can't bind, either turn AirPlay Receiver off in
> System Settings → General → AirDrop & Handoff, or run
> `SWAXS_HUB_PORT=5100 ./start_platform.sh` and use `localhost:5100`.

---

## Windows — PowerShell

### 1. Install Python

Download **Python 3.11 or 3.12** from
[python.org/downloads/windows](https://www.python.org/downloads/windows/).

> ⚠ **In the installer, tick “Add python.exe to PATH”** on the very first
> screen. It is easy to miss and everything below fails without it.

Also install **Git for Windows** from [git-scm.com/download/win](https://git-scm.com/download/win)
(the defaults are fine).

Now open **PowerShell** (Start menu → type `PowerShell`) and check:

```powershell
python --version
git --version
```

Both should print a version. If `python` opens the Microsoft Store instead, the
PATH tick was missed — reinstall Python with that box ticked.

**Do not use Python 3.9 or older.** There are no prebuilt numpy/scipy packages
for it on Windows, so pip tries to compile them and you get a long red error
about Microsoft Visual C++.

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

Your prompt should now start with `(venv)`.

> **“running scripts is disabled on this system”?** Windows blocks scripts by
> default. Allow them for your own account (once), then repeat the activate:
>
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```
>
> This affects only your user account, not the machine.

### 4. Install

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements-core.txt
```

### 5. Start

```powershell
.\start_platform.ps1
```

Then open **http://localhost:5000**.

The script checks your Python version and that the packages are present, and
tells you exactly what to run if something is missing.

---

## Windows — Anaconda Prompt

Use this if you already have Anaconda or Miniconda. Conda gives you the science
stack as prebuilt binaries, which is the most reliable route on Windows.

Open **Anaconda Prompt** from the Start menu (not PowerShell).

### 1. Get the code

```bat
cd %USERPROFILE%\Documents
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1
```

No git? `conda install -y git` first.

### 2. Make a conda environment

```bat
conda create -y -n swaxs python=3.12
conda activate swaxs
```

Your prompt should now start with `(swaxs)`.

> Don't install into `base`. A broken `base` breaks every other conda project
> you have.

### 3. Install

The science packages from conda-forge (fast, prebuilt), the rest from pip:

```bat
conda install -y -c conda-forge numpy scipy pandas matplotlib pyyaml h5py
python -m pip install -r requirements-core.txt
```

The second command will see numpy/scipy/etc. are already there and only fetch
what's missing (flask, pyFAI, fabio, xraydb, …).

### 4. Start

```bat
start_platform.bat
```

Then open **http://localhost:5000**.

> `start_platform.bat` also works by **double-clicking it** in File Explorer,
> and needs no execution-policy change.

---

## Linux

### 1. System packages

Debian / Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Fedora / RHEL: `sudo dnf install -y python3 python3-pip git`

Check you have 3.10+:

```bash
python3 --version
```

### 2. Get the code, install, start

```bash
cd ~
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements-core.txt

./start_platform.sh
```

Then open **http://localhost:5000**.

> Headless server? The plots are rendered server-side (matplotlib Agg), so no
> display is needed. To reach the UI from your laptop, forward the port:
> `ssh -L 5000:localhost:5000 you@server`.

---

## First run (all platforms)

You should see a banner in the terminal and, at **http://localhost:5000**, nine
app cards.

**1. Choose your project folder.** Top-right of the hub page. This is the folder
holding your experiment data, and it must look like this:

```
<your project folder>/
├── 2D/
│   ├── SAXS/          *.raw detector images (+ *.pdi or a *.csv alongside)
│   └── WAXS/
├── poni/              *.poni calibration + *.edf masks
└── config.yml         reduction settings
```

The apps create `1D/` and `manifest.json` themselves. No data yet? Point it at
the bundled `Demo_Data/` folder to click around safely.

**2. Start an app.** Press **▶ Start** on a card, wait for the dot to turn
green, then **↗ Open**. Work left to right:

| # | App | Port | Does |
|---|-----|------|------|
| 1 | Calibration & Raw Prep | 5009 | copy data off the beamline, check calibration |
| 2 | Reduction & Correction | 5001 | 2D images → 1D curves |
| 3 | Visualisation & Average | 5002 | average the curves |
| 4 | Background Subtraction | 5003 | subtract the blank |
| 5 | Quality Gate | 5006 | sort good vs needs-review |
| 6 | Data Analysis | 5004 | Guinier, Porod, Kratky, model fits |
| 7 | Nanoparticle Analyzer | 5008 | automatic size + PDI, closed-loop optimiser |
| 8 | Flow Synthesis | 5007 | the 5-pump reactor (mock by default) |
| 9 | AI Assistant | 5005 | answers questions about the experiment |

**3. Stop.** Press **■ Stop** on a card, or `Ctrl-C` in the terminal to close the
hub — which closes every app with it.

### Every time after

You only install once. To start again, open a terminal and:

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
.\start_platform.ps1
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

## Optional extras

Install these **only** if you need them. Everything degrades gracefully without
them — the app says what's missing and keeps working.

| You want | Run |
|---|---|
| The real reactor (pumps + EPICS temperature) | `pip install -r requirements-hardware.txt` |
| AI assistant chat | `pip install anthropic` — plus a token, see `SECURITY.md` |
| AI searchable knowledge base | `pip install -r requirements-ai.txt` — **pulls torch, ~2 GB** |
| Model fitting in the analysis app | `pip install sasmodels` |
| To run the test suite | `pip install pytest`, then `pytest -q` |

---

## Troubleshooting

Ordered by how often it actually happens.

### `ModuleNotFoundError: No module named 'flask'` (or numpy, yaml, …)
The environment isn't activated, or you're in the wrong one. Your prompt should
start with `(venv)` or `(swaxs)`. Activate it (see
[Every time after](#every-time-after)) and try again.

### A long red error mentioning “Microsoft Visual C++ 14.0 or greater is required”
pip is trying to **compile** a package because no prebuilt version matched your
Python. Two causes:

1. **You used `requirements.txt`.** Use `requirements-core.txt`.
2. **Your Python is 3.9 or older.** Check with `python --version`. Install 3.11
   or 3.12, delete the `venv` folder, and redo steps 3–4.

You do **not** need to install Visual Studio.

### `.\start_platform.ps1 : File ... cannot be loaded because running scripts is disabled`
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
Or sidestep it entirely: `start_platform.bat`.

### `'python' is not recognized as an internal or external command`
Python isn't on your PATH. Reinstall it with **“Add python.exe to PATH”**
ticked, or try `py` instead of `python`. In Anaconda Prompt, make sure you ran
`conda activate swaxs`.

### `./start_platform.sh: Permission denied` (macOS / Linux)
```bash
chmod +x start_platform.sh
```

### “Port 5000 is already in use” / the page won't load
- **macOS:** AirPlay Receiver also uses 5000. Turn it off in System Settings →
  General → AirDrop & Handoff, or use another port:
  `SWAXS_HUB_PORT=5100 ./start_platform.sh`.
- **Windows/Linux:** something else is on that port. Use
  `$env:SWAXS_HUB_PORT="5100"` (PowerShell) or
  `set SWAXS_HUB_PORT=5100` (Anaconda Prompt) before starting.
- **A previous hub is still running:** the new hub detects it and takes the port
  back automatically. If it reports a foreign process, the message names it.

### An app card says “⚠ CRASHED”
The card shows the reason and the last few log lines. The full log is in
`logs/<app>.log`, and the previous run is kept as `logs/<app>.log.1`. Fix the
cause, then press **▶ Start** again.

### An app card is stuck on “Starting…” then “Not responding”
It bound its port but isn't answering. Check `logs/<app>.log` — usually a bad
path in `config.yml` or a missing `.poni` file.

### The pipeline runs but produces nothing
Check in order:
1. **Averaging** — if the log says *“frames/batch is 30 but the reactor collects
   10 frames per acquisition”*, set frames/batch to match. Until you do, no
   average is ever written and everything downstream waits forever.
2. **Subtraction** — the log names any sample waiting for its blank.
3. **Folders** — each app must watch the previous stage's output folder.

### numpy / chromadb dependency conflict (only if you used `requirements.txt`)

**Symptom:**

```
ERROR: Cannot install -r requirements.txt (line 15) and numpy==2.2.6
because these package versions have conflicting dependencies.
    chromadb 0.5.3 depends on numpy<2.0.0 and >=1.22.5
```

**Cause:** old `chromadb` caps `numpy<2`, but `requirements.txt` pins
`numpy==2.2.6`.

**Fix:** `chromadb` is an *optional* AI-assistant dependency and is not in
`requirements-core.txt` at all, so the conflict disappears. If you want the
searchable knowledge base, add it afterwards — on its own, where a resolver
failure cannot block the platform:

```powershell
pip install -r requirements-ai.txt
```

> If the AI Assistant later complains about its vector database, delete the
> `ai_knowledge/vector_db/` folder and let it re-ingest — that folder is
> disposable and rebuilt locally (it's git-ignored).

### pyopencl fails to build/install

`pyopencl` needs an OpenCL SDK present *at import time* and is the package most
likely to fail on a fresh Windows machine.

**Fix:** nothing imports it. It is not in `requirements-core.txt`. Don't install
it — PyFAI integration runs fine on the CPU, and at this data rate (one frame per
few seconds) the GPU path saves nothing worth the install pain.


### `git clone` fails with an authentication prompt
The repository is private. Ask for access, then use a
[personal access token](https://github.com/settings/tokens) as the password, or
set up SSH keys.

### Still stuck
Run the self-check and send the output:

```bash
python -c "import sys, platform; print(sys.version); print(platform.platform())"
python -m pip list
```

---

## What not to do

- **Don't** `pip install -r requirements.txt` — see the note at the top.
- **Don't** install into conda `base`. Make a named environment.
- **Don't** commit your `.env` file. It holds tokens and is git-ignored for that
  reason.
- **Don't** run two hubs against the same project folder. They'd both write to
  `manifest.json`.

---

## Where to go next

| Document | For |
|---|---|
| `README.md` | what the platform is and how the apps fit together |
| `SYNC.md` | working across two laptops with git |
| `docs/AUTONOMOUS_RUN_STEPS.md` | running a full autonomous campaign |
| `docs/REACTOR_SETUP.md` | wiring the real reactor, pump calibration |
| `docs/PARAMETER_SPACE_AND_CONVERGENCE.md` | how the optimiser searches and converges |
| `SECURITY.md` | tokens, the SLAC AI gateway, what never goes in git |
