"""
Tests for src/watchdog/policy.py — notification policy (pure, no I/O).
"""

from datetime import datetime, timezone, timedelta

import pytest

from src.watchdog.policy import (
    should_send, _in_quiet_hours, set_snooze, clear_snooze,
    category_for_event, CATEGORIES,
)


class TestInQuietHours:
    def test_within_quiet_hours(self):
        quiet_hours = ((23, 0), (7, 0))
        assert _in_quiet_hours(datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc), quiet_hours)
        assert _in_quiet_hours(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), quiet_hours)
        assert _in_quiet_hours(datetime(2026, 1, 1, 6, 59, tzinfo=timezone.utc), quiet_hours)

    def test_outside_quiet_hours(self):
        quiet_hours = ((23, 0), (7, 0))
        assert not _in_quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), quiet_hours)
        assert not _in_quiet_hours(datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc), quiet_hours)
        assert not _in_quiet_hours(datetime(2026, 1, 1, 22, 59, tzinfo=timezone.utc), quiet_hours)

    def test_no_overnight_wrap(self):
        quiet_hours = ((9, 0), (17, 0))
        assert _in_quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), quiet_hours)
        assert not _in_quiet_hours(datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc), quiet_hours)


class TestShouldSend:
    def test_fault_always_sends(self):
        now = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)  # in quiet hours
        cfg = {"quiet_hours": ((23, 0), (7, 0)), "summary": "off"}
        state = {"snooze_until": now.timestamp() + 3600}  # snoozed for an hour
        send, level = should_send("fault", now, cfg, state)
        assert send is True
        assert level == "fault"

    def test_progress_respects_quiet_hours(self):
        now = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)  # in quiet hours
        cfg = {"quiet_hours": ((23, 0), (7, 0)), "summary": "off"}
        state = {"snooze_until": None}
        send, level = should_send("progress", now, cfg, state)
        assert send is False

    def test_progress_sends_outside_quiet_hours(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"quiet_hours": ((23, 0), (7, 0)), "summary": "off"}
        state = {"snooze_until": None}
        send, level = should_send("progress", now, cfg, state)
        assert send is True
        assert level == "info"

    def test_progress_respects_snooze(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"quiet_hours": None, "summary": "off"}
        snooze_until = now.timestamp() + 3600
        state = {"snooze_until": snooze_until}
        send, level = should_send("progress", now, cfg, state)
        assert send is False

    def test_progress_sends_after_snooze(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"quiet_hours": None, "summary": "off"}
        snooze_until = now.timestamp() - 1  # expired
        state = {"snooze_until": snooze_until}
        send, level = should_send("progress", now, cfg, state)
        assert send is True

    def test_no_quiet_hours(self):
        now = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
        cfg = {"quiet_hours": None, "summary": "off"}
        state = {"snooze_until": None}
        send, level = should_send("progress", now, cfg, state)
        assert send is True

    def test_unknown_kind(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"quiet_hours": None, "summary": "off"}
        state = {"snooze_until": None}
        send, level = should_send("unknown", now, cfg, state)
        assert send is False


class TestCategoryForEvent:
    def test_known_events(self):
        assert category_for_event("reactor.estop") == "safety"
        assert category_for_event("reactor.safety") == "safety"
        assert category_for_event("reactor.run_start") == "progress"
        assert category_for_event("reactor.run_complete") == "progress"
        assert category_for_event("fit.complete") == "results"

    def test_unknown_event_defaults_to_progress(self):
        assert category_for_event("something.unheard_of") == "progress"


class TestMasterSwitch:
    def test_off_blocks_fault(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"slack_enabled": False}
        state = {"snooze_until": None}
        send, _ = should_send("fault", now, cfg, state, "safety")
        assert send is False

    def test_off_blocks_progress(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"slack_enabled": False}
        state = {"snooze_until": None}
        send, _ = should_send("progress", now, cfg, state, "progress")
        assert send is False

    def test_on_by_default_when_key_absent(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"quiet_hours": None}
        state = {"snooze_until": None}
        send, _ = should_send("fault", now, cfg, state, "safety")
        assert send is True


class TestCategoryFilter:
    """Every combination of the category on/off, crossed with the two kinds
    that actually carry a category (fault-tier "safety"/"stalls" messages and
    progress/info messages)."""

    @pytest.mark.parametrize("category", CATEGORIES)
    def test_category_on_allows_send(self, category):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"slack_enabled": True, "quiet_hours": None,
               "categories": {c: (c == category) for c in CATEGORIES}}
        state = {"snooze_until": None}
        send, _ = should_send("progress", now, cfg, state, category)
        assert send is True

    @pytest.mark.parametrize("category", [c for c in CATEGORIES if c != "safety"])
    def test_category_off_blocks_send(self, category):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"slack_enabled": True, "quiet_hours": None,
               "categories": {c: (c != category) for c in CATEGORIES}}
        state = {"snooze_until": None}
        send, _ = should_send("progress", now, cfg, state, category)
        assert send is False

    def test_safety_category_cannot_be_filtered_out(self):
        """The categories dict says safety=False, but should_send() must
        still deliver it while the master switch is on — the safety
        exception is enforced here, independent of what settings.py wrote."""
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"slack_enabled": True, "quiet_hours": None,
               "categories": {c: False for c in CATEGORIES}}
        state = {"snooze_until": None}
        send, level = should_send("fault", now, cfg, state, "safety")
        assert send is True
        assert level == "fault"

    def test_no_category_skips_filter(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"slack_enabled": True, "quiet_hours": None,
               "categories": {c: False for c in CATEGORIES}}
        state = {"snooze_until": None}
        send, _ = should_send("progress", now, cfg, state, None)
        assert send is True

    def test_missing_categories_key_defaults_all_on(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        cfg = {"slack_enabled": True, "quiet_hours": None}
        state = {"snooze_until": None}
        for category in CATEGORIES:
            send, _ = should_send("progress", now, cfg, state, category)
            assert send is True


class TestSetSnooze:
    def test_set_snooze(self):
        state = {"snooze_until": None}
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        set_snooze(state, 30, now)
        expected_ts = now.timestamp() + (30 * 60)
        assert state["snooze_until"] == expected_ts

    def test_clear_snooze(self):
        state = {"snooze_until": 1234567890.0}
        clear_snooze(state)
        assert state["snooze_until"] is None
