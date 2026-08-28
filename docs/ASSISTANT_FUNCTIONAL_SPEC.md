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

The interview asked for "persistent fundamentals + per-project context". What
was built has four stores. Numbering below follows the code
(`src/ai/memory.py:6-16`) — earlier revisions of this document numbered them
differently and contradicted §5.

| Layer | Location | Scope | Contents |
|---|---|---|---|
| **Layer 1 — User** | `~/.swaxs/memory/users/<user_id>/` | cross-project, per user | learned corrections (`corrections.jsonl`), preferences (`preferences.yml`), session summaries |
| **Layer 2 — Project** | `<project_root>/.swaxs/memory/` | per experiment, travels with the data | processing history, quality log, per-project chat history |
| **Layer 3 — Facility** | `ai_knowledge/beamline/<beamline_id>.yml` | all users at a facility | instrument quirks, detector artifacts, calibration notes |
| **Group** (unnumbered) | `ai_knowledge/group/sops.json` | cross-project, cross-user | naming schemes, default models, buffer-matching rules |

**Fundamentals are not a memory layer.** Core SAXS/WAXS theory, the model
catalog, QC heuristics and the WAXS d-spacing/crystallinity steps live in the
system prompt string in `src/ai/assistant.py`, not in a module and not on disk.
They are compact, shipped with the platform, and never change per project — the
interview's "Layer 0" requirement, satisfied without a storage layer.

Note the consequence, because it is easy to get backwards: **preferences are
cross-project** (Layer 1, `src/ai/memory.py:83-84`), not per-project. Setting a
default model or verbosity in one experiment carries into the next.

"Balanced" cost is achieved by always sending the fundamentals (small) + the
user's preferences + the group SOPs, and **retrieving** project detail (papers,
prior results) only when the question needs it.

---

## 4. Conflicts / decisions to confirm

**C1 — Read-only vs exports. ✅ RESOLVED (confirmed 2026-06-18).**
**Experiment data** (raw, `.dat`, `manifest.json`, `config.yml`) is strictly
read-only and never touched. Exports (plots, reports, fit tables) are written
**only** to a dedicated `assistant_outputs/` folder inside the project, and
**only after explicit confirmation**. In-chat results need no file write.
Implementation rule: the assistant has exactly one writable path —
`<project>/assistant_outputs/` — and must confirm before each write there.
Enforced in `src/ai/assistant.py:669` and `:2623-2629`.

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

## 5. Build phases

All five phases shipped. 18 tools are live, defined in
`src/ai/assistant.py:66-570`. Open items are collected in §5a.

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

**Phase 2 — Interactive plotting & exports** — ✅ COMPLETE (with open items, §5a)
- ✅ `export` tool + `assistant_outputs/` writer (the ONLY writable path,
  confirmed): **session report (HTML or PDF)**, **fit-results (CSV or XLSX)**,
  and **notes** (figure captions / methods / summaries as Markdown). Experiment
  data never modified; path-safety + format validity covered by tests.
  PDF is generated with matplotlib's `PdfPages`
  (`src/ai/assistant.py:1602-1628`) and falls back to HTML (`:1633+`); fit
  tables at `:1577`.
- ✅ Save figures: every plot tool accepts an optional `save_as` filename to
  write the figure PNG to `assistant_outputs/` (sanitized, sandboxed).
- ✅ Interactive Plotly inline plots (vendored offline): `overlay_curves` emits
  an interactive figure (zoom/pan/hover, per-detector subplots) alongside the
  static PNG. Pipeline: `overlay_plotly` figure builder
  (`src/ai/plots.py:400`, called from `src/ai/assistant.py:1328-1329`) →
  thread-local emit → chat result `plot_interactive` → app response → frontend
  renders Plotly with a hard PNG fallback if Plotly is unavailable or errors.

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
   (`ai_knowledge/group/sops.json`) that's ALWAYS loaded into context when it
   exists. The `group_sops` tool lists/adds/removes conventions (naming schemes,
   default models, buffer-matching rules) that apply across all projects and
   users. The file is **not** shipped — `ai_knowledge/group/` is an empty
   directory until the first `group_sops` add, which creates it
   (`src/ai/memory.py:234`). Until then nothing is loaded.

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
  default model, units, citation style to Layer 1 (user, cross-project — see §3)
  memory; the system prompt injects an audience/verbosity directive so tone
  adapts.
- Per-project chat history: `memory.append_chat`/`load_chat`/`clear_chat` store
  turns at `<project>/.swaxs/memory/chat_history.jsonl`; the assistant app
  preloads a project's recent history into a fresh session and persists each
  turn — continuity survives restarts.
- Learned corrections already persist cross-project (Layer 1, auto-saved).
- Tested: chat survives a simulated restart; preferences flip the prompt
  directive (student/detailed ↔ expert/concise).

---

## 5a. Open

Carried out of the phase list above because they are not done.

- **Only one plot tool is interactive.** `overlay_curves` emits a Plotly figure;
  `plot_metadata`, `fit_model` and `compute_pr` are still PNG-only. Extending
  them means reusing the same `overlay_plotly` → `plot_interactive` pipeline.
- **The interactive path has never been checked in a browser.** The fallback to
  PNG is unconditional on error, so a broken Plotly render degrades silently
  rather than failing loudly — which is why this has not surfaced as a bug.
- `ai_knowledge/group/sops.json` does not exist on disk, so the "always loaded"
  group layer is currently a no-op for a fresh checkout.
- Vector **retrieval/ingestion** needs `chromadb` + `sentence-transformers` in
  the platform venv (`requirements-ai.txt`; sentence-transformers pulls torch,
  ~2 GB). Without them, listing and removing still work; add and retrieve
  degrade gracefully with a clear message.

---

## 6. Non-goals (explicitly out, per interview)
- No driving of reduction/averaging/subtraction apps (guide only).
- No background/scheduled automation (on-demand only).
- No autonomous writes to experiment data (strictly read-only).
- No fully-unrestricted code execution.
