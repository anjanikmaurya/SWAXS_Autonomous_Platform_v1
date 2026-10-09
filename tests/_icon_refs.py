"""Find every sprite icon a page references, including names chosen at runtime.

Static references are <use href="#swaxs-…">. Runtime ones go through the shared
helpers from tools/build_icon_sprite.py (icoSvg('x'), setIco(el,'x',…)) or the
theme toggle's 'swaxs-ui-'+(dark?'sun':'moon'). A typo in any of them draws a
blank square, so tests resolve all of them against the inlined sprite.
"""
from __future__ import annotations

import re

_LIT_AFTER = re.compile(r"(?:^|\?|:|\()\s*'([\w-]+)'")


def static_refs(body: str) -> set[str]:
    """<use href="#id"> targets, minus the ones assembled in JS."""
    return {r for r in re.findall(r'<use href="#([^"]+)"', body) if "'+" not in r}


def runtime_icon_names(body: str) -> set[str]:
    """Icon names (without the swaxs-ui- prefix) chosen in script."""
    names: set[str] = set()
    # icoSvg('x')  /  icoSvg(cond ? 'a' : cond2 ? 'b' : 'c')
    for args in re.findall(r"icoSvg\(([^()]*)\)", body):
        names |= set(_LIT_AFTER.findall("(" + args))
    # setIco(el, 'x', …)  /  setIco(el, cond ? 'a' : 'b', …)  — the 2nd arg only
    for arg2 in re.findall(r"setIco\((?:[^,()]|\([^()]*\))+,\s*([^,]+),", body):
        names |= set(_LIT_AFTER.findall("(" + arg2))
    # 'swaxs-ui-'+(cond?'a':'b')
    for a, b in re.findall(r"swaxs-ui-'\+\(\w+(?:\(\))?\s*\?\s*'([\w-]+)'\s*:\s*'([\w-]+)'\)", body):
        names |= {a, b}
    # JS function bodies use the parameter name, not a literal
    names.discard("n")
    return names
