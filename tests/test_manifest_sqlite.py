"""SQLite manifest backend (src/manifest_store.py + src/manifest.py wiring).

Locks in: the dict API the ten apps use is unchanged; writes persist to a
transactional DB; a reset of the JSON file can no longer wipe history; indexed
search works; and old runs can be merged back append-only (the Run7 recovery).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _add(M, root, path, stage, detector, keyword):
    M.update_manifest(root, lambda m: M.add_file_entry(
        m, path=path, stage=stage, detector=detector, keyword=keyword))


def test_dict_api_unchanged_and_persisted(tmp_path):
    """add_file_entry / update_manifest / load_manifest behave exactly as before,
    and the data survives a reload (now from the DB)."""
    import src.manifest as M
    root = tmp_path / "proj"
    _add(M, root, root / "1D/SAXS/Averaged/Run7_r001_sample_Average.dat",
         "averaged", "saxs", "Run7_r001_sample")
    _add(M, root, root / "1D/SAXS/Reduction/Run17_r005_scan1_0000_SAXS.dat",
         "reduced", "saxs", "Run17_r005_scan1")
    assert (root / "manifest.db").exists()
    assert (root / "manifest.json").exists()          # JSON export still written
    m = M.load_manifest(M.manifest_path_for(root))
    assert len(m["files"]) == 2
    assert all("stage" in v and "keyword" in v for v in m["files"].values())


def test_indexed_search(tmp_path):
    import src.manifest as M
    root = tmp_path / "proj"
    for i in range(3):
        _add(M, root, root / f"a/Run7_r00{i}_sample_Average.dat",
             "averaged", "saxs", f"Run7_r00{i}_sample")
    _add(M, root, root / "a/Run17_r005_scan1_0000_WAXS.dat",
         "reduced", "waxs", "Run17_r005_scan1")
    assert M.manifest_runs(root) == ["Run7", "Run17"]
    assert len(M.query_files(root, run="Run7")) == 3
    assert len(M.query_files(root, run="Run7", stage="averaged")) == 3
    assert len(M.query_files(root, detector="waxs")) == 1
    assert [v["keyword"] for v in M.query_files(root, keyword="r005")] == ["Run17_r005_scan1"]
    c = M.manifest_counts(root)
    assert c["files"] == 4 and c["by_run"]["Run7"] == 3


def test_json_migrates_to_db_on_first_load(tmp_path):
    """A legacy manifest.json with no DB is migrated into the DB on first load,
    losing nothing."""
    import src.manifest as M
    root = tmp_path / "proj"
    root.mkdir()
    legacy = {
        "version": "2.0", "project_root": str(root),
        "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:00+00:00",
        "project_meta": {"users": ["akmaurya"]},
        "files": {f"/d/Run3_r00{i}_sample.dat": {
            "path": f"/d/Run3_r00{i}_sample.dat", "stage": "averaged",
            "detector": "saxs", "keyword": f"Run3_r00{i}_sample", "scan_idx": 0,
            "metadata": {}, "provenance": {}, "status": "ok",
            "notes": "", "quality_flags": []} for i in range(5)},
        "analyses": {}, "background": {}, "events": [],
        "ai_memory": {"corrections": [], "session_summaries": [],
                      "quality_flags": {}, "user_context": {}},
    }
    (root / "manifest.json").write_text(json.dumps(legacy))
    m = M.load_manifest(M.manifest_path_for(root))
    assert (root / "manifest.db").exists()
    assert len(m["files"]) == 5
    assert m["created_at"] == "2026-09-01T00:00:00+00:00"   # created_at preserved
    assert m["project_meta"]["users"] == ["akmaurya"]
    assert M.manifest_runs(root) == ["Run3"]


def test_json_reset_cannot_wipe_history(tmp_path):
    """The exact failure the user hit: the JSON file is truncated/emptied by an
    outside process. Because the DB is the source of truth, history survives, and
    a subsequent load restores a full JSON."""
    import src.manifest as M
    root = tmp_path / "proj"
    for i in range(6):
        _add(M, root, root / f"d/Run9_r00{i}.dat", "reduced", "saxs", f"Run9_r00{i}")
    # Simulate a cloud-sync / external truncation of the JSON to garbage.
    (root / "manifest.json").write_text("")        # emptied file
    m = M.load_manifest(M.manifest_path_for(root))  # reads the DB, not the JSON
    assert len(m["files"]) == 6, "DB must survive a wiped JSON"
    assert M.manifest_runs(root) == ["Run9"]


def test_append_only_merge_recovers_old_runs(tmp_path):
    """merge_manifest_file folds an old backup in without deleting current runs —
    the Run7 recovery path."""
    import src.manifest as M
    root = tmp_path / "proj"
    for i in range(3):
        _add(M, root, root / f"d/Run17_r01{i}.dat", "averaged", "saxs", f"Run17_r01{i}")
    backup = {"version": "2.0", "files": {
        "/old/Run7_r001.dat": {"path": "/old/Run7_r001.dat", "stage": "averaged",
                               "detector": "saxs", "keyword": "Run7_r001"},
        "/old/Run12_r002.dat": {"path": "/old/Run12_r002.dat", "stage": "averaged",
                                "detector": "saxs", "keyword": "Run12_r002"}}}
    bpath = root / "manifest.corrupt-20260930-120000.json"
    bpath.write_text(json.dumps(backup))
    added = M.merge_manifest_file(root, bpath)
    assert added["files"] == 2
    assert set(M.manifest_runs(root)) == {"Run7", "Run12", "Run17"}
    assert M.manifest_counts(root)["files"] == 5      # nothing lost


def test_deletion_in_dict_propagates_to_db(tmp_path):
    """A key removed from the dict (e.g. the analyses cap trimming oldest) is
    reflected in the DB on save, so the DB mirrors the dict."""
    import src.manifest as M
    root = tmp_path / "proj"
    _add(M, root, root / "d/keep.dat", "reduced", "saxs", "Run1_keep")
    _add(M, root, root / "d/drop.dat", "reduced", "saxs", "Run1_drop")

    def _mutate(m):
        m["files"].pop(str((root / "d/drop.dat").resolve()), None)
    M.update_manifest(root, _mutate)
    paths = {Path(v["path"]).name for v in M.query_files(root)}
    assert paths == {"keep.dat"}


def test_snapshots_are_taken(tmp_path):
    import src.manifest as M
    root = tmp_path / "proj"
    _add(M, root, root / "d/Run1_a.dat", "reduced", "saxs", "Run1_a")
    _add(M, root, root / "d/Run1_b.dat", "reduced", "saxs", "Run1_b")
    snaps = list((root / ".manifest_snapshots").glob("manifest-*.db"))
    assert snaps, "a snapshot DB should be written on save"
