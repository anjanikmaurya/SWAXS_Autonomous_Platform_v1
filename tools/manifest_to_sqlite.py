#!/usr/bin/env python
"""
tools/manifest_to_sqlite.py — recover lost runs into the LIVE manifest store.

Why: an old reset renamed the damaged manifest to ``manifest.corrupt-*.json``
and started an empty one, so runs processed earlier vanished from the index even
though their .dat files are still on disk. This tool reads the project's current
``manifest.json`` AND every ``manifest.corrupt-*.json``, and merges them into the
real backend (``src/manifest.py`` -> ``manifest.db`` on the default SQLite
backend), under the normal cross-process manifest lock.

Merge rules (``src.manifest.merge_manifest_sources``):
  * nothing is ever deleted from the live store;
  * a key missing from the store is added (files by path, analyses by id,
    background/quality by output path, reactor runs by recipe id);
  * a key already in the store is replaced only by a source NEWER than the
    store, and among sources the newest wins;
  * events / AI notes are unioned.
Re-running is idempotent. Source JSON files are only read, never modified.

Usage
-----
    python tools/manifest_to_sqlite.py /path/to/project_root
    python tools/manifest_to_sqlite.py /path/to/project_root --export merged.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import manifest as M   # noqa: E402


def manifest_sources(project_root: Path) -> list[Path]:
    """The current manifest.json plus every corrupt backup in ``project_root``."""
    found = []
    cur = project_root / M.MANIFEST_FILENAME
    if cur.is_file():
        found.append(cur)
    found += sorted(project_root.glob("manifest.corrupt-*.json"))
    return found


def import_project(project_root: str | Path) -> dict:
    """Merge every manifest JSON in ``project_root`` into its live store.
    Returns the summary from :func:`src.manifest.merge_manifest_sources`."""
    root = Path(project_root).resolve()
    return M.merge_manifest_sources(root, manifest_sources(root))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project_root", help="experiment folder holding manifest.json")
    ap.add_argument("--export", default=None,
                    help="also write the merged manifest as JSON to this path")
    args = ap.parse_args(argv)

    root = Path(args.project_root).resolve()
    if not root.is_dir():
        print(f"not a folder: {root}")
        return 2
    sources = manifest_sources(root)
    if not sources:
        print(f"no manifest.json or manifest.corrupt-*.json found in {root}")
        return 1

    print(f"backend: {M._BACKEND}   project: {root}")
    res = import_project(root)
    print(f"merged (oldest first): {', '.join(res['sources']) or 'none'}")
    if res["skipped"]:
        print(f"skipped (unreadable): {', '.join(res['skipped'])}")
    for sect in ("files", "analyses", "background", "quality", "reactor_runs"):
        print(f"  {sect:<13}: +{res[sect]} added, {res['updated'][sect]} updated")
    c = M.manifest_counts(root)
    print(f"store now: {c['files']} files  {c['by_stage']}")
    print(f"runs     : {', '.join(M.manifest_runs(root)) or 'none'}")

    if args.export:
        m = M.load_manifest(M.manifest_path_for(root))
        Path(args.export).write_text(json.dumps(m, indent=2, default=str),
                                     encoding="utf-8")
        print(f"exported merged manifest -> {args.export}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
