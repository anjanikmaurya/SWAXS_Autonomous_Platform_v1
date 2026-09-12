"""
tests/test_reduction_csv_wait.py — src/reduction/csv_wait.py::decide_csv_wait.

Pure function, no I/O, no Flask app, no platform running — exactly the
"unit-tested with no platform running" requirement for the wait/go/skip
decision behind the reduction CSV-race fix (see reduction/app.py::_process_one_raw).
"""
from src.reduction.csv_wait import decide_csv_wait, QUIET_POLLS


def test_never_seen_is_a_failure():
    assert decide_csv_wait(None, now=1000.0, poll_interval_s=10.0) == "fail"


def test_recent_arrival_waits():
    # Arrived one poll interval ago — well inside the quiet window.
    assert decide_csv_wait(990.0, now=1000.0, poll_interval_s=10.0) == "wait"


def test_arrival_right_now_waits():
    assert decide_csv_wait(1000.0, now=1000.0, poll_interval_s=10.0) == "wait"


def test_arrival_at_the_quiet_boundary_still_waits():
    # Exactly QUIET_POLLS intervals ago — inclusive boundary.
    now = 1000.0
    last_seen = now - QUIET_POLLS * 10.0
    assert decide_csv_wait(last_seen, now, poll_interval_s=10.0) == "wait"


def test_arrival_just_past_the_quiet_boundary_fails():
    now = 1000.0
    last_seen = now - QUIET_POLLS * 10.0 - 0.001
    assert decide_csv_wait(last_seen, now, poll_interval_s=10.0) == "fail"


def test_long_quiet_prefix_fails():
    # No arrival in ten minutes at a 10s poll — clearly not still acquiring.
    assert decide_csv_wait(0.0, now=600.0, poll_interval_s=10.0) == "fail"


def test_custom_quiet_polls_is_respected():
    now = 1000.0
    last_seen = now - 5 * 10.0
    assert decide_csv_wait(last_seen, now, poll_interval_s=10.0, quiet_polls=3) == "fail"
    assert decide_csv_wait(last_seen, now, poll_interval_s=10.0, quiet_polls=6) == "wait"


def test_does_not_depend_on_frame_count_or_exposure_time():
    """The function's signature itself is the contract: only timestamps and the
    monitor's own poll interval go in — never a frame count or exposure time,
    which reduction cannot know and must not query the reactor for."""
    import inspect
    params = list(inspect.signature(decide_csv_wait).parameters)
    assert params == ["prefix_last_seen", "now", "poll_interval_s", "quiet_polls"]
