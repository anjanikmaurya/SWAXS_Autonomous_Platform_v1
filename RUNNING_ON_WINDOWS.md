# Running the SWAXS Platform on Windows

This guide covers running the platform on a Windows laptop. The project was
developed on macOS, so a few things differ — mainly that `start_platform.sh`
is a bash script and won't run in PowerShell. On Windows you start the hub's
Python directly instead; everything else is the same.

For the cross-laptop git workflow, see `SYNC.md`.

---

## How the platform works

It's a **hub-and-spoke** system. You start one process — the **hub** on port
5000. The hub reads `apps.yml` and, from its web UI, launches each app as its
own subprocess on its own port. Every app reads and writes to the same
**project folder** (your experiment data: `2D/`, `poni/`, `config.yml`), and
the apps coordinate through a `manifest.json` at the root of that folder.

### The data pipeline, in order

Each app consumes the previous one's output:

| Order | App | Port | Turns… into… |
|------|-----|------|--------------|
| 1 | Reduction & Correction | 5001 | raw 2D detector images → 1D curves (`q, I, sigma`) in `1D/.../Reduction/` |
| 2 | Data Viewer | 5002 | reduced curves → averaged (and stitched) curves in `1D/.../Averaged/` |
| 3 | Background Subtraction | 5003 | averaged curves → buffer-subtracted curves in `1D/.../Subtracted/` |
| 4 | Quality Gate | 5006 | subtracted profiles → auto-sorted into `Subtracted/Good/` and `NeedsReview/` |
| 5 | Data Analysis | 5004 | good profiles → Guinier (Rg), Kratky, Porod, p(r), model fits |
| 6 | AI Assistant | 5005 | reads the manifest to answer questions, make plots, give hints |

**Flow Synthesis / Reactor (5007)** is separate from this chain — it controls
the 5-pump continuous-flow reactor.

---

## First-time setup (once)

Open **PowerShell** in the project folder and run:

```powershell
cd C:\Users\akmaurya\dev\SWAXS_Autonomous_Platform_v1
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

You should see `(venv)` at the start of your prompt after activating.

If PowerShell blocks the activate script ("running scripts is disabled on this
system"), run this once, then re-activate:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### (Optional) AI Assistant token

Only the AI Assistant (app #5) needs a token — the other apps work without it.
On Windows the hub looks for it at `C:\Users\<you>\.claude\settings.json` (the
same place as `~/.claude/settings.json` on Mac). Reaching the SLAC AI gateway
requires the SLAC network / VPN. See `SECURITY.md` for details.

---

## Running it (every time)

```powershell
cd C:\Users\akmaurya\dev\SWAXS_Autonomous_Platform_v1
venv\Scripts\Activate.ps1
python hub/app.py
```

`python hub/app.py` is the Windows replacement for `./start_platform.sh`. The
hub launches each sub-app with the same venv Python, so you do **not** need
`uv` on Windows.

Then:

1. Open **http://localhost:5000** in your browser.
2. Click the folder pill (top-right) and select your experiment folder.
3. On each app card, click **Start**, wait for the green "Running" dot, then
   **Open**. Work top to bottom through the pipeline.
4. Press **Ctrl-C** in PowerShell to stop the hub. `deactivate` leaves the venv.

To run a single app directly (useful for debugging — you see its full logs):

```powershell
python reduction\app.py     # or viewer / background / quality / analysis / assistant / reactor / analyzer
```

Ports: hub 5000 · reduction 5001 · viewer 5002 · background 5003 · analysis 5004 · assistant 5005 · quality 5006 · reactor 5007 · analyzer 5008.

---

## Troubleshooting installation

### numpy / chromadb dependency conflict

**Symptom:**

```
ERROR: Cannot install -r requirements.txt (line 15) and numpy==2.2.6
because these package versions have conflicting dependencies.
    chromadb 0.5.3 depends on numpy<2.0.0 and >=1.22.5
```

**Cause:** `chromadb==0.5.3` requires `numpy<2`, but the platform pins
`numpy==2.2.6`. (The macOS venv predates this pin, so it never had to resolve
both together.)

**Fix (already applied in `requirements.txt`):** chromadb dropped the
`numpy<2` cap in 0.5.7, so the requirement is now `chromadb==0.5.23` — the
latest 0.5.x, which works with numpy 2 while keeping the same 0.5.x API the
assistant code was written against. Just re-run:

```powershell
pip install -r requirements.txt
```

> If the AI Assistant later complains about its vector database, delete the
> `ai_knowledge/vector_db/` folder and let it re-ingest — that folder is
> disposable and rebuilt locally (it's git-ignored).

### pyopencl fails to build/install

`pyopencl` needs an OpenCL runtime and is the package most likely to fail on a
fresh Windows machine. If `pip install` errors on it:

- Install your GPU vendor's OpenCL driver (Intel / NVIDIA / AMD), **or**
- Install a prebuilt `pyopencl` wheel matching your Python version, then re-run
  `pip install -r requirements.txt`.

`pyopencl` accelerates PyFAI integration but is not strictly required for the
core reduction to run.

### Other common issues

| Symptom | Fix |
|---|---|
| `./start_platform.sh` not recognized | On Windows use `python hub/app.py` — the `.sh` script is macOS/Linux only. |
| Activate script blocked by PowerShell | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then re-activate. |
| Browser can't connect to localhost:5000 | The hub isn't running — check PowerShell for errors. |
| An app card stuck on "Starting…" | Run that app directly (`python reduction\app.py`) to see the real error. |
| Assistant says the token isn't set | Add it to `C:\Users\<you>\.claude\settings.json`; connect to SLAC VPN. |
| "Bus" badge stays grey | `pip install flask-sock`; apps still work, only live events are affected. |
| Reduction: `'i0' not found in metadata` | `metadata_format` in `config.yml` doesn't match your files (`pdi` vs `csv`). |
| Port already in use | An old copy is still running — close it or reboot. |

For data-folder layout and `config.yml` details, see `README.md` and `CLAUDE.md`.
