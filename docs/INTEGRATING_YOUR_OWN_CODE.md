# Integrating your own code with the platform

**Who this is for:** you have a program of your own — an ML model, a fitting
routine, a lab-automation script — and you want it to take part in the
pipeline instead of, or alongside, one of the built-in apps.

The short version: **you do not have to touch this codebase.** Every stage
hands off through a folder of files plus `manifest.json`, so a program in any
language can read one stage's output and write the next stage's input. The
built-in apps have no privileged channel between them — they use exactly the
contract described here.

---

## 1. The worked example: replacing Auto-Fit & Optimiser with your own ML

This is the case that motivated the doc. You want:

```
Background Subtraction (5104)          your ML program            Autonomous Synthesis (5108)
  writes subtracted .dat    ───────▶   reads it, predicts   ───▶   runs the next recipe
                                       structure, proposes
                                       the next conditions
```

and you do **not** want to run the Auto-Fit & Optimiser app (5107) at all.

You need exactly three things: where to read, what the files look like, and
where to write. Nothing else — not a plugin API, not a registration step.

### 1.1 Where to read — depends on whether you run the Quality Gate

Which folder you watch is a choice, and it is the one decision in this section:

| Are you running the Quality Gate (5105)? | Watch |
|---|---|
| **Yes** | `1D/SAXS/Subtracted/Good/` |
| **No** | `1D/SAXS/Subtracted/` |

**With the Quality Gate.** The gate scores each subtracted profile and copies
it to `Subtracted/Good/` or `Subtracted/NeedsReview/`. Reading `Good/` means a
profile the gate rejected — a bad subtraction, an aggregated sample, a frame
the beam dropped out of — can never reach your model or the reactor. This is
what the built-in analyzer does by default, and it is the safer setting for an
unattended overnight run.

**Without the Quality Gate.** Read the flat `1D/SAXS/Subtracted/` folder and
you get every subtracted profile the moment it is written, with no filtering
and no dependency on a second app being started. Choose this when you would
rather your own model judge data quality, or when you are testing and do not
want a gate silently withholding files.

Two things to know either way:

- **The gate copies, it does not move.** `Subtracted/` keeps *every* profile,
  accepted or not, so the two folders overlap. If you watch both you will
  process the same profile twice.
- **`Good/` only exists once the gate has run.** If you watch `Good/` and
  forget to start the Quality Gate, your program sits there seeing nothing, and
  nothing in the platform will tell you why. Either handle the missing folder
  explicitly, or use the auto-detect below.
- WAXS is the same tree with `WAXS` in place of `SAXS`.

**Auto-detect (what the built-in analyzer does).** If you want one program that
works in both setups, resolve the folder at startup: use `Good/` when it
exists, otherwise fall back to the flat folder.

```python
sub  = PROJECT / "1D/SAXS/Subtracted"
good = sub / "Good"
WATCH = good if good.is_dir() else sub     # gate running? use its output
```

The analyzer app exposes this as `gate_mode`: `auto` (this rule, the default),
`good` (always require `Good/`, refuse to run without it), or `off` (always the
flat folder). Copying those three options is worth it if other people will run
your program.

**Wait for the file to stop changing before you read it.** A `.dat` is written
by one process and read by another, so a naive "file appeared → read it" loop
will eventually parse a half-written file. Poll the `(size, mtime)` pair and
only act once it is unchanged between two polls — that is precisely what
`src/reactor/intake.py::decide_intake` does, and you can import it if you are
in Python:

```python
from src.reactor.intake import decide_intake   # returns "go" | "wait" | "skip"
```

Prefer push over polling? Subscribe to the event bus instead — see §3.

### 1.2 What a subtracted `.dat` looks like

Three whitespace-separated columns, with all provenance in a `#` **header**
above the data (note: subtracted files put it first — reduced files from the
reduction app instead carry a `# METADATA INFORMATION (YML FORMAT)` footer):

```
# SAXS/WAXS background-subtracted data
# Columns: q_nm-1  I  sigma
# Sample       : /abs/path/sample_0007_SAXS.dat
# Background   : /abs/path/blank_0002_SAXS.dat
# Scale        : 0.9832  (auto, high-q→0; LS=0.9910, zero=0.9832)
# Mode         : auto (auto scale)
# QC           : PASS  (neg=1.2%, highq_ratio=0.031)
# q_nm-1  I  sigma
1.05000000e-01  4.38210000e+02  6.12000000e+00
1.06000000e-01  4.31050000e+02  6.09000000e+00
```

`numpy.loadtxt(path, unpack=True)` gives you the three columns directly, since
it skips `#` lines. That is all most integrations need.

The platform's own parser returns **five** values, not four — the header lines
come first:

```python
from src.utils.read_dat_metadata import read_dat_data_metadata
header, q, I, sigma, meta = read_dat_data_metadata(path)
```

Be aware of what `meta` is: it is populated only from a
`# METADATA INFORMATION` block, and only with float-convertible values.
Subtracted files have no such block, so for the files in this section `meta` is
`{}` and the Sample/Background/Scale/QC lines are in `header` as raw strings.
Parse them yourself if you need them.

**The third column is a real uncertainty**, propagated from Poisson counting
statistics through averaging and subtraction — see
[ERROR_PROPAGATION.md](ERROR_PROPAGATION.md). Weight your fit or your loss by
`1/sigma²`; ignoring it throws away the platform's main quality signal.

### 1.3 Where to write the next condition

Drop a file into the folder the reactor watches:

```
1D/SAXS/Conditions/             ← reactor polls this every ~3 s
1D/SAXS/Conditions/done/        ← files are moved here after a SUCCESSFUL intake
```

Both are set in `reactor/config.yml` under `folders:`, but only the watched
folder can be changed live in the app — `done/` follows the config file, so if
you repoint the watch folder in the UI the two end up in different places.

**A rejected file is not moved.** It stays in `Conditions/` and the reactor logs
why. So "my file is still sitting there" means it was refused, not missed —
read the reactor app's log. Arrival in `done/` is your only positive
confirmation that a condition was accepted.

The reactor accepts `.txt`, `.dat` and `.json` and is deliberately tolerant of
format. The **five required numeric fields** are:

| Field | Meaning |
|---|---|
| `T_reac` | reaction temperature |
| `F_tot` | total flow |
| `x_ODE` | ODE fraction |
| `x_TOP` | TOP fraction |
| `x_oley` | oleylamine fraction |

Optional: `recipe_id`, `run_duration`, `flush_rate`, `flush_duration`,
`arm_mode` (`temperature` or `timed`), `arm_wait_s`.

**Your values must be inside the configured bounds or the file is rejected.**
The shipped defaults (`reactor/config.yml` → `bounds:`) are:

| Field | Allowed | Hard cap (`safety:`) |
|---|---|---|
| `T_reac` | 180–300 °C | 320 °C |
| `F_tot` | 40–120 µL/min | 150 µL/min |
| `x_ODE`, `x_TOP`, `x_oley` | 0.0–0.3 **each** | — |
| their sum | ≤ 0.9 (`x_sum_max`) | — |

Simplest form — `my_pred_001.txt`:

```
# proposed by my_ml v2.3
recipe_id = my_pred_001
T_reac = 220
F_tot = 80
x_ODE = 0.25
x_TOP = 0.20
x_oley = 0.15
```

JSON works identically:

```json
{"recipe_id": "my_pred_001", "T_reac": 220, "F_tot": 80,
 "x_ODE": 0.25, "x_TOP": 0.20, "x_oley": 0.15}
```

Per-pump limits apply on top of the recipe bounds: each fraction is turned into
a pump setpoint (`x · F_tot`), and that must fit the pump's own `max_flow` in
`reactor/config.yml` → `pumps:`. A recipe can satisfy every bound above and
still be rejected on a per-pump ceiling.

The parser (`src/reactor/recipe.py::parse_param_file`) also accepts
`key: value`, bare `key value`, and a CSV header/row pair, and matches field
names case-insensitively with aliases (`T`, `temp`, `temperature`, `T_set` all
mean `T_reac`; `Ftot`, `flow_total` mean `F_tot`; `ODE`/`xODE` mean `x_ODE`).
Write whichever is easiest — but if the file is missing a required field or a
value is non-numeric, the reactor **rejects that file and logs why**; it does
not guess.

Two practical points:

- **Write atomically.** Write `my_pred_001.txt.part`, then rename it into
  place. The reactor's stability check makes a torn read unlikely, not
  impossible, and a rename is free.
- **`recipe_id` is how the loop closes.** The reactor names every acquisition
  `{recipe_id}_sample` / `{recipe_id}_bkg` (the tags are `spec.sample_tag` /
  `spec.bkg_tag`, configurable), and that name survives all the way through
  reduction → averaging → subtraction. So when a new subtracted file appears
  named `my_pred_001_sample_...`, that is the measurement of the condition *you*
  proposed. If you omit `recipe_id`, the reactor uses the filename stem.
- **Never put `_sample` or `_bkg` inside your `recipe_id`.** The pipeline
  recovers the id by splitting at the *first* role tag
  (`src/loop_naming.py`), so an id like `ml_run7_sample_0003` is silently
  truncated to `ml_run7` and your sample/background pairing goes wrong. Derive
  ids from a counter or timestamp, not from the input filename.

### 1.4 Turning the built-in analyzer off

Just don't start it. The hub starts nothing on its own — apps run only when you
press **▶ Start** on their card. Leave Auto-Fit & Optimiser (5107) stopped and
nothing will write competing conditions.

If you would rather it were not in the hub at all, delete its entry from
`apps.yml` and `POST /api/apps/reload`; no code change is needed.

### 1.5 Minimal working skeleton

```python
import time, pathlib, numpy as np

PROJECT = pathlib.Path("/path/to/project")
OUT     = PROJECT / "1D/SAXS/Conditions"

# Quality Gate running? read what it accepted. Otherwise read everything.
_SUB  = PROJECT / "1D/SAXS/Subtracted"
WATCH = (_SUB / "Good") if (_SUB / "Good").is_dir() else _SUB

seen, stable = {}, {}

def emit(rid, p):
    OUT.mkdir(parents=True, exist_ok=True)
    body = f"# proposed by my_ml\nrecipe_id = {rid}\n" + \
           "".join(f"{k} = {v:g}\n" for k, v in p.items())
    tmp = OUT / f"{rid}.txt.part"
    tmp.write_text(body)
    tmp.rename(OUT / f"{rid}.txt")        # atomic publish

while True:
    for f in sorted(WATCH.glob("*.dat")):
        sig = (f.stat().st_size, f.stat().st_mtime_ns)
        if seen.get(str(f)) == sig:       # already handled this version
            continue
        if stable.get(str(f)) != sig:     # still changing — look again next poll
            stable[str(f)] = sig
            continue
        seen[str(f)] = sig

        q, I, sigma = np.loadtxt(f, unpack=True)
        params = my_model.predict(q, I, sigma)     # ← your code
        # NOT f.stem: it contains "_sample", which the pipeline would strip,
        # truncating the id and breaking sample/background pairing.
        emit(time.strftime("ml_%Y%m%d_%H%M%S"), params)
    time.sleep(3)
```

That is the whole integration. Everything below is optional polish.

---

## 2. Optional: record your results in `manifest.json`

`manifest.json` at the project root is the shared provenance record. Writing to
it means the AI assistant, the hub event log and anyone reading the experiment
afterwards can see what your model did and why a given condition was chosen.

Each app owns one top-level key and must not write another's. `analyses` is the
natural home for your predictions — note it is shared by both the Data Analysis
and Auto-Fit & Optimiser apps, so merge into it rather than overwriting it.

```python
from src.manifest import update_manifest, add_analysis_entry, make_provenance
```

Not in Python? It is ordinary JSON — read, modify your own key, write back. Use
a lock or an atomic replace: several apps write this file concurrently.

---

## 3. Optional: use the event bus instead of polling

The hub runs a WebSocket bus at `ws://localhost:5100/ws`. Subscribe and you get
told the moment a file is ready, instead of polling every three seconds:

| Event | Fired when |
|---|---|
| `file.reduced` | reduction wrote a 1D `.dat` |
| `file.averaged` | the average app wrote an averaged curve |
| `file.subtracted` | background subtraction finished one profile |
| `file.classified` | the Quality Gate graded one (good/bad) |
| `fit.complete` | the analyzer's nanoparticle fit finished — `recipe_id`, `file`, `size`, `pdi`, `confidence`, `suspect`, `plot_png` |
| `analysis.complete` | the **Data Analysis** app's Guinier/Porod/Kratky/peak/model fit finished — a different payload (`analysis_type`, `file_path`, `results`). Don't confuse the two: they share a name pattern but not a shape, and this is exactly the mixup that once made the hub log show "undefined on ?" |

Envelope:

```json
{"type": "file.subtracted", "source_app": "background",
 "timestamp": "2026-09-03T14:30:00Z", "data": {"file_path": "/abs/path.dat"},
 "ai_triggered": false}
```

From Python:

```python
from src.events import EventBusClient
EventBusClient("my_ml").on_event(my_handler).connect(retry=True)
```

You can publish too — emitting `fit.complete` (with `recipe_id`, `file`, `size`,
`pdi`, `confidence`, `suspect`) makes your prediction show up in the hub's live
event feed and in the reactor's Slack notifications, exactly like the built-in
analyzer's. Use that exact name and shape — not `analysis.complete`, which is
the Data Analysis app's event under a similar name but a different payload.

**Polling still works and is the more robust choice** if your program may be
restarted: the folder is the durable record, the bus is a live notification and
it does not replay what you missed while you were down. The built-in apps use
both, folder-watching as the source of truth.

---

## 4. The other integration surfaces, briefly

Ranked by how stable they are. The first four are the intended contract; the
rest work but are more exposed to refactoring.

| Surface | Use it for | Stability |
|---|---|---|
| **Watched folders** (§1) | reading a stage's output, injecting the next stage's input | **stable** — the whole pipeline is built on it |
| **`.dat` format** (§1.2) | producing data the pipeline accepts as if it were reduced here | **stable** |
| **`manifest.json`** (§2) | provenance, cross-app state | **stable** — declared contract, one key per app |
| **Event bus** (§3) | live triggers | **stable** — event names are documented |
| **Import `src/`** | reusing the science directly (`from src.analysis.core import guinier_fit`) | stable-ish; it is a normal Python package, but internal signatures can change |
| **Hub control API** `:5100` | `/api/status`, `/api/start/<id>`, `/api/stop/<id>`, `/api/stop_all`, `/api/ports`, `/api/set_project`, `/api/apps/reload` | stable, small, and the only cross-app HTTP API that is meant to be called from outside |
| **Per-app REST routes** | driving an app's UI actions from a script | **not a public contract.** Each app has 14–27 routes serving its own front-end; only `/api/health` exists in all of them. Read the app's `app.py` and expect churn |
| **SSE log streams** `/api/stream` | mirroring live progress into your own UI | as above |
| **Register a new app** in `apps.yml` | shipping your program *as* a hub app, with a card and start/stop | stable — see [CLAUDE.md](../CLAUDE.md) § Adding a New App |
| **Env vars** | headless runs: `SWAXS_PROJECT`, `SWAXS_HUB_PORT`, `SWAXS_USER_ID`, `SWAXS_REACTOR_BACKEND`, `SWAXS_NO_RESUME` | stable |
| **`src/notify/`** | your own Slack/email alerts | stable |
| **`src/beamline/driver.py`** | SPEC bServer / EPICS, for hardware rather than software integration | read [BEAMLINE_SAFETY_AUDIT.md](audits/BEAMLINE_SAFETY_AUDIT.md) first |

---

## 5. Performance and reliability: is folder-polling actually the right way?

Short answer: **stable — yes, the most stable option available here; fast —
comfortably; efficient — only if you avoid the mistakes the built-in apps
made.** The detail matters if your program will run unattended for days.

### 5.1 The latency budget

Default poll intervals: 10 s for background and the Quality Gate, 3 s for the
reactor. So from "a subtracted file exists" to "the reactor accepted your
condition" is roughly:

```
10 s  Quality Gate picks it up (if you run it)
 +    your own poll interval
 3 s  reactor picks up your condition file
≈ 15-25 s
```

A synthesis-plus-acquisition cycle is minutes, so polling latency is around 1%
of the loop. Your model's inference time will dominate it. Do not optimise
this before measuring it.

(The platform-wide figure is worse — four stages polling in series gives a
measured ~264 s frame-to-fit latency, 99% of it waiting. That is tracked as
**N16** in `docs/audits/OPEN_DEFECTS.md` and is about the built-in chain, not
about your program.)

### 5.2 Why polling beats the alternatives *here*

- **The folder is a durable record.** Crash your program, restart it an hour
  later, and the work is still there. The event bus does not replay what you
  missed while you were down.
- **No connection state**, no broker, no ordering assumptions.
- **OS-level filesystem notifications (`inotify`/FSEvents/`watchdog`) are the
  obvious-looking upgrade and are the wrong choice here.** Beamline data
  frequently arrives over NFS/SMB or lands via SFTP, and native notifications
  are unreliable on network filesystems — they silently drop events under
  load. Polling does not care what the filesystem is.
- Atomic `.part`-then-rename writes plus the size/mtime stability check already
  close the torn-read hole.

### 5.3 Three anti-patterns to avoid — all of them real defects in this repo

Copying the built-in apps' *approach* is right. Copying their implementation
is not. Each of these is a filed defect:

1. **Do not re-parse the whole folder every poll.** The average app re-reads
   and re-parses every `.dat` in the Reduction folder on each 10 s cycle with
   no mtime filter; at ~10 000 files it falls progressively behind the
   acquisition it is tracking (**N7**). Keep an index of
   `path -> (size, mtime)` and only touch what changed. A poll then costs one
   `listdir` plus a `stat` per file — microseconds for hundreds of files.
2. **Track consumed files by name, never by count.** The average app's batch
   state is a count, and `read_folder` silently skips unreadable files, so one
   transient read failure shifts every subsequent batch boundary — a frame is
   reused, another dropped, and if the count ever exceeds the group length that
   keyword stops averaging entirely (**N4**). Store a set of filenames.
3. **Persist that set to disk.** In-memory only means a restart re-processes
   the entire experiment — which for the reduction app means rewriting every
   `.dat` and thousands of manifest writes (**N1**), and for the average app
   means re-emitting `file.averaged` for the whole night, which the reactor
   treats as measurement-complete (**N3**). Write your seen-set next to your
   output and reload it on startup.

Also: if you write to `manifest.json` on every prediction, batch it. Manifest
write cost is O(N²) — every entry embeds a full config snapshot, measured at
3.5 ms per write at 100 entries and 609 ms at 20 000 (**O3**).

### 5.4 The recommended shape

```
poll every 3-5 s  ──▶  mtime/size index  ──▶  filename-based dedup  ──▶  persist the seen-set
        ▲
        └── optionally also subscribe to file.subtracted on the bus, purely to wake early
```

Bus for latency, folder for truth. If the bus is down or you were restarted,
the folder loop is still correct on its own — which is the property that lets
this run for days.

### 5.5 When to outgrow it

Folder-polling stops being the right answer when you have tens of thousands of
files in one campaign folder, or several consumers that must not double-process
the same file, or you need transactional "claim this file" semantics. At that
point a small SQLite work-queue beside the folders is the natural step, and it
would fix the manifest cost too. Nothing at current scale demands it, and it is
a platform-level change rather than something to bolt onto one model.

---

## 6. Safety notes for anything that drives the reactor

Your program proposing conditions means your program can heat a reactor and
consume reagents. Before pointing it at real hardware:

- **Test against the mock backend first.** Mock is already the shipped default
  (`SWAXS_REACTOR_BACKEND` sets the startup default; the app's Mock/Real toggle
  governs at runtime). Confirm the app says Mock before you trust it — do not
  assume the env var alone protects you.
- **Out-of-range recipes are rejected, never silently clipped.** Limits come
  from three independent places: `bounds:`/`safety:` in `reactor/config.yml`,
  per-pump `max_flow` in the `pumps:` block, and `reactor_limits.json` (which
  only exists once limits have been edited in the UI for a project). Do not
  rely on any of them as your only guard — validate your own outputs.
- **Fractions must be physical AND in bounds.** Defaults: each of `x_ODE`,
  `x_TOP`, `x_oley` in 0.0–0.3, sum ≤ 0.9. The remainder is the precursor
  fraction (`Recipe.x_precursor`).
- A rejected condition file stays in `Conditions/` and is logged. If the reactor
  seems to ignore you, read its log before assuming the folder is wrong.
- See [docs/AUTONOMOUS_RUN_STEPS.md](AUTONOMOUS_RUN_STEPS.md) and
  [docs/audits/PRE_BEAMTIME_READINESS.md](audits/PRE_BEAMTIME_READINESS.md)
  before an unattended run.
