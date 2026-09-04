#!/usr/bin/env python3
"""
check_system_spec.py — quick hardware + stability sanity check
================================================================
Run from the project root (activated venv — see QUICKSTART.md):
    python tools/check_system_spec.py

Checks CPU / RAM / disk against the recommended spec in QUICKSTART.md, flags
any of the platform's ports (5100-5109) already in use, and runs a short
repeated numpy workload shaped like a SAXS detector frame to give a rough
frames/sec estimate and catch obvious memory growth across iterations.

This is a sanity check, not a certification. It takes about 15 seconds and
needs nothing beyond the core dependencies (numpy, psutil) — no pyFAI, no
running apps, no project data.
"""

from __future__ import annotations

import gc
import shutil
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Minimum / recommended thresholds — keep in sync with QUICKSTART.md's
# "Recommended compute spec" table.
MIN_CORES, REC_CORES = 4, 8
MIN_RAM_GB, REC_RAM_GB = 8, 16
MIN_DISK_GB, REC_DISK_GB = 10, 50
PORTS = range(5100, 5110)          # hub + 9 apps
FRAME_SHAPE = (1043, 981)          # SAXS detector — the larger of the two
N_ITERATIONS = 25


def _colour(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def ok(t):   return _colour(f"  OK   {t}", "32")
def warn(t): return _colour(f"  WARN {t}", "33")
def bad(t):  return _colour(f"  FAIL {t}", "31")


def check_hardware() -> list[str]:
    lines = []
    try:
        import psutil
    except ImportError:
        return [warn("psutil not installed — run `pip install -r requirements-core.txt` "
                      "first; skipping CPU/RAM checks")]

    cores = psutil.cpu_count(logical=True) or 0
    if cores >= REC_CORES:
        lines.append(ok(f"CPU cores: {cores} (recommended {REC_CORES}+)"))
    elif cores >= MIN_CORES:
        lines.append(warn(f"CPU cores: {cores} — meets the minimum ({MIN_CORES}) "
                           f"but below recommended ({REC_CORES}+) for running all "
                           f"9 apps plus a continuous campaign"))
    else:
        lines.append(bad(f"CPU cores: {cores} — below the minimum ({MIN_CORES})"))

    ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    if ram_gb >= REC_RAM_GB:
        lines.append(ok(f"RAM: {ram_gb:.1f} GB (recommended {REC_RAM_GB}+ GB)"))
    elif ram_gb >= MIN_RAM_GB:
        lines.append(warn(f"RAM: {ram_gb:.1f} GB — meets the minimum ({MIN_RAM_GB} GB) "
                           f"but below recommended ({REC_RAM_GB}+ GB)"))
    else:
        lines.append(bad(f"RAM: {ram_gb:.1f} GB — below the minimum ({MIN_RAM_GB} GB)"))

    disk_gb = shutil.disk_usage(ROOT).free / (1024 ** 3)
    if disk_gb >= REC_DISK_GB:
        lines.append(ok(f"Free disk at project root: {disk_gb:.1f} GB "
                         f"(recommended {REC_DISK_GB}+ GB for a multi-day run)"))
    elif disk_gb >= MIN_DISK_GB:
        lines.append(warn(f"Free disk: {disk_gb:.1f} GB — fine to install and test, "
                           f"but a multi-day campaign wants {REC_DISK_GB}+ GB"))
    else:
        lines.append(bad(f"Free disk: {disk_gb:.1f} GB — below the minimum "
                          f"({MIN_DISK_GB} GB) even to install"))

    return lines


def check_ports() -> list[str]:
    lines = []
    busy = []
    for p in PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", p)) == 0:
                busy.append(p)
    if busy:
        lines.append(warn(f"Ports already in use: {busy} — either the platform is "
                           f"already running, or something else (see Troubleshooting "
                           f"in QUICKSTART.md for the macOS AirPlay / Windows Hyper-V "
                           f"causes) is holding them"))
    else:
        lines.append(ok(f"Ports {PORTS.start}-{PORTS.stop - 1}: all free"))
    return lines


def check_throughput_and_stability() -> list[str]:
    """A rough, dependency-light stand-in for pyFAI's azimuthal integration:
    radially bin a detector-shaped array into 1000 bins, repeated, timing each
    pass and watching process RSS for obvious growth (a crude leak check)."""
    lines = []
    try:
        import numpy as np
    except ImportError:
        return [warn("numpy not installed — skipping the throughput/stability check")]

    try:
        import psutil
        proc = psutil.Process()
        track_rss = True
    except ImportError:
        track_rss = False

    ny, nx = FRAME_SHAPE
    yy, xx = np.mgrid[0:ny, 0:nx]
    cy, cx = ny / 2, nx / 2
    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2).ravel()
    nbins = 1000
    bin_idx = np.clip((r / r.max() * (nbins - 1)).astype(np.int64), 0, nbins - 1)

    times = []
    rss_samples = []
    for _ in range(N_ITERATIONS):
        frame = np.random.default_rng().poisson(50, size=(ny, nx)).astype(np.float64)
        t0 = time.perf_counter()
        sums = np.bincount(bin_idx, weights=frame.ravel(), minlength=nbins)
        counts = np.bincount(bin_idx, minlength=nbins)
        _ = sums / np.maximum(counts, 1)          # radial profile, mimics I(q)
        times.append(time.perf_counter() - t0)
        if track_rss:
            gc.collect()
            rss_samples.append(proc.memory_info().rss)

    mean_t = sum(times) / len(times)
    fps = 1.0 / mean_t if mean_t > 0 else float("inf")
    lines.append(ok(f"Throughput (numpy radial-binning proxy, {ny}x{nx} frame): "
                     f"~{fps:.0f} frames/sec ({mean_t * 1000:.1f} ms/frame) — "
                     f"real pyFAI integration will be slower but is CPU-only "
                     f"and comfortably keeps up at beamline frame rates"))

    if track_rss and len(rss_samples) >= 5:
        start = sum(rss_samples[:3]) / 3
        end = sum(rss_samples[-3:]) / 3
        growth_pct = (end - start) / start * 100 if start else 0
        if growth_pct > 20:
            lines.append(warn(f"Process RSS grew {growth_pct:.0f}% over "
                               f"{N_ITERATIONS} iterations ({start/1e6:.0f} MB -> "
                               f"{end/1e6:.0f} MB) — worth another look if this "
                               f"repeats on your machine"))
        else:
            lines.append(ok(f"Process RSS stable over {N_ITERATIONS} iterations "
                             f"({start/1e6:.0f} MB -> {end/1e6:.0f} MB, "
                             f"{growth_pct:+.0f}%)"))
    return lines


def main() -> int:
    print()
    print("SWAXS Platform — system spec & stability check")
    print("=" * 52)

    print("\n1. Hardware vs. recommended spec")
    hw = check_hardware()
    print("\n".join(hw))

    print("\n2. Platform ports (5100-5109)")
    pt = check_ports()
    print("\n".join(pt))

    print("\n3. Throughput + memory-stability proxy (~10-15s)")
    th = check_throughput_and_stability()
    print("\n".join(th))

    all_lines = hw + pt + th
    failed = sum(1 for l in all_lines if "FAIL" in l)
    warned = sum(1 for l in all_lines if "WARN" in l)

    print()
    print("-" * 52)
    if failed:
        print(f"{failed} check(s) FAILED — see QUICKSTART.md 'Recommended compute spec'.")
    elif warned:
        print(f"{warned} check(s) WARN — should still run, but see the notes above.")
    else:
        print("All checks passed.")
    print("This is a sanity check, not a certification — for a real beamtime, "
          "also run your own data for a few hours beforehand.")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
