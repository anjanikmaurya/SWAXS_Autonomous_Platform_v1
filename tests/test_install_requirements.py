"""
tests/test_install_requirements.py — the install must keep working on a fresh laptop.

WHY THIS EXISTS
---------------
Installing for a colleague on Windows failed, and the cause was the dependency
list, not their machine:

  * `requirements.txt` is a `pip freeze` of the original developer's Mac. It
    pins EXACT versions — `numpy==2.2.6`, `scipy==1.15.3`, `matplotlib==3.10.3`
    — and none of those have a prebuilt Windows wheel for Python 3.9. pip then
    tries to COMPILE numpy from source, which needs a full C/Fortran toolchain,
    and the install dies with "Microsoft Visual C++ 14.0 or greater is required".
  * It also carried ~1 GB the platform never imports: PyQt6 (x3), silx,
    pyopencl, sentence-transformers (which pulls torch), jupyter, debugpy.
  * And it was MISSING four packages the code really does import: requests,
    pyserial, sasmodels, pdfminer.six.

`requirements-core.txt` is the fix: minimum versions, no GUI/OpenCL/ML, and it
covers every hard import. Verified against real wheel availability —
Windows py3.9 and py3.13, macOS arm64, Linux x86_64: 16/16 packages, all
prebuilt, no compiler.

These tests keep it that way.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: import name -> distribution name, where they differ
IMPORT_TO_DIST = {
    "yaml": "pyyaml", "PIL": "pillow", "serial": "pyserial",
    "epics": "pyepics", "pyFAI": "pyfai", "flask_sock": "flask-sock",
    "websocket": "websocket-client", "sklearn": "scikit-learn",
    "dateutil": "python-dateutil", "pdfminer": "pdfminer.six",
    "sentence_transformers": "sentence-transformers",
}

#: Imports that are deliberately optional. Each is inside a try/except or a
#: lazy function-local import, the app says what is missing, and the feature
#: degrades instead of crashing. Value = which extras file provides it.
OPTIONAL = {
    "anthropic": "requirements-ai.txt",
    "chromadb": "requirements-ai.txt",
    "sentence_transformers": "requirements-ai.txt",
    "pypdf": "requirements-ai.txt",
    "pdfminer": "requirements-ai.txt",
    "serial": "requirements-hardware.txt",
    "epics": "requirements-hardware.txt",
    "sasmodels": "pip install sasmodels",
    "pytest": "pip install pytest",
    "Py_P_Pump": "vendored in src/reactor/drivers/",
    "jsdom": "tests/ui (node, not pip)",
}


def _requirements(name: str) -> set[str]:
    """Distribution names listed in a requirements file, lower-cased."""
    out = set()
    for line in (ROOT / name).read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        out.add(re.split(r"[<>=!~\[ ]", line)[0].strip().lower())
    return out


def _third_party_imports() -> dict[str, set[str]]:
    """Top-level third-party modules imported anywhere in the shipped code."""
    local = {"src", "hub", "reduction", "average", "background", "quality",
             "analysis", "reactor", "analyzer", "assistant", "calibration",
             "tools", "tests"}
    std = set(sys.stdlib_module_names)
    files = (list(ROOT.glob("*/app.py")) + list((ROOT / "src").rglob("*.py"))
             + list((ROOT / "tools").glob("*.py")))
    found: dict[str, set[str]] = {}
    for f in files:
        try:
            tree = ast.parse(f.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m and m not in std and m not in local:
                    found.setdefault(m, set()).add(str(f.relative_to(ROOT)))
    return found


# ── the core list must be complete ───────────────────────────────────────────
def test_every_required_import_is_in_requirements_core():
    """The original list was missing requests, pyserial, sasmodels and
    pdfminer.six — the code imported packages nobody was told to install."""
    core = _requirements("requirements-core.txt")
    extras = _requirements("requirements-ai.txt") | _requirements("requirements-hardware.txt")
    uncovered = []
    for mod, files in sorted(_third_party_imports().items()):
        dist = IMPORT_TO_DIST.get(mod, mod).lower()
        if dist in core or dist in extras or mod in OPTIONAL:
            continue
        uncovered.append(f"{mod} (imported by {sorted(files)[0]})")
    assert not uncovered, (
        "imported but in no requirements file:\n  " + "\n  ".join(uncovered))


def test_the_core_list_has_no_heavyweight_or_platform_specific_packages():
    """These are what made the Windows install fail or take 20 minutes."""
    core = _requirements("requirements-core.txt")
    banned = {
        "pyqt6": "a Qt GUI toolkit; nothing imports it",
        "pyqt6-qt6": "same", "pyqt6-sip": "same",
        "silx": "pulls Qt", "pyopencl": "needs an OpenCL SDK at import time",
        "torch": "~2 GB", "sentence-transformers": "pulls torch",
        "chromadb": "large; the assistant degrades without it",
        "appnope": "macOS only", "pexpect": "POSIX only", "ptyprocess": "POSIX only",
        "siphash24": "needs a compiler on some platforms",
        "ipykernel": "dev only", "jupyter-client": "dev only",
        "jupyter-core": "dev only", "debugpy": "dev only", "jedi": "dev only",
        "isort": "dev only",
    }
    found = sorted(f"{p} ({why})" for p, why in banned.items() if p in core)
    assert not found, "requirements-core.txt has grown weight:\n  " + "\n  ".join(found)


def test_the_core_list_uses_minimum_versions_not_exact_pins():
    """`numpy==2.2.6` has no Windows wheel for Python 3.9, so pip compiles it and
    the install fails. Minimum versions let pip pick a wheel that exists."""
    exact = []
    for line in (ROOT / "requirements-core.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            exact.append(line)
    assert not exact, ("exact pins are back in requirements-core.txt:\n  "
                       + "\n  ".join(exact))


def test_optional_extras_files_exist_and_are_referenced():
    for f in ("requirements-core.txt", "requirements-hardware.txt",
              "requirements-ai.txt"):
        assert (ROOT / f).is_file(), f"{f} is missing"
    qs = (ROOT / "QUICKSTART.md").read_text()
    for f in ("requirements-core.txt", "requirements-hardware.txt",
              "requirements-ai.txt"):
        assert f in qs, f"QUICKSTART.md never mentions {f}"


# ── every platform gets a launcher ───────────────────────────────────────────
def test_all_three_platforms_have_a_launcher():
    """Windows users had none — only start_platform.sh, which PowerShell cannot
    run. That alone made the first install a research project."""
    for f in ("start_platform.sh", "start_platform.ps1", "start_platform.bat"):
        assert (ROOT / f).is_file(), f"{f} is missing"


def test_the_windows_launchers_are_pure_ascii():
    """A BOM-less .ps1/.bat is read in the ANSI codepage by older PowerShell and
    by cmd.exe, which mangles any non-ASCII character."""
    for f in ("start_platform.ps1", "start_platform.bat"):
        txt = (ROOT / f).read_text()
        bad = sorted({c for c in txt if ord(c) > 127})
        assert not bad, f"{f} contains non-ASCII: {bad}"


def test_the_windows_launchers_set_utf8_and_guard_the_python_version():
    for f in ("start_platform.ps1", "start_platform.bat"):
        txt = (ROOT / f).read_text()
        assert "PYTHONUTF8" in txt, f"{f} does not force UTF-8 (log emoji crash the console)"
        assert "3,10" in txt or "3.10" in txt or "-lt 10" in txt, \
            f"{f} does not refuse a too-old Python"
        assert "requirements-core.txt" in txt, \
            f"{f} points the user at the wrong requirements file"
        assert "hub" in txt and "app.py" in txt, f"{f} never starts the hub"


def test_launchers_find_a_conda_env_as_well_as_a_venv():
    """Half the Windows users on this project have Anaconda, not a bare venv."""
    for f in ("start_platform.ps1", "start_platform.bat"):
        txt = (ROOT / f).read_text()
        assert "CONDA_PREFIX" in txt, f"{f} ignores an activated conda env"
        assert "VIRTUAL_ENV" in txt, f"{f} ignores an activated venv"


# ── the quickstart must stay true ────────────────────────────────────────────
def test_quickstart_covers_every_platform_and_shell_asked_for():
    qs = (ROOT / "QUICKSTART.md").read_text()
    for section in ("## macOS", "## Windows — PowerShell",
                    "## Windows — Anaconda Prompt", "## Linux"):
        assert section in qs, f"QUICKSTART.md has no '{section}' section"
    assert "Troubleshooting" in qs


def test_quickstart_ports_match_apps_yml():
    """A doc that lists the wrong port sends a new user hunting."""
    yaml = pytest.importorskip("yaml")
    real = sorted(a["port"] for a in
                  yaml.safe_load((ROOT / "apps.yml").read_text())["apps"])
    qs = (ROOT / "QUICKSTART.md").read_text()
    listed = sorted({int(p) for p in re.findall(r"\|\s*(5\d{3})\s*\|", qs)})
    assert listed == real, f"QUICKSTART lists {listed}, apps.yml has {real}"


def test_quickstart_references_only_files_that_exist():
    qs = (ROOT / "QUICKSTART.md").read_text()
    refs = set(re.findall(r"`([A-Za-z0-9_/-]+\.(?:md|txt|sh|ps1|bat|py))`", qs))
    # config.yml lives in the user's PROJECT folder, not the repo
    missing = [r for r in sorted(refs) if not (ROOT / r).exists()]
    assert not missing, f"QUICKSTART points at files that do not exist: {missing}"


def test_quickstart_warns_against_the_old_requirements_file():
    """The single most common way this install goes wrong."""
    qs = (ROOT / "QUICKSTART.md").read_text()
    assert "requirements.txt" in qs and "requirements-core.txt" in qs
    warn = qs[:qs.index("## macOS")]
    assert "not" in warn.lower() and "requirements.txt" in warn, \
        "the top of QUICKSTART does not steer users away from requirements.txt"


# ── the GitHub landing page must carry the install, not just link to it ──────
# The README is what GitHub renders on the repository home page, so it is the
# first (and often only) thing a new user reads.
def test_readme_has_the_install_inline_for_every_platform():
    rd = (ROOT / "README.md").read_text()
    qs = rd[rd.index("## Quick start"):rd.index("## Organizing your experiment data")]
    for needle, why in [
        ("macOS", "no macOS install block"),
        ("PowerShell", "no Windows PowerShell block"),
        ("Anaconda Prompt", "no Anaconda Prompt block"),
        ("Linux", "no Linux block"),
        ("Activate.ps1", "Windows activation command is wrong or missing"),
        ("conda activate swaxs", "conda route is incomplete"),
        ("start_platform.ps1", "PowerShell launcher not mentioned"),
        ("start_platform.bat", "batch launcher not mentioned"),
        ("localhost:5100", "never tells the user where to open the UI"),
    ]:
        assert needle in qs, f"README Quick start: {why}"


def test_readme_never_tells_anyone_to_use_the_old_requirements_file():
    rd = (ROOT / "README.md").read_text()
    assert "pip install -r requirements.txt" not in rd, \
        "README still recommends requirements.txt — the Windows install killer"
    assert "requirements-core.txt" in rd


def test_readme_does_not_claim_python_39_works():
    """It does not: numpy/scipy/matplotlib have no Windows wheels for 3.9, so pip
    tries to compile them."""
    rd = (ROOT / "README.md").read_text()
    for stale in ("3.9+ may work", "Python 3.9 or newer", "3.9+"):
        assert stale not in rd, f"README still claims {stale!r}"


def test_readme_details_blocks_are_balanced():
    """An unclosed <details> swallows the rest of the page on GitHub."""
    rd = (ROOT / "README.md").read_text()
    assert rd.count("<details") == rd.count("</details>") > 0
    # opened by default, so an inexperienced reader does not miss them
    assert rd.count("<details open>") == rd.count("<details")


def test_no_doc_anywhere_recommends_the_old_requirements_file():
    """A colleague followed a secondary doc, not the README, and hit the MSVC
    compiler error. Every doc has to point at the same file."""
    offenders = []
    for md in sorted(ROOT.rglob("*.md")):
        if any(part in {"node_modules", ".git", "ai_knowledge"} for part in md.parts):
            continue
        for i, line in enumerate(md.read_text(errors="ignore").splitlines(), 1):
            if "pip install -r requirements.txt" not in line:
                continue
            # A line that warns AGAINST it is fine.
            if any(w in line for w in ("Don't", "Do not", "not `requirements",
                                       "instead of", "ERROR:", "Cannot install")):
                continue
            offenders.append(f"{md.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "docs still tell users to install requirements.txt:\n  " + "\n  ".join(offenders))


def test_quickstart_is_the_only_install_guide():
    """RUNNING_ON_WINDOWS.md was a third, drifting copy of the install steps and
    its own app table; it was merged into QUICKSTART and deleted. Nothing should
    reintroduce a second guide."""
    assert not (ROOT / "RUNNING_ON_WINDOWS.md").exists(), \
        "a second Windows install guide is back - it drifted three times before"
    qs = (ROOT / "QUICKSTART.md").read_text()
    for needle in ("start_platform.ps1", "start_platform.bat",
                   "Microsoft Visual C++", "chromadb", "pyopencl"):
        assert needle in qs, f"QUICKSTART lost {needle!r} in the merge"
