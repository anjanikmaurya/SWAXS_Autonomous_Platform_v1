# SWAXS Platform v2 — Architecture Design

> **Historical design record (original v2).** This document is the ORIGINAL v2
> architecture design and is kept for reference. The platform has since grown
> beyond it: the **Calibration**, **Quality Gate**, **Flow-Synthesis reactor**
> (with SPEC/beamline control), and **Analyzer/optimizer** apps have all
> shipped, along with the autonomous closed loop (reduction → analysis →
> optimizer → reactor). Read the sections below as the original plan, not the
> current app inventory; `apps.yml` is the live registry.

## Core Philosophy

**AI-first.** The data contract, event system, and memory layers are designed before individual app UIs. Every app inherits AI awareness automatically on registration — no retrofitting.

**Hub-and-spoke + event bus.** The hub (port 5100) is both a process manager and a WebSocket event broker. Apps never call each other directly; they publish events and subscribe to others through the hub.

**All logic in `src/`.** Apps remain thin Flask shells. New apps that follow the pattern get AI integration and provenance tracking for free.

---

## Implementation Status (August 2026)

This document describes the full v2 design. Not all of it is built. Use this
table to tell the difference between shipped code and planned work.

| Component | Status | Notes |
|---|---|---|
| Hub, `apps.yml` registry, WebSocket event bus | **Built** | `hub/app.py`, `src/events.py` |
| Manifest v2 (provenance, events, ai_memory) + v1→v2 migration | **Built** | `src/manifest.py` |
| Reduction pipeline (PyFAI, corrections, normalization) | **Built** | `src/reduction/core.py` |
| Average-app averaging / loading | **Built** | `src/plot_reduction.py` |
| AI subsystem (assistant, knowledge base, layered memory, hints) | **Built** | `src/ai/` |
| Analysis: Guinier, Porod, Kratky, peak fit, sasmodels | **Built** | consolidated in `src/analysis/core.py` |
| Pair-distance p(r) | **Built** | `pair_distance_ift` — Tikhonov-regularized IFT, `src/analysis/core.py:231`. **Not BIFT**; no Bayesian IFT was ever written |
| GNOM p(r) | **Built** | ATSAS binary wrapper only (`run_datgnom`, `src/analysis/atsas.py:118`); returns a clean error when `datgnom` is not on `PATH` |
| Export (PDF report, fit tables, annotated `.dat`) | **Built, not where this doc said** | `src/export/` does not exist. PDF via matplotlib `PdfPages` in `src/ai/assistant.py:1602-1628` with an HTML fallback (`:1633+`); CSV/XLSX fit tables at `:1577`; `.dat` footer annotation in `src/analysis/io.py` (`_ANNOTATE_MARKER`, `:45`) |
| Word (`.docx`) export | **Never built** | No `python-docx`/`reportlab` dependency and no `.docx` writer anywhere in the tree |
| SAXS+WAXS auto-stitching | **Never built** | No `src/reduction/stitch.py`, no `auto_stitch()`. The average app's "Stitch SAXS+WAXS" is a display-only checkbox (`average/templates/index.html:1182`, `2338-2345`): it co-plots the two curves, computes no scale factor and writes no merged file |

Four apps shipped after this document was written and are not described
anywhere below: **calibration** (:5101), **quality** (:5105), **reactor**
(:5108), **analyzer** (:5107). See `apps.yml` for the live registry and
`README.md` for what each one does.

> Note: the section 5 module map has been regenerated from the tree. Earlier
> revisions listed analysis as separate files (`guinier.py`,
> `kratky_porod.py`, …); those are all functions inside
> `src/analysis/core.py`.

---

## System Architecture

```
                          ┌─────────────────────────────────┐
                          │  Hub  :5100                      │
                          │  • Dynamic app registry (apps.yml)│
                          │  • Subprocess manager            │
                          │  • WebSocket event broker /ws    │
                          └───────────────┬─────────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │         WebSocket Event Bus (pub/sub)                  │
              └───────────────────────────┬───────────────────────────┘
                                          │
        ┌─────────────────────────────────┴─────────────────────────────────┐
        │  :5101  Calibration      .raw → CBF, pyFAI .poni generation       │
        │  :5102  Reduction        2D → 1D, corrections, normalization      │
        │  :5103  Average          2D/1D display, scan averaging            │
        │  :5104  Background       keyword / scan-matched / manual subtract  │
        │  :5105  Quality Gate     good/bad grading, auto-sort              │
        │  :5106  Analysis         Guinier, Porod, Kratky, p(r), models     │
        │  :5107  Analyzer         auto-fit size/PDI + Bayesian optimizer   │
        │  :5108  Reactor          5-pump flow synthesis + SPEC control     │
        │  :5109  AI Assistant     interpretation, hints, inline plots      │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │
              closed loop: reactor → SPEC collect → reduction → averaging
                         → subtraction → analyzer → optimizer → next recipe

  src/ai/                                 ai_knowledge/
  ├── assistant.py   Claude API client    ├── literature/  SAXS/WAXS PDFs
  ├── knowledge.py   ChromaDB store       ├── beamline/    facility YAML configs
  ├── memory.py      layered memory       ├── group/       shared SOPs
  ├── hints.py       proactive hints      └── vector_db/   ChromaDB persistent
  ├── plots.py       inline plot gen
  ├── code_exec.py   guarded sandbox
  └── loop_advice.py loop narration
```

---

## Component Decisions

### 1. Event Bus

**Pattern:** Hub-mediated WebSocket pub/sub using `flask-sock`.

Apps connect to `ws://localhost:5100/ws` on startup. Each message is a JSON object:

```json
{
  "type": "file.reduced",
  "source_app": "reduction",
  "timestamp": "2026-06-12T14:30:00Z",
  "data": {
    "file_path": "/abs/path/sample_0001_SAXS.dat",
    "keyword": "sample_A",
    "scan_idx": 1
  },
  "ai_triggered": false
}
```

**Event types:**
| Type | Published by | Consumed by |
|---|---|---|
| `file.reduced` | reduction | hub log |
| `file.averaged` | average | reactor — ends the run on the first new SAXS average (`reactor/app.py:307`) |
| `file.subtracted` | background | quality — grades immediately (`quality/app.py:466`) |
| `file.classified` | quality (`quality/app.py:385`) | hub log |
| `analysis.complete` | analyzer (`analyzer/app.py:479`) | reactor — posts the fit into the recipe's Slack thread (`reactor/app.py:309`) |
| `ai.hint` | assistant | whichever app the user is in |
| `watch.new_raw` | reduction (watch mode) | hub log |
| `app.started` / `app.stopped` / `app.reclaimed` | hub | hub UI |
| `app.connected` | any app on bus connect (`src/events.py:376`) | hub UI |
| `project.set` | hub (`hub/app.py:723`) | hub UI |
| `file.stitched` | **nobody** | — |

`file.stitched` has a publisher helper (`emit_file_stitched`,
`src/events.py:255`) and zero callers, because auto-stitching was never built.
The hub UI still colour-codes it (`hub/templates/index.html:368`); that chip can
never appear. Conversely `file.classified` **is** published but is missing from
the hub's `_TYPE_CLASS` map, so it renders unstyled.

The hub appends each event to `manifest["events"]` (rolling last 100,
`_EVENTS_MAX` in `src/manifest.py:119`). After any `file.*` or `analysis.*`
event, the AI assistant is notified and may emit an `ai.hint` back.

---

### 2. Dynamic App Registry — `apps.yml`

Replaces the hardcoded `APPS` list in `hub/app.py`. `apps.yml` is the live
registry — read it rather than this excerpt. Per-entry fields:

| Field | Required | Purpose |
|---|---|---|
| `id` | yes | unique slug used internally |
| `name` | yes | display name on the hub card |
| `port` | yes | TCP port the app listens on |
| `entry` | yes | path to `app.py` relative to the project root |
| `description` | no | one-line blurb on the hub card |
| `icon` | no | emoji on the hub card |
| `icon_image` | no | image URL, overrides `icon` (only `assistant` uses it) |
| `color` | no | hex accent for the hub card |
| `knowledge` | no | path to a `knowledge.md` the AI indexes on registration |
| `manifest_key` | no | top-level manifest section this app owns |

Shipped entries, in registry order:

| id | port | entry | manifest_key | knowledge |
|---|---|---|---|---|
| calibration | 5101 | `calibration/app.py` | — | **none** |
| reduction | 5102 | `reduction/app.py` | `files` | yes |
| average | 5103 | `average/app.py` | `files` | yes |
| background | 5104 | `background/app.py` | `background` | yes |
| quality | 5105 | `quality/app.py` | `quality` | yes |
| analysis | 5106 | `analysis/app.py` | `analyses` | yes |
| reactor | 5108 | `reactor/app.py` | `reactor` | yes |
| analyzer | 5107 | `analyzer/app.py` | `analyses` | **none** |
| assistant | 5109 | `assistant/app.py` | `ai_memory` | yes |

calibration and analyzer carry no `knowledge:` key, so the assistant has no
indexed description of either — questions about `.poni` generation or the
nanoparticle fit fall back to the manifest and the system prompt.

**Adding a new app:** add an entry to `apps.yml`. The hub discovers it on next start (no code changes). The AI automatically indexes `knowledge.md`.

---

### 3. Manifest Schema v2

Full backwards-compatible extension of v1. New keys marked with `# NEW`.

```json
{
  "version": "2.0",
  "project_root": "/abs/path/to/experiment",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",

  "project_meta": {                          // NEW
    "facility": "SSRL",
    "beamline": "1-5",
    "users": ["albert"],
    "beamtime_id": "optional"
  },

  "files": {
    "/abs/path/sample_0001_SAXS.dat": {
      "path": "...",
      "stage": "raw | reduced | averaged | subtracted | analysed",
      "detector": "saxs | waxs | combined",
      "keyword": "sample_A",
      "scan_idx": 1,
      "metadata": { "i0": 1.23, "T": 0.95, "thickness_mm": 1.0 },

      "provenance": {                        // NEW — full audit trail
        "app": "reduction",
        "app_version": "2.0.0",
        "run_id": "uuid4",
        "timestamp": "ISO-8601",
        "input_files": ["/abs/path/sample_0001.raw"],
        "config_hash": "sha256:abc123",
        "config_snapshot": { "npt_radial": 1000, "error_model": "poisson" }
      },

      "status": "ok | stale | locked",      // NEW
      "notes": "",                           // NEW — user free text
      "quality_flags": []                    // NEW — AI + user flags
    }
  },

  "analyses": {
    "uuid4": {
      "id": "uuid4",
      "type": "guinier | porod | kratky | pair_distance | model",
      "file_path": "...",
      "params": {},
      "results": {},
      "fit_range": [0.01, 0.05],            // NEW — q range used
      "quality_score": 0.95,                // NEW — 0-1 fit quality
      "ai_assessment": "Rg = 3.2 nm, ...", // NEW — AI interpretation
      "provenance": { "app": "analysis", "run_id": "..." },  // NEW
      "created_at": "ISO-8601"
    }
  },

  "background": {
    "/abs/path/subtracted.dat": {
      "sample_path": "...",
      "bkg_path": "...",
      "scale": 1.0,
      "scale_method": "auto | manual | concentration",  // NEW
      "scale_confidence": 0.98,                         // NEW
      "mode": "keyword | scan_matched | user_defined",
      "provenance": { "app": "background", "run_id": "..." },  // NEW
      "created_at": "ISO-8601"
    }
  },

  "quality": {                              // added with the Quality Gate app
    "/abs/path/subtracted.dat": {
      "score": 0.87, "verdict": "good | bad",
      "flags": [], "metrics": {}, "reasons": [],
      "detector": "saxs", "sample": "sample_A",
      "source": "ai | user", "llm_note": "...",
      "overridden": false, "override_note": "",
      "analysis_ready": true,
      "provenance": {}, "created_at": "ISO-8601"
    }
  },

  "reactor": {                              // added with the Flow Synthesis app
    "runs": {
      "<recipe_id>": {
        // the controller's run record, verbatim: recipe_id, recipe, setpoints,
        // started, ended, duration_s, reason, status
        "logged_at": "ISO-8601"
      }
    }
  },

  "ai_memory": {                            // NEW — entire section
    "corrections": [
      { "turn": 42, "original": "...", "corrected": "...", "ts": "..." }
    ],
    "session_summaries": [
      { "session_id": "uuid", "summary": "...", "ts": "..." }
    ],
    "quality_flags": {
      "/abs/path/file.dat": ["possible_aggregation", "radiation_damage_suspected"]
    },
    "user_context": {
      "sample_type": "protein",
      "expected_Rg_nm": 3.5,
      "background": "20mM HEPES pH 7.4",
      "concentration_mg_ml": 5.0
    }
  },

  "events": [                               // NEW — rolling last 100
    {
      "type": "file.reduced",
      "source_app": "reduction",
      "timestamp": "ISO-8601",
      "data": {},
      "ai_triggered": false
    }
  ]
}
```

`_empty_manifest` (`src/manifest.py:709-746`) seeds `project_meta`, `files`,
`analyses`, `background`, `ai_memory`, and `events`. `quality` and `reactor` are
**not** seeded — they are created on first write via `setdefault`
(`add_quality_record` at `:515`, `add_reactor_run` at `:561`), so a project that
has never run the Quality Gate or the reactor simply has no such key. Readers
must tolerate their absence.

---

### 4. AI Subsystem

#### Knowledge Sources (all fed into ChromaDB)

| Source | Location | Notes |
|---|---|---|
| SAXS/WAXS literature | `ai_knowledge/literature/*.pdf` | Glatter & Kratky, Feigin & Svergun, SasView docs, review papers |
| User sample PDFs | uploaded at runtime | chunked and added to ChromaDB session collection |
| Per-app knowledge | `<app>/knowledge.md` | auto-indexed when app registered |
| Beamline configs | `ai_knowledge/beamline/*.yml` | instrument quirks, detector artifacts, calibration notes |
| User corrections | `~/.swaxs/memory/users/<user>/corrections.jsonl` | remembered overrides, higher retrieval weight (`src/ai/memory.py:83`) |
| Group SOPs | `ai_knowledge/group/sops.json` | naming schemes, default models, buffer rules — always loaded (`src/ai/memory.py:95`) |
| Experiment history | manifest.json RAG | past fits, keywords, decisions across sessions |

#### Layered Memory

Layer numbering follows `src/ai/memory.py:6-16`: **Layer 1 = user**
(cross-project), **Layer 2 = project**, **Layer 3 = facility**, plus an
unnumbered group layer.

```
~/.swaxs/memory/                    ← Layer 1 (user, cross-project)
└── users/
    └── albert/
        ├── corrections.jsonl       ← user's confirmed AI overrides
        ├── preferences.yml         ← UI prefs, default fit ranges
        └── session_summaries/      ← per-session digests

<project_root>/.swaxs/              ← Layer 2 (project, travels with the data)
└── memory/
    ├── experiment_history.jsonl    ← RAG over past processing decisions
    ├── quality_log.jsonl           ← AI flags per file, per project
    └── chat_history.jsonl          ← per-project chat continuity

ai_knowledge/beamline/              ← Layer 3 (facility, shared)
└── ssrl_1-5.yml                   ← instrument quirks, calibration notes

ai_knowledge/group/                 ← group layer (cross-project, cross-user)
└── sops.json                      ← created on first write
```

#### Context Assembly (per API call)

```python
def build_context(user_query, app_id, project_root, user_id):
    chunks   = knowledge.retrieve(user_query, top_k=8)       # ChromaDB
    manifest = summarise_manifest(project_root)               # current state
    events   = get_recent_events(project_root, n=10)          # event bus log
    memory   = memory.load_layered(user_id, project_root)     # layered
    app_ctx  = f"User is currently in the {app_id} app."
    return system_prompt + chunks + manifest + events + memory + app_ctx
```

#### Claude API Tool Definitions

18 tools ship, defined in `src/ai/assistant.py:66-570`. The original design
listed six; the rest were added across Phases 1–5 (see
`docs/ASSISTANT_FUNCTIONAL_SPEC.md`).

| Tool | Purpose |
|---|---|
| `generate_plot` | matplotlib plot → base64 PNG |
| `plot_metadata` | I0 / bstop / transmission / thickness / CTEMP over time |
| `overlay_curves` | profile comparison; the one tool that also emits an interactive Plotly figure |
| `fit_model` | run a recommended sasmodels fit → params + reduced χ² + fit/residuals plot |
| `list_saxs_models` | enumerate the sasmodels catalog |
| `run_analysis` | Guinier / Kratky / Porod / p(r) on a file |
| `compute_pr` | `pair_distance_ift` → Rg, Dmax, I0 + inline p(r) plot |
| `assess_quality` | frame-outlier (I0 robust-MAD) + transmission/beam sanity |
| `query_manifest` | query the manifest for files matching criteria |
| `add_note` | attach a user note to a file in the manifest |
| `flag_quality` | flag a quality issue on a file |
| `export` | session report (HTML/PDF), fit table (CSV/XLSX), notes → `assistant_outputs/` |
| `set_preferences` | audience, verbosity, default model, units, citation style → Layer 1 |
| `group_sops` | list/add/remove shared group conventions |
| `ingest_pdf` | chunk and index a PDF into the knowledge base |
| `manage_knowledge` | list / add_pdf / add_note / ingest_folder / remove |
| `web_search` | Crossref lookup; online-only, degrades with a clear message |
| `run_python` | guarded sandbox (`src/ai/code_exec.py`) — AST allowlist, `python -I`, rlimits |

#### Proactive Hints

After every event bus message of type `file.*` or `analysis.*`, `src/ai/hints.py` checks:

- **Guinier range** — is qRg within [0.3, 1.3]?
- **Radiation damage** — does I(q) increase at low q between early and late frames?
- **Aggregation** — upturn at low q in averaged curve?
- **Poor S/N** — sigma/I ratio above threshold at high q?
- **I0 outlier** — individual scan I0 deviates >20% from median?

Hints are emitted as `ai.hint` events and displayed inline in whichever app the user is currently in.

---

### 5. src/ Module Map

Regenerated from the tree, not from the original design.

```
src/
├── manifest.py              Manifest v2 read/write/provenance
├── events.py                Event bus WebSocket client (pub/sub)
├── plot_reduction.py        read_folder, average_and_save
├── loop_naming.py           recipe_id ↔ filename conventions, match_recipe_id
├── runstate.py              persisted run state for restart recovery
├── proc_lifecycle.py        subprocess/pid/log-rotation helpers used by the hub
│
├── ai/
│   ├── assistant.py         Claude API — build_context, chat, 18-tool dispatch, exports
│   ├── knowledge.py         ChromaDB — ingest_pdf, ingest_text, retrieve, remove_source
│   ├── memory.py            layered memory (user / project / facility / group)
│   ├── hints.py             Proactive hint checker (per event type)
│   ├── plots.py             matplotlib → base64; overlay_plotly interactive figures
│   ├── code_exec.py         guarded Python sandbox for the run_python tool
│   └── loop_advice.py       narration/advice over the autonomous loop
│
├── analysis/
│   ├── core.py              guinier_fit, porod_fit, kratky_plot, pair_distance_ift,
│   │                        dimensionless_kratky, classical_invariants, guinier_quality,
│   │                        peak_fit, sasmodels_fit, sasmodels_params
│   ├── io.py                Analysed/ paths, save bundle, .dat annotation, batch summary
│   ├── atsas.py             wrappers for autorg / datgnom / datporod / datvc / datmw / dammif
│   └── nanoparticle.py      size/PDI/phase/confidence fit — the optimizer's input
│
├── quality/
│   └── core.py              grade_profile, score_metrics — good/bad grading
│
├── reactor/
│   ├── config.py            load/validate reactor/config.yml
│   ├── controller.py        arm → run → flush state machine, run records
│   ├── hardware.py          pump abstraction, flow calibration, limits
│   ├── recipe.py            Recipe model, param file read/write
│   ├── intake.py            decide_intake — "skip"|"wait"|"go" on a growing file
│   └── drivers/Py_P_Pump.py syringe-pump serial driver
│
├── beamline/
│   └── driver.py            SPEC bServer HTTP driver (shutter, counters, 2D collect)
│
├── optimizer/
│   ├── campaign.py          CampaignController — ask/tell, GP surrogate, stop rule
│   ├── space.py             5-parameter recipe space, bounds, constraints, Sobol pool
│   ├── gp.py                Gaussian-process surrogate
│   ├── diagnostics.py       convergence / replicate / ablation statistics
│   ├── io.py                Conditions/ file contract (to_param_file / parse_param_file)
│   └── plots.py             parameter-space figures (analyzer panel + docs/figures)
│
├── simulator/
│   ├── ground_truth.py      recipe → (R, PDI) hidden landscape
│   ├── pattern.py           form factor, q map from .poni, mask/beamstop, Poisson
│   ├── writer.py            .raw + CSV/PDI metadata, beamline filename convention
│   └── collector.py         SimulatedCollector — orchestration behind MockBeamline
│
├── notify/
│   ├── slack.py             SlackNotifier — threaded per-recipe run notifications
│   ├── email_notify.py      EmailNotifier + smtp_probe
│   └── multi.py             fan one notification out to every configured channel
│
├── preprocess/
│   ├── calib.py             calibrant handling for the calibration app
│   ├── raw_convert.py       .raw → CBF conversion
│   └── sftp_sync.py         pull data from the beamline over SFTP
│
├── reduction/
│   ├── core.py              Experiment, run_pipeline, find_new_raw_files
│   ├── process_metadata.py  CSV/PDI metadata extraction
│   └── read_raw_file.py     binary .raw reader
│
└── utils/
    └── read_dat_metadata.py read_dat_data_metadata
```

Note what is **not** here, contrary to earlier revisions of this document:
no `src/export/`, no `src/pipeline/`, no `src/background/`, no
`src/reduction/stitch.py`, and no per-method files under `src/analysis/`.
Subtraction math still lives in `background/app.py`, which is the one live
violation of the "all logic in `src/`" rule
(see `docs/design/AUTOPILOT_PIPELINE_DESIGN.md` §4).

---

### 6. Build Roadmap

| Phase | Deliverable | Key files | Status |
|---|---|---|---|
| 0 | Foundation — manifest v2, event bus, apps.yml | `src/manifest.py`, `src/events.py`, `hub/app.py`, `apps.yml` | shipped |
| 1 | AI core — knowledge base, memory, Claude client | `src/ai/`, `ai_knowledge/`, `assistant/app.py` | shipped |
| 2 | Reduction — provenance, watch mode | `reduction/app.py` | shipped |
| 3 | Visualisation & Average — 2D display, averaging, cross-project overlay | `average/app.py`, `average/templates/` | shipped |
| 4 | Background — 3 modes, auto scale | `background/app.py` | shipped |
| 5 | Analysis — Guinier, Kratky, Porod, p(r), models | `src/analysis/`, `analysis/app.py` | shipped |
| 6 | Export — PDF report, fit tables, annotated .dat | `src/ai/assistant.py`, `src/analysis/io.py` | shipped, minus Word |

Everything beyond phase 6 — the Quality Gate, reactor, analyzer/optimizer,
calibration, simulator, notifications — was designed after this document and is
not on this roadmap.

---

### 7. Dependencies added for v2

Live versions, from `requirements.txt` / `requirements-core.txt` /
`requirements-ai.txt`:

```
flask-sock>=0.7            # WebSocket event bus (hub :5100/ws)
simple-websocket>=1.0      # flask-sock's transport
pyyaml>=6.0                # config.yml, apps.yml
anthropic>=0.40            # Claude API client
chromadb>=0.5              # vector store (pinned 0.5.23 in requirements.txt)
sentence-transformers>=3.0 # local embeddings — PULLS TORCH (~2 GB)
```

`reportlab` and `python-docx` appear in no requirements file. PDF export uses
matplotlib's `PdfPages`, which is already a dependency; Word export does not
exist.

---

### 8. Per-App `knowledge.md` Template

Each app ships a `knowledge.md` that the AI indexes on registration. Example (`reduction/knowledge.md`):

```markdown
# Reduction App — AI Knowledge

## What this app does
Converts 2D detector images (.raw) to 1D I(q) curves using PyFAI azimuthal integration.
Applies transmission, thickness, and I0 corrections.

## Key parameters
- npt_radial: number of radial integration points (default 1000)
- error_model: "poisson" (detector shot noise) or "azimuthal" (azimuthal variance)
- mask: detector regions to exclude (beamstop, bad pixels)

## Common issues and fixes
- Negative intensities after correction → check i0_offset and bstop_offset
- Ring artifacts → mask file may be misaligned with data
- Transmission > 1.0 → check i0_air / bstop_air values

## Output format
1D .dat files with columns: q (nm⁻¹), I(q), sigma(q)
Footer contains METADATA INFORMATION section with i0, T, thickness values.
```

---

## What Does Not Change

- `src.*` import pattern in all app.py files
- `.dat` file format (q, I, sigma + METADATA INFORMATION footer)
- Flask routing pattern in all apps
- PyFAI / fabio for detector integration

### What did change

- **`uv run` is legacy.** The hub launches every app with `sys.executable`
  (`hub/app.py:319-325`, which documents the migration): there is no
  `pyproject.toml` and no `uv.lock`, and the virtualenv is `venv/` rather than
  `.venv/`, so `uv run` spins up a *separate* environment without the
  pip-installed dependencies and the app dies instantly with
  `ModuleNotFoundError`. `CLAUDE.md` and every app docstring now say plain
  `python` from the activated `venv/`; there is no supported `uv` path here.
- **Three launchers, not one.** `start_platform.sh`, `start_platform.ps1`,
  `start_platform.bat`. All three load `.env` and resolve the AI token before
  starting the hub; `python hub/app.py` does not.
