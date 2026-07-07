# Getting Started — Running the SWAXS Platform

A step-by-step guide to get the platform running on your machine. Commands are
written for macOS / Linux; Windows notes are included where they differ.

There are two parts:

- **Part A — First-time setup** (do this once)
- **Part B — Every time you want to run it** (the routine)

---

## Part A — First-time setup (once)

### Step 1. Check you have the prerequisites

Open a terminal and run:

```bash
python3 --version    # need 3.9+ (3.12 recommended)
git --version
```

If either is missing, install Python from [python.org](https://www.python.org/downloads/)
and Git from [git-scm.com](https://git-scm.com/downloads).

**Recommended:** install `uv` (a fast Python runner the platform uses to launch
its apps):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

> You can run without `uv`, but `start_platform.sh` calls it. If you skip `uv`,
> use the manual start in Step 7 instead.

### Step 2. Get the code

If you don't already have the folder:

```bash
git clone https://github.com/anjanikmaurya/SWAXS_data_correction_reduction_averaging
cd SWAXS_data_correction_reduction_averaging
```

If you already have the project folder, just `cd` into it.

### Step 3. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
```

You should now see `(venv)` at the start of your terminal prompt.

### Step 4. Install the dependencies

```bash
pip install -r requirements.txt
```

This pulls in a large scientific stack (PyFAI, numpy, matplotlib, ChromaDB, …)
and **takes a few minutes**. Let it finish.

### Step 5. (Optional) Add your Claude API key

Only needed if you want the **AI Assistant** (app #5). The other four apps work
without it.

Create a file named `.env` in the project root:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-your-key-here' > .env
```

Setup is done. ✅

---

## Part B — Running the platform (every time)

### Step 6. Activate the environment

From the project folder:

```bash
source venv/bin/activate        # Windows: venv\Scripts\activate
```

### Step 7. Start the hub

```bash
./start_platform.sh
```

You should see a banner and the hub start on port 5000.

> **No `uv`?** Start the hub directly instead:
> ```bash
> python hub/app.py
> ```
> **Permission denied on the script?** Make it executable once with
> `chmod +x start_platform.sh`, then re-run it.

*(Optional)* pre-select your data folder when starting:

```bash
./start_platform.sh /path/to/your/experiment
```

### Step 8. Open the hub in your browser

Go to **http://localhost:5000**

You'll see five app cards and a live event log.

### Step 9. Pick your project folder

Click the **📁 folder pill** in the top-right corner, browse to the folder that
holds your experiment data, and click **Select This Folder**.

> This is the folder containing your `2D/`, `poni/`, and `config.yml` — see the
> data-layout section in `README.md`. All apps read and write here.

### Step 10. Start the app you need

On any card, click **▶ Start**, wait for the dot to turn green ("Running"), then
click **↗ Open** to launch that app in a new tab.

Work through them in order:

| Order | App | Port | Use it to… |
|------|-----|------|------------|
| 1 | ⚙️ Reduction & Correction | 5001 | turn raw 2D images into 1D curves |
| 2 | 📊 Data Viewer | 5002 | view data and average repeated scans |
| 3 | 🔬 Background Subtraction | 5003 | subtract buffer/background |
| 4 | 📈 Data Analysis | 5004 | Guinier, Kratky, Porod, peak fits |
| 5 | 🤖 AI Assistant | 5005 | ask questions, get plots and hints |

### Step 11. Stop when you're done

- Stop an individual app with its **■ Stop** button in the hub.
- Stop the whole platform by pressing **Ctrl-C** in the terminal running the hub.
- Leave the virtual environment with `deactivate`.

---

## Quick reference

```bash
# Every-time routine, from the project folder:
source venv/bin/activate
./start_platform.sh
# → open http://localhost:5000, pick folder, Start + Open apps
# → Ctrl-C in the terminal to stop
```

You can also start a single app directly (handy for debugging — you'll see its
full logs in the terminal):

```bash
uv run reduction/app.py     # or viewer / background / analysis / assistant
```

Ports: hub 5000 · reduction 5001 · viewer 5002 · background 5003 · analysis 5004 · assistant 5005.

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| `./start_platform.sh: command not found` or permission denied | Run `chmod +x start_platform.sh`, or use `python hub/app.py`. |
| `uv: command not found` | Install `uv` (Step 1) or start with `python hub/app.py`. |
| Browser shows "can't connect" at localhost:5000 | The hub isn't running — check the terminal for errors. |
| An app card is stuck on "Starting…" | Start that app directly (`uv run reduction/app.py`) to see the real error. |
| Assistant says the API key isn't set | Add `ANTHROPIC_API_KEY` to `.env` (Step 5) and restart. |
| "Bus" badge stays grey | `pip install flask-sock`; apps still work, only live events are affected. |
| Reduction: `'i0' not found in metadata` | `metadata_format` in `config.yml` doesn't match your files (`pdi` vs `csv`). |
| Port already in use | Another copy is running, or another program holds the port — stop it, or reboot. |

For data-folder layout and `config.yml` details, see `README.md` and `CLAUDE.md`.
