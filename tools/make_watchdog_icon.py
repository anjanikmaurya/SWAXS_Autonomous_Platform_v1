#!/usr/bin/env python3
"""
make_watchdog_icon.py — hub card / tab icon for Auto Watch.

Auto Watch was the one app still carrying a stock emoji (🐕) after the
scattering-icon pass, which the Auto Watch audit recorded as W25. A dog says
nothing about what the app does; it watches the loop produce curves and raises
an alarm when one stops arriving.

So: the same monodisperse nanoparticle SAXS profile the other icons use — the
thing being watched — in the app's accent red, ending in a large dot at the
newest point. The curve is the loop's output; the dot is the frame being
watched right now.

Two earlier attempts were rejected by looking at the result downscaled, which
is the only test that matters here:

  * a faint dashed "expected but not arrived" continuation past the dot — it
    vanished at 16 px and read as dirt on the curve at 32;
  * a thin white ring around the dot — invisible on a light tab strip, and
    indistinguishable from the dot on a dark one.

What survived is the shape the analysis and average icons already prove works
at this size: plateau, knee, steep descent. NOW_FRAC is tuned so the cut lands
AFTER the knee — cutting before it (0.72) left a plain line-and-dot with no
scattering character at all.

Same conventions as the other generators in this folder, for the same reasons:

  * monodisperse sphere form factor with NO polydispersity term, so the minima
    stay sharp instead of smearing into a smooth slope;
  * transparent background — the hub card and the browser tab supply their own;
  * deliberately heavy strokes and oversized markers. This is rendered at
    34x34 px on the hub card and 16-32 px in a tab; anything with the line
    weight of a real log-log plot vanishes at that size.

    python tools/make_watchdog_icon.py [output.png]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

rng = np.random.default_rng(11)

BKG_FLOOR = 0.8
ACCENT = "#D32F2F"        # apps.yml watchdog color — the alarm colour
TRACE = "#f4a7a4"         # the data already in, lighter so ACCENT reads on top

#: Where the "now" dot sits along the curve, as a fraction of the q range.
#: 0.88 keeps the whole recognisable shape — plateau, knee, descent — behind
#: the dot. Lower (0.72) cuts before the knee and the glyph stops looking like
#: a scattering profile; higher (0.93) puts the dot on the vertical tail where
#: it merges with the line.
NOW_FRAC = 0.88


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
    I = noisy(truth, 0.13, rng)

    cut = int(len(q) * NOW_FRAC)

    fig = plt.figure(figsize=(2.0, 2.0), dpi=200)
    fig.patch.set_alpha(0.0)
    ax = fig.add_axes([0.06, 0.10, 0.88, 0.80])
    ax.patch.set_alpha(0.0)
    ax.axis("off")

    # The profile as far as it has arrived. Thick and full-frame, matching the
    # analysis and average icons — at 16 px the stroke IS the glyph.
    ax.loglog(q[:cut], I[:cut], color=TRACE, lw=2.4, alpha=0.9,
              solid_capstyle="round", zorder=2)
    ax.loglog(q[:cut], truth[:cut], color=ACCENT, lw=5.0,
              solid_capstyle="round", zorder=3)

    # The frame being watched right now. One big filled dot, no thin ring: a
    # 3 px ring is invisible at tab size and reads as dirt on the curve, and
    # the earlier dashed "expected" continuation was worse — it disappeared at
    # 16 px and looked like noise at 32.
    qn, In = q[cut - 1], truth[cut - 1]
    ax.loglog([qn], [In], "o", color=ACCENT, ms=24.0, mew=0, zorder=5)

    ax.set_xlim(q.min(), q.max())
    ax.set_ylim(BKG_FLOOR * 0.4, truth.max() * 1.6)
    fig.savefig(path, dpi=200, transparent=True)
    plt.close(fig)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out = (Path(sys.argv[1]) if len(sys.argv) > 1 else
           root / "watchdog" / "static" / "watchdog_icon.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    build(out)
    print(f"✓ wrote {out} ({out.stat().st_size:,} bytes)")
    print("  Also copy to hub/static/ — the hub serves its own static folder, "
          "not the app's.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
