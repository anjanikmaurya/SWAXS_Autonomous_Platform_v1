#!/usr/bin/env python3
"""
plot_beam_stability.py — I0 & bstop over time for each averaged sample.

For every averaged sample in a project's manifest, find its constituent reduced
frames (same detector + sample/x-position, excluding the dark set), order them by
acquisition timestamp, and plot the incident (I0) and transmitted (bstop) beam
monitors vs elapsed time. Useful for spotting beam drift / dosing trends.

    uv run tools/plot_beam_stability.py /path/to/project [out.png]
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

XTOK = re.compile(r"x-?[\d.]+")


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def frames_for(reduced, keyword: str, detector: str):
    """Reduced frames belonging to an averaged sample, sorted by timestamp."""
    m = XTOK.search(keyword)
    xtok = m.group(0) if m else None
    base = keyword[: m.start()] if m else keyword
    toks = [t for t in base.split("_") if t]
    want_dark = "dark" in keyword.lower()
    rows = []
    for v in reduced:
        if v.get("detector") != detector:
            continue
        name = os.path.basename(v["path"])
        if xtok and xtok not in name:
            continue
        if not all(t in name for t in toks):
            continue
        if ("dark" in name.lower()) != want_dark:
            continue
        md = v.get("metadata") or {}
        if md.get("i0") is None or md.get("bstop") is None:
            continue
        rows.append((_ts((v.get("provenance") or {}).get("timestamp", "")),
                     float(md["i0"]), float(md["bstop"])))
    rows.sort()
    return rows


def short(kw: str) -> str:
    return kw.replace("Run1_", "").replace("_PES_support", "")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "QC_I0_bstop_over_time.png"
    mf = json.loads((root / "manifest.json").read_text())
    files = mf.get("files", {})
    reduced = [v for v in files.values() if v.get("stage") == "reduced"]
    averaged = [v for v in files.values() if v.get("stage") == "averaged"]
    if not averaged:
        print("No averaged samples in manifest.")
        return 1

    groups = {"saxs": [], "waxs": []}
    for v in averaged:
        det = v.get("detector", "saxs")
        rows = frames_for(reduced, v["keyword"], det)
        groups.setdefault(det, []).append((short(v["keyword"]), rows))

    fig, ax = plt.subplots(2, 2, figsize=(13, 8), sharex=True)
    cmap = plt.get_cmap("tab10")
    for col, det in enumerate(("saxs", "waxs")):
        for i, (lab, rows) in enumerate(sorted(groups.get(det, []))):
            if not rows:
                continue
            t0 = rows[0][0]
            t = [r[0] - t0 for r in rows]          # elapsed seconds
            i0 = [r[1] for r in rows]
            bs = [r[2] for r in rows]
            c = cmap(i % 10)
            ax[0, col].plot(t, i0, marker="o", ms=3, lw=1.2, color=c, label=lab)
            ax[1, col].plot(t, bs, marker="o", ms=3, lw=1.2, color=c, label=lab)
        ax[0, col].set_title(f"{det.upper()} — averaged samples", fontsize=12, fontweight="bold")
        ax[1, col].set_xlabel("Elapsed time within scan (s)")
    ax[0, 0].set_ylabel("I₀  (incident monitor)")
    ax[1, 0].set_ylabel("bstop  (transmitted monitor)")
    for a in ax.flat:
        a.grid(alpha=0.3)
        a.tick_params(labelsize=9)
    for r in (0, 1):
        ax[r, 1].legend(fontsize=7, loc="center left",
                        bbox_to_anchor=(1.01, 0.5), title="Sample")
    fig.suptitle("Beam stability: I₀ and bstop over time, per averaged sample",
                 fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out, dpi=130, bbox_inches="tight")
    print(f"✓ saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
