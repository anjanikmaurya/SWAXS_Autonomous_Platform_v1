#!/usr/bin/env bash
# start_platform.sh
# ──────────────────────────────────────────────────────────────────────────────
# Start the SWAXS Platform Hub.
# The Hub will then start/stop individual apps on demand via its web interface.
#
# Usage:
#   ./start_platform.sh
#   ./start_platform.sh /path/to/experiment   # pre-set project folder
#
# Open:  http://localhost:5000
# ──────────────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

# ── Load .env if present (local, non-committed config) ────────────────────────
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# ── SLAC AI gateway (see KB0015379 / SECURITY.md) ─────────────────────────────
# The AI Assistant reads its token + endpoint + model from ~/.claude/settings.json
# (SLAC's sanctioned location) when they aren't already in the environment — one
# place to maintain, nothing secret in this repo. To override, export
# ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL before running.
# Requires SLAC network / VPN to reach the gateway.
_claude_settings="$HOME/.claude/settings.json"
if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "  AI auth: token from environment"
elif [ -f "$_claude_settings" ] && grep -q '"ANTHROPIC_AUTH_TOKEN"' "$_claude_settings" \
     && ! grep -q 'yourSlacApiKeyHere' "$_claude_settings"; then
  echo "  AI auth: ~/.claude/settings.json (SLAC gateway)"
else
  echo "  AI auth: no token found — AI Assistant disabled."
  echo "           Add your token to ~/.claude/settings.json (see SECURITY.md)."
fi

if [ -n "$1" ]; then
  export SWAXS_PROJECT="$1"
  echo "Project: $SWAXS_PROJECT"
fi

echo ""
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║   SWAXS Platform Hub                                 ║"
echo "  ║   → http://localhost:5000                            ║"
echo "  ║                                                      ║"
echo "  ║   Apps (started from the hub UI):                    ║"
echo "  ║     5001 Reduction & Correction                      ║"
echo "  ║     5002 Data Viewer                                 ║"
echo "  ║     5003 Background Subtraction                      ║"
echo "  ║     5006 Quality Gate (AI good/bad grading)          ║"
echo "  ║     5007 Flow Synthesis (5-pump reactor control)     ║"
echo "  ║     5004 Data Analysis                               ║"
echo "  ║     5005 AI Assistant                                ║"
echo "  ║                                                      ║"
echo "  ║   Press  Ctrl-C  to stop the hub                     ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo ""

uv run hub/app.py
