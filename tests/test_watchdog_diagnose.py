"""
Tests for src/watchdog/diagnose.py — stall diagnostics.

One fixture per known pattern (A-F), asserting the message names the matching
pattern and reports the numbers behind it. The LLM path (Layer 2) is mocked —
these tests never make a network call.
"""

from src.watchdog.diagnose import diagnose_stall, _tail_log

EMPTY_PROBES = {"monitors": {}, "status": {}, "analyzer": {}, "reactor": {}}


def _base_loop(**overrides):
    loop = {
        "recipe_id": "run3",
        "reactor": {"state": "idle", "detail": "no run", "recipe_id": "run3"},
        "reduce": {"state": "idle", "detail": "no frames yet", "frames": 0,
                   "detector": None, "lane": "sample", "recipe_id": "run3"},
        "average": {"have": 0, "expected": 10, "state": "idle",
                    "detail": "no frames yet", "lane": "sample",
                    "recipe_id": "run3", "ghost_gate": False},
        "subtract": {"state": "idle", "detail": "no averages yet",
                     "recipe_id": "", "have_background": False, "have_sample": False},
        "fit": {"state": "idle", "detail": "nothing to fit", "recipe_id": ""},
        "overdue": None,
        "gate_live": True,
    }
    loop.update(overrides)
    return loop


class TestPatternA:
    def test_gate_not_full_reactor_collecting_is_not_a_stall(self):
        loop = _base_loop(
            reactor={"state": "running", "detail": "collecting sample", "recipe_id": "run3"},
            average={"have": 7, "expected": 10, "state": "waiting", "detail": "7 / 10 frames",
                     "lane": "sample", "recipe_id": "run3", "ghost_gate": False},
        )
        title, text, is_stall = diagnose_stall("average", 3600, EMPTY_PROBES, loop)
        assert is_stall is False
        assert "pattern a" in title.lower()
        assert "run3" in text
        assert "7" in text and "10" in text
        assert "collecting" in text.lower()


class TestPatternB:
    def test_gate_not_full_reactor_idle_is_a_real_stall(self):
        loop = _base_loop(
            reactor={"state": "idle", "detail": "no run", "recipe_id": "run3"},
            reduce={"state": "running", "detail": "reduced 7 frames", "frames": 7,
                    "detector": "saxs", "lane": "sample", "recipe_id": "run3"},
            average={"have": 7, "expected": 10, "state": "waiting", "detail": "7 / 10 frames",
                     "lane": "sample", "recipe_id": "run3", "ghost_gate": False},
        )
        title, text, is_stall = diagnose_stall("average", 3600, EMPTY_PROBES, loop)
        assert is_stall is True
        assert "pattern b" in title.lower()
        assert "7" in text and "10" in text
        assert "run3" in text
        assert "5108" in text  # reactor port


class TestPatternC:
    def test_gate_not_full_reduction_dead(self):
        loop = _base_loop(
            reactor={"state": "idle", "detail": "no run", "recipe_id": "run3"},
            reduce={"state": "waiting", "detail": "reduced 4 frames", "frames": 4,
                    "detector": "saxs", "lane": "sample", "recipe_id": "run3"},
            average={"have": 4, "expected": 10, "state": "waiting", "detail": "4 / 10 frames",
                     "lane": "sample", "recipe_id": "run3", "ghost_gate": False},
        )
        title, text, is_stall = diagnose_stall("average", 3600, EMPTY_PROBES, loop)
        assert is_stall is True
        assert "pattern c" in title.lower()
        assert "4" in text and "10" in text
        assert "5102" in text  # reduction port


class TestPatternD:
    def test_ghost_gate_is_not_a_stall(self):
        loop = _base_loop(
            average={"have": 10, "expected": 10, "state": "done", "detail": "averaged",
                      "lane": "background", "recipe_id": "run3", "ghost_gate": True},
        )
        title, text, is_stall = diagnose_stall("average", 3600, EMPTY_PROBES, loop)
        assert is_stall is False
        assert "pattern d" in title.lower()
        assert "ghost" in text.lower()
        assert "_avg_pending" in text or "avg_pending" in text
        assert "run3" in text


class TestPatternE:
    def test_gate_full_no_average_written(self):
        loop = _base_loop(
            average={"have": 10, "expected": 10, "state": "stalled",
                      "detail": "no average written", "lane": "sample",
                      "recipe_id": "run3", "ghost_gate": False},
        )
        title, text, is_stall = diagnose_stall("average", 3600, EMPTY_PROBES, loop)
        assert is_stall is True
        assert "pattern e" in title.lower()
        assert "10" in text
        assert "run3" in text
        assert "5103" in text  # average app port — this is the case that needs the log


class TestPatternF:
    def test_both_lanes_averaged_no_subtracted_file(self):
        loop = _base_loop(
            subtract={"state": "stalled", "detail": "no subtracted file",
                      "recipe_id": "run3", "have_background": True, "have_sample": True},
        )
        title, text, is_stall = diagnose_stall("subtract", 1800, EMPTY_PROBES, loop)
        assert is_stall is True
        assert "pattern f" in title.lower()
        assert "run3" in text
        assert "5104" in text  # background app port


class TestPatternG:
    def test_reduction_permanently_skipped_frames_gate_not_full(self):
        loop = _base_loop(
            reduce={"state": "running", "detail": "reduced 4 frames", "frames": 4,
                    "detector": "saxs", "lane": "sample", "recipe_id": "run3",
                    "skipped": 6},
            average={"have": 4, "expected": 10, "state": "waiting", "detail": "4 / 10 frames",
                     "lane": "sample", "recipe_id": "run3", "ghost_gate": False},
        )
        title, text, is_stall = diagnose_stall("average", 3600, EMPTY_PROBES, loop)
        assert is_stall is True
        assert "pattern g" in title.lower()
        assert "6" in text
        assert "10" in text
        assert "run3" in text
        assert "5102" in text  # reduction app port

    def test_skipped_frames_but_gate_already_full_falls_through_to_e(self):
        # A full gate means the batch will complete regardless of any earlier
        # skips — G must not preempt E once expected == have.
        loop = _base_loop(
            reduce={"state": "running", "detail": "reduced 10 frames", "frames": 10,
                    "detector": "saxs", "lane": "sample", "recipe_id": "run3",
                    "skipped": 2},
            average={"have": 10, "expected": 10, "state": "stalled",
                     "detail": "no average written", "lane": "sample",
                     "recipe_id": "run3", "ghost_gate": False},
        )
        title, text, is_stall = diagnose_stall("average", 3600, EMPTY_PROBES, loop)
        assert is_stall is True
        assert "pattern e" in title.lower()

    def test_zero_skipped_does_not_match_g(self):
        loop = _base_loop(
            average={"have": 7, "expected": 10, "state": "waiting", "detail": "7 / 10 frames",
                     "lane": "sample", "recipe_id": "run3", "ghost_gate": False},
        )
        title, text, is_stall = diagnose_stall("average", 3600, EMPTY_PROBES, loop)
        assert "pattern g" not in title.lower()


class TestUnrecognised:
    def test_subtract_missing_one_lane_is_unrecognised(self):
        loop = _base_loop(
            subtract={"state": "waiting", "detail": "waiting for background average",
                      "recipe_id": "run3", "have_background": False, "have_sample": True},
        )
        title, text, is_stall = diagnose_stall("subtract", 1800, EMPTY_PROBES, loop)
        assert is_stall is True
        assert "unrecognised" in title.lower()
        assert "background" in text.lower()

    def test_reduce_stage_has_no_named_pattern(self):
        loop = _base_loop()
        title, text, is_stall = diagnose_stall("reduce", 3600, EMPTY_PROBES, loop)
        assert is_stall is True
        assert "unrecognised" in title.lower()

    def test_unknown_stage_falls_back(self):
        title, text, is_stall = diagnose_stall("nonsense", 60, EMPTY_PROBES, _base_loop())
        assert is_stall is True
        assert "Stalled" in title


class TestAIFallback:
    """Layer 2 only fires for the unrecognised case, and only when enabled.
    The LLM call is mocked — no network access in these tests."""

    def test_disabled_by_default_no_ai_paragraph(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "src.watchdog.diagnose._ai_log_reading",
            lambda app, stage, facts: called.append(1) or {"likely_cause": "x",
                                                             "evidence": "y", "suggested_check": "z"},
        )
        loop = _base_loop()
        title, text, is_stall = diagnose_stall("reduce", 3600, EMPTY_PROBES, loop)
        assert not called
        assert "AI reading of the log" not in text

    def test_enabled_appends_ai_paragraph(self, monkeypatch):
        monkeypatch.setattr(
            "src.watchdog.diagnose._ai_log_reading",
            lambda app, stage, facts: {"likely_cause": "reduction app crashed on a bad frame",
                                        "evidence": "Traceback in the last 5 lines",
                                        "suggested_check": "restart the reduction monitor"},
        )
        loop = _base_loop()
        title, text, is_stall = diagnose_stall(
            "reduce", 3600, EMPTY_PROBES, loop, ai_fallback_enabled=True,
        )
        assert is_stall is True
        assert "AI reading of the log:" in text
        assert "reduction app crashed on a bad frame" in text

    def test_enabled_but_llm_unavailable_is_a_silent_no_op(self, monkeypatch):
        monkeypatch.setattr(
            "src.watchdog.diagnose._ai_log_reading", lambda app, stage, facts: None,
        )
        loop = _base_loop()
        title, text, is_stall = diagnose_stall(
            "reduce", 3600, EMPTY_PROBES, loop, ai_fallback_enabled=True,
        )
        assert is_stall is True
        assert "AI reading of the log" not in text

    def test_ai_fallback_never_used_for_named_patterns(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "src.watchdog.diagnose._ai_log_reading",
            lambda app, stage, facts: called.append(1) or None,
        )
        loop = _base_loop(
            average={"have": 10, "expected": 10, "state": "done", "detail": "averaged",
                      "lane": "background", "recipe_id": "run3", "ghost_gate": True},
        )
        diagnose_stall("average", 3600, EMPTY_PROBES, loop, ai_fallback_enabled=True)
        assert not called

    def test_tail_log_missing_file_returns_empty_string(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.watchdog.diagnose._REPO_ROOT", tmp_path)
        assert _tail_log("reduction") == ""

    def test_tail_log_reads_last_n_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.watchdog.diagnose._REPO_ROOT", tmp_path)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "reduction.log").write_text("\n".join(f"line{i}" for i in range(100)))
        tail = _tail_log("reduction", n=5)
        assert tail.splitlines() == [f"line{i}" for i in range(95, 100)]
