"""
src/manifest.py — Shared Experiment Manifest (v2)
==================================================
manifest.json is the single shared data contract between all SWAXS platform
apps.  Each app reads and writes only its own section; apps must never
overwrite other apps' keys.

Schema v2 top-level keys
─────────────────────────
  version        : str   — "2.0"
  project_root   : str   — absolute path to experiment root
  created_at     : str   — ISO timestamp of manifest creation
  updated_at     : str   — ISO timestamp of last update
  project_meta   : dict  — facility, beamline, users, beamtime_id
  files          : dict  — keyed by absolute file path (see below)
  analyses       : dict  — keyed by uuid4 (see below)
  background     : dict  — background subtraction records (see below)
  ai_memory      : dict  — AI corrections, summaries, quality flags
  events         : list  — rolling log of the last 100 bus events

files[path] schema
───────────────────
  path          : str
  stage         : "raw" | "reduced" | "averaged" | "subtracted" | "analysed"
  detector      : "saxs" | "waxs" | "combined"
  keyword       : str
  scan_idx      : int
  metadata      : dict          — float-valued instrument metadata
  provenance    : dict          — NEW v2: app, version, run_id, inputs, config
  status        : str           — NEW v2: "ok" | "stale" | "locked"
  notes         : str           — NEW v2: user free-text annotation
  quality_flags : list[str]     — NEW v2: AI + user quality flags

analyses[uuid] schema
──────────────────────
  id, type, file_path, params, results, created_at (v1)
  fit_range      : [q_min, q_max]   — NEW v2
  quality_score  : float | None     — NEW v2
  ai_assessment  : str              — NEW v2
  provenance     : dict             — NEW v2

background[path] schema
────────────────────────
  sample_path, bkg_path, scale, mode, created_at (v1)
  scale_method    : "auto"|"manual"|"concentration"  — NEW v2
  scale_confidence: float | None                      — NEW v2
  provenance      : dict                              — NEW v2

ai_memory schema
─────────────────
  corrections       : list  — {turn, original, corrected, ts}
  session_summaries : list  — {session_id, summary, ts}
  quality_flags     : dict  — {abs_path: [flag_str, ...]}
  user_context      : dict  — sample_type, expected_Rg, background, concentration

events[] schema
────────────────
  type        : str   — "file.reduced" | "file.averaged" | ...
  source_app  : str
  timestamp   : str   — ISO-8601
  data        : dict  — event payload
  ai_triggered: bool
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl  # POSIX advisory file locking
    _HAVE_FCNTL = True
except ImportError:          # pragma: no cover (non-POSIX, e.g. Windows)
    _HAVE_FCNTL = False

logger = logging.getLogger("swaxs_platform")

__all__ = [
    # ── Locating / loading ────────────────────────────────────────────────
    "find_manifest",
    "manifest_path_for",
    "load_manifest",
    "save_manifest",
    "get_or_create_manifest",
    "manifest_lock",
    "update_manifest",
    # ── Writing file entries ──────────────────────────────────────────────
    "add_file_entry",
    "add_analysis_entry",
    "add_background_entry",
    # ── v2: file-level mutations ──────────────────────────────────────────
    "update_file_status",
    "add_file_note",
    "add_quality_flag",
    "add_quality_entry",
    "add_reactor_run",
    # ── v2: events ────────────────────────────────────────────────────────
    "add_event",
    # ── v2: AI memory ─────────────────────────────────────────────────────
    "update_ai_memory",
    "add_ai_correction",
    # ── v2: project metadata ──────────────────────────────────────────────
    "set_project_meta",
    # ── v2: provenance helper ─────────────────────────────────────────────
    "make_provenance",
]

# ── Constants ─────────────────────────────────────────────────────────────────

MANIFEST_FILENAME = "manifest.json"
MANIFEST_VERSION  = "2.0"
_EVENTS_MAX       = 100   # rolling window for events[]


# ── Locating the manifest ─────────────────────────────────────────────────────

def find_manifest(start: str | Path) -> Path | None:
    """Walk *up* from ``start`` until manifest.json is found, or return None."""
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for candidate in [p, *p.parents]:
        m = candidate / MANIFEST_FILENAME
        if m.exists():
            return m
    return None


def manifest_path_for(project_root: str | Path) -> Path:
    """Return the expected manifest path for a given project root."""
    return Path(project_root).resolve() / MANIFEST_FILENAME


# ── Load / save ───────────────────────────────────────────────────────────────

def load_manifest(path: str | Path) -> dict:
    """
    Load manifest from *path*.
    Returns an empty v2 manifest dict if the file is absent.
    Old v1 manifests are migrated to v2 in-memory on load.
    """
    p = Path(path)
    # Tolerate being handed a project DIRECTORY instead of the manifest file:
    # resolve to <dir>/manifest.json. Prevents "Is a directory" errors that
    # would otherwise look like a corrupt/empty manifest.
    if p.is_dir():
        p = p / "manifest.json"
    if not p.exists():
        return _empty_manifest(p.parent)
    try:
        with p.open("r") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        # Corrupted manifest (e.g. concurrent-write damage → "Extra data").
        # First TRY TO SALVAGE: the most common damage is a valid JSON object
        # followed by trailing bytes (a shorter write left over older content),
        # which `raw_decode` can recover in full. Only if salvage fails do we
        # back up the bad file and start fresh, so processing can continue.
        salvaged = _salvage_manifest_text(p)
        stamp  = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = p.with_name(f"manifest.corrupt-{stamp}.json")
        try:
            # Keep a copy of the damaged file regardless, for inspection.
            import shutil
            shutil.copy2(p, backup)
        except Exception:
            backup = None
        if salvaged is not None:
            logger.warning(
                "[manifest] %s was corrupt (%s); RECOVERED the valid leading "
                "object (%d top-level keys). Damaged copy kept as %s.",
                p.name, exc, len(salvaged), backup.name if backup else "(none)")
            # Rewrite the file cleanly with the recovered content.
            try:
                save_manifest(salvaged, p)
            except Exception:
                pass
            return _migrate_to_v2(salvaged, p.parent)
        try:
            p.replace(backup) if backup else None
            logger.warning("[manifest] %s was corrupt (%s); unrecoverable, "
                           "backed up to %s and recreated.",
                           p.name, exc, backup.name if backup else "(none)")
        except Exception:
            logger.warning("[manifest] %s was corrupt (%s); recreating.", p.name, exc)
        return _empty_manifest(p.parent)
    if not isinstance(data, dict):
        logger.warning("[manifest] %s did not contain a JSON object; recreating.", p.name)
        return _empty_manifest(p.parent)
    return _migrate_to_v2(data, p.parent)


def _salvage_manifest_text(path: Path) -> dict | None:
    """
    Attempt to recover a manifest from a damaged file. Returns the recovered
    dict, or None if nothing usable could be parsed.

    Strategy: read the raw text and use ``json.JSONDecoder().raw_decode`` to
    parse the first complete JSON value, ignoring any trailing garbage (the
    signature of "Extra data" concurrent-write corruption).
    """
    try:
        text = path.read_text().lstrip()
    except Exception:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) and obj else None


def save_manifest(manifest: dict, path: str | Path) -> None:
    """
    Atomically write *manifest* to *path* (write to a unique tmp then rename).

    The tmp filename includes the PID + a random suffix so that concurrent
    writers (hub, reduction, assistant) never share a temp file — sharing one
    was the source of "Extra data" corruption. ``replace`` is atomic on POSIX,
    so readers always see a complete file.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = _now()
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w") as fh:
            json.dump(manifest, fh, indent=2)
        tmp.replace(p)   # atomic on POSIX
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


# ── Convenience helpers ───────────────────────────────────────────────────────

def get_or_create_manifest(project_root: str | Path) -> tuple[dict, Path]:
    """
    Load the manifest if it exists, otherwise create a fresh v2 manifest.
    Returns (manifest_dict, manifest_path).
    """
    root  = Path(project_root).resolve()
    mpath = manifest_path_for(root)
    m     = load_manifest(mpath)   # load_manifest handles missing file gracefully
    return m, mpath


# ── Concurrency-safe updates ────────────────────────────────────────────────────

@contextlib.contextmanager
def manifest_lock(project_root: str | Path):
    """
    Hold an exclusive, cross-process lock for a project's manifest.

    All apps (hub, reduction, viewer, background, analysis, assistant) that
    mutate ``manifest.json`` should do so inside this lock (use
    :func:`update_manifest`). On platforms without ``fcntl`` the lock degrades
    to a no-op (single-process safety only).
    """
    root = Path(project_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".manifest.lock"
    fh = open(lock_path, "w")
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if _HAVE_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def update_manifest(project_root: str | Path, mutator: Callable[[dict], Any]) -> Any:
    """
    Atomically apply ``mutator`` to the project's manifest across processes:
    **lock → load → mutate → save → unlock**. This prevents the lost-update
    races that occur when several apps read-modify-write the manifest at once.

    Parameters
    ----------
    project_root : str | Path
        Experiment root containing (or to contain) ``manifest.json``.
    mutator : callable(manifest_dict) -> Any
        Receives the loaded manifest and mutates it in place. Any value it
        returns is passed back to the caller (e.g. a new analysis id).

    Returns whatever ``mutator`` returns.
    """
    with manifest_lock(project_root):
        m, mpath = get_or_create_manifest(project_root)
        result = mutator(m)
        save_manifest(m, mpath)
        return result


# ── Writing file entries ──────────────────────────────────────────────────────

def add_file_entry(
    manifest: dict,
    *,
    path:          str | Path,
    stage:         str,
    detector:      str,
    keyword:       str,
    scan_idx:      int = 0,
    metadata:      dict[str, Any] | None = None,
    # ── v2 additions (all optional — backwards-compatible) ────────────────
    provenance:    dict[str, Any] | None = None,
    status:        str = "ok",
    notes:         str = "",
    quality_flags: list[str] | None = None,
) -> None:
    """
    Upsert a file record into manifest["files"].

    ``stage``    — one of: raw | reduced | averaged | subtracted | analysed
    ``detector`` — one of: saxs | waxs | combined
    ``provenance`` — build with :func:`make_provenance` for full audit trail
    ``status``   — "ok" | "stale" | "locked"
    """
    key = str(Path(path).resolve())
    manifest.setdefault("files", {})[key] = {
        "path":          key,
        "stage":         stage,
        "detector":      detector,
        "keyword":       keyword,
        "scan_idx":      int(scan_idx),
        "metadata":      metadata      or {},
        "provenance":    provenance    or {},
        "status":        status,
        "notes":         notes,
        "quality_flags": quality_flags or [],
    }


def add_analysis_entry(
    manifest: dict,
    *,
    analysis_type:  str,
    file_path:      str | Path,
    params:         dict[str, Any],
    results:        dict[str, Any],
    # ── v2 additions ──────────────────────────────────────────────────────
    fit_range:      list[float] | None = None,
    quality_score:  float | None = None,
    ai_assessment:  str = "",
    provenance:     dict[str, Any] | None = None,
) -> str:
    """
    Append an analysis record to manifest["analyses"].
    Returns the new analysis ID (uuid4).
    """
    aid = str(uuid.uuid4())
    manifest.setdefault("analyses", {})[aid] = {
        "id":            aid,
        "type":          analysis_type,
        "file_path":     str(Path(file_path).resolve()),
        "params":        params,
        "results":       results,
        "fit_range":     fit_range     or [],
        "quality_score": quality_score,
        "ai_assessment": ai_assessment,
        "provenance":    provenance    or {},
        "created_at":    _now(),
    }
    return aid


def add_background_entry(
    manifest: dict,
    *,
    output_path:      str | Path,
    sample_path:      str | Path,
    bkg_path:         str | Path,
    scale:            float,
    mode:             str,
    # ── v2 additions ──────────────────────────────────────────────────────
    scale_method:     str = "manual",
    scale_confidence: float | None = None,
    provenance:       dict[str, Any] | None = None,
) -> None:
    """
    Record a background subtraction operation in manifest["background"].

    ``mode``         — "keyword" | "scan_matched" | "user_defined"
    ``scale_method`` — "auto" | "manual" | "concentration"
    """
    key = str(Path(output_path).resolve())
    manifest.setdefault("background", {})[key] = {
        "sample_path":      str(Path(sample_path).resolve()),
        "bkg_path":         str(Path(bkg_path).resolve()),
        "scale":            float(scale),
        "scale_method":     scale_method,
        "scale_confidence": scale_confidence,
        "mode":             mode,
        "provenance":       provenance or {},
        "created_at":       _now(),
    }


# ── v2: File-level mutations ──────────────────────────────────────────────────

def update_file_status(
    manifest: dict,
    path: str | Path,
    status: str,
) -> bool:
    """
    Set the status of a file entry.
    ``status`` must be one of "ok", "stale", or "locked".
    Returns True if the entry existed, False otherwise.
    """
    key = str(Path(path).resolve())
    entry = manifest.get("files", {}).get(key)
    if entry is None:
        return False
    entry["status"] = status
    return True


def add_file_note(
    manifest: dict,
    path: str | Path,
    note: str,
    *,
    append: bool = True,
) -> bool:
    """
    Add a user note to a file entry.
    If ``append`` is True (default), the note is appended to any existing
    note separated by a newline.  If False the note replaces any existing one.
    Returns True if the entry existed, False otherwise.
    """
    key = str(Path(path).resolve())
    entry = manifest.get("files", {}).get(key)
    if entry is None:
        return False
    if append and entry.get("notes"):
        entry["notes"] = entry["notes"].rstrip() + "\n" + note
    else:
        entry["notes"] = note
    return True


def add_quality_flag(
    manifest: dict,
    path: str | Path,
    flag: str,
    *,
    source: str = "user",
) -> bool:
    """
    Append a quality flag to a file entry and to ai_memory["quality_flags"].

    ``flag``   — e.g. "possible_aggregation", "radiation_damage", "poor_snr"
    ``source`` — "user" | "ai"
    Returns True if the file entry existed.
    """
    key        = str(Path(path).resolve())
    entry      = manifest.get("files", {}).get(key)
    entry_found = entry is not None
    if entry_found and flag not in entry.get("quality_flags", []):
        entry.setdefault("quality_flags", []).append(flag)

    # Mirror in ai_memory for the AI subsystem
    ai_flags = manifest.setdefault("ai_memory", {}).setdefault("quality_flags", {})
    if flag not in ai_flags.get(key, []):
        ai_flags.setdefault(key, []).append(flag)

    return entry_found


def add_quality_entry(
    manifest: dict,
    *,
    path:       str | Path,
    score:      float,
    verdict:    str,
    flags:      list[str] | None = None,
    metrics:    dict | None = None,
    reasons:    list[str] | None = None,
    detector:   str | None = None,
    sample:     str | None = None,
    source:     str = "ai",
    llm_note:   str | None = None,
    overridden: bool = False,
    override_note: str | None = None,
    analysis_ready: bool | None = None,
    provenance: dict[str, Any] | None = None,
) -> None:
    """
    Record a Quality Gate verdict for a subtracted profile.

    Writes a full record to ``manifest["quality"][abs_path]`` AND mirrors the
    summary onto the file entry (``quality_score`` + ``quality_flags``) and into
    ``ai_memory["quality_flags"]`` so the Assistant and downstream apps can read
    it.  ``verdict`` is "good" | "bad"; ``source`` is "ai" | "user".
    """
    key = str(Path(path).resolve())
    if analysis_ready is None:
        analysis_ready = (verdict == "good")
    manifest.setdefault("quality", {})[key] = {
        "score":          float(score),
        "verdict":        verdict,
        "flags":          list(flags or []),
        "metrics":        metrics or {},
        "reasons":        list(reasons or []),
        "detector":       detector,
        "sample":         sample,
        "source":         source,
        "llm_note":       llm_note,
        "overridden":     bool(overridden),
        "override_note":  override_note,
        "analysis_ready": bool(analysis_ready),
        "provenance":     provenance or {},
        "created_at":     _now(),
    }

    # Mark the file entry analysis-ready so the Analysis app can filter on it.
    fentry = manifest.get("files", {}).get(key)
    if fentry is not None:
        fentry["analysis_ready"] = bool(analysis_ready)

    # Mirror summary onto the file entry, when present.
    entry = manifest.get("files", {}).get(key)
    if entry is not None:
        entry["quality_score"] = float(score)
        existing = entry.setdefault("quality_flags", [])
        for f in (flags or []):
            if f not in existing:
                existing.append(f)

    # Mirror flags into ai_memory for the AI subsystem.
    ai_flags = manifest.setdefault("ai_memory", {}).setdefault("quality_flags", {})
    cur = ai_flags.setdefault(key, [])
    for f in (flags or []):
        if f not in cur:
            cur.append(f)


def add_reactor_run(manifest: dict, *, record: dict) -> None:
    """Record a Flow Synthesis reactor run in manifest["reactor"]["runs"].

    ``record`` is the controller's run record (recipe_id, recipe, setpoints,
    started/ended, duration_s, reason, status).
    """
    rid = record.get("recipe_id") or _now()
    runs = manifest.setdefault("reactor", {}).setdefault("runs", {})
    runs[str(rid)] = {**record, "logged_at": _now()}


# ── v2: Events ────────────────────────────────────────────────────────────────

def add_event(
    manifest: dict,
    event_type: str,
    source_app: str,
    data: dict,
    *,
    ai_triggered: bool = False,
) -> None:
    """
    Append an event to the rolling events log (last :data:`_EVENTS_MAX` entries).
    Called by the Hub's event broker whenever a bus message is received.
    """
    event = {
        "type":         event_type,
        "source_app":   source_app,
        "timestamp":    _now(),
        "data":         data,
        "ai_triggered": ai_triggered,
    }
    events = manifest.setdefault("events", [])
    events.append(event)
    if len(events) > _EVENTS_MAX:
        manifest["events"] = events[-_EVENTS_MAX:]


# ── v2: AI memory ─────────────────────────────────────────────────────────────

def update_ai_memory(manifest: dict, **kwargs: Any) -> None:
    """
    Merge ``kwargs`` into manifest["ai_memory"]["user_context"].

    Example::

        update_ai_memory(manifest,
                         sample_type="protein",
                         expected_Rg_nm=3.5,
                         background="20 mM HEPES pH 7.4")
    """
    manifest.setdefault("ai_memory", {}).setdefault("user_context", {}).update(kwargs)


def add_ai_correction(
    manifest: dict,
    *,
    turn: int,
    original: str,
    corrected: str,
) -> None:
    """
    Record a user correction to an AI response.
    These are used by the AI memory layer to improve future answers.
    """
    record = {
        "turn":      turn,
        "original":  original,
        "corrected": corrected,
        "ts":        _now(),
    }
    manifest.setdefault("ai_memory", {}).setdefault("corrections", []).append(record)


# ── v2: Project metadata ──────────────────────────────────────────────────────

def set_project_meta(manifest: dict, **kwargs: Any) -> None:
    """
    Set or update top-level project metadata.

    Example::

        set_project_meta(manifest,
                         facility="SSRL",
                         beamline="1-5",
                         users=["albert"],
                         beamtime_id="bt-2026-01")
    """
    manifest.setdefault("project_meta", {}).update(kwargs)


# ── v2: Provenance helper ─────────────────────────────────────────────────────

def make_provenance(
    app: str,
    *,
    app_version: str = MANIFEST_VERSION,
    input_files: list[str | Path] | None = None,
    config: dict[str, Any] | None = None,
    run_id: str | None = None,
    user: str = "",
) -> dict:
    """
    Build a provenance dict suitable for passing to :func:`add_file_entry`,
    :func:`add_analysis_entry`, or :func:`add_background_entry`.

    Parameters
    ----------
    app : str
        The app that produced the output (e.g. "reduction").
    app_version : str
        Version string of the app.
    input_files : list
        Absolute paths of all input files.
    config : dict
        The configuration dict used (will be hashed and stored in full).
    run_id : str | None
        Optional explicit run ID (uuid4 generated automatically if omitted).

    Example
    -------
    ::

        prov = make_provenance(
            "reduction",
            input_files=[raw_path],
            config={"npt_radial": 1000, "error_model": "poisson"},
        )
        add_file_entry(manifest, ..., provenance=prov)
    """
    cfg         = config or {}
    cfg_str     = json.dumps(cfg, sort_keys=True, default=str)
    cfg_hash    = "sha256:" + hashlib.sha256(cfg_str.encode()).hexdigest()[:16]

    return {
        "app":              app,
        "app_version":      app_version,
        "run_id":           run_id or str(uuid.uuid4()),
        "timestamp":        _now(),
        "user":             user,
        "input_files":      [str(Path(f).resolve()) for f in (input_files or [])],
        "config_hash":      cfg_hash,
        "config_snapshot":  cfg,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_manifest(project_root: Path) -> dict:
    now = _now()
    return {
        "version":      MANIFEST_VERSION,
        "project_root": str(project_root),
        "created_at":   now,
        "updated_at":   now,
        "project_meta": {},
        "files":        {},
        "analyses":     {},
        "background":   {},
        "ai_memory": {
            "corrections":       [],
            "session_summaries": [],
            "quality_flags":     {},
            "user_context":      {},
        },
        "events": [],
    }


def _migrate_to_v2(data: dict, project_root: Path) -> dict:
    """
    Migrate a v1 manifest to v2 in-memory (non-destructive).
    Adds any missing v2 sections without touching existing data.
    """
    if data.get("version") == MANIFEST_VERSION:
        return data   # already v2 — nothing to do

    # Bump version
    data["version"] = MANIFEST_VERSION

    # Add missing top-level sections
    data.setdefault("project_meta", {})
    data.setdefault("ai_memory", {
        "corrections":       [],
        "session_summaries": [],
        "quality_flags":     {},
        "user_context":      {},
    })
    data.setdefault("events", [])

    # Migrate individual file entries — add missing v2 fields
    for entry in data.get("files", {}).values():
        entry.setdefault("provenance",    {})
        entry.setdefault("status",        "ok")
        entry.setdefault("notes",         "")
        entry.setdefault("quality_flags", [])

    # Migrate analysis entries
    for entry in data.get("analyses", {}).values():
        entry.setdefault("fit_range",     [])
        entry.setdefault("quality_score", None)
        entry.setdefault("ai_assessment", "")
        entry.setdefault("provenance",    {})

    # Migrate background entries
    for entry in data.get("background", {}).values():
        entry.setdefault("scale_method",     "manual")
        entry.setdefault("scale_confidence", None)
        entry.setdefault("provenance",       {})

    return data
