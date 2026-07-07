#!/usr/bin/env python3
"""
make_saxs_loader.py — realistic SAXS detector frame for the "collecting live"
loader (dark beamstop at center, Debye-Scherrer rings + photon speckle, intensity
decaying outward). No face features — this is meant to read as real data.

    uv run tools/make_saxs_loader.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

N = 320
rng = np.random.default_rng(3)


def build() -> np.ndarray:
    ax = np.linspace(-1.0, 1.0, N)
    xx, yy = np.meshgrid(ax, ax)
    r = np.sqrt(xx ** 2 + yy ** 2)

    # bright low-q halo just outside the beamstop, decaying outward (I ~ q^-n)
    img = 60.0 / (r * 6.0 + 0.06) ** 2.2
    img += 5.0

    # several Debye-Scherrer rings, fading with radius
    for rr, amp in ((0.30, 26), (0.46, 20), (0.62, 14), (0.80, 9), (0.95, 6)):
        img += amp * np.exp(-((r - rr) ** 2) / (2 * 0.020 ** 2))

    # photon speckle (multiplicative) + faint read noise
    img *= rng.gamma(shape=16.0, scale=1 / 16.0, size=img.shape)
    img += rng.normal(0, 1.0, img.shape).clip(min=0)

    # central beamstop shadow + support arm coming up from the bottom
    beamstop = (r < 0.07)
    arm = (np.abs(xx) < 0.022) & (yy < 0.0)
    img[beamstop | arm] = 0.18

    return np.clip(img, 0.18, None)


def main() -> int:
    out = (Path(sys.argv[1]) if len(sys.argv) > 1 else
           Path(__file__).resolve().parent.parent / "assistant" / "static" / "saxs_loader.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = build()
    fig = plt.figure(figsize=(3.2, 3.2), dpi=100)
    a = fig.add_axes([0, 0, 1, 1])
    a.imshow(img, origin="lower", cmap="inferno",
             norm=LogNorm(vmin=img.min(), vmax=img.max() * 0.9),
             interpolation="bilinear")
    a.axis("off")
    fig.savefig(out, dpi=100)
    plt.close(fig)
    print(f"✓ wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
