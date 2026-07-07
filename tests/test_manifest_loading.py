"""
tests/test_manifest_loading.py
==============================
Regression tests for manifest loading and the AI assistant's `query_manifest`
tool. These lock in the fixes for the bug where the assistant passed a project
DIRECTORY to ``load_manifest`` (which expects the manifest.json FILE):

  • the old self-heal then ran ``path.replace(backup)`` and RENAMED the whole
    project folder to ``manifest.corrupt-<ts>.json`` (apparent data loss);
  • every ``query_manifest`` call returned an empty manifest.

Covered here:
  • load_manifest accepts a directory and resolves <dir>/manifest.json
  • load_manifest given a directory NEVER renames/moves it or creates a
    manifest.corrupt-* backup
  • load_manifest salvages "Extra data" corruption instead of discarding it
  • SWAXSAssistant._tool_query_manifest returns correct counts for a directory
    project_root

Run:
    uv run pytest tests/test_manifest_loading.py
    python tests/test_manifest_loading.py        # standalone (stdlib only)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import manifest  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_project(n_reduced: int = 3, n_averaged: int = 2) -> Path:
    """Create a temp project dir with a populated manifest.json."""
    d = Path(tempfile.mkdtemp())
    def _mut(m):
        for i in range(n_reduced):
            manifest.add_file_entry(m, path=str(d / f"r{i}_SAXS.dat"),
                                    stage="reduced", detector="saxs",
                                    keyword=f"r{i}", scan_idx=i)
        for i in range(n_averaged):
            manifest.add_file_entry(m, path=str(d / f"a{i}_SAXS.dat"),
                                    stage="averaged", detector="saxs",
                                    keyword=f"a{i}", scan_idx=i)
    manifest.update_manifest(d, _mut)
    return d


# ── load_manifest directory tolerance ──────────────────────────────────────────

def test_load_manifest_accepts_directory():
    d = _make_project(3, 2)
    # Passing the DIRECTORY must resolve to <dir>/manifest.json, not fail.
    m = manifest.load_manifest(d)
    assert len(m.get("files", {})) == 5, "directory load should see all entries"


def test_load_manifest_accepts_file_path():
    d = _make_project(2, 0)
    m = manifest.load_manifest(manifest.manifest_path_for(d))
    assert len(m.get("files", {})) == 2


def test_directory_is_never_renamed_or_backed_up():
    """The destructive-rename regression: a directory arg must leave the folder
    intact and create NO manifest.corrupt-* backup."""
    d = _make_project(4, 1)
    before = sorted(p.name for p in d.iterdir())
    manifest.load_manifest(d)            # must be a no-op on the filesystem
    after = sorted(p.name for p in d.iterdir())
    assert d.is_dir(), "project directory must still exist"
    assert before == after, "directory contents must be unchanged"
    assert not list(d.glob("manifest.corrupt-*")), "no corrupt backup may be created"


# ── Salvage of "Extra data" corruption ─────────────────────────────────────────

def test_salvage_extra_data_corruption():
    d = _make_project(3, 0)
    p = manifest.manifest_path_for(d)
    good = p.read_text()
    p.write_text(good + "\n{trailing garbage that breaks json}")
    m = manifest.load_manifest(p)
    assert len(m.get("files", {})) == 3, "valid leading object should be recovered"
    # damaged copy preserved, and the file is rewritten as clean JSON
    assert list(d.glob("manifest.corrupt-*")), "damaged copy should be kept"
    json.loads(p.read_text())            # must now parse cleanly


# ── Assistant query_manifest with a directory project_root ──────────────────────

def test_assistant_query_manifest_counts_with_directory():
    d = _make_project(n_reduced=6, n_averaged=4)
    from src.ai.assistant import SWAXSAssistant
    a = SWAXSAssistant(ai_knowledge_dir=str(d), user_id="tester")
    out, plot = a._tool_query_manifest({"query_type": "summary"}, project_root=str(d))
    assert plot is None
    res = json.loads(out)
    assert res["total_files"] == 10, f"expected 10, got {res['total_files']}"
    assert res["reduced_files"] == 6
    assert res["averaged_files"] == 4


def test_assistant_files_query_is_capped_and_compact():
    """A large manifest must not dump every full entry (token blowup → API 400).
    The `files` query is capped and compacted, but still reports the true count."""
    d = _make_project(n_reduced=500, n_averaged=0)
    from src.ai import assistant as A
    a = A.SWAXSAssistant(ai_knowledge_dir=str(d), user_id="tester")
    out, _ = a._tool_query_manifest({"query_type": "files"}, project_root=str(d))
    res = json.loads(out)
    assert res["matched"] == 500, "true match count must be reported"
    assert len(res["files"]) == A._MAX_LIST_ENTRIES, "returned sample must be capped"
    assert "note" in res, "truncation note expected when capped"
    # Compact entries must not carry heavy provenance/input_files.
    assert all("provenance" not in f and "input_files" not in f for f in res["files"])
    # Output stays small regardless of manifest size.
    assert len(out) < 20_000, f"capped files output too large: {len(out)} chars"


def test_fit_model_graceful_without_sasmodels():
    """fit_model must return a friendly message (not raise) when the fitting
    stack is unavailable, and never produce a plot in that case."""
    from src.ai.assistant import SWAXSAssistant
    d = _make_project(2, 1)
    a = SWAXSAssistant(ai_knowledge_dir=str(d), user_id="tester")
    out, plot = a._tool_fit_model(
        {"keyword": "a0", "model_name": "sphere",
         "params": {"radius": 50, "scale": "fit"}, "detector": "SAXS"},
        project_root=str(d))
    assert isinstance(out, str) and out
    assert plot is None  # no fit ran / no data → no plot, but no crash


def test_export_writes_only_to_assistant_outputs():
    """export must write to <project>/assistant_outputs/ ONLY, never touch
    experiment data, and produce a valid report/CSV."""
    import glob
    from src.ai.assistant import SWAXSAssistant
    d = _make_project(3, 2)
    manifest_before = (Path(str(d)) / "manifest.json").read_text()
    a = SWAXSAssistant(ai_knowledge_dir=str(d), user_id="tester")

    out_r, plot_r = a._tool_export({"kind": "session_report"}, project_root=str(d))
    out_c, plot_c = a._tool_export({"kind": "fit_results"}, project_root=str(d))
    assert plot_r is None and plot_c is None

    outputs = Path(str(d)) / "assistant_outputs"
    written = [p for p in outputs.glob("*")]
    assert any(p.suffix == ".html" for p in written), "report not written"
    assert any(p.suffix == ".csv" for p in written), "CSV not written"

    # nothing written outside assistant_outputs (manifest unchanged)
    assert (Path(str(d)) / "manifest.json").read_text() == manifest_before
    stray = [p for p in glob.glob(str(Path(str(d)) / "*")) if Path(p).is_file()]
    assert {Path(p).name for p in stray} == {"manifest.json"}, stray

    html = next(p for p in written if p.suffix == ".html").read_text()
    assert html.lstrip().startswith("<!doctype")


def test_export_formats_pdf_xlsx_notes_and_save_as():
    """PDF/XLSX/notes exports and plot `save_as` all write valid files into
    assistant_outputs/ only."""
    from src.ai.assistant import SWAXSAssistant, _save_png
    d = _make_project(3, 2)
    out = Path(str(d)) / "assistant_outputs"
    a = SWAXSAssistant(ai_knowledge_dir=str(d), user_id="tester")

    a._tool_export({"kind": "session_report", "format": "pdf"}, project_root=str(d))
    a._tool_export({"kind": "fit_results", "format": "xlsx"}, project_root=str(d))
    a._tool_export({"kind": "notes", "content": "Fig 1. caption",
                    "filename": "caps"}, project_root=str(d))

    pdf = next(out.glob("*.pdf")); xlsx = next(out.glob("*.xlsx"))
    md  = out / "caps.md"
    assert pdf.read_bytes()[:4] == b"%PDF", "invalid PDF"
    assert xlsx.read_bytes()[:2] == b"PK", "invalid xlsx (zip) header"
    assert md.read_text() == "Fig 1. caption"

    # _save_png writes a PNG into assistant_outputs and nowhere else
    import base64
    png_b64 = base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
    name = _save_png(png_b64, str(d), "myfig")
    assert (out / name).exists() and name.endswith(".png")
    # filename sanitization strips path traversal
    name2 = _save_png(png_b64, str(d), "../../evil")
    assert "/" not in name2 and (out / name2).exists()


def test_overlay_plotly_figure_builder():
    """The interactive Plotly figure builder filters non-positive points on log
    axes and emits one subplot per detector — and stays JSON-serializable."""
    from src.ai.plots import overlay_plotly
    groups = {"saxs": [{"q": [0.01, 0.1, 1.0, -0.5], "I": [100, 10, 1, 5], "label": "a"}],
              "waxs": [{"q": [10, 20], "I": [5, 3], "label": "a"}]}
    fig = overlay_plotly(groups, axis="loglog", title="t")
    assert len(fig["data"]) == 2
    assert fig["layout"]["xaxis"]["type"] == "log" and "xaxis2" in fig["layout"]
    assert len(fig["data"][0]["x"]) == 3          # negative-q point dropped
    json.dumps(fig)                                 # must serialize


def test_interactive_emit_only_from_plot_tools():
    """A plot tool emits an interactive figure via the thread-local; a non-plot
    tool emits nothing (so the carrier doesn't leak across tool calls)."""
    from src.ai import assistant as A
    d = _make_project(2, 2)
    a = A.SWAXSAssistant(ai_knowledge_dir=str(d), user_id="tester")
    A._emit_interactive(None)
    a._dispatch_tool("query_manifest", {"query_type": "summary"},
                     project_root=str(d), user_id="tester")
    assert getattr(A._PLOT_TL, "fig", None) is None


def test_assistant_tool_registry_unique_and_dispatched():
    """Every registered tool name is unique (duplicate names break the API)."""
    from src.ai import assistant as A
    names = [t["name"] for t in A._TOOLS]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"
    for must in ("fit_model", "assess_quality", "overlay_curves",
                 "plot_metadata", "list_saxs_models"):
        assert must in names, f"missing tool: {must}"


def test_clean_content_strips_empty_text_blocks():
    """Empty text blocks must be removed before storing assistant turns, or the
    next API request fails ('text content blocks must be non-empty')."""
    from src.ai import assistant as A

    class _B:
        def __init__(self, **k): self.__dict__.update(k)

    content = [_B(type="text", text=""), _B(type="tool_use", name="x", id="t1"),
               _B(type="text", text="real")]
    clean = A._clean_content(content)
    types_ = [getattr(b, "type", None) for b in clean]
    assert types_ == ["tool_use", "text"], types_         # empty text dropped
    # if everything would be removed, a non-empty placeholder is returned
    only_empty = A._clean_content([_B(type="text", text="   ")])
    assert only_empty and only_empty[0]["text"].strip() == "" \
        and only_empty[0]["text"] != ""


def test_build_system_prompt_handles_latex_braces():
    """The system prompt contains LaTeX with curly braces (\\frac{R_g^2}{3}).
    Building it must NOT treat those as str.format fields (regression: the
    assistant crashed with KeyError: 'R_g^2 q^2')."""
    from src.ai.assistant import SWAXSAssistant
    a = SWAXSAssistant(ai_knowledge_dir=tempfile.mkdtemp(), user_id="tester")
    sp = a._build_system_prompt(message="hi", user_id="tester",
                                project_root=None, app_id="assistant")
    assert "Current app: assistant" in sp        # app_id injected
    assert "{app_id}" not in sp                   # placeholder consumed
    assert r"\frac{R_g^2 q^2}{3}" in sp           # LaTeX braces preserved


def test_list_saxs_models_returns_catalog_and_filters():
    """list_saxs_models must return a non-empty model catalog (sasmodels or the
    curated fallback) and respect the keyword filter."""
    from src.ai.assistant import SWAXSAssistant
    a = SWAXSAssistant(ai_knowledge_dir=tempfile.mkdtemp(), user_id="tester")
    out, plot = a._tool_list_saxs_models({})
    assert plot is None
    res = json.loads(out)
    assert res["count"] > 0 and res["models"], "model catalog must not be empty"
    # keyword filter narrows results (matches model name; curated also matches
    # the use-case description, which is intentional).
    out2, _ = a._tool_list_saxs_models({"keyword": "sphere"})
    res2 = json.loads(out2)
    assert 0 < res2["count"] <= res["count"], "filter should narrow, not empty/expand"
    if isinstance(res2["models"], dict):
        assert all("sphere" in (k + " " + v).lower()
                   for k, v in res2["models"].items())
    else:
        assert all("sphere" in n.lower() for n in res2["models"])


def test_assistant_query_manifest_no_project_root():
    from src.ai.assistant import SWAXSAssistant
    a = SWAXSAssistant(ai_knowledge_dir=tempfile.mkdtemp(), user_id="tester")
    out, _ = a._tool_query_manifest({"query_type": "summary"}, project_root=None)
    assert "No project root" in out


# ── Standalone runner (works without pytest) ──────────────────────────────────

if __name__ == "__main__":
    tests = sorted(n for n in globals() if n.startswith("test_"))
    passed = failed = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {name}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed  ({len(tests)} total)")
    sys.exit(1 if failed else 0)
