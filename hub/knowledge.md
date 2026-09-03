# Hub App — Knowledge Base

## Purpose
The Hub (port 5000) is the central launcher and monitor for the SWAXS platform.
It starts every sub-app as an independent subprocess, monitors their health,
provides the project-folder selector, and serves the WebSocket event bus at `/ws`.
The hub port is overridable with the `SWAXS_HUB_PORT` environment variable
(default 5000; on macOS 5000 is also used by AirPlay Receiver).

## Architecture

```
Browser → hub:5000
  │
  ├── Launches one subprocess per apps.yml entry (currently 9):
  │     calibration :5009    reduction :5001    average  :5002
  │     background  :5003    analysis  :5004    assistant:5005
  │     quality     :5006    reactor   :5007    analyzer :5008
  │
  ├── WebSocket broker (/ws):
  │     any sub-app publishes → hub → all other connected apps
  │
  └── HTTP API (see the endpoint list below)
```

The hub does not hardcode a list of apps. `_load_apps()` reads `apps.yml` and
launches whatever it finds there, so the number of apps is whatever `apps.yml`
contains.

## Hub HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET  | `/` | main dashboard |
| GET  | `/api/health` | hub liveness — `{"status": "ok", "app": "hub"}` |
| GET  | `/api/status` | snapshot status of all apps (initial page load) |
| GET  | `/api/status/stream` | Server-Sent Events, one status frame every 2 s |
| POST | `/api/start/<app_id>` | launch one app → `{ok, message}` |
| POST | `/api/stop/<app_id>` | stop one app (kills the whole process tree) |
| POST | `/api/stop_all` | stop every app → `{ok, results, stuck}` |
| GET  | `/api/ports` | who is holding each app's port (answers "why won't it start?") |
| POST | `/api/set_project` | set the project folder — body `{"path": "/abs/path"}` |
| GET  | `/api/browse` | directory browser for the project picker |
| POST | `/api/apps/reload` | hot-reload `apps.yml` without restarting the hub |

There is no `POST /api/project` — the project-folder endpoint is
`POST /api/set_project`.

## apps.yml Registry
The hub reads `apps.yml` from the project root at startup. Each entry:
```yaml
- id: reduction
  name: "Reduction & Correction"
  port: 5001
  entry: "reduction/app.py"
  knowledge: "reduction/knowledge.md"
  manifest_key: "files"
  icon: "⚙️"
  color: "#1565C0"
```
`id`, `name`, `port` and `entry` are required. `description`, `icon`,
`icon_image`, `color`, `knowledge` and `manifest_key` are optional and get
defaults (`icon` "🔧", `color` "#455A64", the rest empty/None).

Hot-reload: `POST /api/apps/reload` re-reads `apps.yml` and registers new apps
without restarting the hub.

## App launch details
Each app is launched with the SAME Python interpreter that runs the hub
(`sys.executable <entry>`), with `cwd` set to the project root, in its own
process group, with stdout+stderr redirected to `logs/<app_id>.log` (the previous
log is rotated, not truncated). The child environment gets:

- `SWAXS_PROJECT` = the current project root (only if one is selected)
- `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`

Live PIDs are snapshotted to `logs/hub_children.json` on every start/stop, so a
hub that is SIGKILLed can reap its own orphans on the next startup. A held port
that identifies as the same app is reclaimed automatically rather than reported
as an unfixable error.

## WebSocket Event Bus

### Protocol
Messages are UTF-8 JSON with exactly these five keys
(`src/events.py::make_event`, and the same shape in `src/manifest.py::add_event`):
```json
{
  "type": "file.reduced",
  "source_app": "reduction",
  "timestamp": "2025-01-15T10:00:00+00:00",
  "data": { },
  "ai_triggered": false
}
```
There is no `event_type` key, no `run_id`, and no `ts`. Code that reads an event
must use `event["type"]` and `event["timestamp"]`.

### Standard Event Types
| Event type | Emitter | `data` fields |
|---|---|---|
| `file.reduced` | reduction | `file_path`, `keyword`, `scan_idx`, `detector` |
| `file.averaged` | average | `file_path`, `keyword`, `n_files`, `detector` |
| `file.subtracted` | background | `file_path`, `keyword`, `scale`, `mode` |
| `file.classified` | quality | `file_path`, `score`, `verdict`, `detector`, `flags` |
| `analysis.complete` | analysis, analyzer | `analysis_type`, `file_path`, `results` |
| `watch.new_raw` | reduction | `file_path`, `detector` |
| `ai.hint` | assistant | `hint`, `file_path`, `severity` (`info`/`warning`/`error`) |
| `reactor.run_complete` | reactor | the run record: `recipe_id`, `recipe`, `setpoints`, `measured_flows`, `started`, `ended`, `duration_s`, `reason`, `status` |
| `app.connected` | any app | `app_id` (sent automatically on WS open) |
| `app.started` | hub | `app_id`, `pid` |
| `app.stopped` | hub | `app_id` only |
| `app.crashed` | hub | `app`, `exit_code`, `reason`, `log` |
| `app.reclaimed` | hub | `app_id`, `port`, `killed` (list of PIDs) |
| `project.set` | hub | `path` |

`app.stopped` carries no exit code — an exit code only appears on `app.crashed`,
which is emitted when a child exits without being asked to.

`file.stitched` has a publisher helper (`EventBusClient.emit_file_stitched`,
`src/events.py:255`, fields `file_path`, `keyword`, `scale_factor`) but **nothing
in the platform ever calls it**. No `file.stitched` event is ever emitted; do not
write code that waits for one.

### Subscription (sub-apps)
Sub-apps connect using `EventBusClient` from `src/events.py`. Register the
callback BEFORE connecting, so no event can arrive while there is no handler:
```python
from src.events import EventBusClient

bus = EventBusClient("reduction")
bus.on_event(lambda event: handle(event))
bus.connect(retry=True)      # non-blocking daemon thread, reconnects every 5 s
```
If the hub is down or `websocket-client` is not installed, `connect()` and
`publish()` degrade silently — apps never crash because of a missing bus.

### Hub Broadcast
When a sub-app sends a message to `/ws`, the hub:
1. Receives and parses the JSON message
2. Appends it to `manifest["events"]` via `src.manifest.add_event()`
3. Broadcasts it to all other connected clients (the sender is excluded)

`add_event` keeps a ROLLING window of only the last `_EVENTS_MAX` = 100 events
(`src/manifest.py:119`); older events are dropped from `manifest.json`. Events
are only persisted while a project folder is selected.

## Project Folder Management
The hub holds a global `_project_root: str`. It is persisted to
`.hub_state.json` at the repo root and restored on the next hub start (if the
folder still exists), so a restart does not forget the project.

The project path is PUSHED by the hub, not pulled by the apps. On
`POST /api/set_project` the hub:
1. Validates that the path is an existing directory
2. Stores it and writes `.hub_state.json`
3. POSTs `{"path": "<project_root>"}` to `http://localhost:<port>/api/set_project`
   on every RUNNING sub-app
4. Emits a `project.set` event with `data = {"path": "<project_root>"}`

Apps launched later receive the path instead through the `SWAXS_PROJECT`
environment variable injected at launch.

## Health Monitoring
Each app is probed once per status tick with a single `GET /api/health` request
(1 s timeout). `/api/status` returns:
```json
{
  "apps": {
    "reduction": {
      "running": true,
      "healthy": true,
      "port": 5001,
      "pid": 12345,
      "summary": null,
      "crashed": null
    }
  },
  "project_root": "/path/to/experiment",
  "event_bus": true,
  "ws_clients": 3
}
```
`apps` is a DICT keyed by `app_id` — not a list — and there is no top-level
`"hub"` key and no `status` string. State is read from the booleans `running` and
`healthy`. `summary` is non-null only when the app's `/api/health` returns
`good`/`bad` counts (the Quality Gate does). `crashed` is `null` normally, or
`{exit_code, reason, at, tail}` where `tail` is the last log lines.

`GET /api/status/stream` (SSE) pushes the same `apps`/`project_root`/`ws_clients`/
`event_bus` payload every 2 s, plus `disk_free_gb` (free space on the project
volume) and `hub_error`. A failing tick reports itself in `hub_error` and the
stream continues with a back-off up to 15 s rather than freezing on a stale frame.

## Shutdown behaviour
Closing the hub closes the apps. `_shutdown_all_apps()` is registered on
`atexit` and also called from the `SIGTERM`/`SIGINT`/`SIGHUP` handlers, so
Ctrl-C in the launching terminal does not leave sub-apps running and holding
ports.

## Dependencies
- `flask` — web framework
- `flask-sock` — WebSocket support for the `/ws` event bus (optional; without it
  `event_bus` reports `false` and the bus is unavailable)
- `pyyaml` — `apps.yml` parsing
- `subprocess`, `threading`, `signal` — process launching, health thread, cleanup
- `src.proc_lifecycle` — port reclaim, process-tree kill, orphan reaping
- `src.manifest` — event persistence (imported lazily, failures are non-fatal)
