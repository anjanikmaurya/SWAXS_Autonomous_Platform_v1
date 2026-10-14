# SWAXS Platform: Architecture

How the platform is built today (checked against the code, October 2026). For
install and first run see [../QUICKSTART.md](../QUICKSTART.md); for what each app
does, [../README.md](../README.md); for day-to-day developer rules,
[../CLAUDE.md](../CLAUDE.md).

## Principles

- **Hub and spoke, plus an event bus.** The hub (port 5100) starts, monitors and
  stops ten Flask apps as subprocesses and runs a WebSocket event broker. Apps
  never call each other's Python; they publish and subscribe to bus events, read
  shared folders, and occasionally poll each other's HTTP status routes.
- **All logic in `src/`.** Every `app.py` is a thin Flask shell: put the project
  root on `sys.path`, import from `src/`, define routes.
- **One data contract.** The manifest records every file, analysis, subtraction,
  grade and reactor run with provenance. Each app writes only its own section.

---

## System overview

```
                          ┌──────────────────────────────────┐
                          │  Hub  :5100                       │
                          │  • app registry (apps.yml)        │
                          │  • subprocess manager             │
                          │  • WebSocket event broker /ws     │
                          │  • project folder + Operator box  │
                          └────────────────┬─────────────────┘
                                           │  WebSocket bus (pub/sub)
        ┌──────────────────────────────────┴────────────────────────────────┐
        │  :5101  Calibration      .raw → CBF, pyFAI .poni generation, SFTP  │
        │  :5102  Reduction        2D → 1D, corrections, normalization       │
        │  :5103  Average          2D/1D display, scan averaging             │
        │  :5104  Background       keyword / scan-matched / manual subtract  │
        │  :5105  Quality Gate     good/bad grading, auto-sort               │
        │  :5106  Analysis         Guinier, Porod, Kratky, p(r), models      │
        │  :5107  Analyzer         size/PDI fit + pluggable optimisers       │
        │  :5108  Reactor          five-pump flow synthesis + SPEC control   │
        │  :5109  Assistant        LLM interpretation, hints, inline plots   │
        │  :5110  Watchdog         Autonomous Watch: loop liveness, Slack    │
        └───────────────────────────────────────────────────────────────────┘

  closed loop: reactor → SPEC collect → reduction → average → background
               → quality → analyzer → optimiser → Conditions/ file → reactor
```

The closed loop is driven by **folders and polling**, with the bus used for
signals: each processing app watches its input folder (reduction `2D/`, average
`1D/<det>/Reduction/`, background `Averaged/`, analyzer `Subtracted/`), and the
reactor reads the next recipe from `1D/SAXS/Conditions/`. Two hops are
event-driven: quality grades on `file.subtracted`, and the reactor ends a run on
the first new sample `file.averaged`. See
[INTEGRATING_YOUR_OWN_CODE.md](INTEGRATING_YOUR_OWN_CODE.md) for every surface an
external program can use.

---

## 1. Hub and app registry

`hub/app.py` reads `apps.yml` (the single registry; `POST /api/apps/reload`
re-reads it live), launches each app with `sys.executable` (the interpreter that
started the hub), waits for it to answer before reporting "Started", reclaims
ports held by stale processes, rotates logs (`src/proc_lifecycle.py`) and
reports crashes. It also holds the selected project folder (`project.set`) and
the operator name (`operator.set`, `src/operator_id.py`), and exports
`SWAXS_PROJECT` / `SWAXS_USER_ID` to the apps it starts.

`apps.yml` entry fields:

| Field | Required | Purpose |
|---|---|---|
| `id` | yes | unique slug |
| `name` | yes | display name on the hub card and in the app's top bar |
| `port` | yes | TCP port the app listens on |
| `entry` | yes | path to `app.py` relative to the project root |
| `description` | no | one-line blurb on the hub card |
| `icon` | no | emoji fallback |
| `icon_id` | no | symbol in the shared SVG sprite (what the hub renders) |
| `icon_image` | no | image URL, served from the app's `static/` and copied into `hub/static/` |
| `accent` | no | accent colour token from `swaxs-tokens.css` |
| `color` | no | hex accent (must be unique, `tests/test_favicons.py`) |
| `knowledge` | no | `knowledge.md` indexed by the assistant |
| `manifest_key` | no | manifest section the app owns |

| id | port | manifest_key |
|---|---|---|
| calibration | 5101 | none |
| reduction | 5102 | `files` |
| average | 5103 | `files` |
| background | 5104 | `background` |
| quality | 5105 | `quality` |
| analysis | 5106 | `analyses` |
| analyzer | 5107 | `analyses` |
| reactor | 5108 | `reactor` |
| assistant | 5109 | `ai_memory` |
| watchdog | 5110 | none |

Every app has a `knowledge:` entry (`tests/test_docs_integrity.py`), and
`hub/knowledge.md` is indexed as well. Adding an app is an `apps.yml` entry plus
the port in both launcher banners; no hub code changes.

---

## 2. Event bus

Hub-mediated WebSocket pub/sub (`flask-sock`) at `ws://localhost:5100/ws`.
Clients use `src/events.py`: `EventBusClient("myapp").on_event(cb).connect(retry=True)`
and the `emit_*` helpers. Envelope:

```json
{"type": "file.reduced", "source_app": "reduction",
 "timestamp": "2026-10-14T14:30:00Z",
 "data": {"file_path": "/abs/path/sample_0001_SAXS.dat", "keyword": "sample_A", "scan_idx": 1},
 "ai_triggered": false}
```

Numpy values are coerced (`_json_safe`); an unencodable payload drops that one
event, never the connection. If the hub is down or `websocket-client` is
missing, `connect` and `publish` degrade to no-ops. The hub broadcasts every
event to all clients and appends it to the manifest's rolling `events` window
(last 100).

| Type | Published by | Acted on by |
|---|---|---|
| `file.reduced` | reduction | assistant (hints), watchdog (stage timing) |
| `file.skipped` | reduction (frame not reduced) | watchdog (`campaign` category) |
| `watch.new_raw` | reduction (watch mode) | hub log |
| `file.averaged` | average | reactor (ends the run on the first new sample average), assistant, watchdog |
| `average.skipped` | average | watchdog (`campaign` category) |
| `file.subtracted` | background | quality (grades immediately), assistant, watchdog |
| `file.classified` | quality | hub log |
| `analysis.complete` | analysis | assistant (hints) |
| `fit.complete` | analyzer | watchdog (stage timing, `results` category) |
| `reactor.run_start`, `reactor.run_complete`, `reactor.run_abandoned`, `reactor.ready`, `reactor.spec_collect`, `reactor.backend` | reactor | watchdog (stage timing, progress) |
| `reactor.estop`, `reactor.safety`, `reactor.vent` | reactor | watchdog (`safety` category, never suppressed by quiet hours) |
| `ai.hint` | assistant | the app the user is in |
| `app.started`, `app.stopped`, `app.reclaimed`, `app.crashed`, `project.set`, `operator.set` | hub | hub UI |
| `app.connected` | any app on connect | hub UI |
| `file.stitched` | nobody | the helper exists; SAXS+WAXS stitching was never built |

`fit.complete` (analyzer) and `analysis.complete` (analysis app) are separate
names on purpose: the payloads differ. Notification routing per event type is
`_EVENT_CATEGORY` in `src/watchdog/policy.py`.

---

## 3. Manifest

`src/manifest.py` is the public API (`update_manifest`, `add_file_entry`,
`add_analysis_entry`, `add_background_entry`, `add_quality_entry`,
`add_reactor_run`, `add_event`, `make_provenance`, `query_files`, …). All writes
go through `update_manifest(project_root, mutator)`: cross-process lock
(`.manifest.lock`, `fcntl`) → load → mutate → save.

**Storage.** Default backend is SQLite, `manifest.db` at the project root
(`src/manifest_store.py`): WAL mode, one transaction per save, indexed tables
`files`, `analyses`, `background`, `quality`, `reactor_runs`, `events` and a
`meta` table for the rest. After every save:

- `manifest.json` is exported (atomic temp-file + `replace`), so readers of the
  JSON keep working;
- a routine snapshot goes to `.manifest_snapshots/` (SQLite backup API; at most
  one per `SWAXS_MANIFEST_SNAPSHOT_INTERVAL_S`, default 600 s; 20 kept), plus a
  separate `preshrink-*` snapshot whenever a save would drop most of the file
  index.

On load, a missing, empty or unreadable `manifest.db` is restored from the
newest valid snapshot and the newer JSON export is merged on top; an unreadable
JSON with nothing to restore from raises `ManifestUnreadableError` instead of
starting an empty store. `SWAXS_MANIFEST_BACKEND=json` restores the legacy
single-file store. `query_files`, `manifest_runs` and `manifest_counts` use SQL
on the SQLite backend. The `analyses` section is capped
(`SWAXS_MANIFEST_ANALYSES_CAP`, default 3000).

Known limits, tracked in [audits/OPEN_DEFECTS.md](audits/OPEN_DEFECTS.md): each
write still loads and re-saves the whole manifest (O3); the lock is unguarded on
network file systems and a no-op on Windows (O4, O18).

**Schema** (the in-memory dict, same for both backends):

```text
version, project_root, created_at, updated_at
project_meta   {facility, beamline, users, current_operator, campaign, ...}
files          {abs_path: {path, stage (raw|reduced|averaged|subtracted|analysed),
                           detector, keyword, scan_idx, metadata, provenance,
                           status, notes, quality_flags}}
analyses       {id: {id, type, file_path, params, results, fit_range,
                     quality_score, ai_assessment, provenance, created_at, updated_at}}
background     {out_path: {sample_path, bkg_path, scale, scale_method,
                           scale_confidence, mode, provenance, created_at}}
quality        {path: {score, verdict, flags, metrics, reasons, detector, sample,
                       source (ai|user), llm_note, overridden, override_note,
                       analysis_ready, provenance, created_at}}
reactor.runs   {recipe_id: {controller run record incl. backend, logged_at}}
ai_memory      {corrections, session_summaries, quality_flags, user_context}
events         [last 100 bus envelopes]

provenance     {app, app_version, run_id, timestamp, user, input_files,
                config_hash, config_snapshot}
```

`_empty_manifest` seeds `project_meta`, `files`, `analyses`, `background`,
`ai_memory` and `events`. `quality` and `reactor` are created on first write,
so readers must tolerate their absence. Corrections computed during reduction
(transmission, thickness, normalization factor) go to the manifest only; the
`.dat` footer carries the raw beamline metadata.

---

## 4. Restart and state

Monitor loops persist their settings and progress through `src/runstate.py`
(atomic JSON under `<project>/.swaxs_state/`) and resume on boot unless
`SWAXS_NO_RESUME=1`. `src/runstate.monitor_alive(flag, thread)` is the shared
"is the worker really running" check. On a fresh start, `src/backlog.py`
decides what is already done (output exists), what is history (old, no
output) and what is new, so a Start on a full folder does not reprocess every
earlier run.

---

## 5. AI subsystem

The assistant app (`assistant/app.py`, logic in `src/ai/`) combines:

| Source | Location |
|---|---|
| Literature and user papers | `ai_knowledge/reference/literature/`, `ai_knowledge/reference/user_papers/` (ChromaDB in `ai_knowledge/index/vector_db/`) |
| Per-app knowledge | every `knowledge.md` named in `apps.yml`, plus `hub/knowledge.md` |
| Facility notes | `ai_knowledge/reference/beamline/<beamline_id>.yml` |
| Group SOPs and operator corrections | `ai_knowledge/rules/` (`sops.json`, `corrections.json`, plus the base prompt `system_prompt.md`) |
| Answer traces | `logs/assistant_traces/` (`src/ai/traces.py`) |

The full layout, and the order in which the model sees each part, is in
`ai_knowledge/README.md`; every path is defined once in `src/ai/layout.py`.
| The experiment | the project manifest |

Layered memory (`src/ai/memory.py`): **Layer 1** user, `~/.swaxs/memory/users/<user>/`
(corrections, preferences, session summaries); **Layer 2** project,
`<project>/.swaxs/memory/` (history, quality log, chat history); **Layer 3**
facility, `ai_knowledge/reference/beamline/`; plus the group layer.

The chat loop exposes 18 tools (`src/ai/assistant.py`): `generate_plot`,
`plot_metadata`, `overlay_curves`, `fit_model`, `list_saxs_models`,
`run_analysis`, `compute_pr`, `assess_quality`, `query_manifest`, `add_note`,
`flag_quality`, `export`, `set_preferences`, `group_sops`, `manage_corrections`,
`manage_knowledge`, `web_search`, `run_python` (guarded sandbox,
`src/ai/code_exec.py`). PDF upload is an HTTP route, not a tool.

Proactive hints (`src/ai/hints.py`) run on `file.reduced`, `file.averaged`,
`file.subtracted` and `analysis.complete`: Guinier range, aggregation,
radiation damage, S/N, I0 stability, background scale, negative intensities,
Rg/Dmax ratio. They are published as `ai.hint`. Audit:
[audits/ASSISTANT_AUDIT.md](audits/ASSISTANT_AUDIT.md).

---

## 6. `src/` module map

```
src/
├── manifest.py              public manifest API, lock, load/save, provenance
├── manifest_store.py        SQLite backend (manifest.db); JSON export + snapshots in manifest.py
├── operator_id.py           current operator: hub → manifest → SWAXS_USER_ID → OS login
├── folder_browse.py         GET /api/browse listing for the shared folder picker
├── backlog.py               "already done / history / new" rule for a fresh start
├── runstate.py              atomic monitor-state persistence; monitor_alive()
├── events.py                bus client and emit_* helpers
├── proc_lifecycle.py        hub: ports, process trees, log rotation
├── favicon.py               per-app browser-tab icon from apps.yml
├── plot_lock.py             one process-wide lock for server-side matplotlib
├── plot_reduction.py        read_folder (parse cache, skip_names), averaging
├── loop_naming.py           {recipe_id}_{role} keywords, match_recipe_id
│
├── preprocess/              calib.py · raw_convert.py (.raw → CBF) · sftp_sync.py
├── reduction/               core.py (Experiment, run_pipeline, find_new_raw_files)
│                            · process_metadata.py (CSV/PDI) · read_raw_file.py
│                            · csv_wait.py (wait for a CSV row that lags the frame)
├── utils/read_dat_metadata.py   .dat parser (q, I, sigma + metadata footer)
├── background/core.py       subtraction maths, auto scale, QC metrics, truncate_rebin
├── quality/core.py          grade_profile, score_metrics
├── analysis/                core.py (Guinier, Porod, Kratky, p(r) IFT, peaks, sasmodels)
│                            · io.py (Analysed/ bundle, .dat annotation)
│                            · atsas.py (autorg, datgnom, … wrappers)
│                            · nanoparticle.py (size/PDI/confidence for the optimiser)
│                            · guidelines.py (analysis ladder YAMLs for the assistant)
├── fitting/                 fitter registry: ls_sphere (default), ml_regressor, llm_assisted
├── optimizer/               campaign.py (CampaignController, GP + EI) · space.py · gp.py
│                            · io.py (Conditions/ file contract) · diagnostics.py · plots.py
│                            · study.py (in-silico method comparison)
│                            · methods/ (random, gp_ucb, turbo, bax, pareto_ehvi)
├── reactor/                 controller.py (API, loop, status, shutdown) · sequence.py
│                            · supervisor.py · collection.py · checks.py · hardware.py
│                            · config.py · recipe.py · intake.py · drivers/Py_P_Pump.py
├── beamline/driver.py       SPEC bServer HTTP + EPICS reads (all beamline I/O)
├── synthesis/               instrument contract, phase 0, not used by the reactor yet
├── simulator/               ground_truth · pattern · writer · collector (mock 2D data)
├── watchdog/                policy · settings · transport · messages · expectations
│                            · probes · diagnose
├── notify/                  legacy (slack, email, multi); no app imports it
└── ai/                      assistant · knowledge · memory · hints · plots
                             · code_exec · loop_advice
```

What does **not** exist, though older docs described it: `src/export/` (PDF
export is matplotlib `PdfPages` inside the assistant's `export` tool; no Word
export), `src/pipeline/` (see the unbuilt
[design/AUTOPILOT_PIPELINE_DESIGN.md](design/AUTOPILOT_PIPELINE_DESIGN.md)),
`src/reduction/stitch.py` (never built: the average app's Stitch checkbox
is a display toggle only), and per-method files under `src/analysis/`.

---

## 7. Conventions that do not change

- `.dat` format: `q, I, sigma` columns plus a `# METADATA INFORMATION (YML FORMAT)` footer.
- Output folders under `<project>/1D/<SAXS|WAXS>/`: `Reduction/`, `Averaged/`,
  `Subtracted/{Good,NeedsReview}/`, `Analysed/<Type>/`, `Conditions/`, `Results/`.
- Every write that another app polls is atomic (`.part` then `replace`).
- Python runs from the activated `venv/` with plain `python`. `uv` is
  not supported (`uv run` is not supported either): there is no `pyproject.toml`,
  and the hub launches apps with `sys.executable`.
- Two launchers: `start_platform.sh` (macOS/Linux) and `start_platform.bat`
  (all Windows shells). Both load `.env` and resolve the AI token before
  starting the hub; `python hub/app.py` does not.
- Dependencies: `requirements-core.txt` (everything the apps need),
  `requirements-hardware.txt` (real rig), `requirements-ai.txt` (assistant).
