"""
src/loop_naming.py — filename convention shared across the closed loop.

The reactor names its two per-condition acquisitions:
    {recipe_id}_sample_<scan tokens>_NNNN   (during synthesis)
    {recipe_id}_bkg_<scan tokens>_NNNN      (during flush)

This module is the single source of truth for splitting those names, so the
automated averaging (viewer) groups sample vs background into separate averages,
and the background subtraction pairs them by the shared recipe_id.
"""

from __future__ import annotations

import re

# Longest-first so 'background' wins over 'bg', etc.
ROLE_TAGS = ("sample", "background", "solvent", "buffer", "blank",
             "empty", "water", "bkg", "bg")
BKG_TAGS = ("background", "solvent", "buffer", "blank", "empty", "water", "bkg", "bg")

_ROLE_RE = re.compile(r"_(" + "|".join(ROLE_TAGS) + r")(?=[_.\-]|$)", re.IGNORECASE)


def split_role(name: str):
    """(recipe_id, role) from a filename, or (None, None) if no role tag.
    recipe_id is everything before the first role tag; role is lower-cased."""
    m = _ROLE_RE.search(name or "")
    if not m:
        return (None, None)
    return (name[:m.start()], m.group(1).lower())


def recipe_id_of(name: str) -> str | None:
    """The condition id a file belongs to (shared by its sample & background)."""
    return split_role(name)[0] or None


def condition_keyword(name: str) -> str | None:
    """Stable averaging keyword '{recipe_id}_{role}' — groups all frames of one
    acquisition (sample or background) regardless of scan/index tokens. None when
    the name carries no role tag (non-loop file → caller keeps its own keyword)."""
    rid, role = split_role(name)
    return f"{rid}_{role}" if rid else None


def is_background(name: str) -> bool:
    """True if the file is a background acquisition (its role tag is a bkg token)."""
    return split_role(name)[1] in BKG_TAGS
