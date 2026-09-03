# AI Assistant App — Knowledge Base

## Purpose
The Assistant app (port 5109) provides a conversational AI interface powered by
Claude (Anthropic). It has access to the full experiment manifest, all processed
data files, the living knowledge base (ChromaDB), and the layered memory system.

## Architecture

```
User message
  │
  ▼
SWAXSAssistant.chat()
  │  builds system prompt from:
  │    1. Static SAXS/WAXS expert prompt
  │    2. Layered memory context (user + project + beamline + group SOPs)
  │    3. KB retrieval (top-6 ChromaDB hits, _KB_TOP_K = 6)
  │
  ▼
Claude API (default model claude-sonnet-4-6, max_tokens 4096)
  │  18 tools available; up to _MAX_TOOL_ROUNDS = 5 recursive tool-use loops
  │  per chat turn
  ▼
Response (text + optional plot base64 + optional interactive Plotly figure)
  │
  ▼
Proactive hints (HintChecker rules)
  │
  ▼
History delta (append to conversation)
```

Cost controls: only the `_MAX_HISTORY_USER_TURNS = 6` most recent user prompts
(with their tool exchanges) are re-sent, and any single tool result is truncated
at `_MAX_TOOL_RESULT_CHARS = 8000` characters.

## Available Tools — all 18

`_TOOLS` in `src/ai/assistant.py` defines eighteen tools.

### Plotting and visualisation
- **generate_plot** — a static base64 PNG for inline display. `plot_type` is one
  of `curve`, `guinier`, `kratky`, `porod`, `pair_distance`, `multi`. Accepts
  either a `file_path` (loads q/I/sigma automatically) or explicit arrays.
  Note: the `porod` plot type here draws **q⁴·I(q) vs q⁴** — the classical Porod
  plateau plot. That is NOT what the Analysis app's `/api/porod` route computes
  (that one fits the log-log power-law exponent n in `ln I = a + n·ln q`). Two
  different analyses share the word "Porod"; say which one you mean.
- **plot_metadata** — plot per-frame acquisition metadata over time for each
  averaged sample: `i0`, `bstop`, `transmission`, thickness, normalization factor,
  sample temperature (CTEMP/TEMP). Reads the manifest and `.dat` footers
  automatically; `stage` is `averaged` or `subtracted`, narrowed by `keyword`; the
  x-axis is the beamline Timer clock. Use for beam-stability, dosing and
  temperature-series checks.
- **overlay_curves** — load and overlay multiple processed `.dat` curves on one
  plot, matched by `keywords`. `stage` is `averaged` | `subtracted` | `reduced`,
  `detector` is `SAXS` | `WAXS` | `both`, `axis` is `loglog` | `semilog` |
  `linear`. Optional `q_min`/`q_max` truncate every curve first. SAXS and WAXS are
  rendered in separate panels.

### Analysis
- **run_analysis** — run one analysis on a `.dat` file and return numbers.
  `analysis_type` enum: `guinier`, `kratky`, `porod`, **`pair_distance`**.
  The result always carries `file` and `analysis`, plus:
  - `guinier` → `Rg`, `I0`, `slope`, `intercept`, `R2`, `q_range` (a
    `[q_lo, q_hi]` pair), `qRg_max`. **Note `R2` is capitalised**, and there is
    no `chi2`, no separate `q_min`/`q_max`, no `qRg_lo`, and no `n_points`.
  - `porod` → `n`, `K`, `R2`, `interpretation`, `q_range`.
  - `kratky` → only `n_points` and a `description` telling you to call
    `generate_plot(kratky, ...)`. It returns no fitted numbers.
  - `pair_distance` → `Rg`, `Dmax`, `I0`, `chi2` plus a note pointing at
    `compute_pr` for the inline plot.
  The verbose `plot` sub-dict is stripped from every result to save context.
- **compute_pr** — pair-distance distribution p(r) for ONE curve via a
  regularized indirect Fourier transform; returns Rg, Dmax, I0 and an inline p(r)
  plot. Locates the file by `keyword`; `dmax` is an optional hint (auto-estimated
  if omitted).
- **fit_model** — a sasmodels form-factor fit on ONE averaged sample. Returns
  fitted parameters, reduced χ², and an inline data+fit plot with a residuals
  panel. Takes `keyword`, `model_name`, numeric starting `params`, `free`
  (parameters to optimise), `q_min`/`q_max`, `detector`, `axis`. Intended workflow
  is recommend → confirm → fit → iterate; it is read-only on the data.
- **list_saxs_models** — list the sasmodels catalog available for fitting,
  optionally filtered by a `keyword` substring.
- **assess_quality** — QC on ONE averaged sample: frame-outlier detection
  (I0/intensity via a robust MAD) and beam/transmission sanity (transmission in
  physical range, positive I0/bstop, beam stability). Returns a per-check verdict
  and the indices of any outlier frames.

### Manifest and annotation
- **query_manifest** — inspect sections of `manifest.json`. `query_type` includes
  `summary`, `files`, `averaged`, `background`, `analysis`, `quality_flags`,
  `events`. Always start with `summary` (totals, per-stage and per-detector
  counts, quality-flag counts) — a project can hold thousands of files, and list
  queries are compacted and capped. Pass a `keyword` and/or `detector` filter for
  the list types.
- **add_note** — attach a plain-text note to a specific file entry in the
  manifest. Persistent across sessions.
- **flag_quality** — set a quality flag on a processed file: `good`, `marginal`,
  `bad`, `radiation_damage`, `aggregated`, `low_snr`, `outlier`, `needs_review`.

### Memory and conventions
- **set_preferences** — save the user's PERSISTENT preferences (across all
  projects and sessions): `audience` (`expert`/`student`/`mixed`), `verbosity`
  (`concise`/`detailed`), `default_model`, `units`, `citation_style`.
- **group_sops** — view/add/remove the group's shared SOPs and conventions
  (naming schemes, default models, analysis defaults, buffer-matching rules).
  Actions `list`, `add` (title + text), `remove` (by id or title). These apply
  across ALL projects and users and are always loaded into context.

### Knowledge base
- **manage_knowledge** — visualise, add or remove knowledge-base items. Actions
  `list`, `add_pdf`, `add_note`, `ingest_folder`, `remove`. `collection` is
  `user_papers` or `literature`.
- **ingest_pdf** — ingest one PDF (paper, manual, protocol) into the KB so it is
  retrievable in future conversations. Skips if the file hash is unchanged.
- **web_search** — search the scholarly literature online via Crossref; returns
  title, authors, year, venue, DOI. Online only; it reports clearly when the
  beamline network is offline.

### Output and escape hatch
- **export** — write a file to the project's dedicated `assistant_outputs/`
  folder, the ONLY place the assistant may write. `kind` is `session_report`
  (HTML/PDF summary of samples, stages, QC, analyses), `fit_results`
  (CSV/XLSX of recorded analyses/fits) or `notes` (Markdown). It never touches
  experiment data, and should be called only after the user confirms.
- **run_python** — run a SHORT Python snippet in a guarded sandbox and show
  stdout plus any matplotlib figure inline. Available: numpy (`np`), matplotlib
  (`plt`), scipy, pandas, pathlib, and `load_dat(path) -> (q, I, sigma)`, which
  accepts a BARE FILENAME and searches the project. The sandbox is read-only on
  data (os/network/file-deletion blocked); the only writable folder is
  `assistant_outputs/`. The code must be shown and explicitly confirmed by the
  user first, and the dedicated tools are preferred where they already do the job.

## Layered Memory

### Layer 1 — User (`~/.swaxs/memory/users/<user_id>/`)
`_USER_ROOT_DIR = Path.home() / ".swaxs" / "memory" / "users"`.
- `corrections.jsonl` — JSONL log of AI mistakes the user corrected
- `preferences.yml` — units, display, analysis preferences (written by
  `set_preferences`)
- `session_summaries/` — digests of past conversations

### Layer 2 — Project (`<project_root>/.swaxs/memory/`)
- `experiment_history.jsonl` — log of processing actions
- `quality_log.jsonl` — quality events per file

### Layer 3 — Facility (`ai_knowledge/beamline/<id>.yml`)
- Instrument-specific notes (detector geometry, common artefacts, calibration tips)
- Shared across all users at the same facility. The id comes from
  `SWAXS_BEAMLINE` (default `ssrl_1-5`).

### Group layer — shared SOPs (`ai_knowledge/group/sops.json`)
Cross-project, cross-user conventions, managed by the `group_sops` tool and always
loaded into the system prompt alongside the three layers above.

`load_context()` assembles all of these, taking the most recent
`max_corrections` (10) corrections and `max_summaries` (3) session summaries by
default.

## Knowledge Base Collections

| Collection    | Contents                                        |
|---------------|-------------------------------------------------|
| `literature`  | SAXS textbooks, review papers, instrument docs  |
| `apps`        | Per-app `knowledge.md` files, ingested from the `knowledge:` entries in `apps.yml` — all nine apps plus the hub |
| `user_papers` | User-uploaded sample-specific PDFs              |
| `beamline`    | Facility YAML configs (`ssrl_1-5.yml`, etc.)    |

Retrieval pulls the top 6 hits (`_KB_TOP_K = 6`) into the system prompt.

## Proactive Hints
`HintChecker` (`src/ai/hints.py`) has EIGHT rules:
- `check_guinier_range` — warns when `q_min·Rg < 0.25` (recommended ≥ 0.3) or
  `q_max·Rg > 1.3`, and, when a χ² is supplied, when χ² > 2.0. Emits an `info`
  hint when the range is fine. Note the Analysis app's own gate uses
  shape-dependent upper bounds (1.3 globular / 1.0 rod / 1.7 disc) and a
  `q_min·Rg > 0.65` lower gate, so this hint is the assistant's flatter
  approximation of it.
- `check_aggregation` — low-q upturn: compares the ln I vs ln q slope over the
  first 10% of the q range to the global slope, flagging a 20% slope excess.
- `check_radiation_damage` — an increasing trend in a time-ordered I₀ /
  low-angle intensity series (threshold 10%), needs ≥3 points.
- `check_snr` — σ/I above 0.5 over the top 15% of the q range.
- `check_i0_stability` — individual frames whose I₀ deviates more than 20% from
  the median.
- `check_background_scale` — a background scale below 0.5 or above 1.5. This band
  is the ASSISTANT's rule; the Background app itself only clamps to [0.1, 5] and
  never flags 0.5/1.5.
- `check_negative_intensities` — more than 5% negative points after subtraction.
- `check_rg_dmax_ratio` — sanity-checks Rg/Dmax. For a compact globular particle
  Rg ≈ 0.77·Dmax/2, i.e. a ratio of ≈0.385; a ratio above 0.6 is flagged.

## API Endpoints (assistant/app.py)
- `POST /api/chat` — send a message, receive `{text, plot, tool_calls, hints}`
- `POST /api/chat/stream` — the streaming variant, and the one the UI actually uses
- `GET  /api/history/<session_id>` — retrieve conversation history
- `POST /api/ingest/pdf` — upload and ingest a PDF file
- `GET  /api/events/stream` — SSE stream for real-time event-bus hints
- `GET  /api/memory/context` — view the current memory layers (debug)
- `POST /api/memory/clear` — clear transient session context for this user
- `GET  /api/knowledge/stats` — document counts per KB collection
- `GET  /api/knowledge/list` — every ingested paper/note (works without ChromaDB)
- `POST /api/knowledge/note` — `{name, text, collection?}`, save a text note
- `POST /api/knowledge/remove` — `{name}`, remove a source from the KB and its log
- `GET  /api/health` — liveness plus resolved credential status
- `GET  /api/project` — the project folder the hub currently has selected

## Environment Variables
- `ANTHROPIC_API_KEY` — required; set in the shell (or `.env`) before starting
  the platform
- `ANTHROPIC_MODEL` — optional; overrides the default `claude-sonnet-4-6` (used
  for gateway/proxy deployments)
- `SWAXS_USER_ID` — optional; overrides the OS username for the memory layer
- `SWAXS_BEAMLINE` — facility id for the Layer-3 memory file (default `ssrl_1-5`)
- `SWAXS_HUB_URL` — hub WebSocket URL (default `ws://localhost:5100/ws`)
- `SWAXS_HUB_API` — hub HTTP base URL (default `http://localhost:5100`)

## Session Management
Each browser tab gets a unique `session_id` (UUID). Conversation history is kept
in memory in a server-side dict keyed by `session_id`. Sessions expire after
`SESSION_TTL_S = 7200` seconds (2 hours) of inactivity — an expired session is
simply popped from the dict. **Nothing is persisted at session end**: session
history is lost when the session expires or the app restarts.
(`save_session_summary` exists in `src/ai/memory.py` but has no callers, so no
summary is written automatically. Use the `export` tool with
`kind: "session_report"` if a record of a session is needed.)
