# Notification Setup — Step by Step

Two channels. **Email needs no approval and works today**; Slack needs a
workspace-app credential that an admin must approve.

---

## Blocked on Slack app approval? Use email (5 minutes, no permission)

If Slack says *"You'll need approval from someone who manages apps on your
workspace"*, click **Request to Add New Webhook** and use email meanwhile. Same
tiers, same isolation, pushes to your phone — and it can deliver **into Slack**
later via a channel-email address.

```bash
# .env in the project root (git-ignored, sourced by start_platform.sh)
SWAXS_SMTP_HOST=smtp.stanford.edu
SWAXS_SMTP_USER=you@stanford.edu       # omit if your relay needs no auth
SWAXS_SMTP_PASSWORD=your-app-password
SWAXS_NOTIFY_EMAIL=you@stanford.edu    # comma-separate for several people
```

```bash
uv run tools/notify_test.py --check    # PROBES which SMTP ports actually work here
uv run tools/notify_test.py --ping
```

`--check` connection-tests ports 587/465/25 from that machine and tells you which
one works, so you don't have to guess:

```
   probing SMTP options from THIS machine (a few seconds)…
   ✓ smtp.stanford.edu:587 starttls (auth required)
   ✗ smtp.stanford.edu:465 ssl  ConnectionRefusedError
   ✗ smtp.stanford.edu:25 plain  timed out
   ✓ at least one option works — put that host/port in .env
```

Then the app's **🔔 Notify me when I leave** button arms email exactly as it
would Slack. Nothing else changes.

### Getting email into Slack without app approval

In the channel: **Settings → Integrations → Send emails to this channel**. Copy
the generated address into `SWAXS_NOTIFY_EMAIL` and the messages appear in Slack.
Often available to ordinary members, so it can bypass the app-approval queue.

### Text messages

Most carriers accept email-to-SMS (`+15551234567@txt.att.net`,
`@vtext.com`, …). Add it to `SWAXS_NOTIFY_EMAIL` alongside your address and set
`notify.email.tiers: ["alert"]` so only genuine faults buzz your phone.

---

## Slack — for when approval lands

Two things to decide, then three files to touch. ~10 minutes.

---

## Step 1 — Create the channel

Yes, create one. In Slack: **+ → Create a channel** → e.g. `#swaxs-autorun`.

Use a **dedicated** channel, not a general team one. During a 24 h campaign this
posts one thread per condition; mixed into a busy channel it becomes noise and
people mute it — which defeats the purpose. Invite whoever needs to be woken up.

---

## Step 2 — Pick a transport

| | Incoming webhook | Bot token |
|---|---|---|
| Setup time | ~2 min | ~5 min |
| Threading (one thread per recipe) | ✗ flat messages | ✓ |
| QC plot attached on a bad fit | ✗ | ✓ |
| Needs workspace admin approval | sometimes | usually |

**Start with the webhook** if you just want alerts tonight. Use the **bot token**
for the threaded view — with ~70 messages a day, threading is what keeps the
channel readable.

> ⚠ At SLAC/Stanford, installing a Slack app often needs workspace-admin
> approval. If the Install button is greyed out or says "request approval", that's
> what it is — ask your workspace admin. The webhook route sometimes goes through
> more easily.

---

## Step 3a — Incoming webhook (simpler)

1. https://api.slack.com/apps → **Create New App** → **From scratch**
   - Name: `SWAXS Reactor` · Workspace: yours
2. Left sidebar → **Incoming Webhooks** → toggle **On**
3. **Add New Webhook to Workspace** → choose `#swaxs-autorun` → **Allow**
4. Copy the URL (`https://hooks.slack.com/services/T…/B…/…`)

**What goes where:**

```bash
# .env in the project root (git-ignored, auto-loaded by start_platform.sh)
SWAXS_SLACK_WEBHOOK=https://hooks.slack.com/services/PASTE-YOUR-WEBHOOK-PATH
# (Slack gives you the full https://hooks.slack.com/services/... URL.
#  Keep it in .env — it is a credential, not a setting.)
```

Nothing else. The channel is baked into the URL, so `notify.slack.channel` is
ignored in this mode.

---

## Step 3b — Bot token (threading + plots)

1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. **OAuth & Permissions** → *Scopes* → **Bot Token Scopes** → **Add**:
   - `chat:write` — post messages *(required)*
   - `files:write` — upload the QC plot *(optional)*
3. Scroll up → **Install to Workspace** → **Allow**
4. Copy **Bot User OAuth Token** (starts `xoxb-`)
5. **In Slack**, in the channel, type: `/invite @SWAXS Reactor`
   *(a bot cannot post to a channel it isn't in — this step is easy to miss)*

**What goes where:**

```bash
# .env in the project root
SWAXS_SLACK_BOT_TOKEN=xoxb-PASTE-YOUR-BOT-TOKEN-HERE
# (Slack shows it as xoxb- followed by two number groups and a
#  24-character suffix. Never commit the real value — .env is
#  git-ignored for exactly this reason.)
```

```yaml
# reactor/config.yml
notify:
  slack:
    enabled: false            # leave false — arm it from the app's button
    channel: "#swaxs-autorun" # ← the channel you created
```

---

## Step 4 — Verify before you rely on it

```bash
cp .env.example .env          # if you don't have one yet, then edit it
# …paste your token/URL into .env…

uv run tools/slack_test.py --check
```

Expected:

```
✓ reactor/config.yml → notify.slack.enabled: False
✓ SWAXS_SLACK_BOT_TOKEN is set (xoxb-1234…mno) → bot mode (threading + plot uploads)
✓ channel: #swaxs-autorun
✓ notifier would be INACTIVE at reactor startup      ← expected: the button arms it
```

Then send a real one:

```bash
uv run tools/slack_test.py --ping     # one message
uv run tools/slack_test.py --demo     # a full simulated campaign — see the volume
```

If `--ping` arrives, you're done.

---

## Step 5 — Use it

1. `./start_platform.sh` (it sources `.env` for you)
2. Set up and **start the measurement**
3. Confirm the first condition looks right
4. Reactor app → bottom-left **🔔 Leaving the beamline**
   - **Send test message** → check Slack
   - **🔔 Notify me on Slack** → turns green, posts a confirmation to the channel
5. Leave.

---

## Summary of what goes where

| Item | Where | Why |
|---|---|---|
| Bot token / webhook URL | **`.env`** (project root) | git-ignored; `start_platform.sh` sources it |
| Channel name | `reactor/config.yml` → `notify.slack.channel` | bot mode only; not a secret |
| Which events to send | `reactor/config.yml` → `notify.slack.tiers` | `alert` / `progress` / `session` |
| On/off | **the app's button** | arm it when you leave, not at startup |

**Never** put the token in `reactor/config.yml` — that file is committed. The code
reads credentials only from the environment, and ignores a token placed in the
config (there's a test asserting this).

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Button says "Slack not configured" | `.env` not loaded — did you start via `start_platform.sh`? Check `uv run tools/slack_test.py --check` |
| `not_in_channel` in the reactor log | The bot wasn't invited: `/invite @SWAXS Reactor` |
| `invalid_auth` | Token copied wrong, or the app was reinstalled (token changes) |
| `channel_not_found` | `notify.slack.channel` typo, or a private channel the bot isn't in |
| Messages arrive but aren't threaded | Webhook mode — webhooks can't thread. Use a bot token |
| Nothing arrives, no errors | Notifications not armed. Click the button (or set `enabled: true`) |
