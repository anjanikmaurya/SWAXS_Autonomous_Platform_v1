# Hub App — Knowledge Base

## Purpose
The Hub (port 5000) is the central launcher and monitor for the SWAXS platform.
It starts all sub-apps as subprocesses, monitors their health, provides a
project-folder selector, and runs the WebSocket event bus.

## Architecture

```
Browser → hub:5000
  │
  ├── Launches subprocesses:
  │     reduction  :5001
  │     viewer     :5002
  │     background :5003
  │     analysis   :5004
  │     assistant  :5005
  │
  ├── WebSocket broker (/ws):
  │     any sub-app publishes → hub → all other sub-apps
  │
  └── HTTP API:
        GET  /              — main dashboard
        POST /api/project   — set project folder path
        GET  /api/status    — health of all apps
        POST /api/apps/reload — hot-reload apps.yml
```

## apps.yml Registry
The hub reads `apps.yml` from the project root at startup.  Each entry:
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

Hot-reload: `POST /api/apps/reload` re-reads `apps.yml` and adds new apps
without restarting the hub.

## WebSocket Event Bus

### Protocol
Messages are UTF-8 JSON with schema:
```json
{
  "event_type": "file.reduced",
  "source_app": "reduction",
  "run_id": "<uuid>",
  "ts": "2025-01-15T10:00:00Z",
  "data": { ... }
}
```

### Standard Event Types
| Event Type          | Emitter     | Data Fields                           |
|---------------------|-------------|---------------------------------------|
| `file.reduced`      | reduction   | file_path, keyword, detector, metadata|
| `file.averaged`     | viewer      | file_path, keyword, detector, n_frames|
| `file.stitched`     | viewer      | file_path, keyword, saxs_path, waxs_path|
| `file.subtracted`   | background  | file_path, sample, background, scale  |
| `analysis.complete` | analysis    | file_path, analysis_type, results     |
| `watch.new_raw`     | reduction   | file_path, detector                   |
| `ai.hint`           | assistant   | severity, message, file_path          |
| `app.started`       | hub         | app_id, port                          |
| `app.stopped`       | hub         | app_id, exit_code                     |
| `project.set`       | hub         | project_root                          |

### Subscription (sub-apps)
Sub-apps connect using `EventBusClient` from `src/events.py`:
```python
from src.events import EventBusClient

bus = EventBusClient("reduction").connect()
bus.on_event(lambda event: handle(event))
```

### Hub Broadcast
When a sub-app sends a message to `/ws`, the hub:
1. Receives the JSON message
2. Broadcasts it to all other connected clients
3. Appends it to `manifest.json` via `src.manifest.add_event()`

## Project Folder Management
The hub holds a global `_current_project: str` variable.
Sub-apps retrieve the project path via `GET /api/status` response
(`project_root` field) or from their own config/startup args.

When the project changes, hub emits a `project.set` event so all apps
can reload their manifest.

## Health Monitoring
`/api/status` returns:
```json
{
  "hub": "running",
  "project_root": "/path/to/experiment",
  "ws_clients": 3,
  "event_bus": "active",
  "apps": [
    { "id": "reduction", "port": 5001, "status": "running", "pid": 12345 }
  ]
}
```
Status values: `"running"`, `"stopped"`, `"error"`, `"starting"`.

## Dependencies
- `flask` — web framework
- `flask-sock` — WebSocket support (for /ws event bus)
- `pyyaml` — apps.yml parsing
- `subprocess` — launching sub-apps
- `threading` — health-check background thread
- `src.manifest` — event persistence (optional import)
