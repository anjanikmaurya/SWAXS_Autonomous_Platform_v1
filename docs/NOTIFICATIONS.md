# Notifications for Unattended Runs — Slack and Email

The reactor app reports while the platform runs unattended: each recipe it applies,
the fitted result, and immediately any fault. Two transports sit behind one object
— `MultiNotifier` (`src/notify/multi.py`, built at `reactor/app.py:121-124`) — and
one button arms both.

**Email needs no approval and works today**; Slack needs a workspace-app credential
an admin usually must approve. Setting up for a beamtime tonight? Do the email
section and stop there. Either way, notifications **cannot affect the run**: every
send is fire-and-forget on a worker thread with a timeout, all errors swallowed.

## Blocked on Slack app approval? Use email (5 minutes, no permission)

If Slack says *"You'll need approval from someone who manages apps on your
workspace"*, click **Request to Add New Webhook** and use email meanwhile. Same
tiers, same isolation, pushes to your phone — and it can deliver **into Slack**
later via a channel-email address.

```bash
# .env in the project root (git-ignored, sourced by start_platform.sh)
SWAXS_SMTP_HOST=smtp.stanford.edu
SWAXS_SMTP_PORT=587                    # optional; inferred from the mode
SWAXS_SMTP_USER=you@stanford.edu       # omit if your relay needs no auth
SWAXS_SMTP_PASSWORD=your-app-password
SWAXS_SMTP_FROM=you@stanford.edu       # optional; defaults to the user
SWAXS_NOTIFY_EMAIL=you@stanford.edu    # comma-separate for several people
```

`.env.example` ships **only the two Slack variables** — there is no SMTP template
in it, so add the block above by hand. Then verify: `python tools/notify_test.py
--check` connection-tests ports 587/465/25 from that machine, so you don't have to
guess which one your network permits.

```
   probing SMTP options from THIS machine (a few seconds)…
   ✓ smtp.stanford.edu:587 starttls (auth required)
   ✗ smtp.stanford.edu:465 ssl  ConnectionRefusedError
   ✓ at least one option works — put that host/port in .env
```

`python tools/notify_test.py --ping` then sends one real message, and the app's
**🔔 Notify me when I leave** button arms email exactly as it would Slack.

**Into Slack without app approval:** in the channel choose **Settings → Integrations
→ Send emails to this channel** and put the generated address in
`SWAXS_NOTIFY_EMAIL`. Ordinary members can often do this, bypassing the queue.

**Text messages:** most carriers accept email-to-SMS (`+15551234567@txt.att.net`,
`@vtext.com`, …). Add one to `SWAXS_NOTIFY_EMAIL` and set
`notify.email.tiers: ["alert"]` so only genuine faults buzz your phone.

## Slack — for when approval lands

### Step 1 — Create the channel

In Slack: **+ → Create a channel** → e.g. `#swaxs-autorun`. Use a **dedicated**
channel: a 24 h campaign posts one thread per condition, and mixed into a busy
channel that becomes noise people mute — which defeats the purpose. Invite whoever
needs to be woken up.

### Step 2 — Pick a transport

| | Incoming webhook | Bot token |
|---|---|---|
| Setup time | ~2 min | ~5 min |
| Threading (one thread per recipe) | ✗ flat messages | ✓ |
| QC plot attached on a bad fit | ✗ | ✓ |
| Needs workspace admin approval | sometimes | usually |

**Start with the webhook** if you just want alerts tonight. Use the **bot token**
for the threaded view — at ~70 messages a day, threading is what keeps the channel
readable.

> ⚠ At SLAC/Stanford, installing a Slack app often needs workspace-admin approval.
> A greyed-out Install button, or one saying "request approval", is exactly that.
> The webhook route sometimes goes through more easily.

### Step 3a — Incoming webhook (simpler)

1. https://api.slack.com/apps → **Create New App** → **From scratch**
   (name `SWAXS Reactor`, your workspace)
2. **Incoming Webhooks** → toggle **On** → **Add New Webhook to Workspace** →
   choose `#swaxs-autorun` → **Allow**, then copy the URL Slack generates

```bash
# .env in the project root
SWAXS_SLACK_WEBHOOK=https://hooks.slack.com/services/PASTE-YOUR-WEBHOOK-PATH
```

Nothing else. The channel is baked into the URL, so `notify.slack.channel` is
ignored here and `thread_per_recipe` is forced off (webhooks cannot thread).

### Step 3b — Bot token (threading + plots)

1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. **OAuth & Permissions** → **Bot Token Scopes**: `chat:write` *(required)*,
   `files:write` *(optional — the QC plot upload)*
3. **Install to Workspace** → **Allow**, then copy the **Bot User OAuth Token**
   (starts `xoxb-`)
4. **In Slack**, in the channel: `/invite @SWAXS Reactor`
   *(a bot cannot post to a channel it isn't in — easy step to miss)*

```bash
# .env in the project root
SWAXS_SLACK_BOT_TOKEN=xoxb-PASTE-YOUR-BOT-TOKEN-HERE
```

```yaml
# reactor/config.yml
notify:
  slack:
    enabled: false            # leave false — arm it from the app's button
    channel: "#swaxs-autorun" # ← the channel you created
```

`.env` is the primary home for credentials because it survives opening a new
terminal, and `start_platform.sh:17-22` sources it (so does `start_platform.ps1`;
`start_platform.bat` does **not** — on that path export the variables yourself).
A plain `export SWAXS_SLACK_BOT_TOKEN=…` in the shell also works, for a one-off.

## Step 4 — Verify before you rely on it

`tools/notify_test.py` checks both channels without waiting for a beamtime run. It
defaults to `--check`, so running it bare never sends anything.

```bash
python tools/notify_test.py --check              # config + env only, sends NOTHING
python tools/notify_test.py --check --no-probe   # same, minus the SMTP probe (faster)
python tools/notify_test.py --demo --dry-run     # print the messages, no network
python tools/notify_test.py --ping               # ONE real message
python tools/notify_test.py --demo               # simulated 2-condition campaign: threaded
                                                 #   fits, low-confidence fit + QC plot, faults
python tools/notify_test.py --fault              # only what would wake you at 3 a.m.
```

**Every** mode runs both checks first (`notify_test.py:267-268`), so `--ping` and
`--demo` are slow until you add `--no-probe`. `--check` always prints two sections
— Slack, then **Email notifications** with the SMTP probe — and says what is missing:

```
── Slack notification check ─────────────────────────────────
✗ reactor/config.yml → notify.slack.enabled: False   → set it to true
✓ SWAXS_SLACK_BOT_TOKEN is set (xoxb-1234…abcd) → bot mode (threading + plot uploads)
! SWAXS_SLACK_WEBHOOK not set
✓ channel: #swaxs-autorun    ✓ tiers: alert, progress, session
✓ notifier would be ACTIVE at reactor startup
── Email notifications ──────────────────────────────────────
! config notify.email.enabled: False   (the app's button can arm it anyway)
✓ would send as you@stanford.edu via smtp.stanford.edu:587 (starttls)
   …then the SMTP probe rows shown above…
✓ at least one channel is usable
```

That last line is what matters — the script exits non-zero only when *neither*
channel is usable. "would be ACTIVE" reflects the credentials, not
`notify.slack.enabled`: the check forces `enabled: True` to isolate the credential
question, and the button is what actually arms it. The demo posts under
`slacktest_*` recipe ids, so it cannot be confused with real data.

**Offline unit tests** (no network): `python -m pytest tests/test_slack_notify.py
tests/test_email_notify.py -v` — 31 Slack tests and 20 email/multi tests, covering
the isolation guarantees (hangs, exceptions, full queue, rejected auth, an
unreachable SMTP server), tier filtering, threading in both transports, fan-out
when one channel is broken, and that credentials never reach a payload.

**End to end:** with a credential in `.env`, start the platform in mock mode and
queue two conditions — the simulator produces frames, the analyzer fits them, the
results arrive threaded. Same code path beamtime will use.

## Step 5 — Arming from the app: "Leaving the beamline"

The intended workflow is: set everything up, start the measurement, confirm the
first condition looks right, **then** arm notifications on your way out. The card
bottom-left in the reactor app (under E-STOP) does exactly that. So: run
`./start_platform.sh` (it sources `.env`), start the measurement, check the first
condition, then → **🔔 Leaving the beamline** → **Send test message**, then
**🔔 Notify me when I leave**. It turns green (**🔔 Notifications ARMED — click to
stop**) and posts a confirmation, so you know it works *before* you go.

With no credentials the button is disabled and reads **🔔 Notifications not
configured**, naming the variables in its hint — it cannot silently do nothing.
`notify.slack.enabled` / `notify.email.enabled` in `config.yml` are only STARTUP
defaults; the button overrides them at runtime, no restart needed.

Routes: `GET /api/slack` (status for both channels); `POST /api/slack` (`{}`
toggles, or `{"enabled": true|false}`, plus optional
`{"channel": "slack"|"email"|"all"}` to arm one transport only —
`reactor/app.py:624` → `MultiNotifier.enable(which)`); `POST /api/slack/test`.

## What gets posted

Three tiers, selected independently by `notify.slack.tiers` and
`notify.email.tiers`:

| Tier | Events | Volume |
|---|---|---|
| `alert` | E-stop, over-temperature, over-pressure, pump setpoint over limit, pump fault/lost, stale temperature reading, low-confidence fit | rare — someone should go to the hutch |
| `progress` | recipe applied (conditions), run complete (reason, duration), fitted size/PDI/confidence/loss | 2–3 per condition |
| `session` | autonomous session start / finish summary, backend switched | a few per campaign |

`mention_on_alert: "<!channel>"` adds a mention to Slack alerts only; email marks
the alert tier in the subject line instead. With a bot token each recipe gets **one
parent message** and everything else is threaded under it, so a 24 h campaign reads
as one line per condition; email mirrors this with mail-thread headers.

```
▶ Autonomous session started (backend mock)
🧪 Recipe applied — r1
   • T_reac: 240   • F_tot: 80   • x_TOP: 0.15   • run duration: 10m 0s
   ↳ ✅ Run complete — r1
        • stopped by: duration elapsed   • ran: 10m 1s
   ↳ 📊 Fit result — r1
        • size (nm): 4.123   • PDI: 0.0234   • confidence: 0.91   • loss: 0.115
🧪 Recipe applied — r2   …
🚨 EMERGENCY STOP
   pumps that did NOT idle: top — check them immediately
```

The fit result is posted by `_slack_analysis` (`reactor/app.py:319-341`) when the
analyzer publishes `analysis.complete`; it forwards size, PDI, confidence, loss,
distribution and phase into that recipe's thread. When the fit is flagged
`suspect` the message escalates to the `alert` tier and the QC plot is attached via
`upload_png` (`reactor/app.py:337-341`; bot token + `files:write` required).

## Configuration reference

```yaml
notify:
  slack:
    enabled:  false            # startup default; the app's button arms it
    channel:  "#swaxs-autorun" # bot-token mode only
    tiers:    ["alert", "progress", "session"]
    thread_per_recipe: true    # auto-disabled on webhook transport
    mention_on_alert: ""       # "<!channel>" / "<@U012ABC>"
    timeout_s: 6.0             # per request
    min_interval_s: 0.4        # throttle against Slack's rate limit
  email:
    enabled:  false            # startup default; the app's button arms it
    mode:     ""               # "" = infer (starttls with auth, plain without);
                               # or force "starttls" | "ssl" | "plain"
    tiers:    ["alert", "progress", "session"]
    subject_prefix: "[SWAXS]"
    timeout_s: 15.0
    min_interval_s: 1.0        # email is slower than Slack — throttle harder
```

Channel name, tiers and timeouts belong in `reactor/config.yml`; on/off belongs to
the button. **Never** put the Slack bot token, the webhook URL, or the SMTP
username/password there — `reactor/config.yml` is committed to git. Those, plus the
recipient list, live in **`.env`** only (git-ignored). The code reads credentials
solely from the environment and **ignores** a token placed in the config;
`tests/test_slack_notify.py:240-242` asserts that a `bot_token:` in the config
leaves the notifier disabled.

## Isolation guarantees (all tested — and they apply to email too)

- `notify()` returns in **< 0.2 s even if the transport hangs for 5 s** — it only enqueues.
- A transport exception does not propagate and does not kill the worker thread.
- A full queue (200 items) **drops** notifications rather than blocking the caller.
- Slack rejections (`invalid_auth`, rate limits) and unreachable SMTP servers are
  logged to the reactor log, never raised.
- One broken channel cannot stop the other: each forward in `MultiNotifier` is
  individually guarded (`src/notify/multi.py:102-112`).
- The notifier is driven from the controller's `event_cb`, which runs **outside** the
  controller lock — so it can never add latency to an E-stop.
- Credentials are never included in a message payload.

## Architecture

```
ReactorController._event("reactor.run_start", …)
        │
reactor/app.py::_event_cb          ← also publishes to the hub event bus
        │  try/except (cannot raise)
src/notify/multi.py::MultiNotifier.notify()
        ├── SlackNotifier.notify()  → queue.put_nowait (never blocks)
        │      worker thread → urllib POST, timeout, errors swallowed
        └── EmailNotifier.notify()  → queue.put_nowait (never blocks)
               worker thread → smtplib send, timeout, errors swallowed
```

Only stdlib is used (`urllib`, `smtplib`, `queue`, `threading`) — no new dependency.

Events consumed from the controller (`reactor/app.py:130-154`):
`reactor.run_start`, `reactor.run_complete`, `reactor.estop`, `reactor.safety`
(carries `check` + `detail`), `reactor.backend`. Events consumed from the hub bus
(`reactor/app.py:304-316`): `file.averaged`, `analysis.complete`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Button says "Notifications not configured" | `.env` not loaded — did you start via `start_platform.sh`? Check `python tools/notify_test.py --check` |
| `not_in_channel` in the reactor log | The bot wasn't invited: `/invite @SWAXS Reactor` |
| `invalid_auth` | Token copied wrong, or the app was reinstalled (token changes) |
| `channel_not_found` | `notify.slack.channel` typo, or a private channel the bot isn't in |
| Messages arrive but aren't threaded | Webhook mode — webhooks cannot thread. Use a bot token |
| No SMTP option worked in the probe | Outbound mail is blocked from that host — ask IT which relay/port is permitted |
| Nothing arrives, no errors | Notifications not armed. Click the button (or set `enabled: true`) |
