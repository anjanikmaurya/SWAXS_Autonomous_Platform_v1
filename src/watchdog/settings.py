"""
src/watchdog/settings.py — Load and validate watchdog config.

Config lives in watchdog/config.yml and holds non-secret policy:
- notify.quiet_hours: "HH:MM-HH:MM" time window to suppress progress (not faults)
- notify.summary: "off" | "hourly" batching mode
- notify.snooze_default_min: default snooze duration
- notify.min_interval_s: throttle between sends

The webhook URL comes from the environment (SWAXS_SLACK_WEBHOOK_URL), never
config.yml (which is in git).

A malformed config fails loudly at load time, not silently at 3 a.m.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.watchdog.policy import CATEGORIES

logger = logging.getLogger(__name__)


class ValidationError(ValueError):
    """Raised when config is malformed."""

    pass


def _parse_time(s: str) -> tuple[int, int]:
    """Parse "HH:MM" into (hour, minute). Raises ValidationError."""
    if not isinstance(s, str):
        raise ValidationError(f"time must be a string, got {type(s).__name__}")
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValidationError(f"invalid time format '{s}' — must be HH:MM")
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        raise ValidationError(f"invalid time format '{s}' — HH and MM must be integers")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValidationError(f"invalid time '{s}' — H must be 0–23, M must be 0–59")
    return h, m


def _parse_quiet_hours(cfg: dict) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Parse quiet_hours from config. Returns ((start_h, start_m), (end_h, end_m)) or None."""
    s = cfg.get("quiet_hours")
    if not s:
        return None
    if not isinstance(s, str):
        raise ValidationError(f"quiet_hours must be a string, got {type(s).__name__}")
    parts = s.split("-")
    if len(parts) != 2:
        raise ValidationError(
            f"invalid quiet_hours format '{s}' — must be 'HH:MM-HH:MM'"
        )
    start = _parse_time(parts[0])
    end = _parse_time(parts[1])
    return start, end


def load_settings(config_path: str | Path) -> dict:
    """
    Load and validate watchdog config.yml.

    Returns a dict with keys:
      - quiet_hours: ((h, m), (h, m)) | None
      - summary: "off" | "hourly"
      - snooze_default_min: int
      - min_interval_s: float

    Raises ValidationError if config is malformed or missing required keys.
    """
    try:
        import yaml
    except ImportError:
        raise ValidationError("PyYAML not installed")

    config_path = Path(config_path)
    if not config_path.is_file():
        raise ValidationError(f"config.yml not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValidationError(f"failed to parse config.yml: {exc}")

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValidationError(f"config.yml must be a dict, got {type(raw).__name__}")

    notify_cfg = raw.get("notify", {})
    if not isinstance(notify_cfg, dict):
        raise ValidationError(f"notify section must be a dict, got {type(notify_cfg).__name__}")

    quiet_hours = _parse_quiet_hours(notify_cfg)

    summary = str(notify_cfg.get("summary", "off")).strip().lower()
    if summary not in ("off", "hourly"):
        raise ValidationError(f"summary must be 'off' or 'hourly', got '{summary}'")

    try:
        snooze_default_min = int(notify_cfg.get("snooze_default_min", 30))
    except (ValueError, TypeError):
        raise ValidationError("snooze_default_min must be an integer")
    if snooze_default_min < 1:
        raise ValidationError("snooze_default_min must be >= 1")

    try:
        min_interval_s = float(notify_cfg.get("min_interval_s", 3.0))
    except (ValueError, TypeError):
        raise ValidationError("min_interval_s must be a number")
    if min_interval_s < 0:
        raise ValidationError("min_interval_s must be >= 0")

    slack_enabled = bool(notify_cfg.get("slack_enabled", True))

    raw_categories = notify_cfg.get("categories", {})
    if not isinstance(raw_categories, dict):
        raise ValidationError(f"categories must be a dict, got {type(raw_categories).__name__}")
    categories = {}
    for c in CATEGORIES:
        v = raw_categories.get(c, True)
        if not isinstance(v, bool):
            raise ValidationError(f"categories.{c} must be a boolean")
        categories[c] = v
    # Safety exception: cannot be silenced by the category filter while the
    # master switch is on — Stop is the only way to silence it.
    if slack_enabled:
        categories["safety"] = True

    return {
        "quiet_hours": quiet_hours,
        "summary": summary,
        "snooze_default_min": snooze_default_min,
        "min_interval_s": min_interval_s,
        "slack_enabled": slack_enabled,
        "categories": categories,
    }


def save_notify_settings(
    config_path: str | Path,
    *,
    slack_enabled: bool | None = None,
    categories: dict | None = None,
) -> dict:
    """Persist the Slack master switch and/or category toggles to config.yml.

    Regenerates the whole file from a fixed template — rather than a generic
    yaml.dump of the loaded dict — so the file's explanatory comments survive
    an update made from the UI. Other notify.* keys (quiet_hours, summary,
    snooze_default_min, min_interval_s) are read from the current file and
    carried through unchanged.

    Returns the resolved {"slack_enabled": ..., "categories": {...}}.
    """
    import yaml

    config_path = Path(config_path)
    raw = {}
    if config_path.is_file():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    notify_cfg = raw.get("notify", {}) if isinstance(raw, dict) else {}
    if not isinstance(notify_cfg, dict):
        notify_cfg = {}

    quiet_hours = notify_cfg.get("quiet_hours", "23:00-07:00")
    summary = notify_cfg.get("summary", "off")
    snooze_default_min = int(notify_cfg.get("snooze_default_min", 30) or 30)
    min_interval_s = notify_cfg.get("min_interval_s", 3)

    resolved_enabled = bool(notify_cfg.get("slack_enabled", True))
    if slack_enabled is not None:
        resolved_enabled = bool(slack_enabled)

    cur_categories = notify_cfg.get("categories") or {}
    resolved_categories = {c: bool(cur_categories.get(c, True)) for c in CATEGORIES}
    if categories is not None:
        for k, v in categories.items():
            if k in CATEGORIES:
                resolved_categories[k] = bool(v)
    # Safety exception, enforced here too (belt and suspenders with load_settings
    # and policy.should_send): cannot be false while sending is on.
    if resolved_enabled:
        resolved_categories["safety"] = True

    lines = [
        "# Auto Watch configuration — non-secret policy",
        "# The webhook URL is a secret and goes in .env: SWAXS_SLACK_WEBHOOK_URL",
        "",
        "notify:",
        "  # Suppress progress messages during these hours (faults always send)",
        '  # Format: "HH:MM-HH:MM" or null to disable',
        f"  quiet_hours: {json_or_null(quiet_hours)}",
        "",
        '  # Batch progress messages: "off" | "hourly"',
        "  # off: one message per event",
        "  # hourly: one digest per hour per recipe (placeholder for later)",
        f"  summary: {json_or_null(summary)}",
        "",
        "  # Default snooze duration when POST /api/snooze {minutes} is called",
        f"  snooze_default_min: {snooze_default_min}",
        "",
        "  # Minimum time (seconds) between webhook POSTs",
        f"  min_interval_s: {min_interval_s}",
        "",
        "  # Master switch for Slack sending. When false, nothing is sent at all —",
        "  # checked before quiet hours, snooze, and the category filters below.",
        f"  slack_enabled: {str(resolved_enabled).lower()}",
        "",
        "  # Independent category toggles — any combination, including all or none.",
        "  # 'safety' cannot be false while slack_enabled is true: the only way to",
        "  # silence safety alerts is to turn slack_enabled off entirely.",
        "  categories:",
    ]
    for c in CATEGORIES:
        lines.append(f"    {c}: {str(resolved_categories[c]).lower()}")
    lines.append("")

    config_path.write_text("\n".join(lines), encoding="utf-8")
    return {"slack_enabled": resolved_enabled, "categories": resolved_categories}


def json_or_null(v) -> str:
    """Render a scalar for the hand-written config.yml template."""
    if v is None:
        return "null"
    return f'"{v}"'
