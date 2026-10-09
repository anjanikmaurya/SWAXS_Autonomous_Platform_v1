"""
src/manifest_store.py — SQLite backend for the shared experiment manifest.

Why this exists
───────────────
The manifest used to be ONE growing ``manifest.json``. Every write loaded,
mutated and rewrote the whole file, which (a) gets slow and corruption-prone as
history grows, (b) can only be searched by crude substring matching, and (c) can
be reset to empty if the single file is ever truncated by an external process
(e.g. a cloud-sync folder). See docs/CHANGELOG.md, October 2026.

This module keeps the SAME in-memory manifest dict the ten apps already use, but
persists it in a transactional SQLite database (``manifest.db``) with indexed
columns, so:
  • writes are transactional (crash-safe) and never rewrite the whole history,
  • search is real SQL (by run / stage / detector / keyword / time),
  • a missing or truncated file can no longer silently wipe the index, because
    the DB is the source of truth and every save also snapshots it.

``src/manifest.py`` is the public API; it calls :func:`load_dict` /
:func:`save_dict` here and exports a ``manifest.json`` alongside for
compatibility and portability. Nothing in the apps changes.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

DB_FILENAME = "manifest.db"
_RUN_RE = re.compile(r"Run\d+")


def db_path_for(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / DB_FILENAME


def _dumps(obj: Any) -> str:
    # default=str mirrors save_manifest / events so a stray numpy scalar never
    # raises mid-transaction and silently drops a record.
    return json.dumps(obj, default=str)


def _run_tag(*candidates: str) -> str:
    for c in candidates:
        if not c:
            continue
        m = _RUN_RE.search(str(c))
        if m:
            return m.group(0)
    return ""


# ── connection / schema ─────────────────────────────────────────────────────────

def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the DB in WAL mode with a generous busy timeout.

    WAL lets readers run while a writer holds the DB; the busy timeout makes the
    rare cross-process collision wait rather than raise. (The manifest file lock
    in manifest.py already serialises writers; this is belt-and-braces.)
    """
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    _init_schema(con)
    return con


def _init_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS files (
            path      TEXT PRIMARY KEY,
            run       TEXT,
            stage     TEXT,
            detector  TEXT,
            keyword   TEXT,
            scan_idx  INTEGER,
            data      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_files_run      ON files(run);
        CREATE INDEX IF NOT EXISTS ix_files_stage    ON files(stage);
        CREATE INDEX IF NOT EXISTS ix_files_detector ON files(detector);
        CREATE INDEX IF NOT EXISTS ix_files_keyword  ON files(keyword);

        CREATE TABLE IF NOT EXISTS analyses (
            id        TEXT PRIMARY KEY,
            file_path TEXT,
            type      TEXT,
            data      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_analyses_type ON analyses(type);

        CREATE TABLE IF NOT EXISTS background (
            key  TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quality (
            key  TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reactor_runs (
            id   TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            seq  INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL
        );
        """
    )
    con.commit()


# ── save (dict -> DB) ─────────────────────────────────────────────────────────

#: keyed top-level sections and their table + id field
_KEYED = {
    "files":      ("files",        "path"),
    "analyses":   ("analyses",     "id"),
    "background": ("background",   "key"),
    "quality":    ("quality",      "key"),
}


def save_dict(db_path: str | Path, m: dict, *, events_max: int = 100) -> None:
    """Persist the whole manifest dict into the DB in ONE transaction.

    Keyed sections are synced (rows absent from the dict are deleted, present
    rows upserted) so the DB mirrors the dict exactly. The events list is
    replaced wholesale (it is already a rolling window in the dict).
    """
    con = connect(db_path)
    try:
        with con:                                  # one atomic transaction
            # ── scalar + blob meta ──────────────────────────────────────────
            meta = {
                "version":      m.get("version", "2.0"),
                "project_root": m.get("project_root", ""),
                "created_at":   m.get("created_at", ""),
                "updated_at":   m.get("updated_at", ""),
                "project_meta": _dumps(m.get("project_meta", {})),
                "ai_memory":    _dumps(m.get("ai_memory", {})),
            }
            con.executemany(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(meta.items()))

            # ── files ───────────────────────────────────────────────────────
            files = m.get("files", {}) or {}
            _sync_keyed(con, "files", set(files),
                        lambda k, v: (k, _run_tag(v.get("keyword", ""), v.get("path", k)),
                                      v.get("stage", ""), v.get("detector", ""),
                                      v.get("keyword", ""), int(v.get("scan_idx", 0) or 0),
                                      _dumps(v)),
                        files,
                        cols="(path,run,stage,detector,keyword,scan_idx,data)",
                        nvals=7, idcol="path")

            # ── analyses ────────────────────────────────────────────────────
            analyses = m.get("analyses", {}) or {}
            _sync_keyed(con, "analyses", set(analyses),
                        lambda k, v: (k, v.get("file_path", ""), v.get("type", ""), _dumps(v)),
                        analyses, cols="(id,file_path,type,data)", nvals=4, idcol="id")

            # ── background / quality (simple key + blob) ────────────────────
            for sect in ("background", "quality"):
                d = m.get(sect, {}) or {}
                _sync_keyed(con, sect, set(d),
                            lambda k, v: (k, _dumps(v)),
                            d, cols="(key,data)", nvals=2, idcol="key")

            # ── reactor.runs ────────────────────────────────────────────────
            runs = ((m.get("reactor") or {}).get("runs") or {})
            _sync_keyed(con, "reactor_runs", set(runs),
                        lambda k, v: (k, _dumps(v)),
                        runs, cols="(id,data)", nvals=2, idcol="id")

            # ── events (rolling window, replace wholesale) ──────────────────
            con.execute("DELETE FROM events")
            evs = (m.get("events") or [])[-int(events_max):]
            con.executemany("INSERT INTO events(data) VALUES(?)",
                            [(_dumps(e),) for e in evs])
    finally:
        con.close()


def _sync_keyed(con, table, keys, rowfn, src, *, cols, nvals, idcol):
    """Delete rows whose id is not in ``keys``, then upsert every present row."""
    existing = {r[0] for r in con.execute(f"SELECT {idcol} FROM {table}")}
    stale = existing - set(keys)
    if stale:
        con.executemany(f"DELETE FROM {table} WHERE {idcol}=?", [(k,) for k in stale])
    if not src:
        return
    placeholders = ",".join(["?"] * nvals)
    # upsert: on PK conflict, overwrite all non-id columns with the new row
    collist = cols.strip("()").split(",")
    setclause = ",".join(f"{c}=excluded.{c}" for c in collist if c != idcol)
    sql = (f"INSERT INTO {table}{cols} VALUES({placeholders}) "
           f"ON CONFLICT({idcol}) DO UPDATE SET {setclause}")
    con.executemany(sql, [rowfn(k, v) for k, v in src.items()])


# ── load (DB -> dict) ─────────────────────────────────────────────────────────

def load_dict(db_path: str | Path) -> dict:
    """Rebuild the full manifest dict from the DB, in the canonical v2 shape."""
    con = connect(db_path)
    try:
        meta = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM meta")}
        m: dict = {
            "version":      meta.get("version", "2.0"),
            "project_root": meta.get("project_root", str(Path(db_path).resolve().parent)),
            "created_at":   meta.get("created_at", ""),
            "updated_at":   meta.get("updated_at", ""),
            "project_meta": _loads(meta.get("project_meta"), {}),
            "files":        {},
            "analyses":     {},
            "background":   {},
            "ai_memory":    _loads(meta.get("ai_memory"), {
                "corrections": [], "session_summaries": [],
                "quality_flags": {}, "user_context": {}}),
            "events":       [],
        }
        for r in con.execute("SELECT path,data FROM files"):
            m["files"][r["path"]] = _loads(r["data"], {})
        for r in con.execute("SELECT id,data FROM analyses"):
            m["analyses"][r["id"]] = _loads(r["data"], {})
        for r in con.execute("SELECT key,data FROM background"):
            m["background"][r["key"]] = _loads(r["data"], {})
        quality = {r["key"]: _loads(r["data"], {})
                   for r in con.execute("SELECT key,data FROM quality")}
        if quality:
            m["quality"] = quality
        runs = {r["id"]: _loads(r["data"], {})
                for r in con.execute("SELECT id,data FROM reactor_runs")}
        if runs:
            m["reactor"] = {"runs": runs}
        m["events"] = [_loads(r["data"], {})
                       for r in con.execute("SELECT data FROM events ORDER BY seq")]
        return m
    finally:
        con.close()


def _loads(s: Any, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


def has_rows(db_path: str | Path) -> bool:
    """True if the DB holds any file/analysis/background rows (i.e. real data)."""
    p = Path(db_path)
    if not p.exists():
        return False
    con = connect(p)
    try:
        for t in ("files", "analyses", "background", "reactor_runs"):
            if con.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone():
                return True
        return False
    finally:
        con.close()


def file_count(db_path: str | Path) -> int:
    p = Path(db_path)
    if not p.exists():
        return 0
    con = connect(p)
    try:
        return int(con.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    finally:
        con.close()


# ── query (fast, indexed — the real win over substring-in-JSON) ──────────────────

def query_files(db_path: str | Path, *, run: str | None = None,
                stage: str | None = None, detector: str | None = None,
                keyword: str | None = None, limit: int | None = None) -> list[dict]:
    """Return full file records matching any combination of filters.

    ``keyword`` is a substring (LIKE) match; the rest are exact. All filters are
    indexed, so this stays fast no matter how large the history grows.
    """
    p = Path(db_path)
    if not p.exists():
        return []
    where, args = [], []
    if run:
        where.append("run=?"); args.append(run)
    if stage:
        where.append("stage=?"); args.append(stage.lower())
    if detector:
        where.append("detector=?"); args.append(detector.lower())
    if keyword:
        where.append("keyword LIKE ?"); args.append(f"%{keyword}%")
    sql = "SELECT data FROM files"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if limit:
        sql += f" LIMIT {int(limit)}"
    con = connect(p)
    try:
        return [_loads(r["data"], {}) for r in con.execute(sql, args)]
    finally:
        con.close()


def distinct_runs(db_path: str | Path) -> list[str]:
    p = Path(db_path)
    if not p.exists():
        return []
    con = connect(p)
    try:
        rows = con.execute(
            "SELECT DISTINCT run FROM files WHERE run<>'' ORDER BY run").fetchall()
        # natural sort Run2 < Run10
        return sorted((r[0] for r in rows),
                      key=lambda s: int(re.sub(r"\D", "", s) or 0))
    finally:
        con.close()


def counts(db_path: str | Path) -> dict:
    """Totals per stage and per run — a cheap overview for the assistant/UI."""
    p = Path(db_path)
    if not p.exists():
        return {"files": 0, "by_stage": {}, "by_run": {}}
    con = connect(p)
    try:
        by_stage = {r["stage"]: r["n"] for r in con.execute(
            "SELECT stage, COUNT(*) n FROM files GROUP BY stage")}
        by_run = {r["run"]: r["n"] for r in con.execute(
            "SELECT run, COUNT(*) n FROM files WHERE run<>'' GROUP BY run")}
        total = int(con.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        return {"files": total, "by_stage": by_stage, "by_run": by_run}
    finally:
        con.close()


# ── merge / import (append-only recovery) ─────────────────────────────────────

def merge_dict(db_path: str | Path, other: dict) -> dict:
    """Append-only merge of another manifest dict into the DB.

    Rows present in ``other`` are upserted; NOTHING is deleted. This is how a
    ``manifest.corrupt-*.json`` backup (or a second project's manifest) is folded
    back in without losing anything already in the DB. Returns a small summary.
    """
    con = connect(db_path)
    added = {"files": 0, "analyses": 0, "background": 0, "quality": 0, "reactor_runs": 0}
    try:
        with con:
            for k, v in (other.get("files") or {}).items():
                con.execute(
                    "INSERT INTO files(path,run,stage,detector,keyword,scan_idx,data) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                    "run=excluded.run,stage=excluded.stage,detector=excluded.detector,"
                    "keyword=excluded.keyword,scan_idx=excluded.scan_idx,data=excluded.data",
                    (k, _run_tag(v.get("keyword", ""), v.get("path", k)),
                     v.get("stage", ""), v.get("detector", ""), v.get("keyword", ""),
                     int(v.get("scan_idx", 0) or 0), _dumps(v)))
                added["files"] += 1
            for k, v in (other.get("analyses") or {}).items():
                con.execute(
                    "INSERT INTO analyses(id,file_path,type,data) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET file_path=excluded.file_path,"
                    "type=excluded.type,data=excluded.data",
                    (k, v.get("file_path", ""), v.get("type", ""), _dumps(v)))
                added["analyses"] += 1
            for sect in ("background", "quality"):
                for k, v in (other.get(sect) or {}).items():
                    con.execute(
                        f"INSERT INTO {sect}(key,data) VALUES(?,?) "
                        f"ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                        (k, _dumps(v)))
                    added[sect] += 1
            for k, v in ((other.get("reactor") or {}).get("runs") or {}).items():
                con.execute(
                    "INSERT INTO reactor_runs(id,data) VALUES(?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (k, _dumps(v)))
                added["reactor_runs"] += 1
            # ensure meta exists (created_at kept if already set)
            cur = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM meta")}
            if "created_at" not in cur:
                for key in ("version", "project_root", "created_at", "updated_at"):
                    if other.get(key):
                        con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    (key, str(other[key])))
    finally:
        con.close()
    return added
