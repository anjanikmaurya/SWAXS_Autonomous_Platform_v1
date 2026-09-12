"""
src/watchdog/policy.py — Notification policy (pure, testable).

Pure functions deciding whether a message should be sent given the current time,
config, and state. No I/O, no side effects. Always pass `now` in so tests can
control the clock.

Faults ALWAYS send regardless of quiet hours or snooze.
Progress messages respect quiet hours, snooze, and summary batching.
"""
from __future__ import annotations

from datetime import datetime, timezone

# The five independent message categories a message can belong to. "safety"
# is special: should_send() refuses to let it be filtered out by the
# categories dict while sending is on — Stop is the only way to silence it.
CATEGORIES = ("safety", "stalls", "results", "progress", "campaign")

# Bus event type -> category, for messages built from event_to_message().
# Anything not listed here (e.g. a rare info-only event) buckets under
# "progress" rather than being uncategorized and unfilterable.
_EVENT_CATEGORY = {
    "reactor.estop": "safety",
    "reactor.safety": "safety",
    "reactor.run_start": "progress",
    "reactor.run_complete": "progress",
    "reactor.backend": "progress",
    "fit.complete": "results",
}


def category_for_event(event_type: str) -> str:
    """Which notification category a bus event's message belongs to."""
    return _EVENT_CATEGORY.get(event_type, "progress")


def should_send(
    kind: str,
    now: datetime,
    cfg: dict,
    state: dict,
    category: str | None = None,
) -> tuple[bool, str]:
    """
    Decide whether to send a message right now.

    Args:
        kind: "fault" | "progress" | "info"
        now: Current time (datetime with UTC timezone)
        cfg: Settings dict from load_settings()
        state: Mutable state dict tracking snooze/summary window
        category: one of CATEGORIES, or None to skip category filtering
            (kept optional so callers that don't have a category, and
            existing tests, are unaffected)

    Returns:
        (send: bool, level: str)
        level is "fault" or "info" (for Slack webhook)
    """
    default_level = "fault" if kind == "fault" else "info"

    # Master switch, checked first: nothing is sent at all when off.
    if not cfg.get("slack_enabled", True):
        return False, default_level

    # Category filter. "safety" can never be filtered out while sending is
    # on — the only way to silence it is the master switch above.
    if category is not None and category != "safety":
        categories = cfg.get("categories") or {}
        if not categories.get(category, True):
            return False, default_level

    if kind == "fault":
        return True, "fault"

    if kind not in ("progress", "info"):
        return False, "info"

    # Check if we're in quiet hours
    quiet_hours = cfg.get("quiet_hours")
    if quiet_hours:
        if _in_quiet_hours(now, quiet_hours):
            return False, "info"

    # Check if snoozed
    snooze_until = state.get("snooze_until")
    if snooze_until is not None:
        snooze_ts = snooze_until if isinstance(snooze_until, float) else 0.0
        if now.timestamp() < snooze_ts:
            return False, "info"

    return True, "info"


def _in_quiet_hours(now: datetime, quiet_hours: tuple) -> bool:
    """Check if the given time falls within the quiet_hours window."""
    start_h, start_m = quiet_hours[0]
    end_h, end_m = quiet_hours[1]

    now_h = now.hour
    now_m = now.minute
    now_minutes = now_h * 60 + now_m

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes < end_minutes
    else:
        return now_minutes >= start_minutes or now_minutes < end_minutes


def set_snooze(state: dict, duration_min: int, now: datetime) -> None:
    """Record a snooze in state until (now + duration_min)."""
    snooze_until = now.timestamp() + (duration_min * 60)
    state["snooze_until"] = snooze_until


def clear_snooze(state: dict) -> None:
    """Clear the snooze."""
    state["snooze_until"] = None
