# Quick Start

Getting the SWAXS platform running on a fresh machine — laptop, desktop, or
beamline control PC. Every command is written out in full, and every step
says what you should see if it worked.

Pick your section:

- [macOS](#macos) · [Windows — PowerShell](#windows--powershell) ·
  [Windows — Anaconda Prompt](#windows--anaconda-prompt) · [Linux](#linux)
- Then: [First run](#first-run-all-platforms) →
  [Recommended compute spec](#recommended-compute-spec) →
  [Troubleshooting](#troubleshooting)

**Time:** about 10 minutes, most of it waiting for the download.
**Disk:** ~500 MB to install; see [below](#recommended-compute-spec) for how
much you need for a multi-day run. **Internet:** needed for the install, not
to run.

> **One rule before you start.** Install from `requirements-core.txt`. There is
> no requirements.txt in this repo — an old snapshot of the original
> developer's Mac used to sit there, carrying ~1 GB of packages this platform
> never uses (PyQt6, silx, pyopencl, torch) and pinning exact versions with no
> Windows build for older Pythons, so pip tried to compile numpy from source
> and failed. That was the single most common reason an install went wrong, so
> the file was removed rather than just discouraged. If you have an old clone
> with one still sitting in it, delete it — nothing here reads it.

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

Then open **http://localhost:5100** in your browser.

> **If port 5100 is taken:** the hub uses 5100 (and the apps 5101–5109) to stay
> clear of macOS AirPlay Receiver, which owns 5000. If 5100 itself is busy, run
> `SWAXS_HUB_PORT=5200 ./start_platform.sh` and use `localhost:5200`. On Windows
> a port can be blocked with nothing running on it — see the troubleshooting
> section at the end.

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
.\start_platform.bat
```

Then open **http://localhost:5100**.

Yes, a `.bat` from PowerShell — that is deliberate. It is the single Windows
launcher and it needs no execution-policy change. The script checks your Python
version and that the packages are present, and tells you exactly what to run if
something is missing.

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

Then open **http://localhost:5100**.

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

Then open **http://localhost:5100**.

> Headless server? The plots are rendered server-side (matplotlib Agg), so no
> display is needed. To reach the UI from your laptop, forward the port:
> `ssh -L 5100:localhost:5100 you@server`.

---

## First run (all platforms)

You should see a banner in the terminal and, at **http://localhost:5100**, nine
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
| 1 | Calibration & Raw Prep | 5101 | copy data off the beamline, check calibration |
| 2 | Reduction & Correction | 5102 | 2D images → 1D curves |
| 3 | Visualisation & Average | 5103 | average the curves |
| 4 | Background Subtraction | 5104 | subtract the blank |
| 5 | Quality Gate | 5105 | sort good vs needs-review (optional) |
| 6 | Data Analysis | 5106 | Guinier, Porod, Kratky, model fits |
| 7 | Auto-Fit & Optimiser | 5107 | automatic size + PDI, closed-loop optimiser |
| 8 | Autonomous Synthesis | 5108 | the 5-pump reactor (mock by default) |
| 9 | Tassone Group | 5109 | the AI assistant — answers questions about the experiment |

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

## Recommended compute spec

The platform runs on almost anything — it's nine small Flask apps plus
CPU-only pyFAI integration, no GPU anywhere. "Fresh laptop" above is really
"fresh machine": several groups run it on a beamline control PC or a lab
desktop instead. What matters is how long and how unattended the run is.

| | Minimum (install, short test runs) | Recommended (multi-day autonomous run) |
|---|---|---|
| CPU | 4 cores | 8+ cores — up to 9 apps run as separate processes at once |
| RAM | 8 GB | 16 GB — headroom for the AI assistant extras (`requirements-ai.txt` pulls torch/chromadb) and for matplotlib figure rendering in Analysis/Analyzer |
| Disk | 10 GB free, any drive | 50–100 GB free, **SSD** — raw frames plus every derived stage (`Reduction/`, `Averaged/`, `Subtracted/`, `Analysed/`, `Results/`) accumulate for as long as the campaign runs |
| OS | macOS 12+, Windows 10/11, Ubuntu 20.04+ | same |
| Network | none required to run | only for the *optional* outbound calls — Claude API, SFTP, SPEC bServer, Slack/SMTP |

Why SSD matters more than CPU here: several apps poll a folder every ~2-3
seconds (see `docs/CONTINUOUS_RUN_HARDENING_PLAN.md`), and that cost compounds
over a multi-day run on a slow disk far more than it does on a fast CPU.

**Check your machine against these numbers, plus a quick throughput and
stability sanity check, in about 15 seconds:**

```bash
python tools/check_system_spec.py
```

It reports CPU/RAM/disk against the table above, flags any of ports
5100–5109 already in use (the AirPlay/Hyper-V conflicts described in
[Troubleshooting](#troubleshooting)), and runs a short repeated numpy
workload shaped like a SAXS detector frame to estimate frames/sec and check
memory doesn't grow across iterations (a leak would show up as **RSS**
climbing run over run). It's a sanity check, not a certification — the real
proof is running your own data for a few hours before a beamtime.

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

## Optional: plugging your own program into the pipeline

You do not have to use the built-in apps for every stage. Because each stage
hands off through a **folder of files** plus `manifest.json`, a program of your
own — in any language — can read one stage's output and write the next stage's
input, with no change to this codebase and no plugin API to learn.

The common case: **your own ML model instead of Auto-Fit & Optimiser.** It
watches `1D/SAXS/Subtracted/Good/` for newly subtracted profiles, predicts the
structure, and drops the next set of conditions into `1D/SAXS/Conditions/`,
which the Autonomous Synthesis app already polls. You leave app 5107 stopped and the
loop closes through your model instead. That is the entire integration — three
folder paths and one small file format.

Other ways in: subscribe to the WebSocket event bus for push notifications
instead of polling, import `src/` directly as a Python library, record your
results in `manifest.json` for provenance, or register your program in
`apps.yml` so it gets its own hub card with start/stop.

**→ [docs/INTEGRATING_YOUR_OWN_CODE.md](docs/INTEGRATING_YOUR_OWN_CODE.md)** —
exact folder paths, the condition-file format and its required fields, a
working ~30-line Python skeleton, every integration surface ranked by how
stable it is, and the safety notes for anything that can drive the reactor.

---

## Upgrading from a version before September 2026

If you used the platform earlier, a few things moved. Nothing about your **data**
changed — project folders, `config.yml` and `manifest.json` are all unaffected.

| Was | Now | Why |
|---|---|---|
| Hub on **5000**, apps 5001–5009 | Hub on **5100**, apps **5101–5109** | macOS AirPlay Receiver owns 5000, so the hub could not bind on a stock Mac |
| **Data Viewer** (`viewer/`) | **Visualisation & Average** (`average/`) | the folder name said "viewer" while the app's main job is averaging |
| **Nanoparticle Analyzer** | **Auto-Fit & Optimiser** | the old name hid the Bayesian optimizer half, and read like a sibling of Data Analysis |
| **Tassone Group Assistant** | **Tassone Group** | shorter |
| `start_platform.ps1` **and** `.bat` | **`start_platform.bat` only** | a `.bat` runs from PowerShell too and needs no execution-policy change, so the second launcher was upkeep for no gain |
| **Flow Synthesis** | **Autonomous Synthesis** | the reactor is a fixture — flow rate is one setting, not the point; the point is that it runs the loop unattended |
| `requirements.txt` present | **removed entirely** | it was a `pip freeze` of one Mac that made numpy try to compile from source on Windows — the single most common install failure |

The app order also now follows the pipeline everywhere — hub cards, launcher
banner and docs all read calibration → reduction → average → background →
quality → analysis → auto-fit → autonomous synthesis → assistant.

What to do:

- **Update your bookmarks** — the hub used to be on `localhost:5000`; it is now
  `localhost:5100`.
- **`git pull`, then just start it.** No reinstall, no config edit.
- **Windows:** run `.\start_platform.bat` even in PowerShell. If you had a
  shortcut to `start_platform.ps1`, repoint it — that file is gone.
- If your clone still has a requirements.txt, delete it (`rm requirements.txt`)
  — it's gone from the repo and installing from a leftover local copy is
  exactly the Windows-numpy-compile failure this removal fixes.
- Old `logs/viewer.log` and `.swaxs_state/viewer_monitor.json` are dead files;
  the app writes `average.log` and `average_monitor.json` now. Deleting the old
  ones is safe but not required.
- Manifests written before the rename record `"app": "viewer"` in their
  provenance. That is deliberate — those files really were produced under the
  old name — and nothing in the code compares against it.

> **If a Windows launcher ever behaves strangely after a `git pull`**, check its
> line endings. `.bat` files must be CRLF — `cmd.exe` mishandles LF-only batch
> files, and the symptom is a script that fails for reasons you cannot see by
> reading it. `.gitattributes` now pins `*.bat` to CRLF, so a fresh clone is
> correct; an old working copy may need `git rm --cached start_platform.bat`
> then `git checkout -- start_platform.bat` to pick the new setting up.

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

1. **You installed a leftover requirements.txt from an old clone** (it was
   removed from the repo — delete your local copy). Use `requirements-core.txt`.
2. **Your Python is 3.9 or older.** Check with `python --version`. Install 3.11
   or 3.12, delete the `venv` folder, and redo steps 3–4.

You do **not** need to install Visual Studio.

### `File ... cannot be loaded because running scripts is disabled`
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

### “Port 5100 is already in use” / the page won't load
The platform uses **5100 (hub) and 5101–5109 (apps)**. It moved off 5000–5009 in
September 2026 because macOS AirPlay takes port 5000 for itself (below).

- **macOS:** AirPlay Receiver listens on 5000 and 7000 on Monterey and later, so
  the old hub port could not bind on a stock Mac. 5100 avoids it. If you still
  hit a clash, check with `lsof -i :5100`, or pick another port:
  `SWAXS_HUB_PORT=5200 ./start_platform.sh`.
- **Windows:** Hyper-V and WSL2 reserve *random* blocks of ~100 ports, and ranges
  overlapping 5100–5109 have been seen in the wild. This is the one case where
  the port is blocked even though nothing is running on it. Check with:
  ```powershell
  netsh int ipv4 show excludedportrange protocol=tcp
  ```
  If our range appears there, either start the platform on a free range
  (`$env:SWAXS_HUB_PORT="5200"`) or reserve ours back, as Administrator:
  ```powershell
  netsh int ipv4 add excludedportrange protocol=tcp startport=5100 numberofports=10
  ```
  (run before Hyper-V claims it — i.e. right after a reboot).
- **Linux:** something else is genuinely on that port; `ss -ltnp | grep 510`
  names it. Use `SWAXS_HUB_PORT=5200 ./start_platform.sh`.
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

### numpy / chromadb dependency conflict (only if you have a leftover requirements.txt)

**Symptom:**

```
ERROR: Cannot install -r requirements.txt (line 15) and numpy==2.2.6
because these package versions have conflicting dependencies.
    chromadb 0.5.3 depends on numpy<2.0.0 and >=1.22.5
```

**Cause:** old `chromadb` caps `numpy<2`, but the removed requirements.txt
pinned `numpy==2.2.6` — a leftover local copy from before it was deleted will
still hit this.

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

- **Don't** `pip install -r requirements.txt` from a leftover copy in an old
  clone — it's gone from the repo; see the note at the top.
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
