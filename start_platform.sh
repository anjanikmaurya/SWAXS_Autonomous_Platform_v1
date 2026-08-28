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
echo "  ║   → http://localhost:${SWAXS_HUB_PORT:-5000}                            ║"
echo "  ║                                                      ║"
echo "  ║   Apps (started from the hub UI):                    ║"
echo "  ║     5009 Calibration & Raw Prep                      ║"
echo "  ║     5001 Reduction & Correction                      ║"
echo "  ║     5002 Data Viewer                                 ║"
echo "  ║     5003 Background Subtraction                      ║"
echo "  ║     5006 Quality Gate (AI good/bad grading)          ║"
echo "  ║     5007 Flow Synthesis (5-pump reactor control)     ║"
echo "  ║     5004 Data Analysis                               ║"
echo "  ║     5008 Nanoparticle Analyzer (auto SAXS fit)       ║"
echo "  ║     5005 AI Assistant                                ║"
echo "  ║                                                      ║"
echo "  ║   Press  Ctrl-C  to stop the hub AND its apps         ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo ""

# ── Find the interpreter ──────────────────────────────────────────────────────
# The hub launches its sub-apps with sys.executable, so whichever Python starts
# the hub is the one that must have the dependencies. Previously this line was
# `uv run hub/app.py`, which broke the documented install: README/QUICKSTART tell
# you to `python -m venv venv && pip install -r requirements-core.txt`, and uv
# either is not installed ("uv: command not found") or resolves a different,
# empty environment. There is no pyproject.toml or uv.lock here for it to use.
if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    PY="$VIRTUAL_ENV/bin/python"
elif [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
    PY="$CONDA_PREFIX/bin/python"
elif [ -x "venv/bin/python" ]; then
    PY="venv/bin/python"          # present but not activated - use it anyway
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    PY="python"
fi

# Refuse a Python too old for the science stack (same gate as the .ps1/.bat).
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)'; then
    echo "  x $("$PY" -V 2>&1) is too old - this platform needs Python 3.10 or newer."
    echo "    See QUICKSTART.md."
    exit 1
fi

# Say which one, so a wrong-environment problem is visible instead of silent.
echo "  Python: $PY ($("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"

MISSING=$("$PY" - <<'PROBE'
import importlib.util as u
print(",".join(m for m in ("flask","numpy","scipy","matplotlib","pandas","yaml","fabio","pyFAI")
                if u.find_spec(m) is None))
PROBE
)
if [ -n "$MISSING" ]; then
    echo "  x Missing packages: $MISSING"
    echo ""
    echo "    $PY -m pip install -r requirements-core.txt"
    echo ""
    echo "  See QUICKSTART.md if that fails."
    exit 1
fi

exec "$PY" hub/app.py
