# Notifications — Watchdog App

**The watchdog app (port 5110) is the sole place the platform sends notifications.** It subscribes to the event bus, applies policy (quiet hours, snooze, throttle), and sends to Slack via a Workflow Builder webhook.

## Setup

### 1. Create a Slack Workflow (one-time, by any workspace member)

1. Go to [Slack Workflow Builder](https://slack.com/intl/en-gb/features/workflow-automation)
2. Click **Create New** → **From Scratch**
3. Name it `auto-run`
4. Select **Webhook** as the trigger
5. Copy the webhook URL
6. **Set up the posting step:**
   - Add a **Send a message** action
   - Post to channel: `#autoq` (create the channel if needed)
   - Message text: 
     ```
     :title: {{trigger.title}}
     {{trigger.text}}
     [{{trigger.level}}]
     ```
7. **Publish** the workflow
8. Save the webhook URL (looks like `https://hooks.slack.com/triggers/...`)

### 2. Set the webhook URL in `.env`

```bash
# .env in the project root (git-ignored, sourced by start_platform.sh)
SWAXS_SLACK_WEBHOOK_URL=https://hooks.slack.com/triggers/T.../...
```

If the URL is ever rotated, re-run the Workflow Builder and update `.env`.

### 3. Test it

```bash
# From the watchdog UI at http://localhost:5110, click "Test"
# Or from the command line:
curl -X POST http://localhost:5110/api/test
```

You should see a test message in `#autoq`.

## How It Works

### Events from the Bus

The watchdog subscribes to platform events:
- **reactor.run_start** — recipe applied to the reactor
- **reactor.run_complete** — synthesis run finished
- **reactor.estop** — emergency stop triggered
- **reactor.safety** — safety violation (pump, temperature, etc.)
- **reactor.backend** — backend switched (mock ↔ real)
- **fit.complete** — auto-fit result available

Each is translated to `{title, text, level}` and sent via webhook.

### Policy

Before sending:
- **Master switch** (`config.yml:notify.slack_enabled`) — when off, nothing is sent at all, checked before everything else below
- **Category filter** (`config.yml:notify.categories`) — each message belongs to one of five categories (below); a category that's off is silently dropped
- **Quiet hours** (`config.yml:notify.quiet_hours`, e.g. `23:00-07:00`) suppress progress messages, never faults
- **Snooze** (`POST /api/snooze {minutes}`) silences progress messages
- **Throttle** (`config.yml:notify.min_interval_s`) ensures Slack isn't rate-limited
- **Faults always send** regardless of quiet hours/snooze, but still respect the master switch and category filter

All of this is resolved in one place — `should_send()` in `src/watchdog/policy.py`, a pure function with no I/O — so the Flask routes never make policy decisions themselves.

### Message Categories

Every message is tagged with exactly one category. The watchdog UI's **Alerts** page has an independent checkbox per category — any combination, including all or none:

| Category | Covers |
|---|---|
| `safety` | E-stop, safety trips, pump faults |
| `stalls` | loop stalled, with the diagnosis |
| `results` | fit results, low-confidence fits |
| `progress` | recipe started, run complete |
| `campaign` | converged, budget exhausted, hourly summary |

`safety` cannot be unchecked while the master switch is on — `should_send()` refuses to let the category filter block it. The only way to silence safety messages is to stop sending entirely, so nobody quietly disables the E-stop alert while trimming noise. Turning the master switch off silences `safety` too, along with everything else.

Bus events map to a category via `category_for_event()` in `src/watchdog/policy.py`; anything not explicitly mapped defaults to `progress` rather than being unfilterable. Stall diagnoses (which aren't built from a bus event) are always tagged `stalls`.

### Stall Detection

Every 5 minutes, the watchdog checks if any pipeline stage is overdue:
- Expected events: `file.reduced`, `file.averaged`, `file.subtracted`, `fit.complete`
- Timeout per stage: 1–3 hours depending on stage
- If a stage is overdue, watchdog probes the next app (`/api/monitor/status`) and sends a fault message naming the dead stage

## Configuration

`watchdog/config.yml`:

```yaml
notify:
  quiet_hours: "23:00-07:00"    # suppress progress, never faults
  summary: "off"                 # off | hourly (hourly for later)
  snooze_default_min: 30         # default when you press Snooze
  min_interval_s: 3              # throttle between sends

  slack_enabled: true            # master switch — false sends nothing at all
  categories:                    # independent per-category toggles
    safety: true
    stalls: true
    results: true
    progress: true
    campaign: true
```

The master switch and categories persist here, so they survive a restart. The
webhook URL is never in this file — it's a secret and lives in `.env`
(`SWAXS_SLACK_WEBHOOK_URL`).

Both can also be changed live from the watchdog UI's **Alerts** page (or via
`POST /api/settings`), which writes straight back to this file.

## Routes

- `GET /api/health` — status (always `{"status": "ok"}`)
- `GET /api/settings` — current policy (including `slack_enabled` and `categories`) and recent messages
- `POST /api/settings {slack_enabled?, categories?}` — update the master switch and/or one or more categories; either key is optional, `categories` may be a partial dict (only the keys you send change); the `safety` exception is enforced server-side regardless of what's sent
- `POST /api/test` — send a test message to Slack
- `POST /api/snooze {minutes}` — snooze progress messages
- `POST /api/set_project {path}` — set project root

## Troubleshooting

### No messages appear in #autoq

1. Check the **Alerts** page — sending must be **ON**, and the category the message belongs to must be checked
2. Check `SWAXS_SLACK_WEBHOOK_URL` is set in `.env` and the platform is restarted
3. Test with `curl -X POST http://localhost:5110/api/test` — this bypasses the master switch and category filter entirely (it's a webhook wiring check, not a policy check), so it succeeding doesn't mean real messages will send too
4. Check that `#autoq` exists and the watchdog app has permission to post there
5. Look at the watchdog console for errors

### Messages are being snoozed

Check the watchdog UI (`http://localhost:5110`). If **Snoozed** is on, progress messages won't send. Click **Snooze** again with `0` to clear it.

### Platform stalls but watchdog doesn't send a stall message

Stall detection looks for missing events. If no events have arrived at all (idle platform), no stall is detected. Only active campaigns generate stall alerts.

## Design Notes

- **Fire-and-forget**: sends are on a background thread, never block the reactor or any app
- **No secrets in git**: webhook URL lives in `.env` (git-ignored)
- **Webhook only**: uses Slack Workflow Builder (zero setup, no app approval needed), not a bot token
- **No file uploads**: PNG plots are mentioned in message text instead
- **No email**: Slack-only for now (email support is out of scope)
- **Category resolution is pure**: the master switch and category filter are both resolved inside `should_send()` (`src/watchdog/policy.py`), not in the Flask route — the same function the quiet-hours/snooze logic already lived in
- **Defense in depth on the safety exception**: "safety cannot be silenced while sending is on" is enforced in three places — `load_settings()` and `save_notify_settings()` in `src/watchdog/settings.py` (load/save time), and `should_send()` itself (send time) — so no single code path can regress it
