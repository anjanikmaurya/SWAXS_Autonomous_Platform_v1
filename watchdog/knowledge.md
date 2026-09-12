# Auto Watch — Platform liveness monitoring and notifications

The Auto Watch app (port 5110) is the sole point where platform notifications are sent. It detects when an autonomous loop has stalled and watches for faults, communicating exclusively through Slack.

## What it does

- **Subscribes to the event bus** from all platform apps (calibration, reduction, average, background, quality, analysis, analyzer, reactor)
- **Translates platform events into notifications**: run start/complete, safety faults, emergency stops, fit results
- **Applies notification policy**: a master Start/Stop switch, five independent message categories, quiet hours, snooze, and throttling — faults always send immediately, subject only to the master switch and category filter
- **Detects stalls**: monitors which stage of the pipeline hasn't emitted an event recently and reports which stage is dead
- **Sends to Slack** via a Workflow Builder webhook (fire-and-forget, never blocks, degrades gracefully when the webhook is unset or unreachable)

## Notifications to Slack

All messages go to `#autoq` (private channel on the Stanford Enterprise Grid, configured via Slack Workflow Builder).

Two orthogonal things gate a message before it reaches Slack, both resolved inside the pure function `should_send()` in `src/watchdog/policy.py`:

- **Level** (`fault` / `progress` / `info`) — faults always send (subject only to the master switch and category filter below); progress/info respect quiet hours and snooze.
- **Category** — every message belongs to exactly one of five categories, each independently toggleable on the **Alerts** page:
  1. `safety` — E-stop, safety trips, pump faults
  2. `stalls` — loop stalled, with the diagnosis
  3. `results` — fit results, low-confidence fits
  4. `progress` — recipe started, run complete
  5. `campaign` — converged, budget exhausted, hourly summary

  `safety` cannot be turned off while the master switch (below) is on — that's enforced in `should_send()` itself, not just the UI, so nothing else can regress it.

## Configuration

- `watchdog/config.yml` holds non-secret policy: quiet hours, snooze defaults, throttle interval, the **master switch** (`notify.slack_enabled` — when `false`, nothing sends at all, checked before every other rule), and the **category toggles** (`notify.categories`)
- Both the master switch and categories can be changed live from the **Alerts** page or `POST /api/settings`, and persist back to `config.yml`
- `SWAXS_SLACK_WEBHOOK_URL` environment variable (`.env`, git-ignored) holds the Workflow Builder webhook URL
- If the webhook is unset, Auto Watch logs once and keeps running; nothing in the platform is affected

## Slack Workflow Builder

The workflow is named `auto-run` and is triggered by a webhook with this JSON body:

```json
{"title": "...", "text": "...", "level": "info"}
```

The three keys are fixed by the published workflow. Level is exactly "info" or "fault".

## The dashboard page

The page is a wall display: dark only (no light mode), sized to be read from two
metres, and it shows one thing — where the autonomous loop currently is.

- **Health row** (top bar, beside the CPU/memory/disk numbers): one big blinking
  dot per loop app that is **active right now**. "Active" is each app's own
  notion of taking part: a monitor app while its monitor thread runs, the reactor
  while it has a run or auto-run armed, the analyzer whenever it answers. An app
  that is not taking part is not listed at all. A dot turns red when an app is
  enrolled but unhealthy (e.g. a reactor whose control loop has died) or when the
  probe snapshot is too stale to trust.
- **The cycle**: one reactor, one reduce app, one average app — not two. The
  reactor collects BACKGROUND first (on the clean capillary, during the
  flush), then SAMPLE (during synthesis); which of the two is currently
  flowing through reduce/average is shown as a lane tag on those nodes and as
  a two-step phase tracker (① Background → ② Sample) on the reactor node
  itself, not by duplicating the reduce/average boxes. Both phases' averages
  then feed subtraction, which runs *only* when both exist, then fit+predict,
  which writes the next condition and hands control back to the reactor.
  Every node shows its state (idle / running / waiting / done / stalled) and
  the recipe_id it is on.
- **The two gates**, because they are where the loop actually parks and from the
  outside a gate looks like a crash: AVERAGE shows `have / expected` frames with
  a meter and says how many more it is waiting for; SUBTRACT shows a tick box per
  lane so a one-sided wait is visible at a glance.
- **The Alerts page** (separate nav item, not part of the dashboard view): the
  Slack master Start/Stop switch, the five category checkboxes with All/None
  shortcuts, and a live one-line summary of what's currently active. The
  `safety` checkbox is shown checked and disabled whenever sending is on — the
  UI mirrors the backend rule rather than re-implementing it.

Stall detection itself is unchanged by the page — `src/watchdog/expectations.py`
decides what is overdue, and the dashboard only renders that verdict.

## Routes

- `GET /api/health` — Auto Watch status (always `{"status": "ok"}`)
- `GET /api/metrics` — one dashboard snapshot; `GET /api/stream` — the same at 1 Hz (SSE).
  Both carry `loop` (per-node state for the cycle), `health` (active loop apps),
  `probe` (snapshot age), plus throughput, stage durations, file counts and run outcomes.
- `GET /api/settings` — current policy (quiet hours, snooze state, summary mode, `slack_enabled`, `categories`) and recent messages
- `POST /api/settings {slack_enabled?, categories?}` — update the master switch and/or one or more categories (partial dict — only sent keys change); `safety` is forced back on server-side if sending is on, no matter what's sent
- `POST /api/test` — send a test message to Slack (returns `{"ok": true}` if sent); bypasses the master switch and category filter — it's a webhook wiring check, not a policy check
- `POST /api/snooze {minutes}` — silence progress messages for N minutes
- `POST /api/set_project {path}` — set the project folder (same as all apps)
