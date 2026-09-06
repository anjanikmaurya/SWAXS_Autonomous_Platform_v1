"""
src/optimizer/io.py — serialize an optimizer-proposed condition into the exact
``key = value`` param-file the reactor's folder watcher already parses, and
match a measured profile back to the condition that produced it.
"""

from __future__ import annotations


def to_param_file(recipe_id: str, params: dict) -> str:
    """Render a condition as a reactor-readable .txt (parsed by parse_param_file)."""
    lines = [f"# autopilot condition {recipe_id}", f"recipe_id = {recipe_id}"]
    for k in ("T_reac", "F_tot", "x_ODE", "x_TOP", "x_oley"):
        if k in params:
            # repr() emits the shortest string that round-trips to the same double,
            # so the value the reactor reads back is bit-identical to the one the
            # campaign proposed. `:g` truncated to 6 sig figs, drifting provenance
            # (the recorded condition no longer matched what was actually dosed).
            lines.append(f"{k} = {repr(float(params[k]))}")
    return "\n".join(lines) + "\n"


def recipe_id_from_filename(filename: str, tags=("sample", "bkg")) -> str:
    """Recover the recipe_id from a measurement filename WITHOUT a candidate list.

    The reactor names every acquisition ``{recipe_id}_{sample|bkg}``, so the id is
    everything before the role tag. Needed for notifications and provenance,
    where there is no set of pending ids to match against (``match_recipe_id``).
    Returns "" when the filename doesn't follow the convention.
    """
    name = str(filename or "")
    for tag in tags:
        marker = f"_{tag}"
        i = name.find(marker)
        if i > 0:
            return name[:i]
    return ""


def match_recipe_id(filename: str, pending_ids) -> str | None:
    """Return the pending recipe_id that this measurement filename belongs to
    (the id is carried in the filename), or None. Longest id first so a longer
    id can't be shadowed by a shorter one that is its prefix.

    The id must be followed by the role delimiter ``_`` (every acquisition is
    named ``{recipe_id}_{role}``). A bare substring test let ``Run1`` match
    ``Run12_sample_...`` and ``auto_..._5`` match ``auto_..._50`` once ids ran
    past a shared prefix; requiring the ``_`` boundary removes that collision."""
    for rid in sorted(pending_ids, key=len, reverse=True):
        if rid and f"{rid}_" in filename:
            return rid
    return None
