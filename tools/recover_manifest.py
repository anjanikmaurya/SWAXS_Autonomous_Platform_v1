#!/usr/bin/env python3
"""
recover_manifest.py — restore a SWAXS manifest.json from a corrupt backup
=========================================================================
The platform's self-heal renames a damaged ``manifest.json`` to
``manifest.corrupt-<timestamp>.json`` and starts a fresh empty index. That does
NOT touch your experiment data (.raw/.dat files) — only the JSON catalog that
tracks what has been processed. This script salvages the valid content from the
newest corrupt backup and writes it back to ``manifest.json``.

Usage
-----
    uv run tools/recover_manifest.py /path/to/project_root
    uv run tools/recover_manifest.py /path/to/project_root --dry-run

If no path is given it uses the current working directory.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def salvage(text: str) -> dict | None:
    """Recover the first complete JSON object, ignoring trailing garbage."""
    text = text.lstrip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) and obj else None


def _count(d: dict) -> str:
    files = d.get("files", {})
    return (f"{len(files)} files, "
            f"{len(d.get('background', {}))} background, "
            f"{len(d.get('analyses', {}))} analyses, "
            f"{len(d.get('events', []))} events")


def main() -> int:
    ap = argparse.ArgumentParser(description="Recover manifest.json from a corrupt backup.")
    ap.add_argument("project_root", nargs="?", default=".",
                    help="Experiment folder containing manifest.json (default: cwd)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be recovered without writing.")
    args = ap.parse_args()

    root = Path(args.project_root).resolve()
    if not root.is_dir():
        print(f"✗ Not a directory: {root}")
        return 2

    backups = sorted(root.glob("manifest.corrupt-*.json"))
    if not backups:
        print(f"✗ No manifest.corrupt-*.json found in {root}")
        return 1

    print(f"Found {len(backups)} corrupt backup(s):")
    for b in backups:
        print(f"   • {b.name}  ({b.stat().st_size:,} bytes)")

    # Pick the one that salvages the MOST content (usually the newest/largest).
    best: tuple[int, Path, dict] | None = None
    for b in backups:
        rec = salvage(b.read_text())
        if rec is None:
            print(f"   – {b.name}: nothing salvageable")
            continue
        score = len(rec.get("files", {})) + len(rec.get("background", {})) + len(rec.get("analyses", {}))
        print(f"   ✓ {b.name}: salvageable → {_count(rec)}")
        if best is None or score > best[0]:
            best = (score, b, rec)

    if best is None:
        print("✗ Could not salvage any usable manifest content.")
        return 1

    _, src, recovered = best
    target = root / "manifest.json"
    print(f"\nBest source: {src.name} → {_count(recovered)}")

    # Compare with the current (likely empty) manifest.
    if target.exists():
        try:
            cur = json.loads(target.read_text())
            print(f"Current manifest.json: {_count(cur)}")
        except Exception:
            print("Current manifest.json: present but unreadable")

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    # Back up whatever manifest.json currently is, then write the recovered one.
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        pre = root / f"manifest.pre-recovery-{stamp}.json"
        shutil.copy2(target, pre)
        print(f"Backed up current manifest.json → {pre.name}")

    tmp = target.with_suffix(".json.tmp")
    with tmp.open("w") as fh:
        json.dump(recovered, fh, indent=2)
    tmp.replace(target)
    print(f"✓ Recovered manifest written to {target}")
    print("  Restart the apps; your processed-file history should be back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
