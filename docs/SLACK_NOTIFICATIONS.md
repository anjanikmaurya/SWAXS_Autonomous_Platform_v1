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

## Arming from the app — "Leaving the beamline"

The intended workflow is: set everything up, start the measurement, confirm the
first condition looks right, **then** arm notifications on your way out. There's a
card in the reactor app (bottom-left, under E-STOP) that does exactly that:

```
🔔 Leaving the beamline
   Arm Slack notifications once the measurement is running, so recipes,
   results and any fault reach you while you're away.
 ┌──────────────────────────────────────────┐
 │      🔔  Notify me on Slack              │   ← blue when ready
 └──────────────────────────────────────────┘
 [ Send test message ]   ready · bot mode
```

Armed, it turns green (`🔔 Notifications ARMED — click to stop`) and posts a
confirmation into the channel so you know it works *before* you walk away.

- **Send test message** — one message now, without arming. Do this first.
- The button is **disabled with an explanation** when no credentials are exported,
  so it can't silently do nothing.
- `notify.slack.enabled` in config.yml is only the STARTUP default. The button
  overrides it at runtime, no restart needed.

Routes: `GET /api/slack` (status), `POST /api/slack` (`{}` toggles, or
`{"enabled": true|false}`), `POST /api/slack/test`.

---

## Testing it

`tools/slack_test.py` verifies everything without waiting for a beamtime run.
It defaults to `--check`, so running it bare never sends anything.

```bash
# 1. Is it wired up? Reads config + env, sends NOTHING.
uv run tools/slack_test.py --check

# 2. See the exact messages without touching the network (no credentials needed).
uv run tools/slack_test.py --demo --dry-run

# 3. One real message, to confirm the token/channel work.
uv run tools/slack_test.py --ping

# 4. A full simulated 2-condition campaign: threaded fit results,
#    a low-confidence fit with the QC plot attached, and the fault alerts.
uv run tools/slack_test.py --demo

# 5. Just the alert tier — what would wake you at 3 a.m.
uv run tools/slack_test.py --fault
```

`--check` tells you precisely what is missing:

```
✓ reactor/config.yml → notify.slack.enabled: True
✓ SWAXS_SLACK_BOT_TOKEN is set (xoxb-1234…abcd) → bot mode (threading + plot uploads)
! SWAXS_SLACK_WEBHOOK not set
✓ channel: #swaxs-autorun
✓ tiers: alert, progress, session
✓ thread_per_recipe: True

✓ notifier would be ACTIVE at reactor startup
```

The demo posts under `slacktest_*` recipe ids so it is obvious in the channel
and can't be confused with real data.

### Offline unit tests

```bash
uv run pytest tests/test_slack_notify.py -v      # 25 tests, no network
```

These cover the isolation guarantees (hangs, exceptions, full queue, rejected
auth), tier filtering, threading, and that credentials never reach a payload.

### End-to-end with the mock rig

With `enabled: true` and a token exported, start the platform in mock mode and
queue a couple of conditions — the simulator produces real frames, the analyzer
fits them, and the results appear threaded in Slack. That exercises the same code
path beamtime will use.

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
