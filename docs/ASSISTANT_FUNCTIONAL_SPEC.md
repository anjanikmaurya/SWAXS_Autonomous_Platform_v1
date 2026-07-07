# Tassone Group Assistant — Functional Specification

Derived from a 24-question requirements interview (2026-06-18). This captures the
intended full functionality, the design decisions, conflicts to resolve, and a
build roadmap mapped against what already exists.

---

## 1. Vision

A SAXS/WAXS analysis copilot embedded in the SWAXS Platform that **interprets
data, guides analysis, and watches quality** — proactively, but acting only with
confirmation, and never modifying the user's data. It adapts its depth to the
audience (expert → student → PI), grounds its science in both built-in theory and
the group's own literature, and recommends models with concrete starting
parameters before running fits.

---

## 2. Decisions from the interview

### Role & interaction
| Area | Decision |
|---|---|
| Core focus | Data interpretation · guided analysis · QC & anomaly detection |
| Autonomy | **Act only with confirmation** (proposes, then acts on approval) |
| Proactivity | **Proactive hints always** — volunteer observations & next steps |
| Audience | All: expert, students, collaborators, PIs → **adaptive verbosity** |

### Analysis
| Area | Decision |
|---|---|
| Methods | Guinier/Porod/Kratky · p(r) & Dmax · sasmodels fitting · peak/d-spacing |
| Fitting flow | **Recommend best model + initial guesses → ask → run → iterate** |
| Auto-QC | Frame-outlier rejection · beam & transmission sanity (others on demand) |
| Scope | **Single sample** at a time |

### Plotting & reporting
| Area | Decision |
|---|---|
| Plot types | Profile overlays · metadata-vs-time · analysis plots · fit+residuals · ad-hoc |
| Rendering | **Interactive** (zoom/pan/hover) |
| Exports | Save plots · session report (PDF/HTML) · fit-results table · captions/methods |
| Axis default | **Remember last choice** (log-log / semilog / linear) |

### Knowledge & literature
| Area | Decision |
|---|---|
| Sources | Built-in SAXS/WAXS KB · user papers · live web search · group SOPs · user guidance |
| Adding papers | **Auto-ingest a papers folder** |
| Citations | **Numbered markers + reference list** |
| Domain | SAXS/WAXS + soft matter/polymers + broad materials |

### Workflow, memory & automation
| Area | Decision |
|---|---|
| Driving apps | **Guide only** — never auto-run reduction/averaging/subtraction |
| Memory | Project context · my preferences · my corrections · group conventions |
| Knowledge layering | **Persistent SAXS/WAXS fundamentals** + **per-project** refreshed context & literature |
| Automation | **None / on-demand only** |

### Deployment, cost & safety
| Area | Decision |
|---|---|
| Cost | **Balanced** — compact context, retrieve detail only when needed |
| Network | **Hybrid** — offline core; online features degrade gracefully |
| Code execution | **Guarded** — sandboxed Python, restricted dir, read-only data, confirm step |
| Data safety | **Experiment data strictly read-only**; exports only to `assistant_outputs/` after confirmation (see §4 C1, confirmed) |

---

## 3. Layered knowledge & memory model (per your "fundamentals + per-project" answer)

A 3-layer scheme (extends the existing `src/ai/memory.py`):

1. **Layer 0 — Fundamentals (persistent, always loaded):** core SAXS/WAXS theory,
   model catalog, QC heuristics, WAXS d-spacing/crystallinity steps. Compact,
   shipped with the platform, never changes per project.
2. **Layer 1 — Project context (per project folder):** this project's samples,
   conventions, prior results/flags, and the **user papers ingested for this
   project**. Refreshed when the project changes.
3. **Layer 2 — User/group preferences (cross-project):** plot styles, default
   models, units, citation style, learned corrections, group SOPs.

"Balanced" cost is achieved by always sending Layer 0 (small) + the user's
preferences, and **retrieving** Layer 1 detail (papers, prior results) only when
the question needs it.

---

## 4. Conflicts / decisions to confirm

**C1 — Read-only vs exports. ✅ RESOLVED (confirmed 2026-06-18).**
**Experiment data** (raw, `.dat`, `manifest.json`, `config.yml`) is strictly
read-only and never touched. Exports (plots, reports, fit tables) are written
**only** to a dedicated `assistant_outputs/` folder inside the project, and
**only after explicit confirmation**. In-chat results need no file write.
Implementation rule: the assistant has exactly one writable path —
`<project>/assistant_outputs/` — and must confirm before each write there.

**C2 — Interactive plots.** Plotly renders fully offline (no network) and the
figure isn't sent to the model, so this is compatible with both "hybrid/offline"
and "balanced cost." Inline interactive plots will be embedded in the chat bubble.

**C3 — Live web search vs offline.** Web search is an **online-only** feature that
degrades gracefully (the assistant says so and falls back to local KB) when the
beamline network is isolated.

**C4 — Guarded code execution vs strictly read-only.** The sandbox will mount data
**read-only**, run in a temp working dir, block file deletion/network, and show
the code + ask before running. This satisfies both answers.

---

## 5. Current state vs. to-build

### Already implemented
- Tools: `generate_plot`, `plot_metadata` (I0/bstop/transmission/thickness/CTEMP
  over time), `overlay_curves` (profile comparison), `list_saxs_models`,
  `run_analysis` (Guinier/Porod/Kratky), `query_manifest`, `add_note`,
  `flag_quality`, `ingest_pdf`.
- Comparative-interpretation + model-recommendation guidance in the system prompt.
- Rich rendering: Markdown + LaTeX (KaTeX), vendored offline.
- Cost controls: trimmed history, capped tool results, compact manifest queries.
- Layered memory + KB scaffolding (`src/ai/memory.py`, `src/ai/knowledge.py`).

### To build (roadmap)

**Phase 1 — Analysis depth (high value, low risk)** — ✅ COMPLETE
- ✅ `fit_model` tool: runs a recommended sasmodels fit, returns params +
  reduced-χ² + a **fit & residuals** plot; system prompt wires the
  recommend → confirm → run → iterate loop.
- ✅ `assess_quality` tool: single-sample frame-outlier (I0 robust-MAD) +
  transmission/beam sanity, surfaced as proactive hints.
- ✅ `compute_pr` tool + `pair_distance_ift` (regularized indirect Fourier
  transform, numpy-only): returns Rg, Dmax, I0 + inline p(r) plot. Validated
  against an analytic sphere (Rg within 0.1%, Dmax exact). `run_analysis`
  pair_distance now uses it too.

**Phase 2 — Interactive plotting & exports** — *exports complete*
- ✅ `export` tool + `assistant_outputs/` writer (the ONLY writable path,
  confirmed): **session report (HTML or PDF)**, **fit-results (CSV or XLSX)**,
  and **notes** (figure captions / methods / summaries as Markdown). Experiment
  data never modified; path-safety + format validity covered by tests.
- ✅ Save figures: every plot tool accepts an optional `save_as` filename to
  write the figure PNG to `assistant_outputs/` (sanitized, sandboxed).
- ✅ Interactive Plotly inline plots (vendored offline): `overlay_curves` now
  emits an interactive figure (zoom/pan/hover, per-detector subplots) alongside
  the static PNG. Pipeline: `overlay_plotly` figure builder → thread-local emit
  → chat result `plot_interactive` → app response → frontend renders Plotly with
  a hard PNG fallback if Plotly is unavailable or errors. **Needs a visual check
  in the browser.** Other plot tools (metadata/fit/p(r)) can follow the same
  pattern once confirmed.

**Phase 3 — Knowledge & literature** — ✅ COMPLETE

What Phase 3 includes:
1. **Manage your knowledge & literature** (visualise / add / remove) — ✅ done:
   - `manage_knowledge` tool with actions `list` (see every indexed paper/note
     with chunk counts + dates), `add_pdf` (index a PDF), `add_note` (save a
     text fact/snippet), `ingest_folder` (index every PDF in a folder), and
     `remove` (delete a paper/note by name; reversible by re-adding).
   - New KB methods `ingest_text` and `remove_source`; `list`/`remove` work even
     without ChromaDB (operate on `ingestion_log.json`).
2. **Numbered citations + reference list** — ✅ done: retrieved excerpts are
   numbered and the model is told to cite `[n]` and end with a References list.
3. **Auto-ingest a papers folder** — ✅ on-demand via `ingest_folder`; with no
   path it scans BOTH the per-project `<project>/papers/` and the global
   `ai_knowledge/user_papers/`. Background watching was de-scoped (interview:
   on-demand only).
4. **Live web search** — ✅ `web_search` tool via Crossref (free, no key):
   returns title/authors/year/venue/DOI. Online-only with a clear offline
   message (hybrid network requirement). Mock-tested parsing + offline fallback.
5. **Knowledge panel in the assistant UI** — ✅ sidebar "Knowledge & Literature"
   panel: lists every indexed paper/note, remove (✕) per item, add-note box,
   add-PDF upload, refresh. Backed by `/api/knowledge/{list,note,remove}`.
6. **Group methods/SOPs** — ✅ a shared Group memory layer
   (`ai_knowledge/group/sops.json`) that's ALWAYS loaded into context. The
   `group_sops` tool lists/adds/removes conventions (naming schemes, default
   models, buffer-matching rules) that apply across all projects and users.

Note: vector **retrieval/ingestion** needs `chromadb` + `sentence-transformers`
in the platform venv. Without them, listing/removing still work; add/retrieve
degrade gracefully with a clear message.

**Phase 4 — Guarded code execution** — ✅ COMPLETE
- `run_python` tool + `src/ai/code_exec.py` sandbox. Guard layers: (1) static
  AST check — import allowlist (numpy/scipy/pandas/matplotlib/safe stdlib) +
  denylist of dangerous calls/attributes (os.system, subprocess, sockets,
  urllib, file deletion, eval/exec/open-write, dunder escapes); (2) isolated
  `python -I` subprocess with CPU/memory rlimits, temp cwd, and a wall-clock
  timeout; (3) read-only data, `assistant_outputs/` the only writable path;
  (4) system prompt requires show-code-and-confirm before running. Provides
  `np`, `plt`, scipy, pandas, and `load_dat(path)`; captures stdout + an inline
  figure. Tested: 10 dangerous patterns blocked, safe code runs with a figure.

**Phase 5 — Adaptive UX & memory polish** — ✅ COMPLETE
- `set_preferences` tool saves audience (expert/student/mixed), verbosity,
  default model, units, citation style to Layer-1 (cross-project) memory; the
  system prompt injects an audience/verbosity directive so tone adapts.
- Per-project chat history: `memory.append_chat`/`load_chat`/`clear_chat` store
  turns at `<project>/.swaxs/memory/chat_history.jsonl`; the assistant app
  preloads a project's recent history into a fresh session and persists each
  turn — continuity survives restarts.
- Learned corrections already persist cross-project (Layer 1, auto-saved).
- Tested: chat survives a simulated restart; preferences flip the prompt
  directive (student/detailed ↔ expert/concise).

---

## 6. Non-goals (explicitly out, per interview)
- No driving of reduction/averaging/subtraction apps (guide only).
- No background/scheduled automation (on-demand only).
- No autonomous writes to experiment data (strictly read-only).
- No fully-unrestricted code execution.
