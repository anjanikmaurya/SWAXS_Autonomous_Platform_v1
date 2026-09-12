"""
Tests for src/watchdog/expectations.py — stall detection (pure, no I/O).
"""

from datetime import datetime, timezone, timedelta

import pytest

from src.watchdog.expectations import (
    which_stage_is_overdue,
    format_stall_message,
    STAGE_TIMEOUTS,
)


class TestWhichStageIsOverdue:
    def test_no_events(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert which_stage_is_overdue(now, []) is None

    def test_recent_run_start_no_stall(self):
        now = datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
        events = [
            {
                "type": "reactor.run_start",
                "timestamp": datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
            }
        ]
        result = which_stage_is_overdue(now, events)
        assert result is None

    def test_run_start_overdue_for_reduction(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        run_start = now - timedelta(hours=1, seconds=1)  # 1h 1s ago
        events = [
            {
                "type": "reactor.run_start",
                "timestamp": run_start.isoformat(),
            }
        ]
        result = which_stage_is_overdue(now, events)
        # "reduce" timeout is 3600s (1h), so we're overdue after 1h 1s
        assert result is not None
        stage, overdue_s = result
        assert stage == "reduce"
        assert overdue_s > 0

    def test_progression_through_stages(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t0 = now - timedelta(hours=1)
        t1 = now - timedelta(minutes=50)
        t2 = now - timedelta(minutes=40)

        events = [
            {"type": "reactor.run_start", "timestamp": t0.isoformat()},
            {"type": "file.reduced", "timestamp": t1.isoformat()},
            {"type": "file.averaged", "timestamp": t2.isoformat()},
        ]

        result = which_stage_is_overdue(now, events)
        # We're at 'average'. Next is 'subtract', timeout is 1800s (30min).
        # 40 minutes elapsed since file.averaged, so 10 minutes overdue.
        assert result is not None
        stage, overdue_s = result
        assert stage == "subtract"
        assert overdue_s >= 600  # at least 10 minutes overdue

    def test_at_final_stage(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t = now - timedelta(minutes=10)
        events = [
            {"type": "fit.complete", "timestamp": t.isoformat()}
        ]
        result = which_stage_is_overdue(now, events)
        # fit is the last stage, so no overdue
        assert result is None

    def test_old_events_ignored(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        old_run_start = now - timedelta(hours=10)  # too old (> 2h)
        events = [
            {"type": "reactor.run_start", "timestamp": old_run_start.isoformat()}
        ]
        result = which_stage_is_overdue(now, events, max_age_s=7200)
        assert result is None

    def test_invalid_timestamp_skipped(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        events = [
            {"type": "reactor.run_start", "timestamp": "not-a-timestamp"}
        ]
        result = which_stage_is_overdue(now, events)
        assert result is None

    def test_latest_event_determines_stage(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t_old = now - timedelta(hours=2)
        t_new = now - timedelta(minutes=50)

        events = [
            {"type": "reactor.run_start", "timestamp": t_old.isoformat()},
            {"type": "file.averaged", "timestamp": t_new.isoformat()},
        ]

        result = which_stage_is_overdue(now, events)
        # Should check 'subtract', not 'reduce'
        if result:
            stage, _ = result
            assert stage == "subtract"


class TestFormatStallMessage:
    def test_format_reduce_stall(self):
        title, text = format_stall_message("reduce", 900, 1800)
        assert "Reduction" in title
        assert "15 minutes" in text
        assert "30m" in text

    def test_format_fit_stall(self):
        title, text = format_stall_message("fit", 7200, 3600)
        assert "Auto-fit" in title
        assert "120 minutes" in text
