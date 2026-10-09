"""
src/operator_id.py — who is running the beamline right now.

The operator's name / initials are entered ONCE, in the hub (October 2026; it
used to be a field in the reduction app only). The hub saves it and writes it
into the selected project's manifest as ``project_meta.current_operator``; it
also exports ``SWAXS_USER_ID`` to every app it launches. Any app that records
provenance resolves the operator through :func:`current_operator`, in this order:

    explicit value (e.g. an API caller)  →  the hub's current operator in the
    project manifest  →  SWAXS_USER_ID env  →  OS login  →  "unknown"

Reading the manifest (not just the env) means a change made in the hub reaches
apps that are already running, without restarting them.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

#: hub-side persistence of the last operator, so it survives a hub restart
OPERATOR_FILE = Path(__file__).resolve().parent.parent / "logs" / "hub_operator.txt"
MAX_LEN = 40


def clean(name: object) -> str:
    """Trim, collapse whitespace, cap length. Empty means 'not set'."""
    return " ".join(str(name or "").split())[:MAX_LEN]


def from_manifest(project_root: str | Path | None) -> str:
    if not project_root:
        return ""
    try:
        from src.manifest import load_manifest, manifest_path_for
        m = load_manifest(manifest_path_for(project_root))
        return clean((m.get("project_meta") or {}).get("current_operator"))
    except Exception:
        return ""


def current_operator(project_root: str | Path | None = None,
                     explicit: object = None) -> str:
    for cand in (clean(explicit), from_manifest(project_root),
                 clean(os.environ.get("SWAXS_USER_ID"))):
        if cand:
            return cand
    try:
        return clean(getpass.getuser()) or "unknown"
    except Exception:
        return "unknown"


def load_saved() -> str:
    try:
        return clean(OPERATOR_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""


def save(name: str) -> None:
    OPERATOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPERATOR_FILE.write_text(clean(name), encoding="utf-8")


def write_to_project(project_root: str | Path | None, name: str) -> None:
    """Record the hub's operator in the project manifest (append-only meta)."""
    if not project_root:
        return
    from src.manifest import update_manifest, set_project_meta
    update_manifest(project_root, lambda m: set_project_meta(m, current_operator=clean(name)))
