# Guinier: the AI assistant app (port 5109)

## 1. What it is for

Guinier is the platform's conversational analysis assistant, powered by Claude.
It answers questions about the current experiment, runs model-free SAXS/WAXS
analysis (Guinier, Porod, Kratky, p(r)), fits sasmodels form factors, plots
curves and beam metadata, checks data quality, and explains what the data does
and does not support. It reads experiment data; it never modifies it. The only
folder it writes to is `<project>/assistant_outputs/`.

## 2. Where it sits in the platform

**Reads**
- The project manifest (`manifest.db`, exported as `manifest.json`), through the
  `query_manifest` tool. The project folder is the one selected in the hub.
- Processed `.dat` files under `1D/` (reduced, averaged, subtracted).
- `config.yml` (q units, energy) when checking the q convention.
- Its own app knowledge: the in-scope app's `knowledge.md` plus the hub's
  manifest-contract section are injected directly into the system prompt, read
  from disk on every turn (`src/ai/assistant.py`, `_resolve_app_knowledge`). Edits
  to a `knowledge.md` take effect on the next turn, with no re-ingestion.
- RAG collections in ChromaDB (`ai_knowledge/vector_db/`): `literature`,
  `user_papers`, and `beamline`. The top 6 hits are added to each prompt.

**Writes**
- Manifest: quality flags from `flag_quality` (on the file entry and mirrored
  in the `ai_memory` section) and per-file notes from `add_note`. Nothing else
  in the manifest is changed.
- `<project>/assistant_outputs/`: exports, `run_python` figures.
- `<project>/.swaxs/memory/`: chat history (`chat_history.jsonl`), experiment
  history and quality log for this project.
- `~/.swaxs/memory/users/<user_id>/`: personal preferences and session
  summaries, which follow the user across projects.
- `ai_knowledge/group/sops.json`: group SOPs (shared by all users and projects).
- `ai_knowledge/corrections.json`: authoritative operator corrections.
- `ai_knowledge/ingestion_log.json`: what has been added to the knowledge base.

**Event bus**: subscribes to the hub bus and turns events into live hints
(the **Live hints** panel, fed by `/api/events/stream`). See the hub doc for
the bus and manifest contract.

## 3. How to use it

1. Start the platform from the hub and open Guinier (port 5109). The badge at
   the top shows whether AI credentials were found.
2. **Project** card (sidebar): the hub's project is used automatically. To
   change it, type a path or click **Browse…**, then **Set project**.
3. Type in the box at the bottom ("Ask about your scattering data…"), Enter to
   send, Shift+Enter for a new line. The microphone button dictates (offline
   Whisper, needs `faster-whisper`).
4. Under the input box choose **Effort** (Low, Medium default, High) and
   optionally a **Model** (blank means the configured default).
5. Name samples by keyword, for example `Run7` for a series or `Run7_r001` for
   one recipe. Structural analysis runs on the subtracted curve; beam metadata
   (i0, bstop) comes from averaged data.
6. **Quick prompts** (sidebar) insert ready-made requests: plot i0/bstop vs
   Timer, overlay subtracted curves, Guinier fit, p(r), full workup and model
   recommendation.
7. **Knowledge and literature** card: shows indexed items (⟳ refreshes, × removes
   one). Pick a **Library** (My papers or Literature) and click **Add PDF**, or
   type a fact and click **+ Add note**.
8. Top bar: **Clear** starts a new chat in this tab; **Memory** shows what the
   assistant currently remembers (preferences, SOPs, history, beamline notes).

Recommended analysis workflow: ask for the model-free workup first
(`analysis_tier1`), then a model recommendation, confirm it, fit, iterate.

## Tools (19)

Defined in `_TOOLS` in `src/ai/assistant.py`. All are read-only on experiment
data unless stated.

**Analysis**
- `analysis_tier1`: the model-free ladder on ONE curve, located by `keyword`.
  Routes the modality from the data (Bragg peaks: WAXS, smooth decay: SAXS, both:
  SWAXS), computes Guinier Rg/I0 and linearity, qRg validity, dimensionless
  Kratky, high-q power law, Porod volume, MW if the concentration is known, and
  returns which higher-tier steps are allowed or refused (with the remedy). It
  also runs the pre-flight gate (q convention, geometry, subtraction sanity,
  damage, low/high-q artefacts, detector gaps, concentration, uncertainties).
  Confirmation flags (`q_units_confirmed`, `geometry_confirmed`,
  `known_isotropic`, `known_dilute`, `frames_stable`, `low_q_triaged`,
  `detector_gaps_ok`) are set only after the operator confirms. Call this before
  proposing any model or P(r) step.
- `run_analysis`: one analysis on a `.dat` file. `analysis_type` is `guinier`,
  `kratky`, `porod` or `pair_distance`. Results:
  `guinier` gives `Rg`, `I0`, `slope`, `intercept`, `R2` (capital R), `q_range`
  (`[q_lo, q_hi]`), `qRg_max`; there is no `chi2`. `porod` gives `n`, `K`, `R2`,
  `interpretation`, `q_range`. `kratky` returns no fitted numbers (use
  `generate_plot`). `pair_distance` gives `Rg`, `Dmax`, `I0`, `chi2`.
- `compute_pr`: p(r) for ONE curve by regularised indirect Fourier transform;
  returns Rg, Dmax, I0 and an inline plot. `dmax` is an optional hint in nm.
- `fit_model`: sasmodels fit on ONE sample (`keyword`, `model_name`, starting
  `params`, `free`, optional `q_min`/`q_max`, `detector`, `axis`). Returns fitted
  parameters, reduced χ² and a data+fit plot with residuals.
- `list_saxs_models`: the sasmodels catalogue, optionally filtered by keyword.
- `assess_quality`: QC on ONE averaged sample: frame outliers (robust MAD on
  I0/intensity) and beam/transmission sanity. Returns a verdict per check and
  outlier frame indices.

**Plots**
- `generate_plot`: static PNG. `plot_type`: `curve`, `guinier`, `kratky`,
  `porod`, `pair_distance`, `multi`. Easiest with a `keyword` (resolves the
  subtracted curve and, for Guinier, auto-fits). Note: this `porod` plot is
  q⁴·I(q) vs q⁴ (Porod plateau). The Analysis app's Porod fit is different: it
  fits the power-law exponent n in ln I = a + n·ln q. Say which one you mean.
- `plot_metadata`: per-frame i0, bstop, transmission, thickness, normalisation
  factor, temperature (CTEMP/TEMP) vs the beamline Timer clock. `stage` is
  `averaged` or `subtracted`, narrowed by `keyword`.
- `overlay_curves`: overlay curves matched by `keywords`; `stage` `averaged` /
  `subtracted` / `reduced`, `detector` `SAXS` / `WAXS` / `both` (separate panels),
  `axis` `loglog` / `semilog` / `linear`, optional `q_min`/`q_max`.

**Manifest**
- `query_manifest`: `query_type` `summary`, `files`, `averaged`, `background`,
  `analysis`, `quality_flags`, `events`. Start with `summary`; list queries are
  capped, so pass a `keyword` or `detector`.
- `add_note`: attach a persistent note to a file entry. Writes the manifest.
- `flag_quality`: `good`, `marginal`, `bad`, `radiation_damage`, `aggregated`,
  `low_snr`, `outlier`, `needs_review`. Writes the manifest.

**Memory and knowledge**
- `set_preferences`: persistent per-user `audience` (expert/student/mixed),
  `verbosity` (concise/detailed), `default_model`, `units`, `citation_style`.
- `group_sops`: `list`, `add` (title + text), `remove` group conventions.
- `manage_corrections`: authoritative operator corrections that outrank any
  `knowledge.md`. `add` and `confirm` only on the operator's explicit
  instruction; `propose` records an unconfirmed suggestion; also `list`,
  `remove`. Scope `global`, `app:<id>`, `entry:<id>` or `doc:<name>`.
- `manage_knowledge`: `list`, `add_pdf`, `add_note`, `ingest_folder`, `remove`,
  in `user_papers` (default) or `literature`. Re-adding an unchanged PDF is skipped.
- `web_search`: literature search via Crossref (title, authors, year, venue,
  DOI). Needs internet; says so when offline.

**Output**
- `export`: writes to `assistant_outputs/` only, after the user confirms.
  `kind` `session_report` (html or pdf), `fit_results` (csv or xlsx), `notes` (md).
- `run_python`: short snippet in a guarded sandbox with `np`, `plt`, scipy,
  pandas, pathlib and `load_dat(name) -> (q, I, sigma)` (accepts a bare
  filename). It runs in a separate Python process with a time limit; a runtime
  guard blocks network access, starting programs, reading outside the project
  and the Python install, and writing outside `assistant_outputs/` and a temp
  folder. It is a guard, not an OS sandbox. Disabled
  unless the assistant was started with `SWAXS_ALLOW_CODE_EXEC=1`. The code must
  be shown and confirmed by the user first; prefer the dedicated tools.

Up to 8 tool rounds per turn.

## Memory

- **User** (`~/.swaxs/memory/users/<user_id>/`): `preferences.yml`,
  `session_summaries/`. User id: `SWAXS_USER_ID`, else the OS login.
- **Project** (`<project>/.swaxs/memory/`): `chat_history.jsonl`,
  `experiment_history.jsonl`, `quality_log.jsonl`. The last 20 turns of the
  project's chat are reloaded when a new session starts in that project.
- **Facility** (`ai_knowledge/beamline/<id>.yml`, id from `SWAXS_BEAMLINE`,
  default `ssrl_1-5`): instrument notes, also indexed in the `beamline` collection.
- **Group** (`ai_knowledge/group/sops.json`): shared SOPs, always in context.
- **Corrections** (`ai_knowledge/corrections.json`): authoritative corrections,
  always in context. If one conflicts with a `knowledge.md`, the assistant
  surfaces the conflict rather than applying it silently.

## Proactive hints

`HintChecker` (`src/ai/hints.py`) rules:
- `check_guinier_range`: q_min·Rg < 0.25 (0.3 recommended), q_max·Rg > 1.3, or
  χ² > 2.0. The Analysis app's own gate is shape-dependent (1.3 globular, 1.0 rod,
  1.7 disc); this hint is a simpler approximation.
- `check_aggregation`: low-q slope (first 10% of q) exceeds the global slope by 20%.
- `check_radiation_damage`: rising trend (over 10%) in a time-ordered I0 or
  low-angle intensity series, at least 3 points.
- `check_snr`: σ/I above 0.5 in the top 15% of q.
- `check_i0_stability`: frames whose I0 deviates over 20% from the median.
- `check_background_scale`: scale below 0.5 or above 1.5. This band is the
  assistant's rule; the Background app only clamps to [0.1, 5].
- `check_negative_intensities`: more than 5% negative points after subtraction.
- `check_rg_dmax_ratio`: Rg/Dmax above 0.6 (compact sphere ≈ 0.385).

## 4. Settings and defaults

**Credentials** (see [SECURITY.md](../SECURITY.md)). Resolved in this order:
- `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` (SLAC gateway,
  `https://ai-api.slac.stanford.edu`), preferred.
- Read automatically from `~/.claude/settings.json` (`env` block, including
  `ANTHROPIC_DEFAULT_SONNET_MODEL`) if not already in the environment.
- `ANTHROPIC_API_KEY` (direct Anthropic API), from the environment or `.env`.
Never put the gateway token in `.env` or the repo.

**Model**: `ANTHROPIC_MODEL`, else `ANTHROPIC_DEFAULT_SONNET_MODEL` from
`~/.claude/settings.json`, else `us.anthropic.claude-sonnet-4-6`. The SLAC
gateway needs the `us.anthropic.` prefix; the bare `claude-sonnet-4-6` is
rejected. UI choices: `us.anthropic.claude-sonnet-4-6` (default),
`us.anthropic.claude-sonnet-5`, `us.anthropic.claude-opus-4-8`,
`us.anthropic.claude-opus-5`, `us.anthropic.claude-haiku-4-5-20251001-v1:0`.

**Effort** (per turn): Low (3072 output tokens, short prose), Medium (6144,
default), High (16000, more cross-checking, slower). This is not extended thinking.

**Context limits**: the 6 most recent user turns (with their tool exchanges) are
re-sent each turn. Browser sessions expire after 2 hours idle; the per-project
chat history on disk remains.

**Other environment variables**
- `SWAXS_USER_ID`: overrides the user id for memory.
- `SWAXS_BEAMLINE`: facility id (default `ssrl_1-5`).
- `SWAXS_ALLOW_CODE_EXEC=1`: enables `run_python`.
- `SWAXS_WHISPER_MODEL`: voice model (default `base.en`).
- `SWAXS_HUB_URL` (default `ws://localhost:5100/ws`), `SWAXS_HUB_API` (default
  `http://localhost:5100`). Without them both follow `SWAXS_HUB_PORT`, which
  the hub sets for every app it starts.
- The assistant listens on 127.0.0.1 only, and refuses POSTs whose `Origin` or
  `Referer` is not a localhost page.

**Install**: `pip install -r requirements-ai.txt` (`anthropic`, `chromadb`,
`sentence-transformers`, `faster-whisper`). Chat and manifest questions work with
`anthropic` alone; the knowledge base needs `chromadb` and
`sentence-transformers` (pulls torch, about 2 GB).

## 5. Troubleshooting

- **Badge says no credentials / "No AI credentials are configured"**: set the
  token as above, then restart the assistant from the hub. `GET /api/health`
  shows `credentials` (`gateway-token`, `api-key` or `none`), `base_url`, `model`.
- **"Invalid model name"**: use a `us.anthropic.` model id on the SLAC gateway.
- **Authentication error**: the token is invalid or expired, or you are off the
  SLAC network/VPN.
- **"[KB] chromadb or sentence-transformers not installed"**: install
  `requirements-ai.txt`. Chat still works without the knowledge base.
- **Assistant says the manifest is empty**: no project selected, or the wrong
  one. Check the project path in the top bar and the hub.
- **`run_python` refuses**: restart with `SWAXS_ALLOW_CODE_EXEC=1`.
- **Microphone gives an error**: `pip install faster-whisper`; the first use
  downloads the model (about 150 MB) and needs internet.
- **`web_search` fails**: the beamline network is offline; everything else works.

## 6. API routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | UI |
| POST | `/api/chat` | One turn: `{message, session_id?, app_id?, project_root?, model?, effort?}` returns `{text, plot, tool_calls, hints, ...}` |
| POST | `/api/chat/stream` | Same, as Server-Sent Events (used by the UI) |
| GET | `/api/history/<session_id>` | Conversation history of a session |
| POST | `/api/ingest/pdf` | Upload and index a PDF |
| GET | `/api/events/stream` | SSE stream of live hints from the bus |
| GET | `/api/memory/context` | Current memory layers |
| POST | `/api/memory/clear` | Clear the transient user context |
| GET | `/api/knowledge/stats` | Chunk counts per collection |
| GET | `/api/knowledge/list` | Every indexed paper and note (works without ChromaDB) |
| POST | `/api/knowledge/note` | `{name, text, collection?}` save a note |
| POST | `/api/knowledge/remove` | `{name}` remove a source |
| POST | `/api/transcribe` | WAV upload to text (local Whisper) |
| GET | `/api/health` | Liveness and credential status |
| GET | `/api/project` | Project folder selected in the hub |
