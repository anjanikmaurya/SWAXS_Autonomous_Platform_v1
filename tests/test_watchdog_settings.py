"""
Tests for src/watchdog/settings.py — config loading and validation.
"""

import tempfile
from pathlib import Path

import pytest

from src.watchdog.settings import (
    load_settings,
    save_notify_settings,
    ValidationError,
    _parse_time,
    _parse_quiet_hours,
)
from src.watchdog.policy import CATEGORIES


class TestParseTime:
    def test_valid_time(self):
        assert _parse_time("09:30") == (9, 30)
        assert _parse_time("23:59") == (23, 59)
        assert _parse_time("00:00") == (0, 0)

    def test_invalid_format(self):
        with pytest.raises(ValidationError, match="must be HH:MM"):
            _parse_time("9:30:00")
        with pytest.raises(ValidationError, match="must be HH:MM"):
            _parse_time("930")

    def test_invalid_values(self):
        with pytest.raises(ValidationError, match="H must be 0–23"):
            _parse_time("25:00")
        with pytest.raises(ValidationError, match="M must be 0–59"):
            _parse_time("12:60")

    def test_non_string(self):
        with pytest.raises(ValidationError, match="must be a string"):
            _parse_time(930)


class TestParseQuietHours:
    def test_valid_quiet_hours(self):
        result = _parse_quiet_hours({"quiet_hours": "23:00-07:00"})
        assert result == ((23, 0), (7, 0))

    def test_no_quiet_hours(self):
        assert _parse_quiet_hours({}) is None
        assert _parse_quiet_hours({"quiet_hours": ""}) is None
        assert _parse_quiet_hours({"quiet_hours": None}) is None

    def test_invalid_format(self):
        with pytest.raises(ValidationError, match="HH:MM-HH:MM"):
            _parse_quiet_hours({"quiet_hours": "23:00 to 07:00"})


class TestLoadSettings:
    def test_minimal_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("notify:\n  quiet_hours: '23:00-07:00'\n")
            f.flush()
            try:
                result = load_settings(f.name)
                assert result["quiet_hours"] == ((23, 0), (7, 0))
                assert result["summary"] == "off"
                assert result["snooze_default_min"] == 30
                assert result["min_interval_s"] == 3.0
            finally:
                Path(f.name).unlink()

    def test_full_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(
                "notify:\n"
                "  quiet_hours: '22:00-06:00'\n"
                "  summary: 'hourly'\n"
                "  snooze_default_min: 60\n"
                "  min_interval_s: 5.5\n"
            )
            f.flush()
            try:
                result = load_settings(f.name)
                assert result["quiet_hours"] == ((22, 0), (6, 0))
                assert result["summary"] == "hourly"
                assert result["snooze_default_min"] == 60
                assert result["min_interval_s"] == 5.5
            finally:
                Path(f.name).unlink()

    def test_missing_file(self):
        with pytest.raises(ValidationError, match="not found"):
            load_settings("/nonexistent/config.yml")

    def test_invalid_yaml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("notify:\n  - bad\n  - list\n  syntax: [broken")
            f.flush()
            try:
                with pytest.raises(ValidationError, match="failed to parse"):
                    load_settings(f.name)
            finally:
                Path(f.name).unlink()

    def test_invalid_summary(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("notify:\n  summary: 'daily'\n")
            f.flush()
            try:
                with pytest.raises(ValidationError, match="must be 'off' or 'hourly'"):
                    load_settings(f.name)
            finally:
                Path(f.name).unlink()

    def test_invalid_snooze_default(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("notify:\n  snooze_default_min: 0\n")
            f.flush()
            try:
                with pytest.raises(ValidationError, match=">= 1"):
                    load_settings(f.name)
            finally:
                Path(f.name).unlink()

    def test_invalid_min_interval(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("notify:\n  min_interval_s: -1\n")
            f.flush()
            try:
                with pytest.raises(ValidationError, match=">= 0"):
                    load_settings(f.name)
            finally:
                Path(f.name).unlink()

    def test_defaults_slack_enabled_and_all_categories_on(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("notify:\n  quiet_hours: null\n")
            f.flush()
            try:
                result = load_settings(f.name)
                assert result["slack_enabled"] is True
                assert result["categories"] == {c: True for c in CATEGORIES}
            finally:
                Path(f.name).unlink()

    def test_slack_disabled_and_partial_categories(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(
                "notify:\n"
                "  slack_enabled: false\n"
                "  categories:\n"
                "    stalls: false\n"
                "    results: false\n"
            )
            f.flush()
            try:
                result = load_settings(f.name)
                assert result["slack_enabled"] is False
                assert result["categories"] == {
                    "safety": True, "stalls": False, "results": False,
                    "progress": True, "campaign": True,
                }
            finally:
                Path(f.name).unlink()

    def test_safety_forced_on_while_slack_enabled(self):
        """The category filter can never silence safety while the master
        switch is on, no matter what the file says."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(
                "notify:\n"
                "  slack_enabled: true\n"
                "  categories:\n"
                "    safety: false\n"
            )
            f.flush()
            try:
                result = load_settings(f.name)
                assert result["categories"]["safety"] is True
            finally:
                Path(f.name).unlink()

    def test_safety_may_be_off_while_slack_disabled(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(
                "notify:\n"
                "  slack_enabled: false\n"
                "  categories:\n"
                "    safety: false\n"
            )
            f.flush()
            try:
                result = load_settings(f.name)
                assert result["categories"]["safety"] is False
            finally:
                Path(f.name).unlink()

    def test_invalid_categories_type(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("notify:\n  categories: 'nope'\n")
            f.flush()
            try:
                with pytest.raises(ValidationError, match="categories must be a dict"):
                    load_settings(f.name)
            finally:
                Path(f.name).unlink()

    def test_invalid_category_value_type(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("notify:\n  categories:\n    stalls: 'yes'\n")
            f.flush()
            try:
                with pytest.raises(ValidationError, match="categories.stalls must be a boolean"):
                    load_settings(f.name)
            finally:
                Path(f.name).unlink()


class TestSaveNotifySettings:
    def _write(self, tmp_path, text):
        p = tmp_path / "config.yml"
        p.write_text(text)
        return p

    def test_persists_master_switch(self, tmp_path):
        p = self._write(tmp_path, "notify:\n  quiet_hours: '23:00-07:00'\n")
        result = save_notify_settings(p, slack_enabled=False)
        assert result["slack_enabled"] is False
        reloaded = load_settings(p)
        assert reloaded["slack_enabled"] is False
        # quiet_hours (a key this call didn't touch) survives the rewrite.
        assert reloaded["quiet_hours"] == ((23, 0), (7, 0))

    @pytest.mark.parametrize("category", CATEGORIES)
    def test_persists_each_category_individually(self, tmp_path, category):
        p = self._write(tmp_path, "notify:\n  quiet_hours: null\n")
        result = save_notify_settings(p, categories={category: False})
        if category == "safety":
            # Exception: can't be turned off while sending stays on.
            assert result["categories"]["safety"] is True
        else:
            assert result["categories"][category] is False
        others = {c: v for c, v in result["categories"].items() if c != category}
        assert all(others.values())
        reloaded = load_settings(p)
        assert reloaded["categories"] == result["categories"]

    def test_all_shortcut(self, tmp_path):
        p = self._write(tmp_path, "notify:\n  quiet_hours: null\n")
        result = save_notify_settings(p, categories={c: True for c in CATEGORIES})
        assert result["categories"] == {c: True for c in CATEGORIES}

    def test_none_shortcut_still_forces_safety_while_enabled(self, tmp_path):
        p = self._write(tmp_path, "notify:\n  quiet_hours: null\n  slack_enabled: true\n")
        result = save_notify_settings(p, categories={c: False for c in CATEGORIES})
        assert result["categories"]["safety"] is True
        assert all(v is False for c, v in result["categories"].items() if c != "safety")

    def test_none_shortcut_all_off_when_slack_disabled(self, tmp_path):
        p = self._write(tmp_path, "notify:\n  quiet_hours: null\n  slack_enabled: false\n")
        result = save_notify_settings(p, categories={c: False for c in CATEGORIES})
        assert result["categories"] == {c: False for c in CATEGORIES}

    def test_repeated_saves_do_not_clobber_other_settings(self, tmp_path):
        p = self._write(
            tmp_path,
            "notify:\n"
            "  quiet_hours: '22:00-06:00'\n"
            "  summary: 'hourly'\n"
            "  snooze_default_min: 45\n"
            "  min_interval_s: 5\n",
        )
        save_notify_settings(p, slack_enabled=False)
        save_notify_settings(p, categories={"stalls": False})
        reloaded = load_settings(p)
        assert reloaded["slack_enabled"] is False
        assert reloaded["categories"]["stalls"] is False
        assert reloaded["quiet_hours"] == ((22, 0), (6, 0))
        assert reloaded["summary"] == "hourly"
        assert reloaded["snooze_default_min"] == 45
        assert reloaded["min_interval_s"] == 5.0
