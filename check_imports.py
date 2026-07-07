#!/usr/bin/env python3
"""
check_imports.py — SWAXS Platform import audit tool
=====================================================
Run from the project root:
    uv run check_imports.py

Prints:
  1. Which src module each app imports from
  2. Which functions from each src module are actually used across all apps
  3. Functions that exist in src but are not yet called by any app (future candidates)
  4. Any broken imports (src files that apps reference but don't exist)
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── App folders to inspect ────────────────────────────────────────────────────
APPS = ["hub", "reduction", "viewer", "background", "analysis", "assistant"]

# ── src modules and their public API ─────────────────────────────────────────
SRC_MODULES = {
    "src.manifest":                 "src/manifest.py",
    "src.plot_reduction":           "src/plot_reduction.py",
    "src.utils.read_dat_metadata":  "src/utils/read_dat_metadata.py",
    "src.reduction.core":           "src/reduction/core.py",
    "src.reduction.process_metadata": "src/reduction/process_metadata.py",
    "src.reduction.read_raw_file":  "src/reduction/read_raw_file.py",
}

# ─────────────────────────────────────────────────────────────────────────────

def _colour(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def green(t):  return _colour(t, "32")
def yellow(t): return _colour(t, "33")
def red(t):    return _colour(t, "31")
def bold(t):   return _colour(t, "1")
def dim(t):    return _colour(t, "2")


def get_all(filepath: Path) -> list[str]:
    """Return the __all__ list from a Python file, or [] if not defined."""
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__all__"
                and isinstance(node.value, ast.List)):
            return [
                elt.s for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.s, str)
            ]
    return []


def get_defs(filepath: Path) -> list[str]:
    """Return all top-level def/class names from a Python file."""
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return []
    return [
        node.name for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def get_imports_from_src(filepath: Path) -> dict[str, list[str]]:
    """
    Parse an app.py and return {module_path: [names_imported]}.
    Handles 'from src.xxx import a, b' and 'from src.xxx import xxx as alias'.
    Also detects lazy imports inside function bodies.
    """
    result: dict[str, list[str]] = {}
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return result

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("src.") or mod == "src":
                names = [alias.name for alias in node.names]
                result.setdefault(mod, []).extend(names)
    return result


def get_all_name_usages(filepath: Path) -> set[str]:
    """Return every Name and Attribute used in a file (to detect function calls)."""
    try:
        source = filepath.read_text()
    except OSError:
        return set()
    # Simple regex for identifiers — fast and good enough for this purpose
    return set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', source))


# ─────────────────────────────────────────────────────────────────────────────
# Main audit
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print()
    print(bold("━" * 62))
    print(bold("  SWAXS Platform — Import & Dependency Audit"))
    print(bold("━" * 62))

    # ── Collect per-app imports ───────────────────────────────────────────────
    app_imports: dict[str, dict[str, list[str]]] = {}   # app → {module → [names]}
    app_usages:  dict[str, set[str]] = {}                # app → set of identifiers used

    for app in APPS:
        app_file = ROOT / app / "app.py"
        if not app_file.exists():
            print(red(f"  ✗ {app}/app.py not found"))
            continue
        app_imports[app] = get_imports_from_src(app_file)
        app_usages[app]  = get_all_name_usages(app_file)

    # ── 1. Per-app dependency table ───────────────────────────────────────────
    print()
    print(bold("1.  What each app imports from src/"))
    print(dim("    " + "─" * 56))
    for app in APPS:
        imports = app_imports.get(app, {})
        if not imports:
            print(f"  {dim(app):30s}  {dim('(no src imports)')}")
            continue
        first = True
        for mod, names in imports.items():
            prefix = f"  {bold(app):30s}" if first else f"  {'':20s}"
            print(f"  {(bold(app) if first else ''):20s}  {green(mod)}")
            for n in names:
                print(f"  {'':20s}    {dim('└─')} {n}")
            first = False

    # ── 2. Per-src-module usage ───────────────────────────────────────────────
    print()
    print(bold("2.  Which apps use each src module"))
    print(dim("    " + "─" * 56))

    for mod_key, rel_path in SRC_MODULES.items():
        mod_file = ROOT / rel_path
        print(f"\n  {bold(mod_key)}")

        if not mod_file.exists():
            print(f"    {red('✗  FILE MISSING:')} {rel_path}")
            continue

        public_api = get_all(mod_file)
        defs       = get_defs(mod_file)
        private    = [d for d in defs if d.startswith("_")]
        public     = [d for d in defs if not d.startswith("_")]

        # Which apps import from this module?
        users = [
            (app, app_imports[app].get(mod_key, []))
            for app in APPS
            if mod_key in app_imports.get(app, {})
        ]
        if users:
            for app, names in users:
                used_names = [n for n in names if n in app_usages.get(app, set())]
                print(f"    {green('✓')} {app:15s} imports: {', '.join(names)}")
        else:
            # Check if imported via alias (e.g. `from src.reduction import core as reduction_core`)
            alias_users = []
            for app in APPS:
                src_text = (ROOT / app / "app.py").read_text() if (ROOT / app / "app.py").exists() else ""
                if mod_key.split(".")[-1] in src_text and "src" in src_text:
                    alias_users.append(app)
            if alias_users:
                for app in alias_users:
                    print(f"    {green('✓')} {app:15s} imports via alias")
            else:
                print(f"    {yellow('○')} not directly imported by any app")

        # Public API coverage
        if public_api:
            all_app_ids = set()
            for a in app_usages.values():
                all_app_ids |= a
            used_in_apps   = [n for n in public_api if n in all_app_ids]
            unused_in_apps = [n for n in public_api if n not in all_app_ids]

            if used_in_apps:
                print(f"    {dim('API used by apps:')}  {', '.join(used_in_apps)}")
            if unused_in_apps:
                print(f"    {yellow('API not yet used:')}  {', '.join(unused_in_apps)}")
        else:
            print(f"    {yellow('⚠')}  no __all__ defined")

    # ── 3. Missing src files ──────────────────────────────────────────────────
    print()
    print(bold("3.  Broken import check"))
    print(dim("    " + "─" * 56))
    broken = False
    for app in APPS:
        for mod_key in app_imports.get(app, {}):
            rel = mod_key.replace(".", "/") + ".py"
            # Also check package __init__
            pkg  = ROOT / (mod_key.replace(".", "/")) / "__init__.py"
            file = ROOT / rel
            if not file.exists() and not pkg.exists():
                print(f"  {red('✗')} {app}/app.py imports {mod_key!r} → {red('FILE NOT FOUND')}")
                broken = True
    if not broken:
        print(f"  {green('✓')} all imports resolve to existing files")

    # ── 4. Summary ───────────────────────────────────────────────────────────
    print()
    print(bold("4.  Summary"))
    print(dim("    " + "─" * 56))
    total_src = len(SRC_MODULES)
    all_used  = sum(
        1 for mod in SRC_MODULES
        if any(mod in app_imports.get(a, {}) for a in APPS)
           or any(mod.split(".")[-1] in (ROOT / a / "app.py").read_text()
                  for a in APPS if (ROOT / a / "app.py").exists())
    )
    print(f"  src modules:        {total_src}")
    print(f"  modules in use:     {green(str(all_used))}")
    print(f"  modules not used:   {yellow(str(total_src - all_used))}")
    print()
    print(dim("  Run this script any time after adding new code to keep the map current."))
    print()


if __name__ == "__main__":
    main()
