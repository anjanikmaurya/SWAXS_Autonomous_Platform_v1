# Hub: Knowledge Base

## What it is for
The Hub (port 5100, http://localhost:5100) is the platform's front page and
launcher. It:

- starts and stops every app as its own process and shows whether each is healthy;
- holds the **project folder** and pushes it to every app;
- holds the **Operator** name (the only place it is entered);
- runs the WebSocket **event bus** at `ws://localhost:5100/ws` that the apps use
  to tell each other what happened;
- keeps the cross-app record (`manifest.db` / `manifest.json`) fed with bus events.

It does no science itself. The hub port can be changed with the `SWAXS_HUB_PORT`
environment variable (default 5100).
Port 5000 is avoided because macOS AirPlay Receiver holds it.

## The apps it launches
The hub reads `apps.yml` and launches whatever is listed there (currently ten),
in pipeline order:

| Port | App (id) | Name in the UI |
|---|---|---|
| 5101 | calibration | Geometry Calibration |
| 5102 | reduction | Reduction & Correction |
| 5103 | average | Visualisation & Average |
| 5104 | background | Background Subtraction |
| 5105 | quality | Quality Gate |
| 5106 | analysis | Data Analysis |
| 5107 | analyzer | Autonomous Analyser |
| 5108 | reactor | Autonomous Synthesis |
| 5109 | assistant | Guinier (AI assistant) |
| 5110 | watchdog | Autonomous Watch |

Two workflows are shown on the page:
- **STANDARD**: Calibration, Reduction, Visualisation & Average, Subtraction,
  Analysis.
- **AUTONOMOUS**: Autonomous Synthesis collects a sample, the pipeline reduces
  it, the Autonomous Analyser proposes the next one, back to Autonomous Synthesis.

## How to use it
1. Start the platform: `./start_platform.sh` (macOS / Linux) or
   `start_platform.bat` (Windows). This starts only the hub; open
   http://localhost:5100.
2. Type your name or initials in the **Operator** box (top bar). It is saved
   with every reduced file and campaign.
3. Click the project pill ("No project selected. Click to choose.", or
   **Change**) and pick the experiment folder: browse, or paste a full path and
   click **Go**, then **Select this folder**. The folder must already exist.
4. Click **Start** on each app card you need; **Open** becomes active once the
   app answers its health check. **Stop** stops one app; **Stop all** (Applications
   header) stops every app.
5. Watch the **Live event log** (bottom) and the **Bus** badge (top bar; the
   number is the count of bus clients, including this browser tab). **Clear**
   empties the log view only. The page shows a persistent warning if the event
   bus is down or the project disk is filling up.

The hub remembers the project folder (in `.hub_state.json` at the repo root)
and the operator (in `logs/hub_operator.txt`) across restarts. Apps are not
started automatically.

## Troubleshooting

| Symptom | What to do |
|---|---|
| An app card stays "Stopped" or shows a crash | Read `logs/<app_id>.log` (the card shows the last lines of a crash). The previous log is kept as a rotated copy. |
| "Port ... already in use" | Click **Ports** to see which process holds each app's port. A stale copy of the same app is reclaimed automatically; anything else must be closed. On Windows, Hyper-V/WSL2 can reserve port blocks: see QUICKSTART.md. |
| The hub itself will not start on 5100 | Run it on another port, e.g. `SWAXS_HUB_PORT=5200 ./start_platform.sh`. |
| Open button greyed out after Start | The app is still importing (reduction and the analyser take a few seconds). Wait for the green status. |
| Apps do not see the project | Set the project in the hub, not in the app. Running apps get it immediately; apps started later get it at launch. |
| Bus badge shows false / no events | `flask-sock` is missing from the environment. Install `requirements-core.txt` and restart the hub. |
| Apps left running after closing the terminal | Normally the hub stops them on exit (Ctrl-C, SIGTERM, SIGHUP). After a hard kill, the next hub start reaps the leftovers recorded in `logs/hub_children.json`. |

## How apps are launched
Each app runs with the same Python interpreter as the hub
(`sys.executable <entry>`), with the repo root as working directory, in its own
process group, and stdout and stderr going to `logs/<app_id>.log`. The child
environment gets:

- `SWAXS_PROJECT`: the current project folder (if one is set)
- `SWAXS_USER_ID`: the operator (if one is set)
- `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`

Live PIDs are saved to `logs/hub_children.json` on every start and stop so a
hub that was killed can clean up after itself on the next start.

## Project folder and operator
The project path is pushed by the hub. On `POST /api/set_project` the hub checks
the folder exists, saves it to `.hub_state.json`, records the operator in the
project, POSTs `{"path": ...}` to `/api/set_project` on every running app, and
emits `project.set`. Apps started later receive it through `SWAXS_PROJECT`.

The operator is set with `POST /api/operator`. It is saved to
`logs/hub_operator.txt`, exported as `SWAXS_USER_ID` to apps started afterwards,
written to the project manifest as `project_meta.current_operator` (so running
apps see it), and announced with `operator.set`. `src/operator_id.py` resolves
the operator: hub operator, then `SWAXS_USER_ID`, then the OS login.

## Health and status
Each running app is probed with `GET /api/health` (2 s timeout) on every status
tick. An app that has answered once stays green through up to 2 missed probes;
it turns red on the 3rd.

`GET /api/status` returns:
```json
{
  "apps": {
    "reduction": {"running": true, "healthy": true, "port": 5102,
                  "pid": 12345, "summary": null, "crashed": null}
  },
  "project_root": "/path/to/experiment",
  "event_bus": true,
  "ws_clients": 3
}
```
`apps` is a dict keyed by app id. `summary` is set only when an app's health
reply carries counts (the Quality Gate's good/bad). `crashed` is `null`, or
`{exit_code, reason, at, tail}` when an app exited without being asked to.

`GET /api/status/stream` (Server-Sent Events) sends the same payload every 2 s,
plus `disk_free_gb` (free space on the project volume) and `hub_error`. If a tick
fails it reports the error and backs off (up to 15 s) instead of freezing.

## Event bus
Every message is UTF-8 JSON with exactly five keys:
```json
{"type": "file.reduced", "source_app": "reduction",
 "timestamp": "2026-01-15T10:00:00+00:00", "data": {}, "ai_triggered": false}
```
Use `event["type"]` and `event["timestamp"]`; there is no `event_type`, `ts` or
`run_id` key.

When an app sends an event, the hub broadcasts it to every other connected app
(not back to the sender) and then appends it to the manifest's `events` list.
Only the last 100 events are kept there, and only while a project is set.

Subscribing from an app (register the callback before connecting):
```python
from src.events import EventBusClient
bus = EventBusClient("myapp")
bus.on_event(lambda event: handle(event))
bus.connect(retry=True)   # background thread, reconnects every 5 s
```
If the hub is down or `websocket-client` is missing, connecting and publishing
fail quietly; apps keep working without the bus.

### Event types
| Event type | Emitter | `data` fields |
|---|---|---|
| `watch.new_raw` | reduction | `file_path`, `detector` |
| `file.reduced` | reduction | `file_path`, `keyword`, `scan_idx`, `detector` |
| `file.skipped` | reduction | `file_path`, `keyword`, `n_failures`, `detector` (reduction gave up on a frame) |
| `file.averaged` | average | `file_path`, `keyword`, `n_files`, `detector` |
| `average.skipped` | average | `keyword`, `detector`, `batch`, `n_files`, `reason`, `files` (a full batch gave no average) |
| `file.subtracted` | background | `file_path`, `keyword`, `scale`, `mode` |
| `file.classified` | quality | `file_path`, `score`, `verdict`, `detector`, `flags` |
| `analysis.complete` | analysis | `analysis_type`, `file_path`, `results` |
| `fit.complete` | analyzer | `recipe_id`, `file`, `size`, `pdi`, `confidence`, `distribution`, `phase`, `guinier_rg`, `suspect`, `plot_png`, `loss` |
| `reactor.*` | reactor | `run_start`, `run_complete`, `run_abandoned`, `ready`, `spec_collect`, `safety`, `estop`, `backend`, `vent` (see reactor/knowledge.md) |
| `ai.hint` | assistant | `hint`, `file_path`, `severity` (`info` / `warning` / `error`) |
| `app.connected` | any app | `app_id` (sent on connect) |
| `app.started` | hub | `app_id`, `pid`, `ready` |
| `app.stopped` | hub | `app_id` |
| `app.crashed` | hub | `app`, `exit_code`, `reason`, `log` |
| `app.reclaimed` | hub | `app_id`, `port`, `killed` (PIDs) |
| `project.set` | hub | `path` |
| `operator.set` | hub | `operator` |

`app.stopped` has no exit code; only `app.crashed` does. A `file.stitched`
helper exists in `src/events.py` but nothing calls it, so that event is never
emitted; do not wait for one.

## manifest.json: cross-app contract
`src/manifest.py` owns the project's manifest. It is stored in SQLite,
`<project>/manifest.db` (`src/manifest_store.py`, with rotating snapshots in
`.manifest_snapshots/`), and exported to `manifest.json` on every save
(`SWAXS_MANIFEST_BACKEND=json` switches back to the single JSON file). Each app
reads and writes only its own section (its `manifest_key` in `apps.yml`) and
never overwrites another app's keys:

| App | Section |
|---|---|
| reduction, average | `files` |
| background | `background` |
| quality | `quality` |
| analysis, analyzer | `analyses` |
| reactor | `reactor.runs` |
| assistant | `ai_memory` |

`events` holds the last 100 bus events and `project_meta` holds project-level
facts such as `current_operator`. The assistant reads the whole manifest to
answer questions about the experiment. Computed corrections from reduction
(transmission, thickness, normalization factor) live only in the manifest, not
in the `.dat` footer.

## apps.yml registry
```yaml
- id: reduction                    # required, unique
  name: "Reduction & Correction"   # required, shown on the card
  port: 5102                       # required
  entry: "reduction/app.py"        # required, relative to the repo root
  knowledge: "reduction/knowledge.md"
  manifest_key: "files"
  description: "..."
  icon_id: "swaxs-reduction"       # sprite icon; accent sets its colour
  accent: "swaxs-reduction"
  color: "#1565C0"                 # fallback colour
```
Optional fields default to: `icon` "🔧", `color` "#455A64", `description` "",
`icon_image`, `knowledge` and `manifest_key` none. `POST /api/apps/reload`
re-reads the file without restarting the hub (new apps are added; removed ones
stay manageable until they exit). To add an app, see "Adding a New App" in
CLAUDE.md.

## Hub HTTP API
| Method | Path | Purpose |
|---|---|---|
| GET | `/` | dashboard |
| GET | `/api/health` | hub liveness, `{"status": "ok", "app": "hub"}` |
| GET | `/api/status` | status of all apps (snapshot) |
| GET | `/api/status/stream` | Server-Sent Events, a status frame every 2 s |
| POST | `/api/start/<app_id>` | start one app, returns `{ok, message}` |
| POST | `/api/stop/<app_id>` | stop one app (whole process tree) |
| POST | `/api/stop_all` | stop every app, returns `{ok, results, stuck}` |
| GET | `/api/ports` | which process holds each app's port |
| POST | `/api/set_project` | set the project folder, body `{"path": "/abs/path"}` |
| GET | `/api/browse` | folder browser for the project picker (`path=`; returns dirs and drives) |
| GET / POST | `/api/operator` | read or set the operator, body `{"operator": "..."}` |
| POST | `/api/apps/reload` | re-read `apps.yml` |
| WS | `/ws` | event bus |

There is no `POST /api/project`; use `POST /api/set_project`.
