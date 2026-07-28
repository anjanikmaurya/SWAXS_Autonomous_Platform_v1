# Slack Notifications for Autonomous Runs

Posts to Slack while the platform runs unattended. Built so it **cannot affect
the run**: every send is fire-and-forget on a worker thread with a timeout, and
all errors are swallowed. A Slack outage, an expired token or a rate limit is
invisible to the reactor.

---

## Setup (5 minutes)

### Option A — bot token (recommended)

Needed for threading and plot uploads.

1. https://api.slack.com/apps → **Create New App** → *From scratch*
2. **OAuth & Permissions** → Bot Token Scopes: `chat:write` (add `files:write`
   for plots)
3. **Install to Workspace**, copy the `xoxb-…` token
4. Invite the bot to the channel: `/invite @YourBot`
5. Export the token and enable it:

```bash
export SWAXS_SLACK_BOT_TOKEN=xoxb-your-token
```

```yaml
# reactor/config.yml
notify:
  slack:
    enabled: true
    channel: "#swaxs-autorun"
```

### Option B — incoming webhook (zero setup, no threading)

```bash
export SWAXS_SLACK_WEBHOOK=https://hooks.slack.com/services/…
```

Same `enabled: true`. The channel comes from the webhook itself.

> **Credentials never go in `config.yml`** — that file is in git. They are read
> only from the environment. A `bot_token:` key placed in the config is ignored
> (there is a test asserting this).

Put the export in whatever starts the platform, e.g. `start_platform.sh`.

---

## What gets posted

Three tiers, selected by `notify.slack.tiers`:

| Tier | Events | Volume |
|---|---|---|
| `alert` | E-stop, over-temperature, over-pressure, pump setpoint over limit, pump fault/lost, **stale temperature reading** | rare — someone should go to the hutch |
| `progress` | recipe applied (conditions), run complete (reason, duration, fitted size/PDI/confidence/loss) | 2 per condition |
| `session` | autonomous session start / finish summary, backend switched | a few per campaign |

`mention_on_alert: "<!channel>"` adds a mention to alerts only.

### Threading

With a bot token, each recipe gets **one parent message** and everything else is
threaded under it, so a 24 h campaign reads as one line per condition:

```
▶ Autonomous session started (backend mock)
🧪 Recipe applied — r1
   • T_reac: 240   • F_tot: 80   • x_TOP: 0.15   • run duration: 10m 0s
   ↳ ✅ Run complete — r1
        • stopped by: duration elapsed   • ran: 10m 1s
        • size: 4.123   • pdi: 0.0234   • confidence: 0.91   • loss: 0.115
🧪 Recipe applied — r2
   …
🚨 EMERGENCY STOP
   pumps that did NOT idle: top — check them immediately
```

---

## Configuration reference

```yaml
notify:
  slack:
    enabled:  false            # master switch
    channel:  "#swaxs-autorun" # bot-token mode only
    tiers:    ["alert", "progress", "session"]
    thread_per_recipe: true    # auto-disabled on webhook transport
    mention_on_alert: ""       # "<!channel>" / "<@U012ABC>"
    timeout_s: 6.0             # per request
    min_interval_s: 0.4        # throttle against Slack's rate limit
```

---

## Isolation guarantees (all tested)

- `notify()` returns in **< 0.2 s even if Slack hangs for 5 s** — it only enqueues.
- A transport exception does not propagate and does not kill the worker thread.
- A full queue (200 items) **drops** notifications rather than blocking the caller.
- Slack rejections (`invalid_auth`, rate limits) are logged to the reactor log,
  never raised.
- The notifier is driven from the controller's `event_cb`, which runs **outside**
  the controller lock — so it can never add latency to an E-stop.
- Credentials are never included in a message payload.

---

## Architecture

```
ReactorController._event("reactor.run_start", …)
        │
reactor/app.py::_event_cb          ← also publishes to the hub event bus
        │  try/except (cannot raise)
src/notify/slack.py::SlackNotifier.notify()
        │  queue.put_nowait  (never blocks)
   worker thread → urllib POST with timeout → all errors swallowed
```

Only stdlib is used (`urllib`, `queue`, `threading`) — no new dependency.

Events consumed: `reactor.run_start`, `reactor.run_complete`, `reactor.estop`,
`reactor.safety` (new — carries `check` + `detail`), `reactor.backend`.

---

## Not yet wired

- **Fitted size/PDI in the run-complete message.** The formatter accepts a
  `result` dict, but the reactor doesn't yet know the analyzer's answer — the
  analyzer app would need to publish `analysis.complete` with the recipe_id and
  have the notifier post it into that recipe's thread.
- **Plot on failure.** `upload_png()` exists (bot token + `files:write`); it
  needs a hook on low-confidence fits / quality-gate rejections to choose when
  to attach the subtracted I(q) curve.
