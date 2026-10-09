"""tools/manifest_to_sqlite.py — the run-recovery importer, against the REAL
manifest backend (src/manifest.py + src/manifest_store.py).

The standalone src/manifest_db.py engine this file used to test was a duplicate
of manifest_store and has been removed. The importer now merges a project's
manifest.json and every manifest.corrupt-*.json into the live store: append-only,
newest source wins per key, idempotent.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import src.manifest as M                       # noqa: E402
from tools import manifest_to_sqlite as tool   # noqa: E402


def _entry(path, stage="averaged", keyword=None):
    return {"path": path, "stage": stage, "detector": "saxs",
            "keyword": keyword or Path(path).stem, "scan_idx": 0,
            "metadata": {}, "provenance": {}, "status": "ok",
            "notes": "", "quality_flags": []}


def _add(root, path, stage, keyword):
    M.update_manifest(root, lambda m: M.add_file_entry(
        m, path=path, stage=stage, detector="saxs", keyword=keyword))


def _backup(root, name, updated_at, files, **extra):
    p = root / name
    p.write_text(json.dumps({"version": "2.0", "updated_at": updated_at,
                             "files": files, **extra}))
    return p


def test_import_recovers_old_runs_without_clobbering_newer(tmp_path):
    root = tmp_path / "proj"
    for i in range(3):
        _add(root, root / f"d/Run17_r01{i}.dat", "subtracted", f"Run17_r01{i}")
    live_key = str((root / "d/Run17_r010.dat").resolve())
    # An OLD backup holds Run7 (lost from the live index) and a stale version of
    # a Run17 entry that must NOT overwrite the live one.
    _backup(root, "manifest.corrupt-20260901-120000.json", "2026-09-01T12:00:00+00:00",
            {"/old/Run7_r001.dat": _entry("/old/Run7_r001.dat"),
             "/old/Run7_r002.dat": _entry("/old/Run7_r002.dat"),
             live_key: _entry(live_key, stage="reduced")},
            analyses={"a1": {"id": "a1", "type": "guinier", "file_path": "/old/Run7_r001.dat"}},
            reactor={"runs": {"Run7": {"recipe_id": "Run7"}}})
    res = tool.import_project(root)
    assert res["files"] == 2 and res["updated"]["files"] == 0
    assert res["analyses"] == 1 and res["reactor_runs"] == 1
    m = M.load_manifest(M.manifest_path_for(root))
    assert m["files"][live_key]["stage"] == "subtracted"     # newer kept
    assert set(M.manifest_runs(root)) == {"Run7", "Run17"}
    assert M.manifest_counts(root)["files"] == 5
    assert m["reactor"]["runs"]["Run7"]["recipe_id"] == "Run7"


def test_import_is_idempotent(tmp_path):
    root = tmp_path / "proj"
    _add(root, root / "d/Run17_a.dat", "averaged", "Run17_a")
    _backup(root, "manifest.corrupt-20260901-120000.json", "2026-09-01T12:00:00+00:00",
            {"/old/Run7_a.dat": _entry("/old/Run7_a.dat")})
    tool.import_project(root)
    before = M.load_manifest(M.manifest_path_for(root))
    again = tool.import_project(root)
    assert again["files"] == 0 and again["updated"]["files"] == 0
    after = M.load_manifest(M.manifest_path_for(root))
    assert after["files"] == before["files"]


def test_newest_source_wins_among_backups(tmp_path):
    root = tmp_path / "proj"
    _add(root, root / "d/Run17_a.dat", "averaged", "Run17_a")
    _backup(root, "manifest.corrupt-20260901-000000.json", "2026-09-01T00:00:00+00:00",
            {"/old/Run7_a.dat": _entry("/old/Run7_a.dat", stage="reduced")})
    _backup(root, "manifest.corrupt-20260905-000000.json", "2026-09-05T00:00:00+00:00",
            {"/old/Run7_a.dat": _entry("/old/Run7_a.dat", stage="averaged")})
    tool.import_project(root)
    m = M.load_manifest(M.manifest_path_for(root))
    assert m["files"]["/old/Run7_a.dat"]["stage"] == "averaged"


def test_salvaged_backup_is_read_only_and_cannot_overwrite_live_db(tmp_path):
    """Regression: salvaging a damaged backup used to call save_manifest(salvaged,
    backup_path), which on the SQLite backend rewrote the LIVE DB with the old
    content, deleting newer rows. Sources must be read-only."""
    root = tmp_path / "proj"
    for i in range(4):
        _add(root, root / f"d/Run17_{i}.dat", "averaged", f"Run17_{i}")
    good = json.dumps({"version": "2.0", "updated_at": "2026-09-01T00:00:00+00:00",
                       "files": {"/old/Run7_a.dat": _entry("/old/Run7_a.dat")}})
    bpath = root / "manifest.corrupt-20260901-000000.json"
    bpath.write_text(good + '"trailing garbage"}')       # "Extra data" damage
    raw = bpath.read_bytes()
    res = M.merge_manifest_file(root, bpath)
    assert res["files"] == 1
    assert bpath.read_bytes() == raw                       # untouched
    assert M.manifest_counts(root)["files"] == 5           # nothing lost


def test_import_rebuilds_store_when_live_json_unreadable(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "manifest.json").write_text("{not json at all")
    _backup(root, "manifest.corrupt-20260901-000000.json", "2026-09-01T00:00:00+00:00",
            {"/old/Run7_a.dat": _entry("/old/Run7_a.dat")})
    res = tool.import_project(root)
    assert res["files"] == 1 and "manifest.json" in res["skipped"]
    assert M.manifest_runs(root) == ["Run7"]
    # the damaged live JSON was preserved as a corrupt copy
    assert any(p.read_text() == "{not json at all"
               for p in root.glob("manifest.corrupt-*.json"))


def test_cli_main(tmp_path, capsys):
    root = tmp_path / "proj"
    _add(root, root / "d/Run17_a.dat", "averaged", "Run17_a")
    _backup(root, "manifest.corrupt-20260901-000000.json", "2026-09-01T00:00:00+00:00",
            {"/old/Run7_a.dat": _entry("/old/Run7_a.dat")})
    out = tmp_path / "merged.json"
    assert tool.main([str(root), "--export", str(out)]) == 0
    assert len(json.loads(out.read_text())["files"]) == 2
    assert "Run7" in capsys.readouterr().out


def test_no_module_imports_removed_engine():
    assert not (_ROOT / "src" / "manifest_db.py").exists()
    for base in ("src", "tools", "tests", "hub", "assistant", "analysis",
                 "analyzer", "reactor", "quality", "background", "average",
                 "reduction", "calibration", "watchdog"):
        for f in (_ROOT / base).rglob("*.py"):
            if f.name == Path(__file__).name:
                continue
            txt = f.read_text(encoding="utf-8", errors="ignore")
            assert "import manifest_db" not in txt and "src.manifest_db" not in txt, f
