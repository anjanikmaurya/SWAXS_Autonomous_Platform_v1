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
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import manifest_store

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
    # ── SQLite backend: fast indexed search ───────────────────────────────
    "query_files",
    "manifest_runs",
    "manifest_counts",
    "merge_manifest_file",
    "merge_manifest_sources",
    "ManifestUnreadableError",
]

# Storage backend. "sqlite" (default) persists to manifest.db — transactional,
# searchable, and safe against the single-file truncation that could reset the
# old manifest.json. "json" restores the legacy single-file behaviour. The JSON
# file is still written either way, so nothing downstream that reads it breaks.
_BACKEND = os.environ.get("SWAXS_MANIFEST_BACKEND", "sqlite").strip().lower()
#: Routine snapshots kept (rotated). Preshrink safety snapshots have their OWN
#: name pattern and rotation, so routine churn can never push them out.
_SNAPSHOTS_KEEP = int(os.environ.get("SWAXS_MANIFEST_SNAPSHOTS", "20"))
_PRESHRINK_KEEP = int(os.environ.get("SWAXS_MANIFEST_PRESHRINK_SNAPSHOTS", "20"))
#: Minimum seconds between routine snapshots (one is always taken on the first
#: save of each process). Reduction saves once per frame, so snapshotting every
#: save would both cost a full DB copy per frame and rotate the window to minutes.
_SNAPSHOT_INTERVAL_S = float(os.environ.get("SWAXS_MANIFEST_SNAPSHOT_INTERVAL_S", "600"))
_SNAPSHOT_DIRNAME = ".manifest_snapshots"
#: db path -> monotonic time of this process's last routine snapshot
_last_snapshot: dict[str, float] = {}


class ManifestUnreadableError(RuntimeError):
    """Prior manifest data exists but cannot be read or recovered. Raised instead
    of silently starting an empty store over it (history must never be erased)."""

# ── Constants ─────────────────────────────────────────────────────────────────

MANIFEST_FILENAME = "manifest.json"
MANIFEST_VERSION  = "2.0"
_EVENTS_MAX       = 100   # rolling window for events[]
#: Cap on the analyses{} section (see add_analysis_entry). Keeps the manifest
#: small and writes fast over a multi-week live loop; the durable per-fit trail
#: lives in Results/Fit/ regardless. 0 disables. Env-overridable.
_ANALYSES_CAP     = int(os.environ.get("SWAXS_MANIFEST_ANALYSES_CAP", 3000))


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
    root = p.parent

    # SQLite backend: the DB is the source of truth. If it exists, read it. If it
    # does not but a legacy manifest.json does, migrate the JSON into a new DB
    # once, then read the DB. A missing/truncated JSON can no longer wipe history,
    # because the DB survives it.
    if _BACKEND == "sqlite":
        return _load_sqlite(p, root)

    # Legacy JSON backend.
    if not p.exists():
        return _empty_manifest(p.parent)
    return _read_json_manifest(p)


def _load_sqlite(p: Path, root: Path) -> dict:
    """SQLite-backend load. Never returns an empty store when prior data exists:

    1. a readable, non-empty manifest.db is the source of truth;
    2. a missing / zero-byte / unreadable DB is restored from the newest valid
       snapshot, then the (usually newer) manifest.json export is merged on top;
    3. with no usable snapshot, a readable manifest.json is migrated into a new DB;
    4. if manifest.json exists but cannot be read or salvaged, raise
       :class:`ManifestUnreadableError` rather than create an empty DB over it;
    5. only when nothing existed at all is a fresh empty manifest returned.
    """
    dbp = manifest_store.db_path_for(root)
    if dbp.exists() and dbp.stat().st_size > 0:
        try:
            return manifest_store.load_dict(dbp)
        except sqlite3.DatabaseError as exc:
            logger.error("[manifest] %s is unreadable (%s); moving it aside and "
                         "restoring from snapshot", dbp.name, exc)
            _move_db_aside(dbp, "unreadable")
    elif dbp.exists():
        logger.error("[manifest] %s is zero bytes (truncated externally?); "
                     "moving it aside and restoring from snapshot", dbp.name)
        _move_db_aside(dbp, "empty")

    json_data, json_err = (_try_read_json(p) if p.exists() else (None, None))

    snap = _restore_from_snapshot(dbp)
    if snap is not None:
        if json_data:
            # The JSON export is written after every save, so it is normally
            # newer than the snapshot: fold it in, newest-wins, nothing deleted.
            # (No lock taken here: load may already run inside manifest_lock.)
            live = manifest_store.load_dict(dbp)
            try:
                _merge_into(live, [(p.name, _source_ts(json_data, p), json_data)])
                manifest_store.save_dict(dbp, live, events_max=_EVENTS_MAX)
            except Exception as exc:               # pragma: no cover
                logger.warning("[manifest] merging %s after restore failed: %s",
                               p.name, exc)
            return live
        return manifest_store.load_dict(dbp)

    if p.exists():
        if json_data is None:
            backup = _keep_corrupt_copy(p)
            msg = (f"{p} exists but could not be read or salvaged ({json_err}); "
                   f"no manifest.db or snapshot to recover from. Refusing to start "
                   f"an empty manifest over it. Damaged copy: "
                   f"{backup.name if backup else '(copy failed)'}. Repair the file, "
                   f"or move it aside to start fresh deliberately.")
            logger.error("[manifest] %s", msg)
            raise ManifestUnreadableError(msg)
        data = _migrate_to_v2(json_data, root)
        manifest_store.save_dict(dbp, data, events_max=_EVENTS_MAX)
        logger.info("[manifest] migrated %s → %s (%d files)",
                    p.name, dbp.name, len(data.get("files", {})))
        return manifest_store.load_dict(dbp)
    return _empty_manifest(root)


def _try_read_json(p: Path) -> tuple[dict | None, Exception | None]:
    """Read a manifest JSON WITHOUT touching the file. Returns (dict, None),
    salvaging a valid leading object from "Extra data" damage, or (None, error)."""
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data, None
        return None, ValueError("not a JSON object")
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        salvaged = _salvage_manifest_text(p)
        if salvaged is not None:
            logger.warning("[manifest] %s was damaged (%s); salvaged the valid "
                           "leading object", p.name, exc)
            return salvaged, None
        return None, exc


def _keep_corrupt_copy(p: Path) -> Path | None:
    """Copy a damaged manifest.json to manifest.corrupt-<ts>.json (once per
    distinct content), leaving the original in place."""
    try:
        blob = p.read_bytes()
        for old in p.parent.glob("manifest.corrupt-*.json"):
            try:
                if old.stat().st_size == len(blob) and old.read_bytes() == blob:
                    return old
            except OSError:
                continue
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = p.with_name(f"manifest.corrupt-{stamp}.json")
        shutil.copy2(p, backup)
        return backup
    except Exception:
        return None


def _move_db_aside(dbp: Path, why: str) -> None:
    """Rename a bad manifest.db (plus its -wal/-shm) out of the way; never delete."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    for suffix in ("", "-wal", "-shm"):
        src = dbp.with_name(dbp.name + suffix)
        if src.exists():
            try:
                src.replace(dbp.with_name(f"manifest.{why}-{stamp}.db{suffix}"))
            except OSError as exc:                 # pragma: no cover
                logger.error("[manifest] could not move %s aside: %s", src.name, exc)


_SNAP_STAMP_RE = re.compile(r"(\d{8}-\d{6})(?:-(\d{6}))?")


def _snapshot_sort_key(path: Path) -> str:
    m = _SNAP_STAMP_RE.search(path.name)
    return (m.group(1) + (m.group(2) or "000000")) if m else ""


def _list_snapshots(snapdir: Path) -> tuple[list[Path], list[Path]]:
    """(routine, preshrink) snapshot lists, each oldest first. Recognises the
    legacy ``manifest-<ts>-preshrink.db`` name as preshrink too."""
    if not snapdir.is_dir():
        return [], []
    routine, pre = [], []
    for f in snapdir.glob("*.db"):
        if f.name.startswith("preshrink-") or f.name.endswith("-preshrink.db"):
            pre.append(f)
        elif f.name.startswith("manifest-"):
            routine.append(f)
    return (sorted(routine, key=_snapshot_sort_key),
            sorted(pre, key=_snapshot_sort_key))


def _restore_from_snapshot(dbp: Path) -> Path | None:
    """Restore manifest.db from the newest snapshot that opens cleanly. Returns
    the snapshot used, or None if there was none."""
    routine, pre = _list_snapshots(dbp.parent / _SNAPSHOT_DIRNAME)
    for snap in sorted(routine + pre, key=_snapshot_sort_key, reverse=True):
        try:
            src = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
            try:
                src.execute("SELECT COUNT(*) FROM files").fetchone()
                for suffix in ("-wal", "-shm"):   # stale sidecars of a lost DB
                    side = dbp.with_name(dbp.name + suffix)
                    if side.exists():
                        side.replace(dbp.with_name(
                            f"manifest.orphan-{datetime.now(timezone.utc):%Y%m%d-%H%M%S-%f}"
                            f".db{suffix}"))
                dst = sqlite3.connect(str(dbp))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.DatabaseError as exc:
            logger.warning("[manifest] snapshot %s unusable (%s); trying older",
                           snap.name, exc)
            continue
        logger.error("[manifest] manifest.db was missing/unreadable; RESTORED from "
                     "snapshot %s", snap.name)
        return snap
    return None


def _read_json_manifest(p: Path) -> dict:
    """Read and migrate a manifest.json, salvaging a corrupt file if possible."""
    try:
        with p.open("r", encoding="utf-8") as fh:
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
            # Rewrite the file cleanly with the recovered content. Plain JSON
            # write only: save_manifest would, on the SQLite backend, overwrite
            # the live DB in p.parent with this (possibly old) content.
            try:
                _write_json(salvaged, p)
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
        text = path.read_text(encoding="utf-8").lstrip()
    except Exception:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) and obj else None


def save_manifest(manifest: dict, path: str | Path) -> None:
    """
    Persist *manifest*.

    SQLite backend (default): the DB is written transactionally (the source of
    truth), a rotating snapshot is taken, and manifest.json is still exported
    alongside for compatibility and portability. A truncated or deleted JSON can
    no longer wipe history. JSON backend: the legacy atomic single-file write.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = _now()

    if _BACKEND == "sqlite":
        root = p.parent
        dbp = manifest_store.db_path_for(root)
        # Shrink guard: if this save would drop the file index from many rows to
        # almost none, snapshot FIRST so the drop is always recoverable. It does
        # not block the write (a deliberate reset is allowed), only makes it safe.
        try:
            prev = manifest_store.file_count(dbp)
            now_n = len(manifest.get("files", {}) or {})
            if prev > 20 and now_n < max(1, prev // 2):
                _snapshot_db(dbp, tag="preshrink")
                logger.warning("[manifest] file index shrinking %d → %d; "
                               "snapshot taken before save", prev, now_n)
        except Exception:
            pass
        manifest_store.save_dict(dbp, manifest, events_max=_EVENTS_MAX)
        _maybe_routine_snapshot(dbp)
        # Best-effort JSON export; never let it undo the committed DB write.
        try:
            _write_json(manifest, p)
        except Exception as exc:                    # pragma: no cover
            logger.warning("[manifest] JSON export failed (%s); DB is current", exc)
        return

    _write_json(manifest, p)


def _maybe_routine_snapshot(db_path: Path) -> None:
    """Routine snapshot, throttled: always on a process's first save of this DB,
    then at most once per ``_SNAPSHOT_INTERVAL_S``."""
    key = str(Path(db_path).resolve())
    now = time.monotonic()
    last = _last_snapshot.get(key)
    if last is not None and now - last < _SNAPSHOT_INTERVAL_S:
        return
    if _snapshot_db(db_path) is not None:
        _last_snapshot[key] = now


def _snapshot_db(db_path: Path, *, tag: str = "") -> Path | None:
    """Snapshot the DB into <root>/.manifest_snapshots/ and rotate.

    Uses the SQLite online-backup API, NOT a file copy: the DB runs in WAL mode,
    so recently committed transactions may live only in manifest.db-wal until a
    checkpoint, and a plain copy of manifest.db would silently miss them. The
    snapshot is converted to a self-contained rollback-journal file.

    Routine snapshots are ``manifest-<ts>.db`` (keep ``_SNAPSHOTS_KEEP``);
    ``tag="preshrink"`` snapshots are ``preshrink-<ts>.db`` with their own
    rotation (``_PRESHRINK_KEEP``), so routine churn never evicts them.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    snapdir = db_path.parent / _SNAPSHOT_DIRNAME
    snapdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    prefix = "preshrink" if tag == "preshrink" else "manifest"
    final = snapdir / f"{prefix}-{stamp}.db"
    tmp = snapdir / f".{final.name}.tmp.{os.getpid()}"
    try:
        src = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        tmp.replace(final)
    except Exception as exc:
        logger.warning("[manifest] snapshot failed (%s)", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    routine, pre = _list_snapshots(snapdir)
    victims = []
    if _SNAPSHOTS_KEEP > 0:
        victims += routine[:-_SNAPSHOTS_KEEP]
    if _PRESHRINK_KEEP > 0:
        victims += pre[:-_PRESHRINK_KEEP]
    for old in victims:
        try:
            old.unlink()
        except Exception:
            pass
    return final


def _write_json(manifest: dict, p: Path) -> None:
    """Atomic single-file JSON write (unique tmp then rename).

    The tmp filename includes the PID + a random suffix so that concurrent
    writers never share a temp file — sharing one was the source of "Extra data"
    corruption. ``replace`` is atomic on POSIX, so readers see a complete file.
    """
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            # default=str mirrors every sibling serializer (events._json_safe,
            # runstate, make_provenance): without it a stray np.int64/np.bool_/
            # ndarray in any app's stored section raises TypeError INSIDE the flock,
            # the mutation is discarded, and every caller swallows it into one warn
            # line — the .dat lands but its provenance/analysis record vanishes.
            json.dump(manifest, fh, indent=2, default=str)
        tmp.replace(p)   # atomic on POSIX and Windows (os.replace)
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

    All apps (hub, reduction, average, background, analysis, assistant) that
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


# ── Fast indexed search (SQLite backend) ────────────────────────────────────────

def query_files(project_root: str | Path, *, run: str | None = None,
                stage: str | None = None, detector: str | None = None,
                keyword: str | None = None, limit: int | None = None) -> list[dict]:
    """Return file records matching the given filters, without loading the whole
    manifest. On the SQLite backend this is an indexed query (fast at any size);
    on the JSON backend it falls back to filtering the loaded dict."""
    if _BACKEND == "sqlite":
        return manifest_store.query_files(
            manifest_store.db_path_for(project_root),
            run=run, stage=stage, detector=detector, keyword=keyword, limit=limit)
    m = load_manifest(manifest_path_for(project_root))
    out = []
    for v in (m.get("files", {}) or {}).values():
        if stage and v.get("stage", "").lower() != stage.lower():
            continue
        if detector and v.get("detector", "").lower() != detector.lower():
            continue
        if run and run not in f"{v.get('keyword','')} {v.get('path','')}":
            continue
        if keyword and keyword.lower() not in v.get("keyword", "").lower():
            continue
        out.append(v)
        if limit and len(out) >= limit:
            break
    return out


def manifest_runs(project_root: str | Path) -> list[str]:
    """Distinct run tags (Run1, Run2, …) present in the file index, natural-sorted."""
    if _BACKEND == "sqlite":
        return manifest_store.distinct_runs(manifest_store.db_path_for(project_root))
    seen = set()
    for v in query_files(project_root):
        m = re.search(r"Run\d+", f"{v.get('keyword','')} {v.get('path','')}")
        if m:
            seen.add(m.group(0))
    return sorted(seen, key=lambda s: int(re.sub(r"\D", "", s) or 0))


def manifest_counts(project_root: str | Path) -> dict:
    """Totals per stage and per run — a cheap overview."""
    if _BACKEND == "sqlite":
        return manifest_store.counts(manifest_store.db_path_for(project_root))
    files = query_files(project_root)
    by_stage, by_run = {}, {}
    for v in files:
        by_stage[v.get("stage", "")] = by_stage.get(v.get("stage", ""), 0) + 1
    return {"files": len(files), "by_stage": by_stage, "by_run": by_run}


def merge_manifest_file(project_root: str | Path, json_path: str | Path) -> dict:
    """Append-only merge of another manifest.json (e.g. a manifest.corrupt-*.json
    backup, or a second project's manifest) into this project's store. Nothing is
    deleted; an existing entry is only replaced if the source file is newer than
    the live store. Returns per-section counts (see :func:`merge_manifest_sources`)."""
    return merge_manifest_sources(project_root, [json_path])


#: keyed sections merged per key: (section path in the dict, summary name)
_MERGE_KEYED = (
    (("files",), "files"),
    (("analyses",), "analyses"),
    (("background",), "background"),
    (("quality",), "quality"),
    (("reactor", "runs"), "reactor_runs"),
)


def _source_ts(data: dict, path: Path | None = None) -> str:
    """A source's age, for newest-wins ordering: its ``updated_at``, else the
    file's mtime (UTC ISO, so it compares with ``_now()`` strings)."""
    ts = str(data.get("updated_at") or "")
    if ts:
        return ts
    if path is not None:
        try:
            return datetime.fromtimestamp(Path(path).stat().st_mtime,
                                          timezone.utc).isoformat()
        except OSError:
            pass
    return ""


def _dig(d: dict, keys: tuple, create: bool = False) -> dict | None:
    for k in keys:
        nxt = d.get(k)
        if not isinstance(nxt, dict):
            if not create:
                return None
            nxt = d[k] = {}
        d = nxt
    return d


def _merge_into(live: dict, sources: list[tuple[str, str, dict]]) -> dict:
    """Merge ``sources`` [(name, ts, manifest_dict), ...] into ``live`` in place.

    Never deletes. Sources are applied oldest first, so among sources the newest
    wins per key. Against the live store: a key missing from ``live`` is added;
    a key already present is replaced only if its source is newer than the live
    store's ``updated_at``. ``files`` are keyed by path, ``analyses`` by id,
    ``background``/``quality`` by output path, ``reactor.runs`` by recipe id.
    Lists (events, AI corrections, summaries) are unioned; dicts of context are
    filled without overwriting. Returns {section: n_added, ..., "updated": {...}}.
    """
    live_ts = str(live.get("updated_at") or "")
    added = {name: 0 for _, name in _MERGE_KEYED}
    updated = {name: 0 for _, name in _MERGE_KEYED}
    ordered = sorted(sources, key=lambda s: s[1] or "")

    # 1. collapse the sources (oldest -> newest), remembering each key's source ts
    acc: dict[str, dict[str, tuple[str, Any]]] = {name: {} for _, name in _MERGE_KEYED}
    for _name, ts, data in ordered:
        for keys, name in _MERGE_KEYED:
            sect = _dig(data, keys) or {}
            for k, v in sect.items():
                acc[name][k] = (ts, v)

    # 2. fold into live
    for keys, name in _MERGE_KEYED:
        if not acc[name]:
            continue
        dest = _dig(live, keys, create=True)
        for k, (ts, v) in acc[name].items():
            if k not in dest:
                dest[k] = v
                added[name] += 1
            elif ts and ts > live_ts and dest[k] != v:
                dest[k] = v
                updated[name] += 1

    # 3. non-keyed content: union, never drop
    def _key(o: Any) -> str:
        return json.dumps(o, sort_keys=True, default=str)

    for _name, _ts, data in ordered:
        if data.get("created_at") and (not live.get("created_at")
                                       or str(data["created_at"]) < str(live["created_at"])):
            live["created_at"] = data["created_at"]
        pm = live.setdefault("project_meta", {})
        for k, v in (data.get("project_meta") or {}).items():
            pm.setdefault(k, v)
        src_ai = data.get("ai_memory") or {}
        ai = live.setdefault("ai_memory", {})
        for lst in ("corrections", "session_summaries"):
            cur = ai.setdefault(lst, [])
            seen = {_key(x) for x in cur}
            for item in src_ai.get(lst) or []:
                if _key(item) not in seen:
                    cur.append(item)
                    seen.add(_key(item))
        qf = ai.setdefault("quality_flags", {})
        for path, flags in (src_ai.get("quality_flags") or {}).items():
            cur = qf.setdefault(path, [])
            cur.extend(f for f in flags or [] if f not in cur)
        uc = ai.setdefault("user_context", {})
        for k, v in (src_ai.get("user_context") or {}).items():
            uc.setdefault(k, v)
        evs = live.setdefault("events", [])
        seen = {_key(e) for e in evs}
        for e in data.get("events") or []:
            if _key(e) not in seen:
                evs.append(e)
                seen.add(_key(e))
    if live.get("events"):
        live["events"] = sorted(live["events"],
                                key=lambda e: str((e or {}).get("timestamp", "")))[-_EVENTS_MAX:]
    return {**added, "updated": updated}


def merge_manifest_sources(project_root: str | Path,
                           json_paths: list[str | Path]) -> dict:
    """Recover runs from manifest JSON files into the LIVE store (whatever the
    backend), append-only and newest-wins (see :func:`_merge_into`).

    Source files are only read, never renamed or rewritten; damaged files are
    salvaged in memory when possible and skipped otherwise. Idempotent: a second
    run adds and updates nothing. Returns per-section counts plus ``updated``,
    ``sources`` (names merged) and ``skipped`` (unreadable names).
    """
    sources, skipped = [], []
    for jp in json_paths:
        jp = Path(jp)
        data, err = _try_read_json(jp)
        if not data:
            logger.warning("[manifest] merge: skipping %s (%s)", jp.name, err)
            skipped.append(jp.name)
            continue
        sources.append((jp.name, _source_ts(data, jp), data))

    holder: dict = {}

    def _mut(m: dict) -> None:
        holder.update(_merge_into(m, sources))

    if sources:
        try:
            update_manifest(project_root, _mut)
        except ManifestUnreadableError as exc:
            # The live manifest.json is unrecoverable and there is no DB or
            # snapshot (a damaged copy was already kept). The readable sources
            # ARE the surviving history: build the store from them.
            logger.warning("[manifest] merge: live manifest unreadable (%s); "
                           "rebuilding the store from the readable sources", exc)
            with manifest_lock(project_root):
                root = Path(project_root).resolve()
                m = _empty_manifest(root)
                m["created_at"] = ""
                holder.update(_merge_into(m, sources))
                m["created_at"] = m["created_at"] or _now()
                save_manifest(m, manifest_path_for(root))
    else:
        holder.update({name: 0 for _, name in _MERGE_KEYED},
                      updated={name: 0 for _, name in _MERGE_KEYED})
    holder["sources"] = [s[0] for s in sorted(sources, key=lambda s: s[1] or "")]
    holder["skipped"] = skipped
    return holder


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
    resolved = str(Path(file_path).resolve())
    analyses = manifest.setdefault("analyses", {})
    # Upsert by (type, file_path): re-analysing the same file (a restart, a re-fit,
    # a file rewritten by an earlier stage) must UPDATE its record, not append a
    # near-duplicate under a fresh uuid — which bloated the manifest and left stale
    # sizes sitting beside current ones. A DIFFERENT analysis_type on the same file
    # (Guinier vs Porod vs nanoparticle) is legitimately a separate record.
    aid = next((k for k, v in analyses.items()
                if v.get("type") == analysis_type and v.get("file_path") == resolved),
               None)
    created = analyses.get(aid, {}).get("created_at") if aid else None
    aid = aid or str(uuid.uuid4())
    analyses[aid] = {
        "id":            aid,
        "type":          analysis_type,
        "file_path":     resolved,
        "params":        params,
        "results":       results,
        "fit_range":     fit_range     or [],
        "quality_score": quality_score,
        "ai_assessment": ai_assessment,
        "provenance":    provenance    or {},
        "created_at":    created or _now(),
        "updated_at":    _now(),
    }
    # Cap the analyses section. It upserts by (type, file_path), so re-fits don't
    # duplicate, but over weeks of a live loop one entry accrues per distinct
    # profile — thousands of them — and the WHOLE manifest.json is rewritten on
    # every fit, so write latency climbs with N. The durable per-fit trail lives
    # in Results/Fit/ regardless; here we keep only the most recent entries so the
    # manifest stays small and writes stay fast. 0 disables the cap.
    if _ANALYSES_CAP > 0 and len(analyses) > _ANALYSES_CAP:
        # Drop oldest by updated_at (falls back to created_at), keeping newest cap.
        ordered = sorted(analyses.items(),
                         key=lambda kv: (kv[1].get("updated_at")
                                         or kv[1].get("created_at") or ""))
        for k, _ in ordered[:len(analyses) - _ANALYSES_CAP]:
            analyses.pop(k, None)
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
    """Record an Autonomous Synthesis reactor run in manifest["reactor"]["runs"].

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
