"""
src/reactor/intake.py — folder-watcher intake decision (pure, testable).

The reactor watches a folder for recipe files dropped by the ML/BO pipeline.
Those files may appear *while still being written*, so reading one immediately
can yield a truncated, unparseable file. The old watcher marked every file
"seen" before parsing, so a file caught mid-write was rejected once and then
never retried — the condition was silently lost.

``decide_intake`` makes the read/skip decision from file signatures only (no
I/O), so it is easy to unit-test and keeps the logic out of the Flask app:

  • Wait until a file is STABLE (size + mtime unchanged across two polls) before
    reading it, so partial writes are never parsed.
  • Only remember a file as "handled" once it has been ingested or genuinely
    rejected — and remember it *by signature*, so a corrected re-write of the
    same filename is picked up again.
"""

from __future__ import annotations

# Signatures are (size_bytes, mtime_ns) tuples.
Signature = tuple


def decide_intake(key: str, sig: Signature,
                  handled: dict[str, Signature],
                  last_seen: dict[str, Signature]) -> str:
    """Decide what to do with one folder file this poll. Pure — mutates nothing.

    ``handled``   maps path -> signature of the version already ingested/rejected.
    ``last_seen`` maps path -> signature observed on the *previous* poll.

    Returns one of:
      "skip" — already handled this exact version; do nothing.
      "wait" — signature changed since last poll (new or still being written);
               caller should record the signature and re-check next poll.
      "go"   — file is stable and not yet handled; caller should ingest it.
    """
    if handled.get(key) == sig:
        return "skip"
    if last_seen.get(key) != sig:
        return "wait"
    return "go"
