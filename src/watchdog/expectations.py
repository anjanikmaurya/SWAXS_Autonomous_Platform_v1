"""
src/watchdog/expectations.py — Stall detection (pure, testable).

Given the recent event history and current state, which stage is overdue?
No I/O, no clock reads inside functions — pass `now` in.

A stage is "overdue" when an expected event hasn't arrived within a threshold.
For example, if a `reactor.run_start` event arrived but `file.averaged` never
followed within 1 hour, the platform has stalled waiting for the SAXS side to
produce a result.
"""
from __future__ import annotations

from datetime import datetime


# Timeout windows (seconds) — how long to wait for the NEXT stage after we enter current one
# Key is the stage we're waiting for, not the stage we're in
STAGE_TIMEOUTS = {
    "reduce": 3600,           # After run_start, expect file.reduced within 1 hour (2D collection)
    "average": 1800,          # After file.reduced, expect file.averaged within 30 min
    "subtract": 1800,         # After file.averaged, expect file.subtracted within 30 min
    "fit": 1800,              # After file.subtracted, expect fit.complete within 30 min
}


def which_stage_is_overdue(
    now: datetime,
    recent_events: list[dict],
    max_age_s: int = 7200,
) -> tuple[str, float] | None:
    """
    Scan recent events and return (stage_name, overdue_seconds) if a stage is stalled.

    Parameters:
        now: Current time (datetime with UTC timezone)
        recent_events: List of dicts with 'type', 'timestamp' (ISO string)
        max_age_s: Don't flag events older than this (they may be from a prior campaign)

    Returns:
        (stage_name, overdue_s) if a stage is overdue, None otherwise.

    Events that trigger stage advances:
        collect       → reactor.run_start event fires (synthesis begins)
        reduce        → file.reduced event fires (2D image converted to 1D curve)
        average       → file.averaged event fires (scans averaged)
        subtract      → file.subtracted event fires (background removed)
        fit           → fit.complete event fires (auto-fit finished, result available)

    Example: if the last event was "file.reduced" 2 hours ago, and reduce→average
    timeout is 1800s, then we're 2400s overdue on the 'average' stage.
    """
    if not recent_events:
        return None

    now_ts = now.timestamp()

    # Find the most recent event of each relevant type
    last_by_type = {}
    for event in reversed(recent_events):
        etype = event.get("type", "")
        if etype not in last_by_type:
            last_by_type[etype] = event

    # Map events to stages they trigger
    stage_markers = {
        "reactor.run_start": "collect",
        "file.reduced": "reduce",
        "file.averaged": "average",
        "file.subtracted": "subtract",
        "fit.complete": "fit",
    }

    # Build a timeline: stage → when we entered it (last event that triggered it)
    stage_timeline = {}
    for etype, stage in stage_markers.items():
        if etype in last_by_type:
            event = last_by_type[etype]
            ts_str = event.get("timestamp", "")
            try:
                event_ts = datetime.fromisoformat(ts_str).timestamp()
            except (ValueError, TypeError):
                continue
            if now_ts - event_ts < max_age_s:
                stage_timeline[stage] = event_ts

    if not stage_timeline:
        return None

    # Find the *latest* stage we've entered
    latest_stage_name = max(
        stage_timeline.keys(),
        key=lambda s: stage_timeline[s],
    )
    latest_stage_ts = stage_timeline[latest_stage_name]

    # If we've entered a stage, check if the NEXT stage is overdue
    stages_in_order = ["collect", "reduce", "average", "subtract", "fit"]
    try:
        idx = stages_in_order.index(latest_stage_name)
    except ValueError:
        return None

    # If we're not at the end, check if we should have advanced
    if idx < len(stages_in_order) - 1:
        next_stage = stages_in_order[idx + 1]
        # Timeout for the next stage (how long we wait for its event)
        timeout_s = STAGE_TIMEOUTS.get(next_stage, 1800)
        elapsed_s = now_ts - latest_stage_ts
        if elapsed_s > timeout_s:
            overdue_s = elapsed_s - timeout_s
            return next_stage, overdue_s

    return None


def format_stall_message(stage: str, overdue_s: float, timeout_s: int) -> tuple[str, str]:
    """Format a stall message as (title, text) given the stage and how long it's been stalled."""
    stage_names = {
        "collect": "2D data collection",
        "reduce": "Reduction (2D→1D)",
        "average": "Averaging",
        "subtract": "Background subtraction",
        "fit": "Auto-fit & analysis",
    }
    stage_label = stage_names.get(stage, stage)

    elapsed_min = int(overdue_s / 60)
    timeout_min = int(timeout_s / 60)

    title = f"Stalled — {stage_label}"
    text = (
        f"No update for {elapsed_min} minutes (timeout: {timeout_min}m)\n"
        f"Last event more than {timeout_min}m ago"
    )

    return title, text
