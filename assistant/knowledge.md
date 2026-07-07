# AI Assistant App — Knowledge Base

## Purpose
The Assistant app (port 5005) provides a conversational AI interface powered by
Claude (Anthropic).  It has access to the full experiment manifest, all processed
data files, the living knowledge base (ChromaDB), and the 3-layer memory system.

## Architecture

```
User message
  │
  ▼
SWAXSAssistant.chat()
  │  builds system prompt from:
  │    1. Static SAXS/WAXS expert prompt
  │    2. Layered memory context (user + project + beamline)
  │    3. KB retrieval (top-6 ChromaDB hits)
  │
  ▼
Claude API (claude-sonnet-4-6)  ─── Tools ───►  generate_plot
  │                                              run_analysis
  │                                              query_manifest
  │                                              add_note
  │                                              flag_quality
  │                                              ingest_pdf
  ▼
Response (text + optional plot base64)
  │
  ▼
Proactive hints (HintChecker rules)
  │
  ▼
History delta (append to conversation)
```

## Available Tools

### generate_plot
Generate a SAXS/WAXS plot inline:
- `curve` — plain I(q) vs q (log-log)
- `guinier` — ln I vs q² with fit overlay
- `kratky` — q²I vs q, optionally dimensionless
- `porod` — q⁴I vs q⁴
- `pair_distance` — p(r) distribution
- `multi` — overlay multiple curves

Accepts either a `file_path` (loads automatically) or explicit `q`, `I`, `sigma`
arrays.

### run_analysis
Run Guinier / Kratky / Porod on a .dat file and return numeric results.
Returns: `{Rg, I0, chi2, q_min, q_max, qRg_lo, qRg_hi, n_points, r2}`.

### query_manifest
Query sections of `manifest.json`:
- `files` — all reduced files (filterable by keyword/detector)
- `averaged` — averaged file records
- `background` — subtraction records
- `analysis` — analysis results
- `quality_flags` — AI-set quality flags
- `events` — event bus log
- `summary` — quick counts

### add_note
Attach a plain-text note to a file in the manifest.  Persistent across sessions.

### flag_quality
Set a quality flag on a file: `good`, `marginal`, `bad`, `radiation_damage`,
`aggregated`, `low_snr`, `outlier`, `needs_review`.

### ingest_pdf
Ingest a PDF into the `user_papers` or `literature` ChromaDB collection.
Skip if file hash unchanged.

## 3-Layer Memory

### Layer 1 — User (`~/.swaxs/memory/users/<user_id>/`)
- `corrections.jsonl` — JSONL log of AI mistakes user corrected
- `preferences.yml` — units, display, analysis preferences
- `session_summaries/` — digest of past conversations

### Layer 2 — Project (`<project_root>/.swaxs/memory/`)
- `experiment_history.jsonl` — log of all processing actions
- `quality_log.jsonl` — quality events per file

### Layer 3 — Facility (`ai_knowledge/beamline/<id>.yml`)
- Instrument-specific notes (detector geometry, common artefacts, calibration tips)
- Shared across all users at the same facility

## Knowledge Base Collections

| Collection    | Contents                                        |
|---------------|-------------------------------------------------|
| `literature`  | SAXS textbooks, review papers, instrument docs  |
| `apps`        | Per-app knowledge.md files (this file, etc.)    |
| `user_papers` | User-uploaded sample-specific PDFs              |
| `beamline`    | Facility YAML configs (ssrl_1-5.yml, etc.)     |

## Proactive Hints
After each analysis tool call, `HintChecker` automatically runs:
- `check_guinier_range` — warns if qRg outside [0.3, 1.3]
- `check_aggregation` — detects low-q upturn
- `check_radiation_damage` — detects I₀ increase over frames
- `check_snr` — flags σ/I > 0.5 at high q
- `check_i0_stability` — I₀ outliers > 20% from median
- `check_background_scale` — scale factor outside [0.5, 1.5]
- `check_negative_intensities` — > 5% negative points post-subtraction

## API Endpoints (assistant/app.py)
- `POST /api/chat` — send a message, receive {text, plot, tool_calls, hints}
- `GET  /api/history/<session_id>` — retrieve conversation history
- `POST /api/ingest/pdf` — upload and ingest a PDF file
- `GET  /api/events/stream` — SSE stream for real-time event bus hints
- `GET  /api/memory/context` — view current memory layers (debug)
- `POST /api/memory/clear` — clear session context for this user

## Environment Variables
- `ANTHROPIC_API_KEY` — required; set in shell before starting the platform
- `SWAXS_USER_ID` — optional; overrides OS username for memory layer

## Session Management
Each browser tab gets a unique `session_id` (UUID).  Conversation history is
kept in memory (server-side dict keyed by session_id).  Sessions expire after
2 hours of inactivity.  Summaries are saved to LayeredMemory at session end.
