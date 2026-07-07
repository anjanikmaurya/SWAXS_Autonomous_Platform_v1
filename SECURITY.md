# API Key / Token Handling — SWAXS Platform

The AI Assistant authenticates to **SLAC-managed AI services** through the
enterprise gateway (see SLAC IT KB0015379). The token is a privileged credential
— treat it like a password. **Never** put it in chat, email, tickets, scripts,
or version control.

## What you need

| Item | Value / source | Secret? |
|---|---|---|
| Endpoint | `https://ai-api.slac.stanford.edu` | No |
| Auth | `ANTHROPIC_AUTH_TOKEN` (Bearer) — your SLAC API token | **Yes** |
| Model | `us.anthropic.claude-sonnet-4-6` (gateway/Bedrock id) | No |
| Network | SLAC onsite **or** SLAC VPN (gateway is unreachable offsite) | — |

Request a token via ServiceNow:
<https://slacprod.servicenowservices.com/it_services?id=sc_cat_item&sys_id=515f28711b607110c5d320eae54bcb64>

## How this project consumes it

`src/ai/assistant.py` reads everything from the **environment** (never the repo):

- `ANTHROPIC_AUTH_TOKEN` → Bearer auth (preferred, SLAC gateway)
- `ANTHROPIC_BASE_URL`   → gateway endpoint
- `ANTHROPIC_MODEL`      → gateway model id
- `ANTHROPIC_API_KEY`    → only a fallback for the direct Anthropic API

If those aren't already set in the environment, the app automatically loads them
from **`~/.claude/settings.json`** (the same file the Claude Code CLI uses) — so
you configure the token in **one place** and both tools share it. Real
environment variables always take precedence; the doc placeholder token is
ignored.

## Recommended setup — single source: `~/.claude/settings.json`

This is the SLAC-sanctioned location (KB0015379), shared with the Claude Code CLI.
Configure the token once here and the SWAXS app uses it too.

1. Create the directory and file (use a text editor so the token never lands in
   your shell history):
   ```bash
   mkdir -p ~/.claude && chmod 700 ~/.claude
   nano ~/.claude/settings.json      # or TextEdit / VS Code
   ```
2. Paste the JSON block from KB0015379, replacing `yourSlacApiKeyHere` with your
   issued SLAC token. The `env` block must include at least:
   ```json
   {
     "env": {
       "ANTHROPIC_BASE_URL": "https://ai-api.slac.stanford.edu",
       "ANTHROPIC_AUTH_TOKEN": "<your token>",
       "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-4-6"
     }
   }
   ```
3. Lock the file permissions:
   ```bash
   chmod og-rwx ~/.claude/settings.json
   ```
4. Run `./start_platform.sh` on the SLAC network/VPN. The banner shows
   `AI auth: ~/.claude/settings.json (SLAC gateway)`, and
   `GET http://localhost:5005/api/health` reports `credentials: gateway-token`.

**Alternative / override:** export `ANTHROPIC_AUTH_TOKEN` yourself before running
(e.g. from macOS Keychain: `export ANTHROPIC_AUTH_TOKEN="$(security
find-generic-password -s slac-anthropic -w)"`). Environment variables always win
over the settings file. Never put the token in `.env` or anywhere in the repo.

## Rotation & exposure

- If the token may have been exposed (committed, shared, pasted), notify
  `#slac-it-ai` immediately to **revoke and rotate**, then store the new one.
- Don't share your token; if a colleague needs access, they request their own.
- Quick repo check (should print nothing):
  ```bash
  git log -p -S ANTHROPIC_AUTH_TOKEN ; git log -p -S ANTHROPIC_API_KEY
  ```

## Compliance notes

- The endpoint **must** be a `slac.stanford.edu` URL at SLAC — using
  `api.anthropic.com` there is a policy violation.
- `.env` is git-ignored. If you keep one for local non-secret config, lock it:
  `chmod 600 .env`.
- Feedback / questions: Slack `#slac-it-ai`.
