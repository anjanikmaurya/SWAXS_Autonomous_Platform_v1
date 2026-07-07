#!/usr/bin/env python3
"""
make_saxs_smiley.py — generate a synthetic SAXS-style 2D detector image whose
bright scattering features form a smiley face. Used as the AI Assistant icon.

    uv run tools/make_saxs_smiley.py [output.png]

Look & feel: dark detector background, faint Debye-Scherrer rings, photon
speckle, a small central beamstop, and high-intensity Gaussian "features"
(two eyes + a curved smile) rendered through a log-scaled `inferno` colormap.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

N = 512                      # detector pixels (square)
rng = np.random.default_rng(7)


def gaussian_blob(xx, yy, cx, cy, amp, sigma):
    return amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2)))


def build_image() -> np.ndarray:
    ax = np.linspace(-1.0, 1.0, N)
    xx, yy = np.meshgrid(ax, ax)
    r = np.sqrt(xx ** 2 + yy ** 2)

    # ── Detector background: bright direct beam + steep radial decay (I ~ q^-n)
    img = 50.0 / (r * 6.0 + 0.05) ** 2.2
    img += 6.0                                   # diffuse background floor

    # ── Faint Debye-Scherrer rings (powder-like) ──────────────────────────────
    for rr in (0.42, 0.66, 0.88):
        img += 14.0 * np.exp(-((r - rr) ** 2) / (2 * 0.018 ** 2))

    # ── Smiley features (the "scattering" that forms the face) ────────────────
    # Eyes
    img += gaussian_blob(xx, yy, -0.34, 0.34, 320, 0.085)
    img += gaussian_blob(xx, yy,  0.34, 0.34, 320, 0.085)
    # Smile: Gaussians along an upward-opening parabola (a grin)
    for t in np.linspace(-0.55, 0.55, 26):
        cx = t
        cy = -0.18 + 0.95 * t ** 2 - 0.30        # curve up at the corners
        img += gaussian_blob(xx, yy, cx, cy, 150, 0.055)

    # ── Photon speckle (multiplicative) + read noise (additive) ───────────────
    img *= rng.gamma(shape=18.0, scale=1 / 18.0, size=img.shape)   # ~unit-mean speckle
    img += rng.normal(0, 1.2, img.shape).clip(min=0)

    # ── Central beamstop shadow + support arm (classic SAXS) ──────────────────
    beamstop = (r < 0.05)
    arm = (np.abs(xx) < 0.018) & (yy < 0.0)       # arm coming up from the bottom
    img[beamstop | arm] = 0.20

    return np.clip(img, 0.2, None)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).resolve().parent.parent / "assistant" / "static" / "saxs_smiley.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    img = build_image()
    fig = plt.figure(figsize=(5.12, 5.12), dpi=100)
    a = fig.add_axes([0, 0, 1, 1])
    a.imshow(img, origin="lower", cmap="inferno",
             norm=LogNorm(vmin=img.min(), vmax=img.max() * 0.9),
             interpolation="bilinear")
    a.axis("off")
    fig.savefig(out, dpi=100)
    plt.close(fig)
    print(f"✓ wrote {out} ({out.stat().st_size:,} bytes, {img.shape[0]}x{img.shape[1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
