"""
src/runstate.py — small, durable state that must survive an app restart.

Motivation (platform audit O1/O2): every automation loop and the Bayesian
campaign lived only in process memory, so a 03:00 restart left the platform
looking correctly restored — the hub even remembered the project folder — while
doing nothing at all. Frames accumulated, nothing was processed, and every card
was green.

This is deliberately NOT the manifest. The manifest is the scientific record and
is already expensive to write (a full read-modify-write under an exclusive lock);
run-state is small, changes often, and must never contend with it. Files live in
``<project>/.swaxs_state/`` and are written with the same temp+rename discipline
so a crash mid-write cannot leave a half-file.

Resume is OFF by DEFAULT: every stop or hub restart brings an app up FRESH — no
monitor auto-resume, no restoring the processed/batch/subtraction memos, no reusing
the previous run's settings or the Bayesian campaign. Nothing from a previous run is
carried over. Opt back into the old resume-after-crash behavior with
``SWAXS_RESUME=1`` (and ``SWAXS_NO_RESUME=1`` still force-disables it, which now only
matters when SWAXS_RESUME is also set).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DIR = ".swaxs_state"
ENV_NO_RESUME = "SWAXS_NO_RESUME"
ENV_RESUME = "SWAXS_RESUME"

_TRUTHY = ("1", "true", "yes")


def resume_disabled() -> bool:
    """True when saved run-state must NOT be resumed.

    Resume is OFF by default (fresh start on every stop/restart). It is enabled
    ONLY when SWAXS_RESUME is truthy AND SWAXS_NO_RESUME is not — so an explicit
    NO_RESUME still wins as a hard override.
    """
    if str(os.environ.get(ENV_NO_RESUME, "")).strip().lower() in _TRUTHY:
        return True
    return str(os.environ.get(ENV_RESUME, "")).strip().lower() not in _TRUTHY


def state_dir(project_root: str | Path) -> Path | None:
    if not project_root:
        return None
    d = Path(project_root) / STATE_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        logger.warning("cannot create %s — run-state will not persist", d)
        return None


def state_path(project_root: str | Path, name: str) -> Path | None:
    d = state_dir(project_root)
    return (d / f"{name}.json") if d is not None else None


def save_state(project_root: str | Path, name: str, data: dict) -> bool:
    """Atomically write one small state file. Returns True on success.

    Never raises: losing run-state must not break the app that was trying to
    save it.
    """
    p = state_path(project_root, name)
    if p is None:
        return False
    payload = {**data, "_saved_at": time.time()}
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception as exc:
        logger.warning("could not save run-state %s: %s", name, exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def load_state(project_root: str | Path, name: str,
               max_age_s: float | None = None,
               *, honour_no_resume: bool = True) -> dict | None:
    """Read a state file. Returns None when absent, unreadable, resume is
    disabled, or the file is older than ``max_age_s``.

    The age guard matters: resuming a monitor from a state file written days ago
    (a different sample series) is worse than not resuming at all.

    ``honour_no_resume=False`` reads the file regardless of the resume setting.
    NOTE (Sept 2026): the platform now starts FRESH by default (see
    ``resume_disabled``), and that includes forgetting the passive processed/batch
    /subtraction memos — the operator asked that nothing from a previous run carry
    over. So all callers now use the default (``honour_no_resume=True``). This
    parameter is kept for callers that must read state unconditionally regardless
    of the resume policy.
    """
    if honour_no_resume and resume_disabled():
        logger.info("%s=1 — ignoring saved run-state '%s'", ENV_NO_RESUME, name)
        return None
    p = state_path(project_root, name)
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("run-state %s is unreadable (%s) — ignoring", name, exc)
        return None
    if not isinstance(data, dict):
        return None
    if max_age_s is not None:
        age = time.time() - float(data.get("_saved_at", 0) or 0)
        if age > max_age_s:
            logger.info("run-state '%s' is %.0f h old — not resuming",
                        name, age / 3600.0)
            return None
    return data


def clear_state(project_root: str | Path, name: str) -> None:
    p = state_path(project_root, name)
    try:
        if p is not None:
            p.unlink(missing_ok=True)
    except Exception:
        pass


# ── monitor helpers (used by every app with an auto-processing loop) ──────────
def save_monitor(project_root: str | Path, app: str, running: bool,
                 params: dict | None = None) -> None:
    """Record whether this app's processing loop was running, and with what
    settings, so it can be re-issued verbatim after a restart."""
    save_state(project_root, f"{app}_monitor",
               {"running": bool(running), "params": params or {}})


def load_monitor(project_root: str | Path, app: str,
                 max_age_h: float = 48.0) -> dict | None:
    """The saved start request, or None if it shouldn't be resumed."""
    st = load_state(project_root, f"{app}_monitor", max_age_s=max_age_h * 3600.0)
    if not st or not st.get("running"):
        return None
    return st.get("params") or {}


# ── monitor liveness ─────────────────────────────────────────────────────────
def monitor_alive(flag: bool, thread) -> bool:
    """Is a processing monitor ACTUALLY running?

    Four apps kept a bare `_monitoring = True` flag that only the worker loop
    itself ever cleared. If that thread died — an unhandled exception, a killed
    interpreter thread — the flag stayed True forever, so:

      * ``/api/monitor/status`` reported a healthy monitor,
      * the hub card stayed green,
      * and ``/api/monitor/start`` refused with "Already monitoring",

    which meant the app could not be recovered from the UI at all and nothing was
    processed for the rest of the night. Exactly the failure mode the restart-resume
    work exists to prevent, reached by a different route.

    The thread is the ground truth. Use this for BOTH the status payload and the
    already-running guard, so the two can never disagree.
    """
    return bool(flag and thread is not None and thread.is_alive())
