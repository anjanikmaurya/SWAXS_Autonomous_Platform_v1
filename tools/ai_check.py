#!/usr/bin/env python3
"""ai_check.py: is the AI connected? One tiny real call per model; no secret is printed.

    python tools/ai_check.py                    # the default model from your settings
    python tools/ai_check.py --all              # every model offered in the Analyser
    python tools/ai_check.py --model us.anthropic.claude-opus-5 --thinking standard

Reads the same login as the apps: ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY,
else ~/.claude/settings.json (and ANTHROPIC_BASE_URL for a gateway).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.optimizer import ai_design as A  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="")
    ap.add_argument("--thinking", default="off", choices=list(A.THINKING))
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    from src.ai.models import login_summary
    s = login_summary()
    print("anthropic package:", "installed" if s["package"] else "MISSING (pip install -r requirements-ai.txt)")
    print("settings.json:    ", "found" if s["settings_file"] else "not found (~/.claude/settings.json)")
    print("login:            ", s["login"])
    print("gateway:          ", "set (ANTHROPIC_BASE_URL)" if s["gateway"] else "not set (direct API)")
    print("default model:    ", A._model())
    models = [m for m, _ in A.MODELS] if a.all else [a.model]
    ok_any = False
    for m in models:
        r = A.test_connection(m, a.thinking)
        ok_any |= r["ok"]
        print(f"  {r['model']:<22} {'OK ' + str(r['ms']) + ' ms' if r['ok'] else 'FAILED: ' + r['error']}")
    return 0 if ok_any else 1


def _has(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


if __name__ == "__main__":
    sys.exit(main())
