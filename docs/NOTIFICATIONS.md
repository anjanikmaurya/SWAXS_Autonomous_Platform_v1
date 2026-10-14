# Notifications: the Autonomous Watch app

**What this is for.** Getting Slack messages during an unattended run: E-stops and
safety trips, a stalled pipeline stage (with a diagnosis), fit results and run
progress. **Autonomous Watch (port 5110) is the only app that sends
notifications**; no other app has a notification setting. It listens to the
platform's event bus, applies your settings (categories, quiet hours, throttle) and
posts to Slack through a Workflow Builder webhook.

Set this up before an overnight run.

---

## Setup

### 1. Create a Slack workflow (once, by any workspace member)

1. Open [Slack Workflow Builder](https://slack.com/intl/en-gb/features/workflow-automation).
2. **Create New** → **From Scratch**, name it (for example `auto-run`).
3. Choose **Webhook** as the trigger and add three text variables: `title`, `text`,
   `level`.
4. Add a **Send a message** step to your channel (for example `#autoq`) with the
   message:
   ```
   :title: {{trigger.title}}
   {{trigger.text}}
   [{{trigger.level}}]
   ```
5. **Publish** and copy the webhook URL (`https://hooks.slack.com/triggers/...`).

### 2. Put the webhook URL in `.env`

Create or edit `.env` in the platform folder (it is git-ignored; never commit it):

```bash
SWAXS_SLACK_WEBHOOK_URL=https://hooks.slack.com/triggers/T.../...
```

Restart the platform (or at least the Autonomous Watch app) so it is read. If the
URL is ever rotated, update `.env` and restart.

### 3. Test the wiring

With Autonomous Watch running:

```bash
curl -X POST http://localhost:5110/api/test
```

A `[TEST]` message should appear in your channel. This test ignores the sending
switch and categories: it only proves the webhook works.

### 4. Choose what is sent

Open Autonomous Watch (http://localhost:5110) → **Alerts** page:

- **Sending**: the master switch (**Stop sending** / **Start sending**). When off,
  nothing is sent at all, safety included.
- **Quiet hours**: type a window such as `23:00-07:00` and press **Set**; **Turn
  off** removes it. During quiet hours progress messages are held back; faults
  always go out.
- **Categories**: one checkbox per category (**All** / **None**). `safety` stays
  on while sending is on: the only way to silence safety messages is to stop
  sending entirely.

These settings are saved to `watchdog/config.yml`, so they survive a restart.

The other pages: **The loop** shows the live state of the closed loop and run
outcomes; **Messages** lists recent messages.

---

## What gets sent

| Event | Category | Level | Meaning |
|---|---|---|---|
| `reactor.estop` | safety | fault | emergency stop triggered |
| `reactor.safety` | safety | fault | safety violation (pump, temperature, pressure, ...) |
| `reactor.vent` | safety | fault after an E-stop, else info | lines vented |
| `reactor.run_start` | progress | info | recipe applied to the reactor |
| `reactor.run_complete` | progress | info | synthesis finished: "Ended: …" for a normal finish, "Stopped early: …" otherwise |
| `reactor.run_abandoned` | progress | fault | a proposed condition was never synthesised |
| `reactor.backend` | progress | info | switched between Simulation and Hardware |
| `fit.complete` | results | fault if the fit is suspect, else info | analyser fit result for a recipe |
| `average.skipped` | campaign | fault | a full batch was used up with no average written |
| `file.skipped` | campaign | info | reduction gave up on a frame |
| stall diagnosis | stalls | fault | a pipeline stage is overdue (below) |

High-frequency events (each reduced, averaged or subtracted file) are not sent;
they appear on **The loop** page instead.

### Categories

| Category | Covers |
|---|---|
| `safety` | E-stop, safety trips, venting |
| `stalls` | a stage of the loop stalled, with the diagnosis |
| `results` | fit results, suspect fits |
| `progress` | recipe started, run finished, condition abandoned, backend switch |
| `campaign` | pipeline losses (skipped frames, a batch with no average) |

### Rules applied before a message is sent

1. **Sending** off → nothing is sent.
2. Category unchecked → dropped (never `safety` while sending is on).
3. **Faults** always go out, even in quiet hours or while snoozed.
4. **Quiet hours** and **snooze** hold back progress and info messages.
   Snooze has no button; use `curl -X POST http://localhost:5110/api/snooze -H
   "Content-Type: application/json" -d '{"minutes": 30}'`. It ends after that time,
   or when the app restarts.
5. A throttle (`min_interval_s`, 3 s) spaces out sends so Slack does not rate-limit.

### Stall detection

Every 5 minutes Autonomous Watch checks whether the next stage of the loop is
overdue: a reduced file within 1 hour of a run starting, then an average, a
subtraction and a fit within 30 minutes each. If one is overdue it asks that app
for its monitor status and sends a fault naming the stalled stage and the likely
cause. An idle platform (no events at all) does not raise stalls.

---

## `watchdog/config.yml`

```yaml
notify:
  quiet_hours: null              # e.g. "23:00-07:00"; null = off
  summary: "off"                 # "hourly" is a placeholder, not implemented
  snooze_default_min: 30         # used when /api/snooze gets no minutes
  min_interval_s: 3              # throttle between sends
  slack_enabled: true            # master switch
  categories:
    safety: true
    stalls: true
    results: true
    progress: true
    campaign: true

diagnosis:
  ai_fallback_enabled: false     # true = add an AI reading of the stalled app's log
                                 #   to stall messages (needs the AI token)
```

The webhook URL is a secret and is never in this file; it lives in `.env`.

---

## Troubleshooting

**No messages in the channel**
1. **Alerts** page: **Sending** is on and the message's category is checked.
2. `SWAXS_SLACK_WEBHOOK_URL` is in `.env` and the app was restarted after setting it.
3. `curl -X POST http://localhost:5110/api/test`. If this fails, the webhook or the
   workflow is wrong; if it works but real messages do not arrive, check step 1 and
   quiet hours.
4. Look at `logs/watchdog.log` for errors.

**A stall happened but no stall message came**
Stall detection needs recent events from an active run. If nothing has happened
for more than 2 hours, it treats the platform as idle.

**Design notes**
- Sending happens on a background thread and never blocks the reactor or any app.
- Webhook only (no Slack bot token, no app approval needed). No file uploads; no
  email.
- The rules above are decided in one place, `src/watchdog/policy.py`.
