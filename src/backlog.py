"""
src/backlog.py — "has this already been done?" on a fresh operator start (pure).

The problem this exists to solve, stated once for all four monitor apps:

Pressing **Start** on a project folder that already holds data used to make the
monitors reprocess the whole folder. Reduce re-reduced, average re-averaged,
subtract re-subtracted and the analyzer re-fit every file of every previous
run, oldest first, before touching the frames of the run the operator had just
started. Each stage is a single worker thread, so the live frames sat behind
the entire back-catalogue and the loop appeared stalled for minutes to hours.

Each app forgot for its own reason:

* ``average``   — ``monitor_start`` set ``_avg_batch_state = {}`` unless the
  request carried ``resume=True``
* ``background``— ``monitor_start`` set ``_sub_done = {}`` on the same condition
* ``analyzer``  — cleared ``_handled`` on abort / folder change without reseeding
  (fixed separately in ``analyzer/app.py::_reseed_intake``)
* ``reduction`` — survived only because ``_already_reduced`` checks the disk

Those clears came from the N3 fix, which was about the *boot resume* wrongly
clearing state. "Fresh start" was then read as "forget everything", which is
not what an operator means by it: they mean *start a new run*, not *redo the
folder*.

THE RULE, applied identically everywhere:

1. An input whose **output already exists** is done. Nothing to redo.
2. An input with no output but **older than the crash-gap window** is history.
   A fresh run must not reprocess it. (Most commonly: it was processed before
   the current output-naming or record-keeping existed.)
3. An input with no output and **newer than the window** is left for normal
   processing — it may be a frame that landed just before the last crash and
   was genuinely never processed.

Pure and clock-injected, the same shape as ``src/reactor/intake.py`` and
``src/reduction/csv_wait.py``: the caller supplies the candidates, an
``has_output`` predicate and ``now``; this decides nothing about I/O itself.
"""
from __future__ import annotations

from typing import Callable, Iterable

#: How recent an input with no output has to be to count as a possible
#: crash-gap case rather than history. Monitors poll every few seconds, so a
#: few minutes comfortably covers "written just before the process died" while
#: still treating anything genuinely old as the back-catalogue it is.
CRASH_GAP_WINDOW_S = 600.0


def partition_backlog(
    candidates: Iterable[tuple],
    *,
    has_output: Callable[[object], bool],
    now: float,
    window_s: float = CRASH_GAP_WINDOW_S,
) -> tuple[list, list]:
    """Split inputs into (already_done, leave_for_normal_processing).

    ``candidates``: an iterable of ``(key, mtime)``. ``key`` is whatever the
        caller wants back — a path, a filename, a (detector, keyword) tuple —
        and is passed straight to ``has_output``.
    ``has_output``: True when this input's output is already on disk.
    ``now``: epoch seconds, passed in so this stays pure and testable.
    ``window_s``: the crash-gap window; see CRASH_GAP_WINDOW_S.

    Returns ``(done, todo)``, both lists of ``key``, preserving input order so
    a caller that needs chronological order keeps it.
    """
    done, todo = [], []
    for key, mtime in candidates:
        if has_output(key):
            done.append(key)
            continue
        try:
            age = now - float(mtime)
        except (TypeError, ValueError):
            age = 0.0
        (done if age >= window_s else todo).append(key)
    return done, todo


def describe(done: list, todo: list, what: str = "file") -> str:
    """One operator-facing log line. Says the number NOT redone, because that
    is the surprising part — silence about it is what made this look like a
    hang rather than a decision."""
    if not done:
        return ""
    plural = "" if len(done) == 1 else "s"
    tail = f"; {len(todo)} recent {what}(s) left to process" if todo else ""
    return (f"{len(done)} existing {what}{plural} already processed — skipping "
            f"(this is a new run, not a re-run of the folder){tail}")
