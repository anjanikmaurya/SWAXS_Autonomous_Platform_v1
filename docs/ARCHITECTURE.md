# SWAXS Platform v2 — Architecture Design

## Core Philosophy

**AI-first.** The data contract, event system, and memory layers are designed before individual app UIs. Every app inherits AI awareness automatically on registration — no retrofitting.

**Hub-and-spoke + event bus.** The hub (port 5000) is both a process manager and a WebSocket event broker. Apps never call each other directly; they publish events and subscribe to others through the hub.

**All logic in `src/`.** Apps remain thin Flask shells. New apps that follow the pattern get AI integration and provenance tracking for free.

---

## Implementation Status (June 2026)

This document describes the full v2 design. Not all of it is built yet. Use this
table to tell the difference between shipped code and planned work.

| Component | Status | Notes |
|---|---|---|
| Hub, `apps.yml` registry, WebSocket event bus | **Built** | `hub/app.py`, `src/events.py` |
| Manifest v2 (provenance, events, ai_memory) + v1→v2 migration | **Built** | `src/manifest.py` |
| Reduction pipeline (PyFAI, corrections, normalization) | **Built** | `src/reduction/core.py` |
| Viewer averaging / loading | **Built** | `src/plot_reduction.py` |
| AI subsystem (assistant, knowledge base, 3-layer memory, hints) | **Built** | `src/ai/` |
| Analysis: Guinier, Porod, Kratky, peak fit, sasmodels | **Built** | consolidated in `src/analysis/core.py` |
| Pair-distance p(r) / BIFT / GNOM | **Planned** | referenced by tools but not implemented |
| SAXS+WAXS auto-stitching (`src/reduction/stitch.py`) | **Planned** | section 5 below is a design sketch |
| Export module (`src/export/` — PDF, Word, annotated .dat) | **Planned** | section 6 module map is aspirational |

> Note: the section 6 module map lists analysis as separate files
> (`guinier.py`, `kratky_porod.py`, …). In the current code these are all
> functions inside `src/analysis/core.py`.

---

## System Architecture

```
                          ┌─────────────────────────────────┐
                          │  Hub  :5000                      │
                          │  • Dynamic app registry (apps.yml)│
                          │  • Subprocess manager            │
                          │  • WebSocket event broker /ws    │
                          └───────────────┬─────────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │         WebSocket Event Bus (pub/sub)                  │
              └──┬──────────┬──────────┬──────────┬──────────┬────────┘
                 │          │          │          │          │
           :5001       :5002       :5003       :5004       :5005
        Reduction    Viewer    Background   Analysis   AI Assistant
                                                            │
                                          ┌─────────────────┘
                                          │  src/ai/
                                          │  ├── assistant.py   Claude API client
                                          │  ├── knowledge.py   ChromaDB vector store
                                          │  ├── memory.py      3-layer memory
                                          │  ├── hints.py       proactive hint gen
                                          │  └── plots.py       inline plot gen
                                          │
                                          │  ai_knowledge/
                                          │  ├── literature/    SAXS/WAXS PDFs
                                          │  ├── beamline/      facility YAML configs
                                          │  └── vector_db/     ChromaDB persistent
```

---

## Component Decisions

### 1. Event Bus

**Pattern:** Hub-mediated WebSocket pub/sub using `flask-sock`.

Apps connect to `ws://localhost:5000/ws` on startup. Each message is a JSON object:

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
| Type | Published by |
|---|---|
| `file.reduced` | reduction |
| `file.averaged` | viewer |
| `file.stitched` | viewer |
| `file.subtracted` | background |
| `analysis.complete` | analysis |
| `ai.hint` | assistant |
| `watch.new_raw` | reduction (watch mode) |
| `app.started` / `app.stopped` | hub |

The hub appends each event to `manifest["events"]` (rolling last 100). After any `file.*` or `analysis.*` event, the AI assistant is notified and may emit an `ai.hint` back.

---

### 2. Dynamic App Registry — `apps.yml`

Replaces the hardcoded `APPS` list in `hub/app.py`.

```yaml
apps:
  - id: reduction
    name: "Reduction & Correction"
    port: 5001
    entry: "reduction/app.py"
    knowledge: "reduction/knowledge.md"    # AI indexes this on registration
    manifest_key: "files"                  # which manifest section this app owns

  - id: viewer
    name: "Data Viewer"
    port: 5002
    entry: "viewer/app.py"
    knowledge: "viewer/knowledge.md"
    manifest_key: "files"

  - id: background
    name: "Background Subtraction"
    port: 5003
    entry: "background/app.py"
    knowledge: "background/knowledge.md"
    manifest_key: "background"

  - id: analysis
    name: "Data Analysis"
    port: 5004
    entry: "analysis/app.py"
    knowledge: "analysis/knowledge.md"
    manifest_key: "analyses"

  - id: assistant
    name: "AI Assistant"
    port: 5005
    entry: "assistant/app.py"
    knowledge: "assistant/knowledge.md"
    manifest_key: "ai_memory"
```

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
      "stage": "reduced | averaged | subtracted | analysed",
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

---

### 4. AI Subsystem

#### Knowledge Sources (all fed into ChromaDB)

| Source | Location | Notes |
|---|---|---|
| SAXS/WAXS literature | `ai_knowledge/literature/*.pdf` | Glatter & Kratky, Feigin & Svergun, SasView docs, review papers |
| User sample PDFs | uploaded at runtime | chunked and added to ChromaDB session collection |
| Per-app knowledge | `<app>/knowledge.md` | auto-indexed when app registered |
| Beamline configs | `ai_knowledge/beamline/*.yml` | instrument quirks, detector artifacts, calibration notes |
| User corrections | `~/.swaxs/memory/<user>/corrections.jsonl` | remembered overrides, higher retrieval weight |
| Experiment history | manifest.json RAG | past fits, keywords, decisions across sessions |

#### 3-Layer Memory

```
~/.swaxs/memory/
└── users/
    └── albert/
        ├── corrections.jsonl       ← user's confirmed AI overrides
        ├── preferences.yml         ← UI prefs, default fit ranges
        └── session_summaries/      ← per-session digests

<project_root>/.swaxs/
└── memory/
    ├── experiment_history.jsonl    ← RAG over past processing decisions
    └── quality_log.jsonl           ← AI flags per file, per project

ai_knowledge/beamline/
└── ssrl_1-5.yml                   ← facility-level shared config
```

#### Context Assembly (per API call)

```python
def build_context(user_query, app_id, project_root, user_id):
    chunks   = knowledge.retrieve(user_query, top_k=8)       # ChromaDB
    manifest = summarise_manifest(project_root)               # current state
    events   = get_recent_events(project_root, n=10)          # event bus log
    memory   = memory.load_layered(user_id, project_root)     # 3-layer
    app_ctx  = f"User is currently in the {app_id} app."
    return system_prompt + chunks + manifest + events + memory + app_ctx
```

#### Claude API Tool Definitions

```python
TOOLS = [
    { "name": "generate_plot",    "description": "Generate a matplotlib plot and return as base64 PNG" },
    { "name": "run_analysis",     "description": "Run a Guinier/Kratky/Porod/p(r) analysis on a file" },
    { "name": "query_manifest",   "description": "Query the manifest for files matching given criteria" },
    { "name": "add_note",         "description": "Attach a user note to a file in the manifest" },
    { "name": "flag_quality",     "description": "Flag a quality issue on a file (aggregation, damage, etc.)" },
    { "name": "ingest_pdf",       "description": "Chunk and index a new PDF into the knowledge base" },
]
```

#### Proactive Hints

After every event bus message of type `file.*` or `analysis.*`, `src/ai/hints.py` checks:

- **Guinier range** — is qRg within [0.3, 1.3]?
- **Radiation damage** — does I(q) increase at low q between early and late frames?
- **Aggregation** — upturn at low q in averaged curve?
- **Poor S/N** — sigma/I ratio above threshold at high q?
- **I0 outlier** — individual scan I0 deviates >20% from median?

Hints are emitted as `ai.hint` events and displayed inline in whichever app the user is currently in.

---

### 5. SAXS+WAXS Auto-Stitching (`src/reduction/stitch.py`)

```python
def auto_stitch(saxs_dat, waxs_dat, overlap_fraction=0.3):
    """
    1. Find q overlap region between SAXS and WAXS curves.
    2. Compute scale factor S to minimise chi-squared in overlap:
           chi2 = sum((I_SAXS - S * I_WAXS)^2 / sigma^2) over overlap
    3. Apply S to WAXS, concatenate and sort by q.
    4. Return merged curve + scale_factor + overlap_q_range.
    """
```

Both individual files (SAXS, WAXS) and the merged file are saved, all with v2 provenance.

---

### 6. src/ Module Map

```
src/
├── manifest.py              Manifest v2 read/write/provenance
├── events.py                Event bus WebSocket client (pub/sub)
├── plot_reduction.py        read_folder, average_and_save
│
├── ai/
│   ├── assistant.py         Claude API — build_context, chat, tool dispatch
│   ├── knowledge.py         ChromaDB — ingest_pdf, ingest_md, retrieve
│   ├── memory.py            3-layer memory load/save
│   ├── hints.py             Proactive hint checker (per event type)
│   └── plots.py             matplotlib → base64 for AI responses
│
├── analysis/
│   ├── guinier.py           fit_guinier(q, I, sigma, q_range) → {Rg, I0, chi2, quality}
│   ├── kratky_porod.py      kratky_plot, porod_analysis, dimensionless_kratky
│   ├── pair_distance.py     indirect_fourier_transform(q, I, sigma) → p(r)
│   └── model_fitting.py     fit_sphere, fit_cylinder, fit_core_shell
│
├── export/
│   ├── pdf_report.py        generate_session_report(manifest, output_path)
│   ├── docx_report.py       generate_word_report(manifest, output_path)
│   └── dat_with_fits.py     write_dat_with_fits(path, q, I, sigma, fit_params)
│
├── reduction/
│   ├── core.py              Experiment, run_pipeline, find_new_raw_files
│   ├── process_metadata.py  CSV/PDI metadata extraction
│   ├── read_raw_file.py     binary .raw reader
│   └── stitch.py            auto_stitch(saxs_dat, waxs_dat)
│
└── utils/
    └── read_dat_metadata.py read_dat_data_metadata
```

---

### 7. Build Roadmap

| Phase | Deliverable | Key files |
|---|---|---|
| 0 | Foundation — manifest v2, event bus, apps.yml | `src/manifest.py`, `src/events.py`, `hub/app.py`, `apps.yml` |
| 1 | AI core — knowledge base, memory, Claude client | `src/ai/`, `ai_knowledge/`, `assistant/app.py` |
| 2 | Reduction — provenance, watch mode, stitch | `src/reduction/stitch.py`, `reduction/app.py` |
| 3 | Viewer — 2D display, averaging, cross-project overlay | `viewer/app.py`, `viewer/templates/` |
| 4 | Background — 3 modes, auto scale | `background/app.py` |
| 5 | Analysis — Guinier, Kratky, Porod, p(r), models | `src/analysis/`, `analysis/app.py` |
| 6 | Export — PDF, Word, annotated .dat | `src/export/` |

---

### 8. New Dependencies

```
# requirements.txt additions
chromadb==0.5.3               # vector database (embedded, no server)
sentence-transformers==3.0.1  # local embeddings for ChromaDB
flask-sock==0.7.0              # WebSocket support for event bus
reportlab==4.2.0               # PDF generation
python-docx==1.1.2            # Word document generation
pyyaml==6.0.2                  # already present — apps.yml parsing
anthropic==0.30.0              # Claude API client
```

---

### 9. Per-App `knowledge.md` Template

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

- `uv run` for all Python execution
- `src.*` import pattern in all app.py files  
- `.dat` file format (q, I, sigma + METADATA INFORMATION footer)
- Flask routing pattern in all apps
- `start_platform.sh` as the single entry point
- PyFAI / fabio for detector integration

---

## Open Question (resolve before Phase 3)

**Averaging strategy** — the best choice for SAXS/WAXS data (I0-weighted, sigma-clipping, pairwise similarity) depends on the specific beamline and detector noise model. A literature review of established practices (ATSAS, SasView, beamline-specific papers) should be completed before implementing `average_and_save` extensions.
