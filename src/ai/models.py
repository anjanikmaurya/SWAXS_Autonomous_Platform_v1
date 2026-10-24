"""
src/ai/models.py: ONE place for which Claude model the platform's AI uses, and
how it logs in. The assistant (Guinier) already worked on the SLAC Bedrock
gateway; everything else (Analyser next-recipe levels, AI fitter, knowledge
drafting, loop advice) now uses the same rules:

* Login: ``~/.claude/settings.json`` (SLAC gateway: ANTHROPIC_AUTH_TOKEN +
  ANTHROPIC_BASE_URL) is loaded into the environment first, the same loader the
  assistant uses; else ANTHROPIC_API_KEY (also read from the project's .env).
* Model: the gateway needs its own model ids ("us.anthropic.claude-sonnet-4-6");
  a bare id such as "claude-sonnet-5" is REJECTED ("Invalid model name"). The
  default is, in order: an app-specific variable (e.g. SWAXS_DESIGN_MODEL), then
  ANTHROPIC_MODEL, then settings.json's ANTHROPIC_DEFAULT_SONNET_MODEL, then
  GATEWAY_DEFAULT. The settings are loaded BEFORE the default is read.
"""
from __future__ import annotations

import os
from pathlib import Path

#: same ids the assistant's model picker offers (assistant/templates/index.html)
GATEWAY_DEFAULT = "us.anthropic.claude-sonnet-4-6"
MODELS = [
    ("us.anthropic.claude-sonnet-4-6", "Sonnet 4.6 (default, balanced)"),
    ("us.anthropic.claude-sonnet-5", "Sonnet 5"),
    ("us.anthropic.claude-opus-4-8", "Opus 4.8 (stronger)"),
    ("us.anthropic.claude-opus-5", "Opus 5 (strongest, slower)"),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "Haiku 4.5 (fastest)"),
]
_ROOT = Path(__file__).resolve().parents[2]


def load_login_env() -> None:
    """Fill ANTHROPIC_* from ~/.claude/settings.json (never overwrites real env)."""
    try:
        from .assistant import _load_claude_settings_into_env
        _load_claude_settings_into_env()
    except Exception:                                          # noqa: BLE001
        pass
    if not (os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        env = _ROOT / ".env"
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if key:
                        os.environ["ANTHROPIC_API_KEY"] = key
                    break
        except OSError:
            pass


def default_model(app_var: str | None = None) -> str:
    load_login_env()
    return ((os.environ.get(app_var, "").strip() if app_var else "")
            or os.environ.get("ANTHROPIC_MODEL", "").strip() or GATEWAY_DEFAULT)


def get_client(timeout: float = 120.0):
    """An Anthropic client with the platform's login, or None. Never raises."""
    load_login_env()
    tok = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not (tok or key):
        return None
    try:
        import anthropic
        kw = {"max_retries": 2, "timeout": float(timeout)}
        base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        if base:
            kw["base_url"] = base
        if tok:
            kw["auth_token"] = tok
        else:
            kw["api_key"] = key
        return anthropic.Anthropic(**kw)
    except Exception:                                          # noqa: BLE001
        return None


def login_summary() -> dict:
    """What is configured, without any secret: for status pages and ai_check."""
    load_login_env()
    import importlib.util
    return {"package": importlib.util.find_spec("anthropic") is not None,
            "login": "gateway token" if os.environ.get("ANTHROPIC_AUTH_TOKEN")
            else ("API key" if os.environ.get("ANTHROPIC_API_KEY") else "none"),
            "gateway": bool(os.environ.get("ANTHROPIC_BASE_URL")),
            "settings_file": (Path.home() / ".claude" / "settings.json").is_file()}
