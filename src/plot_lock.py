"""src/plot_lock.py — one process-wide lock serialising all server-side
matplotlib (pyplot) use.

pyplot keeps GLOBAL state (the "current" figure/axes), so two threads building
figures at once corrupt each other's output or crash. In the analyzer process
that happens routinely: the watcher thread writes a per-fit PNG
(``_write_fit_record``) while a Flask request thread renders a campaign plot
(``src.optimizer.plots.figure``). Both must take this single lock, so import it
from HERE rather than defining a private lock per module — a per-module lock
would not serialise the two against each other.

RLock so a function already holding it can call another plotting helper that
also takes it without deadlocking.
"""
from __future__ import annotations

import threading

MPL_LOCK = threading.RLock()
