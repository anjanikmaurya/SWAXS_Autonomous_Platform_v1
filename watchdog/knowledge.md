# Autonomous Watch: loop monitor and notifications (port 5110)

## 1. What it is for

Autonomous Watch shows where the autonomous loop currently is, detects when it
has stalled, and is the only app that sends platform notifications. No other
app has a notification setting. Messages go to Slack only (no email), through a
Slack Workflow Builder webhook. Setup and the full event table are in
[docs/NOTIFICATIONS.md](../docs/NOTIFICATIONS.md).

## 2. Where it sits in the platform

**Reads**
- Every event on the hub bus (`ws://localhost:5100/ws`). On start, and when the
  project changes, it also recovers the recent event window from the manifest,
  so a stall that began before a restart is still seen.
- Status probes of the loop apps: reduction (5102), average (5103), background
  (5104), quality (5105), analyzer (5107), reactor (5108).
- The project folder (file counts and dashboard metrics).

**Writes**
- Slack messages (fire-and-forget on a background thread; never blocks any app).
- Its settings file: `<project>/watchdog_config.yml` if that file exists, else
  `watchdog/config.yml` in the repo. The Alerts page writes back to whichever
  file is in use.
- It does not write to the manifest.

**Secret**: `SWAXS_SLACK_WEBHOOK_URL` in `.env` at the repo root (git-ignored,
read at startup). If it is unset, Autonomous Watch logs once and keeps running;
nothing else in the platform is affected.

## 3. How to use it

1. One-time: create the Slack workflow and put the webhook URL in `.env` (see
   [docs/NOTIFICATIONS.md](../docs/NOTIFICATIONS.md)), then restart the platform.
2. Check the wiring: `curl -X POST http://localhost:5110/api/test`. A test
   message should appear in `#autoq`. This bypasses the master switch and
   category filter, so success only proves the webhook works.
3. Open Autonomous Watch from the hub. The sidebar has three views:
   - **The loop**: the dashboard (below).
   - **Messages**: the most recent messages it decided to send.
   - **Alerts**: the Slack controls.
4. On **Alerts**, set **Sending** on (the master button), choose **Quiet hours**
   (type `23:00-07:00`, click **Set**; **Turn off** clears them), and tick the
   **Categories** you want (**All** / **None** shortcuts). Changes apply at once
   and persist to the settings file.
5. Leave the page open on a wall display during an unattended run. The theme
   follows the platform setting (saved choice, else the OS); pick dark once.

### The loop dashboard

- **Health row** (top bar, beside CPU, memory, disk): one dot per loop app that
  is taking part right now (a monitor app while its monitor runs, the reactor
  while a run or auto-run is armed, the analyzer whenever it answers). Apps not
  taking part are not shown. A dot turns red when an enrolled app is unhealthy
  (for example the reactor control loop has died) or the probe data is stale.
- **The cycle**: reactor, reduce, average, subtract, fit+predict, back to the
  reactor. The reactor collects BACKGROUND first (clean capillary, during the
  flush), then SAMPLE (during synthesis). A lane tag on reduce/average and a
  two-step tracker (① Background, ② Sample) on the reactor node show which is
  flowing. Subtraction runs only when both averages exist; fit+predict writes the
  next condition. Each node shows its state (idle, running, waiting, done,
  stalled) and its recipe_id.
- **The two gates**, where the loop normally parks (from outside a gate looks
  like a crash): AVERAGE shows `have / expected` frames with a meter; SUBTRACT
  shows one tick per lane, so a one-sided wait is obvious.
- Charts: **Throughput**, **Time per loop run**, **Pipeline funnel**, **Run
  outcomes** (particle diameter per fit, colour = confidence, size ∝ PDI).
  Plotly is served from the repo's vendored copy, so charts work offline.

## 4. Settings and defaults

### What decides whether a message is sent

All in `should_send()` in `src/watchdog/policy.py`, checked in this order:
1. **Master switch** (`notify.slack_enabled`, default `true`). Off means nothing
   is sent at all, including safety.
2. **Category filter** (`notify.categories`). `safety` cannot be filtered out
   while sending is on; the Alerts page shows it checked and disabled.
3. **Level**: faults always send (past quiet hours and snooze). Progress and info
   respect quiet hours and snooze.
4. **Throttle** (`notify.min_interval_s`, default 3 s) between webhook posts.
   Faults go on a priority queue: sent first, never throttled, never dropped in
   favour of an info message.

### Message categories

| Category | Events |
|---|---|
| `safety` | `reactor.estop`, `reactor.safety` (fault), `reactor.vent` (fault if an E-stop was latched, else info) |
| `stalls` | Stall diagnoses from the stall checker |
| `results` | `fit.complete` (fault if the fit is flagged suspect) |
| `progress` | `reactor.run_start`, `reactor.run_complete`, `reactor.backend` (Simulation/Hardware switch), and any unmapped event such as `reactor.run_abandoned` (sent as a fault) |
| `campaign` | `average.skipped` (fault: a batch was consumed with no average), `file.skipped` (info: reduction gave up on a frame) |

High-frequency events (`file.reduced`, `file.averaged`, `file.subtracted`,
`analysis.complete`, ...) are deliberately not sent; they appear on the dashboard.
Converged/budget messages and the hourly summary are not implemented yet.

### Settings file keys (`watchdog/config.yml`)

| Key | Default | Meaning |
|---|---|---|
| `notify.quiet_hours` | `null` | `"HH:MM-HH:MM"` in local time; suppresses progress, never faults |
| `notify.summary` | `"off"` | `"hourly"` is accepted but is a placeholder |
| `notify.snooze_default_min` | `30` | Minutes used by `POST /api/snooze` with no value |
| `notify.min_interval_s` | `3` | Throttle between posts |
| `notify.slack_enabled` | `true` | Master switch |
| `notify.categories` | all `true` | Per-category toggles |
| `diagnosis.ai_fallback_enabled` | `false` | Optional AI reading of the stalled app's log |

A malformed settings file fails closed: sending is held off and the Alerts page
shows the error until the file is fixed.

### Stall detection

Every 5 minutes the checker looks for the next expected event after the last one
seen (`src/watchdog/expectations.py`). Timeouts: reduce 1 h after
`reactor.run_start`; average, subtract and fit 30 min each after the previous
stage. Events older than 2 h are ignored (prior campaign). When a stage is
overdue, `src/watchdog/diagnose.py` matches one of seven known stall patterns
(A to G) using the probes and the dashboard's loop state and reports the numbers;
if none matches it says "unrecognised pattern" and lists every number. With
`ai_fallback_enabled`, an unrecognised stall also gets a paragraph labelled
"AI reading of the log:" (needs AI credentials; advisory only). Repeated alerts
for the same stall back off: 5, 15, 60 min, then hourly.

### Slack payload

The `auto-run` workflow receives `{"title": "...", "text": "...", "level": "..."}`,
with `level` exactly `info` or `fault`. The three keys are fixed by the
published workflow.

## 5. Troubleshooting

- **No messages in `#autoq`**: on **Alerts**, Sending must be on and the
  category checked; check quiet hours (the badge shows when they are active);
  check `SWAXS_SLACK_WEBHOOK_URL` in `.env` and restart; run the `curl` test; look
  at the Alerts note for a config or delivery error.
- **Only faults arrive**: quiet hours or a snooze is active. A snooze is cleared
  when the project is set again, or wait it out.
- **The loop stalled but no stall message**: stall detection needs recent
  events. An idle platform, or events older than 2 h, raise nothing. Also check
  the `stalls` category is on.
- **A node looks stuck at AVERAGE or SUBTRACT**: read the gate. Waiting for
  frames or for the other lane is normal, not a crash.
- **Charts blank**: no vendored Plotly and no internet; the console says so.

## 6. API routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | UI |
| GET | `/api/health` | Always `{"status": "ok"}` |
| GET | `/api/project` | Project folder in use |
| POST | `/api/set_project` | `{path}` set the project, reload settings, clear snooze |
| GET | `/api/metrics` | One dashboard snapshot (`loop`, `health`, `probe`, throughput, stage durations, file counts, run outcomes) |
| GET | `/api/stream` | Same snapshot as SSE, about 1 Hz, sent when it changes |
| GET | `/api/settings` | Policy, `slack_enabled`, `categories`, `webhook_configured`, `config_error`, `delivery_error`, last 20 messages |
| POST | `/api/settings` | Partial update of `slack_enabled`, `categories`, `quiet_hours`; safety is forced on while sending is on |
| POST | `/api/test` | Send a test message (bypasses master switch and categories) |
| POST | `/api/snooze` | `{minutes}` silence progress messages (default 30) |
| GET | `/vendor/<name>` | Vendored JS (Plotly), CDN fallback |
