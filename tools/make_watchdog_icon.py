#!/usr/bin/env python3
"""
make_watchdog_icon.py — hub card / tab icon for Auto Watch.

This used to DRAW the icon: a SAXS profile ending in a dot, in the app's red.
It is now a recolour of supplied artwork — an eye with a power symbol and two
circuit nodes, with a single green status dot — kept as a script rather than a
hand-edited PNG so the transform is reproducible and reviewable.

    watchdog/static/watchdog_icon_source.png   the artwork as supplied (blue)
    watchdog/static/watchdog_icon.png          what ships: white + green dot

The transform: every visible pixel becomes white EXCEPT the green dot, which
keeps its colour. Alpha is preserved untouched, so the antialiased edges stay
smooth and the background stays transparent.

Why white: the hub is dark-only (--surface #161b22, no theme toggle), so white
line art reads cleanly on the card. The green dot is the one element carrying
meaning — "watching, alive" — and is the only thing that should pull the eye.

CAVEAT, and the reason this docstring exists. The same PNG is served at
/app-icon as the BROWSER TAB icon (src/favicon.py), and a tab strip is light in
light mode. White-on-transparent is close to invisible there. That is a real
trade the hub card wins, because the card is where the icon is actually read;
if the tab matters more later, give the favicon its own dark rounded-square
tile rather than tinting this one, since a mid-grey compromise would be muddy
on both.

The green is detected by hue (g > r+40 and g > b+40), not by matching an exact
RGB — the dot is antialiased, so its edge pixels are dozens of slightly
different greens and an equality test would leave a white fringe around it.

    python tools/make_watchdog_icon.py [output.png]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

#: Below this alpha a pixel is background or invisible antialiasing; recolouring
#: it would turn the transparent halo into a white one.
_ALPHA_FLOOR = 8

#: How much greener than red/blue a pixel must be to count as the status dot.
_GREEN_MARGIN = 40

#: The artwork is supplied at 1254 px. It is rendered at 34 px on the hub card
#: and 16-32 px in a tab, so shipping the full size cost 124 KB to display a
#: thumbnail — every page load, for pixels no one sees. 256 px leaves plenty of
#: headroom for a 2x display and is an eighth of the bytes. Downscaling happens
#: AFTER the recolour: doing it first would blend the green dot's edge into the
#: surrounding blue and the hue test would then whiten the blend.
_SHIP_PX = 256


def recolour(src: Path) -> Image.Image:
    """White line art, green dot preserved, alpha untouched."""
    a = np.array(Image.open(src).convert("RGBA"))
    r, g, b, alpha = (a[..., 0].astype(int), a[..., 1].astype(int),
                      a[..., 2].astype(int), a[..., 3])

    visible = alpha > _ALPHA_FLOOR
    green = visible & (g > r + _GREEN_MARGIN) & (g > b + _GREEN_MARGIN)
    to_white = visible & ~green

    out = a.copy()
    out[..., 0][to_white] = 255
    out[..., 1][to_white] = 255
    out[..., 2][to_white] = 255
    return Image.fromarray(out, "RGBA"), int(green.sum()), int(to_white.sum())


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    src = root / "watchdog" / "static" / "watchdog_icon_source.png"
    if not src.is_file():
        print(f"✗ missing source artwork: {src}")
        return 1
    out = (Path(sys.argv[1]) if len(sys.argv) > 1 else
           root / "watchdog" / "static" / "watchdog_icon.png")

    img, n_green, n_white = recolour(src)
    if img.width > _SHIP_PX:
        img = img.resize((_SHIP_PX, _SHIP_PX), Image.LANCZOS)
    if not n_green:
        # Silently shipping an all-white icon would lose the only element that
        # carries meaning, and it would not be obvious at 34 px.
        print("✗ no green pixels found — the source artwork changed, or the "
              "hue test needs adjusting. Refusing to write an all-white icon.")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"✓ wrote {out} ({out.stat().st_size:,} bytes)")
    print(f"  {n_white:,} px → white, {n_green:,} px kept green")

    # The hub serves its OWN static folder (Flask(__name__)), so an app's
    # icon_image is not reachable from a hub card unless it is copied there.
    # tests/test_favicons.py asserts this; it is the step that gets missed.
    hub_copy = root / "hub" / "static" / out.name
    hub_copy.parent.mkdir(parents=True, exist_ok=True)
    img.save(hub_copy)
    print(f"✓ copied to {hub_copy}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
