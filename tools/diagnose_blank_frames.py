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


#: Frames shown per acquisition. Frames within one acquisition are near
#: identical, so a handful is enough — but the cap is applied PER ACQUISITION,
#: never across the whole match. A flat [:10] silently showed ten frames of
#: whichever acquisition sorted first and hid every other one, which is how a
#: run for "r006" reported on Run12_r006 and declared the data healthy while
#: Run20_r006 — the actual failure — was never opened.
_PER_ACQ = 4


def acquisition_of(name: str) -> str:
    """'Run20_r006_bkg' from 'Run20_r006_bkg_scan1_0003_SAXS.dat'.

    The _scanN_NNNN suffix is the frame index; everything before it names the
    acquisition, and a project folder accumulates many of them across runs.
    """
    stem = name
    for suffix in (".raw", ".dat", ".pdi", ".csv"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    for det in ("_SAXS", "_WAXS"):
        if stem.endswith(det):
            stem = stem[: -len(det)]
    parts = stem.split("_")
    for i, p in enumerate(parts):
        if p.startswith("scan") and p[4:].isdigit():
            return "_".join(parts[:i])
    return stem


def _by_acquisition(paths: list[Path]) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for p in sorted(paths):
        groups.setdefault(acquisition_of(p.name), []).append(p)
    return groups


# ── stage 1: the 2D frames ──────────────────────────────────────────────────
def check_raw(two_d: Path, tag: str, shape=None) -> list[dict]:
    out = []
    for det in ("SAXS", "WAXS"):
        d = two_d / det
        if not d.is_dir():
            continue
        for acq, files in _by_acquisition(list(d.glob(f"*{tag}*.raw"))).items():
            for f in files[:_PER_ACQ]:
                a = np.fromfile(f, dtype=np.int32)
                out.append({
                    "file": f.name, "acq": acq, "det": det, "n": a.size,
                    "of": len(files),
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
                all_rows = list(csv.DictReader(fh))
            for r in all_rows[:_PER_ACQ]:
                rows.append({"csv": csv_path.name, "of": len(all_rows), **r})
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
        for acq, files in _by_acquisition(list(d.glob(f"*{tag}*.dat"))).items():
            for f in files[:_PER_ACQ]:
                try:
                    a = np.loadtxt(f)
                    if a.ndim != 2 or a.shape[1] < 3:
                        out.append({"file": f.name, "acq": acq, "det": det,
                                    "of": len(files),
                                    "note": f"unexpected shape {a.shape}"})
                        continue
                    q, I, s = a[:, 0], a[:, 1], a[:, 2]
                    valid = ((q > 0) & (I > 0) & np.isfinite(q)
                             & np.isfinite(I) & np.isfinite(s))
                    out.append({
                        "file": f.name, "acq": acq, "det": det, "rows": len(q),
                        "of": len(files),
                        "valid": int(valid.sum()),      # the average app's rule
                        "I_min": _fmt(np.nanmin(I)), "I_max": _fmt(np.nanmax(I)),
                        "I_positive_pct": 100.0 * (I > 0).mean(),
                        "sigma_finite_pct": 100.0 * np.isfinite(s).mean(),
                    })
                except Exception as exc:
                    out.append({"file": f.name, "acq": acq, "det": det,
                                "of": len(files), "note": f"unreadable: {exc}"})
    return out


def report(project: Path, tag: str) -> dict:
    two_d, one_d = project / "2D", project / "1D"
    raw, ctr, dat = (check_raw(two_d, tag), check_counters(two_d, tag),
                     check_dat(one_d, tag))

    print(f"\n{'='*72}\n  {tag}\n{'='*72}")

    # A project folder accumulates every run, so a loose tag like "r006" hits
    # Run10_r006, Run12_r006, Run20_r006 … Say which acquisitions were opened,
    # up front: reporting on the wrong run and calling the data healthy is the
    # one failure mode that wastes the most time.
    acqs = sorted({r["acq"] for r in raw} | {r["acq"] for r in dat})
    if len(acqs) > 1:
        print(f"\n  ⚠ '{tag}' matches {len(acqs)} acquisitions: {', '.join(acqs)}")
        print("    All are shown below. Pass a more specific tag "
              "(e.g. 'Run20_r006') to look at just one.")
    elif acqs:
        print(f"\n  acquisition: {acqs[0]}")

    print(f"\n  2D .raw")
    if not raw:
        print("    none found — wrong tag, or the frames were never written")
    for r in raw:
        flag = "  ← BLANK" if r["nonzero_pct"] == 0 else ""
        print(f"    {r['file']:<46} {r['nonzero_pct']:6.2f}% nonzero  "
              f"max={r['max']:<8}{flag}")
    for acq, n in sorted({r["acq"]: r["of"] for r in raw}.items()):
        if n > _PER_ACQ:
            print(f"    … {acq}: showing {_PER_ACQ} of {n} frames")

    print(f"\n  counters")
    if not ctr:
        print("    no matching CSV in 2D/ — reduction would have nothing to "
              "normalise by")
    for r in ctr:
        if "error" in r:
            print(f"    {r['csv']}: {r['error']}")
            continue
        i0, bs = r.get("i0"), r.get("bstop")
        try:
            t = f"  T={float(bs)/float(i0):.4f}" if float(i0) else "  T=n/a (i0=0)"
        except (TypeError, ValueError, ZeroDivisionError):
            t = "  T=?"
        print(f"    {r['csv']:<30} i0={_fmt(i0):<11} bstop={_fmt(bs):<11}{t}")

    print(f"\n  1D .dat")
    if not dat:
        print("    none found — the frames were never reduced")
    for r in dat:
        if "note" in r:
            print(f"    {r['file']:<46} {r['note']}")
            continue
        flag = "  ← NO USABLE POINTS" if r["valid"] < 3 else ""
        print(f"    {r['file']:<46} valid={r['valid']:>5}/{r['rows']:<5} "
              f"I={r['I_min']}…{r['I_max']}  σ fin {r['sigma_finite_pct']:.0f}%{flag}")
    for acq, n in sorted({r["acq"]: r["of"] for r in dat}.items()):
        if n > _PER_ACQ:
            print(f"    … {acq}: showing {_PER_ACQ} of {n} profiles")

    return {"raw": raw, "counters": ctr, "dat": dat, "acqs": acqs}


def verdict(bad: dict, tag: str) -> None:
    # Per-acquisition, not across the whole match: with several runs in one
    # folder, a healthy Run12_r006 would otherwise average away a dead
    # Run20_r006 and the verdict would read "nothing conclusive".
    for acq in bad.get("acqs") or []:
        raws = [r for r in bad["raw"] if r["acq"] == acq]
        dats = [r for r in bad["dat"] if r["acq"] == acq]
        if dats and all(r.get("valid", 0) < 3 for r in dats):
            _verdict_one(acq, raws, dats)
            return
    raw_blank = bool(bad["raw"]) and all(r["nonzero_pct"] == 0 for r in bad["raw"])
    dat_dead = bool(bad["dat"]) and all(r.get("valid", 0) < 3 for r in bad["dat"])

    print(f"\n{'─'*72}\n  verdict for {tag}\n{'─'*72}")
    _print_verdict(bad, raw_blank, dat_dead)


def _verdict_one(acq: str, raws: list, dats: list) -> None:
    print(f"\n{'─'*72}\n  verdict — {acq} is the dead one\n{'─'*72}")
    _print_verdict({"raw": raws, "dat": dats},
                   bool(raws) and all(r["nonzero_pct"] == 0 for r in raws),
                   True)


def _print_verdict(bad: dict, raw_blank: bool, dat_dead: bool) -> None:
    if not bad["raw"] and dat_dead:
        print("  The .dat files exist and are dead, but their 2D frames are GONE.\n"
              "  Reduction cannot have produced these from nothing, so the .raw\n"
              "  files were deleted or moved after reduction ran — or written to a\n"
              "  different 2D folder than the one being searched. Check\n"
              "  spec.mock_data_dir and data_directory in config.yml.")
    elif not bad["raw"]:
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
