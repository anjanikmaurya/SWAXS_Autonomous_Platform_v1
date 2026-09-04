#!/usr/bin/env python3
"""
make_background_icon.py — hub card icon for Background Subtraction.

Simulates a monodisperse nanoparticle SAXS profile (sphere form factor) as
the "sample" curve, a smooth featureless decay as the "buffer/background"
curve, both faint in the app's own accent purple, with the subtracted result
(here, just the sample shape — a real subtraction removes the featureless
buffer contribution) bold and near-white on top.

Transparent background (not a filled square): the hub card itself supplies
the background. Bold, thick strokes are deliberate — this renders at
34x34px on the hub card, and anything with the line weight of a full
log-log plot disappears at that size.

    python tools/make_background_icon.py [output.png]
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


def build(path: Path) -> None:
    q = np.logspace(np.log10(0.01), np.log10(1.0), 400)
    sample = monodisperse_curve(q)
    buffer_ = BKG_FLOOR + 0.30 * sample.max() * np.exp(-q * 3)   # smooth, featureless buffer

    fig = plt.figure(figsize=(2.0, 2.0), dpi=200)
    fig.patch.set_alpha(0.0)
    ax = fig.add_axes([0.06, 0.10, 0.88, 0.80])
    ax.patch.set_alpha(0.0)
    ax.axis("off")

    ax.loglog(q, buffer_, color="#b39ddb", lw=3.2, alpha=0.55, solid_capstyle="round")   # buffer, faint
    ax.loglog(q, sample, color="#7e57c2", lw=3.2, alpha=0.55, solid_capstyle="round")    # sample, faint
    ax.loglog(q, sample, color="#f5f0ff", lw=4.6, solid_capstyle="round")                # subtracted, bold on top

    ax.set_xlim(q.min(), q.max())
    ax.set_ylim(BKG_FLOOR * 0.4, sample.max() * 1.6)
    fig.savefig(path, dpi=200, transparent=True)
    plt.close(fig)


def main() -> int:
    out = (Path(sys.argv[1]) if len(sys.argv) > 1 else
           Path(__file__).resolve().parent.parent / "background" / "static" / "background_icon.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    build(out)
    print(f"✓ wrote {out} ({out.stat().st_size:,} bytes)")
    print("  Also copy to hub/static/ — the hub serves its own static folder, "
          "not the app's.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
