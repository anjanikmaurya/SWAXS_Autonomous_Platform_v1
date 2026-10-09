"""Manifest SQLite durability: WAL-consistent snapshots, preshrink retention,
throttled routine snapshots, no empty store over unreadable prior data, and
restore-from-snapshot when manifest.db is lost."""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import src.manifest as M          # noqa: E402
from src import manifest_store    # noqa: E402


def _add(root, name, stage="reduced"):
    M.update_manifest(root, lambda m: M.add_file_entry(
        m, path=root / "d" / name, stage=stage, detector="saxs",
        keyword=Path(name).stem))


def _rows(db):
    con = sqlite3.connect(str(db))
    try:
        return {r[0] for r in con.execute("SELECT path FROM files")}
    finally:
        con.close()


def _snaps(root, prefix):
    return sorted((root / ".manifest_snapshots").glob(f"{prefix}-*.db"))


# ── 1. snapshot captures committed-but-uncheckpointed WAL content ─────────────

def test_snapshot_contains_uncheckpointed_wal_row(tmp_path):
    root = tmp_path / "proj"
    _add(root, "Run1_a.dat")
    dbp = manifest_store.db_path_for(root)
    con = sqlite3.connect(str(dbp))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        with con:
            con.execute("INSERT INTO files(path,run,stage,detector,keyword,scan_idx,data)"
                        " VALUES('WAL_ONLY','Run1','reduced','saxs','k',0,'{}')")
        wal = dbp.with_name(dbp.name + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        # Sanity: a plain copy of the main file (the old method) misses the row.
        plain = tmp_path / "plain.db"
        shutil.copy2(dbp, plain)
        assert "WAL_ONLY" not in _rows(plain)
        snap = M._snapshot_db(dbp)               # connection still open, no checkpoint
        assert snap is not None and "WAL_ONLY" in _rows(snap)
        assert not snap.with_name(snap.name + "-wal").exists()   # self-contained
    finally:
        con.close()


# ── 2. preshrink retention + routine throttling ───────────────────────────────

def test_preshrink_snapshot_survives_many_routine_saves(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_SNAPSHOT_INTERVAL_S", 0.0)   # snapshot every save
    monkeypatch.setattr(M, "_SNAPSHOTS_KEEP", 5)
    root = tmp_path / "proj"
    M.update_manifest(root, lambda m: [M.add_file_entry(
        m, path=root / "d" / f"Run1_{i}.dat", stage="reduced", detector="saxs",
        keyword=f"Run1_{i}") for i in range(30)])
    M.update_manifest(root, lambda m: m.__setitem__("files", {}))   # shrink 30 -> 0
    pre = _snaps(root, "preshrink")
    assert len(pre) == 1 and len(_rows(pre[0])) == 30
    for i in range(50):                                   # lots of routine churn
        _add(root, f"Run2_{i}.dat")
    assert _snaps(root, "preshrink") == pre, "preshrink must not be rotated out"
    assert len(_snaps(root, "manifest")) == 5


def test_routine_snapshots_are_throttled(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_SNAPSHOT_INTERVAL_S", 600.0)
    root = tmp_path / "proj"
    for i in range(25):
        _add(root, f"Run1_{i}.dat")
    assert len(_snaps(root, "manifest")) == 1   # first save of the process only


# ── 3. never an empty store over unreadable prior data ────────────────────────

def test_unreadable_json_migration_does_not_create_empty_db(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "manifest.json").write_text("{\"files\": {\"/a\": {broken")
    with pytest.raises(M.ManifestUnreadableError):
        M.load_manifest(M.manifest_path_for(root))
    with pytest.raises(M.ManifestUnreadableError):
        M.update_manifest(root, lambda m: None)
    assert not (root / "manifest.db").exists()
    assert (root / "manifest.json").read_text().startswith("{\"files\"")  # untouched
    assert len(list(root.glob("manifest.corrupt-*.json"))) == 1           # kept once


def test_missing_db_restored_from_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_SNAPSHOT_INTERVAL_S", 0.0)
    root = tmp_path / "proj"
    for i in range(5):
        _add(root, f"Run3_{i}.dat")
    dbp = manifest_store.db_path_for(root)
    for suffix in ("", "-wal", "-shm"):
        dbp.with_name(dbp.name + suffix).unlink(missing_ok=True)
    (root / "manifest.json").write_text("")             # JSON also gone
    m = M.load_manifest(M.manifest_path_for(root))
    assert len(m["files"]) == 5
    assert dbp.exists() and len(_rows(dbp)) == 5


def test_restore_merges_newer_json_on_top_of_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "_SNAPSHOT_INTERVAL_S", 600.0)
    root = tmp_path / "proj"
    for i in range(4):                       # only the first save is snapshotted
        _add(root, f"Run3_{i}.dat")
    dbp = manifest_store.db_path_for(root)
    dbp.write_bytes(b"")                     # externally truncated DB
    m = M.load_manifest(M.manifest_path_for(root))
    assert len(m["files"]) == 4              # 1 from snapshot + 3 from the JSON
    assert list(root.glob("manifest.empty-*.db")), "bad DB moved aside, not deleted"


def test_unreadable_db_restored_from_snapshot(tmp_path):
    root = tmp_path / "proj"
    _add(root, "Run4_a.dat")
    dbp = manifest_store.db_path_for(root)
    for suffix in ("-wal", "-shm"):
        dbp.with_name(dbp.name + suffix).unlink(missing_ok=True)
    dbp.write_bytes(b"this is not a sqlite database" * 200)
    (root / "manifest.json").unlink()
    m = M.load_manifest(M.manifest_path_for(root))
    assert len(m["files"]) == 1
    assert list(root.glob("manifest.unreadable-*.db"))
