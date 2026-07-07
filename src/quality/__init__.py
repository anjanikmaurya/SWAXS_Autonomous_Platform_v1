"""
src/quality/ — AI-assisted quality grading of subtracted scattering profiles.

Public API (see core.py):
  grade_profile     — score one subtracted .dat (0–100 + verdict + flags + reasons)
  series_consensus  — per-sample best-frames recommendation + damage-onset
  DEFAULT_THRESHOLDS — tunable thresholds dict
"""

from .core import (
    grade_profile,
    score_metrics,
    DEFAULT_THRESHOLDS,
    thresholds_for,
    sample_key,
)

__all__ = [
    "grade_profile",
    "score_metrics",
    "DEFAULT_THRESHOLDS",
    "thresholds_for",
    "sample_key",
]
