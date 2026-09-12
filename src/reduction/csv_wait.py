"""
src/reduction/csv_wait.py — CSV-arrival wait decision (pure, testable).

The metadata CSV for an acquisition is written only when the acquisition
COMPLETES, but .raw frames land throughout it. A frame with no matching CSV
yet is not necessarily broken — the acquisition is very likely still running.
Failing it immediately (as reduction/app.py used to) burns its whole 3-strike
retry budget in the first 30 s of a run that can legitimately take minutes.

``decide_csv_wait`` makes the call from timestamps only: reduction cannot know
how many frames an acquisition has or how long its exposure is, and must not
ask the reactor. Instead it watches whether NEW .raw frames are still landing
for the same filename prefix — while they are, the acquisition (and its CSV)
is presumably still coming. Only once that prefix has gone quiet is a missing
CSV treated as a real failure.

Pure, no I/O, `now` passed in — same shape as src/reactor/intake.py::decide_intake.
"""
from __future__ import annotations

# An acquisition is "still in progress" for a prefix while a new .raw for that
# prefix arrived within this many poll intervals. >1 gives slack for an
# ordinary poll-cycle hiccup without letting a truly quiet prefix look active
# for long after it has actually finished (or died).
QUIET_POLLS = 3


def decide_csv_wait(prefix_last_seen: float | None, now: float,
                     poll_interval_s: float, quiet_polls: float = QUIET_POLLS) -> str:
    """Decide what a missing-CSV failure means for one frame this poll.

    ``prefix_last_seen``: epoch seconds a new .raw last appeared for this
        frame's filename prefix (the caller tracks this across polls, e.g.
        keyed by reduction/app.py's own ``_parse_raw_kw_idx`` keyword), or
        None if no arrival has ever been recorded for it.
    ``now``: current epoch seconds — passed in so this stays pure.
    ``poll_interval_s``: the monitor's own poll interval (never the
        acquisition's frame count or exposure time, which reduction has no
        way to know).
    ``quiet_polls``: how many poll intervals of silence make a prefix "quiet."

    Returns "wait" — the prefix looks active; do not count this as a failure,
    retry indefinitely. Or "fail" — the prefix has gone quiet (or was never
    seen at all); the missing CSV is a real failure, subject to the caller's
    own strike count.
    """
    if prefix_last_seen is None:
        return "fail"
    if now - prefix_last_seen <= quiet_polls * poll_interval_s:
        return "wait"
    return "fail"
