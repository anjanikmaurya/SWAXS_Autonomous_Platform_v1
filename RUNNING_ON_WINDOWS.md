# Running the SWAXS Platform on Windows

This guide covers running the platform on a Windows laptop. The project was
developed on macOS, so a few things differ — `start_platform.sh` is a bash
script and won't run in PowerShell. Use `start_platform.ps1` (PowerShell) or
`start_platform.bat` (Anaconda Prompt, or double-click) instead; everything else
is the same.

**New here? Start with [QUICKSTART.md](QUICKSTART.md)** — it has the
step-by-step install for PowerShell *and* the Anaconda Prompt, plus a
`start_platform.ps1` / `start_platform.bat` launcher so you no longer have to
run the hub's Python by hand. This document is the deeper reference: how the
pieces fit together, and the Windows-specific details behind those steps.

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
pip install -r requirements-core.txt
```

You should see `(venv)` at the start of your prompt after activating.

> **Use `requirements-core.txt`, not `requirements.txt`.**
> `requirements.txt` is a `pip freeze` of the original developer's Mac: it pins
> exact versions (`numpy==2.2.6`, `scipy==1.15.3`, …) that have no prebuilt
> Windows wheel on older Pythons, so pip tries to *compile* numpy and dies with
> "Microsoft Visual C++ 14.0 or greater is required". It also drags in ~1 GB the
> platform never imports (PyQt6, silx, pyopencl, torch, jupyter).
> `requirements-core.txt` is 16 packages, all prebuilt wheels, ~1–3 minutes, no
> compiler. Python **3.10 or newer** (3.11/3.12 recommended).

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

Every failure below comes from installing `requirements.txt`. If you used
`requirements-core.txt`, you should not see any of them — and if you already hit
one, the fix is the same in each case: start a fresh venv and install
`requirements-core.txt`.

### "Microsoft Visual C++ 14.0 or greater is required"

**Cause:** the exact pins in `requirements.txt` (`numpy==2.2.6`,
`scipy==1.15.3`, `matplotlib==3.10.3`) have no prebuilt Windows wheel for your
Python — most often because it is 3.9 or older — so pip falls back to compiling
numpy from source, which needs a full C/Fortran toolchain.

**Fix:** you do **not** need Visual Studio. Use Python 3.11 or 3.12 and install
`requirements-core.txt`, which uses minimum versions so pip picks a wheel that
exists:

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements-core.txt
```

### numpy / chromadb dependency conflict

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

### Other common issues

| Symptom | Fix |
|---|---|
| `./start_platform.sh` not recognized | On Windows use `.\start_platform.ps1` (PowerShell) or `start_platform.bat` (Anaconda Prompt / double-click) — the `.sh` script is macOS/Linux only. |
| Activate script blocked by PowerShell | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then re-activate. |
| Browser can't connect to localhost:5000 | The hub isn't running — check PowerShell for errors. |
| An app card stuck on "Starting…" | Run that app directly (`python reduction\app.py`) to see the real error. |
| Assistant says the token isn't set | Add it to `C:\Users\<you>\.claude\settings.json`; connect to SLAC VPN. |
| "Bus" badge stays grey | `pip install flask-sock`; apps still work, only live events are affected. |
| Reduction: `'i0' not found in metadata` | `metadata_format` in `config.yml` doesn't match your files (`pdi` vs `csv`). |
| Port already in use | An old copy is still running — close it or reboot. |

For data-folder layout and `config.yml` details, see `README.md` and `CLAUDE.md`.
