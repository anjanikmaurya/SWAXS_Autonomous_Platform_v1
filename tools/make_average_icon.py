#!/usr/bin/env python3
"""
make_average_icon.py — hub card icon for Visualisation & Average.

Simulates a monodisperse nanoparticle SAXS profile (sphere form factor), draws
a few noisy per-frame realizations in the app's own accent green, and the
averaged curve bold and near-white on top — averaging recovers the true curve
that single-frame counting noise obscures.

Transparent background (not a filled square): the hub card itself supplies
the background, and matching it exactly here would only break the moment
the theme changes. Bold, thick strokes and a handful of shapes are
deliberate — this renders at 34x34px on the hub card, and anything with the
line weight or point count of a full log-log plot disappears at that size.

    python tools/make_average_icon.py [output.png]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

rng = np.random.default_rng(7)
BKG_FLOOR = 0.8


def sphere_form_factor(q, R):
    """Monodisperse sphere form factor P(q) — no polydispersity term on
    purpose, since that's what would smear out the minima."""
    qR = q * R
    amp = 3.0 * (np.sin(qR) - qR * np.cos(qR)) / qR ** 3
    return amp ** 2


def monodisperse_curve(q, R=8.0, scale=1.0e6):
    return scale * sphere_form_factor(q, R) + BKG_FLOOR


def noisy(I, frac, rng):
    return np.clip(I * rng.normal(1.0, frac, size=I.shape), BKG_FLOOR * 0.5, None)


def build(path: Path) -> None:
    q = np.logspace(np.log10(0.01), np.log10(1.0), 400)
    truth = monodisperse_curve(q)

    fig = plt.figure(figsize=(2.0, 2.0), dpi=200)
    fig.patch.set_alpha(0.0)
    ax = fig.add_axes([0.06, 0.10, 0.88, 0.80])
    ax.patch.set_alpha(0.0)
    ax.axis("off")

    frame_colors = ["#2E7D32", "#43A047", "#66BB6A"]   # the app's own accent green
    avg = np.zeros_like(q)
    for c in frame_colors:
        I = noisy(truth, 0.20, rng)
        avg += I
        ax.loglog(q, I, color=c, lw=3.2, alpha=0.55, solid_capstyle="round")
    avg /= len(frame_colors)
    ax.loglog(q, avg, color="#f2fff2", lw=4.6, solid_capstyle="round")   # the average, on top, bold

    ax.set_xlim(q.min(), q.max())
    ax.set_ylim(BKG_FLOOR * 0.4, truth.max() * 1.6)
    fig.savefig(path, dpi=200, transparent=True)
    plt.close(fig)


def main() -> int:
    out = (Path(sys.argv[1]) if len(sys.argv) > 1 else
           Path(__file__).resolve().parent.parent / "average" / "static" / "average_icon.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    build(out)
    print(f"✓ wrote {out} ({out.stat().st_size:,} bytes)")
    print("  Also copy to hub/static/ — the hub serves its own static folder, "
          "not the app's.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
