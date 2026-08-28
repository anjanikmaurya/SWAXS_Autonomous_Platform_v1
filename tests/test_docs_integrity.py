"""
tests/test_docs_integrity.py — the docs must not drift away from the code again.

WHY THIS EXISTS
---------------
A documentation audit found 43 markdown files carrying, among other things: a
`src/` module map that invented eight files and omitted eight packages; a design
system certifying WCAG contrast the CSS had abandoned; a runbook describing the
reverse of the actual physical run order; and `uv run` in seven files after the
hub had migrated to `sys.executable`. Two files described features
(`src/reduction/stitch.py`, a Word exporter) that were never built.

Prose cannot be unit-tested, but the mechanical claims can: file paths, ports,
app counts, launcher commands, and links. Those are exactly the claims that rot.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _docs() -> list[Path]:
    skip = {"node_modules", ".git", "ai_knowledge", "venv", ".venv"}
    return sorted(p for p in ROOT.rglob("*.md")
                  if not any(part in skip for part in p.parts))


#: Phrases that mark a mention as a deliberate "this does not exist" statement.
#: Documenting a never-built feature so nobody rebuilds the doc is the point;
#: what these tests catch is a doc that describes it as if it were real.
_DISCLAIMERS = (
    "never built", "Never built", "does not exist", "do not exist",
    "no longer exists", "was never", "were never", "not built",
    "proposed", "pick a free one", "display-only", "display toggle",
    "zero callers", "fabricat", "NOT exist", "not here", "no `src/",
)


def _disclaims(line: str) -> bool:
    return any(d in line for d in _DISCLAIMERS)


@pytest.fixture(scope="module")
def apps() -> list[dict]:
    reg = yaml.safe_load((ROOT / "apps.yml").read_text())
    return reg["apps"]


# ── links and referenced paths must resolve ──────────────────────────────────
def test_every_relative_markdown_link_resolves():
    broken = []
    link = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
    for md in _docs():
        for label, tgt in link.findall(md.read_text(errors="ignore")):
            if tgt.startswith(("http", "#", "mailto:")):
                continue
            t = tgt.split("#")[0]
            if t and not (md.parent / t).exists():
                broken.append(f"{md.relative_to(ROOT)}: [{label}]({tgt})")
    assert not broken, "broken links:\n  " + "\n  ".join(broken)


def test_docs_do_not_reference_source_files_that_do_not_exist():
    """`src/reduction/stitch.py`, `src/export/pdf_report.py` and
    `src/analysis/guinier.py` were documented for months and never existed."""
    pat = re.compile(r"`(src/[A-Za-z0-9_/]+\.py)`")
    ghosts = []
    for md in _docs():
        # A design proposal names the modules it would create. It carries a
        # NEVER BUILT banner, which is the honest way to keep an unbuilt plan.
        if md.parent.name == "design":
            assert "NEVER BUILT" in md.read_text(errors="ignore").upper(), \
                f"{md.relative_to(ROOT)} proposes files without saying it is unbuilt"
            continue
        lines = md.read_text(errors="ignore").splitlines()
        for i, line in enumerate(lines, 1):
            # A disclaimer can sit a line or two above the path it disclaims,
            # e.g. "Note what is not here: ... no `src/reduction/stitch.py`".
            if any(_disclaims(l) for l in lines[max(0, i - 3):i]):
                continue
            for m in pat.findall(line):
                if not (ROOT / m).exists():
                    ghosts.append(f"{md.relative_to(ROOT)}:{i}: {m}")
    assert not ghosts, "docs cite src/ files that do not exist:\n  " + "\n  ".join(ghosts)


# ── the registry is the single source of truth for ports and apps ────────────
def test_no_doc_claims_a_wrong_number_of_apps(apps):
    n = len(apps)
    assert n == 9, f"apps.yml now has {n} apps - update the docs and this test"
    wrong = []
    for md in _docs():
        txt = md.read_text(errors="ignore")
        for bad in ("five independent Flask apps", "five sub-apps",
                    "eight small web apps", "five apps on 5001-5005"):
            if bad in txt:
                wrong.append(f"{md.relative_to(ROOT)}: {bad!r}")
    assert not wrong, "stale app counts:\n  " + "\n  ".join(wrong)


def test_every_port_a_doc_mentions_is_a_real_port(apps):
    real = {5000} | {a["port"] for a in apps}
    bad = []
    for md in _docs():
        for i, line in enumerate(md.read_text(errors="ignore").splitlines(), 1):
            if _disclaims(line):
                continue
            for port in re.findall(r"\b(50[0-9][0-9])\b", line):
                if int(port) not in real:
                    bad.append(f"{md.relative_to(ROOT)}:{i}: {port}")
    assert not bad, "docs mention ports no app serves:\n  " + "\n  ".join(bad)


def test_the_calibration_app_is_documented(apps):
    """It shipped on 5009 and was missing from the README table, CLAUDE.md, the
    .sh banner and the operator runbook - the whole first stage of the pipeline
    was invisible."""
    assert any(a["id"] == "calibration" for a in apps)
    for doc in ("README.md", "CLAUDE.md", "QUICKSTART.md",
                "docs/AUTONOMOUS_RUN_STEPS.md"):
        txt = (ROOT / doc).read_text()
        assert "5009" in txt, f"{doc} never mentions port 5009"
        assert "alibration" in txt, f"{doc} never mentions the calibration app"


# ── the launcher story has to be consistent ──────────────────────────────────
def test_uv_run_is_gone_from_docs_and_launchers():
    """The hub launches sub-apps with sys.executable and there is no
    pyproject.toml or uv.lock, so `uv run` resolves a different (empty)
    environment - or fails outright with 'uv: command not found'."""
    assert not (ROOT / "pyproject.toml").exists(), \
        "a pyproject.toml appeared - revisit whether uv is now supported"
    offenders = []
    for f in _docs() + [ROOT / "start_platform.sh", ROOT / "start_platform.ps1",
                        ROOT / "start_platform.bat"]:
        for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if "uv run" in line and "uv run" not in line.split("#")[-1]:
                offenders.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, "`uv run` is back:\n  " + "\n  ".join(offenders)


def test_the_shell_launcher_starts_the_hub_with_a_real_interpreter():
    sh = (ROOT / "start_platform.sh").read_text()
    assert "hub/app.py" in sh
    assert "$PY" in sh, "the .sh launcher must resolve an interpreter, not hardcode one"
    assert "3, 10" in sh or "3.10" in sh, "no Python-version gate in the .sh launcher"


def test_all_three_launcher_banners_list_every_app(apps):
    """Three banners drifted independently; 5009 was missing from the .sh one."""
    for name in ("start_platform.sh", "start_platform.ps1", "start_platform.bat"):
        txt = (ROOT / name).read_text()
        missing = [str(a["port"]) for a in apps if str(a["port"]) not in txt]
        assert not missing, f"{name} banner omits ports {missing}"


# ── the AI knowledge base must cover every app ───────────────────────────────
def test_every_app_has_a_knowledge_file_and_it_is_wired(apps):
    """The ingest list was a hardcoded 6 app names, so quality/ and reactor/
    knowledge.md never reached the KB and the assistant would deny the Quality
    Gate existed. apps.yml's `knowledge:` key was dead config."""
    for a in apps:
        rel = a.get("knowledge")
        assert rel, f"{a['id']} has no knowledge: key in apps.yml"
        assert (ROOT / rel).is_file(), f"{a['id']}: {rel} does not exist"
    assert (ROOT / "hub" / "knowledge.md").is_file()

    src = (ROOT / "assistant" / "app.py").read_text()
    assert '"reduction", "viewer", "background"' not in src, \
        "the hardcoded knowledge ingest list is back"
    assert "_knowledge_files" in src


# ── things that were documented but never built ──────────────────────────────
def test_no_doc_advertises_saxs_waxs_auto_stitching():
    """The viewer's Stitch checkbox is a display toggle: one shared axis vs
    side-by-side panels. There is no overlap fit, no scale factor, no merged
    file, and `emit_file_stitched` has zero callers."""
    assert not (ROOT / "src" / "reduction" / "stitch.py").exists()
    bad = []
    for md in _docs():
        for i, line in enumerate(md.read_text(errors="ignore").splitlines(), 1):
            if _disclaims(line):
                continue
            for claim in ("auto_stitch", "_stitched.dat", "stitch SAXS+WAXS"):
                if claim in line:
                    bad.append(f"{md.relative_to(ROOT)}:{i}: {claim!r}")
    assert not bad, "auto-stitching is advertised again:\n  " + "\n  ".join(bad)


def test_no_doc_promises_a_word_export():
    assert not (ROOT / "src" / "export").exists()
    reqs = " ".join((ROOT / f).read_text() for f in
                    ("requirements-core.txt", "requirements-ai.txt",
                     "requirements-hardware.txt"))
    assert "python-docx" not in reqs
    for md in _docs():
        assert "docx_report" not in md.read_text(errors="ignore"), md


# ── the audits folder stays consolidated ─────────────────────────────────────
def test_the_audits_folder_stays_at_four_files():
    """13 files / ~1900 lines of point-in-time reports, with the open-defect list
    scattered across all of them. Consolidated to one register + two operator
    docs + an index."""
    d = ROOT / "docs" / "audits"
    got = sorted(p.name for p in d.glob("*.md"))
    assert got == ["BEAMLINE_SAFETY_AUDIT.md", "OPEN_DEFECTS.md",
                   "PRE_BEAMTIME_READINESS.md", "README.md"], got


def test_the_open_defect_register_still_carries_the_reactor_residuals():
    """8 reactor safety residuals lived only in a file that was deleted; if they
    fall out of the register they are lost entirely."""
    txt = (ROOT / "docs" / "audits" / "OPEN_DEFECTS.md").read_text()
    for needle in ("E-stop latency", "Negative flows", "end_on_measurement",
                   "shutdown()", "backend` flag", "Serial retry"):
        assert needle in txt, f"reactor residual dropped from the register: {needle!r}"
    for n in ("N1", "N16", "O3", "O4", "O7", "O18", "D3", "R1", "R8"):
        assert n in txt, f"register lost {n}"


def test_deleted_docs_are_not_linked_from_anywhere():
    gone = ["SLACK_NOTIFICATIONS.md", "SLACK_SETUP_WALKTHROUGH.md",
            "RUNNING_ON_WINDOWS.md", "PLATFORM_AUDIT.md", "CODE_AUDIT.md",
            "DESIGN_AUDIT.md", "REACTOR_SAFETY_AUDIT.md", "REACTOR_APP_AUDIT.md",
            "REACTOR_CONTROLS_AUDIT.md", "SUBTRACTION_APP_AUDIT.md",
            "REDUCTION_CORRECTION_AUDIT.md", "AUTONOMOUS_RUN_READINESS.md"]
    dangling = []
    for md in _docs():
        txt = md.read_text(errors="ignore")
        for g in gone:
            if f"]({g}" in txt or f"](docs/audits/{g}" in txt or f"](audits/{g}" in txt:
                dangling.append(f"{md.relative_to(ROOT)} -> {g}")
    assert not dangling, "links to deleted docs:\n  " + "\n  ".join(dangling)
