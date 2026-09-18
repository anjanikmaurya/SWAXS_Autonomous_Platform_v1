#!/usr/bin/env python3
"""
diagnose_blank_frames.py — find the stage where a condition went to I = 0.

Written after Run20, where r001–r005 averaged normally and then both r006
lanes produced .dat files with no positive intensity, so the average app
consumed twenty frames and wrote nothing:

    ⚠ Run20_r006_bkg [saxs] batch 1: no usable frames — skipped

The averaging app was the messenger. The question this answers is which
earlier stage produced the zeros, by walking one condition through all three
and comparing it against a condition that worked:

    2D .raw  →  counters (CSV/PDI)  →  1D .dat

Each stage can only fail in a few ways, and they point at different code:

  * .raw all zero            → the collector/simulator, or the real detector
                               (shutter, beamstop mask covering the frame)
  * .raw fine, i0/bstop odd  → metadata: normalization divides by these, and a
                               huge bstop drives I toward zero without ever
                               making it negative (which would be caught)
  * .raw and counters fine,
    .dat zero                → reduction itself: mask, radial_range, dummy /
                               delta_dummy, or the normalization mode

Usage
-----
    python tools/diagnose_blank_frames.py <project_root> r006 [r005]

The third argument is an optional known-good condition to compare against;
without it the numbers are still printed, just with nothing to contrast.
Read-only — it never writes to the project folder.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fmt(x, nd=4):
    try:
        return f"{float(x):.{nd}g}"
    except (TypeError, ValueError):
        return str(x)


# ── stage 1: the 2D frames ──────────────────────────────────────────────────
def check_raw(two_d: Path, tag: str, shape=None) -> list[dict]:
    out = []
    for det in ("SAXS", "WAXS"):
        d = two_d / det
        if not d.is_dir():
            continue
        for f in sorted(d.glob(f"*{tag}*.raw"))[:10]:
            a = np.fromfile(f, dtype=np.int32)
            out.append({
                "file": f.name, "det": det, "n": a.size,
                "nonzero_pct": 100.0 * (a > 0).mean() if a.size else 0.0,
                "max": int(a.max()) if a.size else 0,
                "sum": int(a.sum()) if a.size else 0,
            })
    return out


# ── stage 2: the counters reduction normalises by ───────────────────────────
def check_counters(two_d: Path, tag: str) -> list[dict]:
    """i0 / bstop per frame, from the experiment CSV (which lives INSIDE 2D/,
    not at the project root — src/reduction/process_metadata.py globs
    raw_file.parent.parent)."""
    rows = []
    for csv_path in sorted(two_d.glob("*.csv")):
        if tag not in csv_path.name:
            continue
        try:
            with csv_path.open(newline="", encoding="utf-8") as fh:
                for i, r in enumerate(csv.DictReader(fh)):
                    if i >= 10:
                        break
                    rows.append({"csv": csv_path.name, **r})
        except Exception as exc:
            rows.append({"csv": csv_path.name, "error": str(exc)})
    return rows


# ── stage 3: the reduced 1D profiles ────────────────────────────────────────
def check_dat(one_d: Path, tag: str) -> list[dict]:
    out = []
    for det in ("SAXS", "WAXS"):
        d = one_d / det / "Reduction"
        if not d.is_dir():
            continue
        for f in sorted(d.glob(f"*{tag}*.dat"))[:10]:
            try:
                a = np.loadtxt(f)
                if a.ndim != 2 or a.shape[1] < 3:
                    out.append({"file": f.name, "det": det,
                                "note": f"unexpected shape {a.shape}"})
                    continue
                q, I, s = a[:, 0], a[:, 1], a[:, 2]
                valid = ((q > 0) & (I > 0) & np.isfinite(q)
                         & np.isfinite(I) & np.isfinite(s))
                out.append({
                    "file": f.name, "det": det, "rows": len(q),
                    "valid": int(valid.sum()),          # the average app's rule
                    "I_min": _fmt(np.nanmin(I)), "I_max": _fmt(np.nanmax(I)),
                    "I_positive_pct": 100.0 * (I > 0).mean(),
                    "sigma_finite_pct": 100.0 * np.isfinite(s).mean(),
                })
            except Exception as exc:
                out.append({"file": f.name, "det": det, "note": f"unreadable: {exc}"})
    return out


def report(project: Path, tag: str) -> dict:
    two_d, one_d = project / "2D", project / "1D"
    raw, ctr, dat = (check_raw(two_d, tag), check_counters(two_d, tag),
                     check_dat(one_d, tag))

    print(f"\n{'='*72}\n  {tag}\n{'='*72}")

    print(f"\n  2D .raw  ({len(raw)} file(s))")
    if not raw:
        print("    none found — wrong tag, or the frames were never written")
    for r in raw:
        flag = "  ← BLANK" if r["nonzero_pct"] == 0 else ""
        print(f"    {r['file']:<44} {r['nonzero_pct']:6.2f}% nonzero  "
              f"max={r['max']:<10}{flag}")

    print(f"\n  counters ({len(ctr)} row(s))")
    for r in ctr[:5]:
        if "error" in r:
            print(f"    {r['csv']}: {r['error']}")
            continue
        i0, bs = r.get("i0"), r.get("bstop")
        try:
            t = f"  T={float(bs)/float(i0):.4f}" if float(i0) else "  T=n/a (i0=0)"
        except (TypeError, ValueError, ZeroDivisionError):
            t = "  T=?"
        print(f"    i0={_fmt(i0):<12} bstop={_fmt(bs):<12}{t}")

    print(f"\n  1D .dat  ({len(dat)} file(s))")
    for r in dat:
        if "note" in r:
            print(f"    {r['file']:<44} {r['note']}")
            continue
        flag = "  ← NO USABLE POINTS" if r["valid"] < 3 else ""
        print(f"    {r['file']:<44} valid={r['valid']:>5}/{r['rows']:<5} "
              f"I={r['I_min']}…{r['I_max']}  σ finite {r['sigma_finite_pct']:.0f}%{flag}")

    return {"raw": raw, "counters": ctr, "dat": dat}


def verdict(bad: dict, tag: str) -> None:
    raw_blank = bool(bad["raw"]) and all(r["nonzero_pct"] == 0 for r in bad["raw"])
    dat_dead = bool(bad["dat"]) and all(r.get("valid", 0) < 3 for r in bad["dat"])

    print(f"\n{'─'*72}\n  verdict for {tag}\n{'─'*72}")
    if not bad["raw"]:
        print("  No 2D frames found for this tag — check the tag spelling, or\n"
              "  the acquisition never ran. Nothing downstream can be blamed.")
    elif raw_blank:
        print("  The 2D frames themselves are blank. Reduction and averaging are\n"
              "  innocent — look at the collector (src/simulator/collector.py for\n"
              "  a mock run, the shutter/beamstop for real beam).")
    elif dat_dead:
        print("  2D frames have counts but the .dat files have no usable points,\n"
              "  so the zeros were introduced BY REDUCTION. In order of likelihood:\n"
              "    1. normalization — a huge bstop/i0 drives I toward zero without\n"
              "       ever going negative, so nothing upstream complains;\n"
              "    2. the mask or radial_range excluding the whole detector;\n"
              "    3. dummy / delta_dummy marking every pixel invalid.\n"
              "  Compare the counters above against the known-good condition.")
    else:
        print("  Nothing conclusive: the frames and profiles both look usable.\n"
              "  If the average app still dropped the batch, re-run it now — its\n"
              "  log names the reason directly (average/knowledge.md).")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    project = Path(argv[1]).expanduser()
    if not project.is_dir():
        print(f"not a directory: {project}")
        return 2
    bad_tag = argv[2]
    bad = report(project, bad_tag)
    if len(argv) > 3:
        report(project, argv[3])
    verdict(bad, bad_tag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
