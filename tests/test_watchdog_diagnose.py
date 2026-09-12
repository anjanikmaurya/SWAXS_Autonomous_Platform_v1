"""
Tests for src/watchdog/diagnose.py — stall diagnostics.
"""

import pytest

from src.watchdog.diagnose import diagnose_stall


class TestDiagnoseStall:
    def test_averaging_stall_reduction_dead(self):
        # When "average" stage is overdue, the reduction app should have produced it
        probes = {
            "monitors": {"reduction": False},
            "analyzer": {},
            "reactor": {},
        }
        title, text = diagnose_stall("average", 3600, probes)
        assert "averaging" in title.lower()
        assert "not monitoring" in text

    def test_averaging_stall_average_alive(self):
        probes = {
            "monitors": {"average": True},
            "analyzer": {},
            "reactor": {},
        }
        title, text = diagnose_stall("average", 3600, probes)
        assert "averaging" in title.lower()
        assert "is running" in text

    def test_subtract_stall(self):
        probes = {
            "monitors": {"average": False},
            "analyzer": {},
            "reactor": {},
        }
        title, text = diagnose_stall("subtract", 1800, probes)
        assert "Stalled" in title

    def test_fit_stall(self):
        probes = {
            "monitors": {},
            "analyzer": {},
            "reactor": {"state": "idle"},
        }
        title, text = diagnose_stall("fit", 7200, probes)
        assert "Stalled" in title
        assert "auto-fit" in title.lower()
