"""generate_plot must resolve a sample by KEYWORD (no file path needed).

The assistant reported it "can't pass a file path by keyword" for Guinier/Kratky
plots, so it skipped the figure. generate_plot now accepts a `keyword`, resolves
the subtracted curve via the manifest, loads the data, and (for Guinier) auto-fits
the line — the model never needs a path.
"""
import base64
import importlib
import os

import numpy as np
import pytest

m = importlib.import_module("src.ai.assistant")


@pytest.fixture
def project(tmp_path, monkeypatch):
    q = np.linspace(0.05, 3.0, 200)
    x = q * 5.0
    F = 3 * (np.sin(x) - x * np.cos(x)) / x**3
    I = 1e4 * F**2 + 1.0
    sig = np.sqrt(I)
    p = tmp_path / "Run5_r005_sample_sub.dat"
    with open(p, "w", encoding="utf-8") as f:
        f.write("# q(q_nm^-1) I sigma\n")
        for a, b, c in zip(q, I, sig):
            f.write(f"{a} {b} {c}\n")
    fake = {"files": {"Run5_r005_sample_sub.dat": {
        "stage": "subtracted", "detector": "SAXS",
        "path": str(p), "created_at": "2026-09-24T10:00"}}}
    monkeypatch.setattr(m, "_load_manifest_cached", lambda *a, **k: fake)
    return str(tmp_path)


def _asst():
    return object.__new__(m.SWAXSAssistant)


@pytest.mark.parametrize("plot_type", ["curve", "guinier", "kratky", "porod"])
def test_plot_by_keyword_renders(project, plot_type):
    out, plot = _asst()._tool_generate_plot(
        {"plot_type": plot_type, "keyword": "Run5_r005"}, project_root=project)
    assert plot and len(base64.b64decode(plot)) > 1000, out


def test_missing_keyword_returns_clean_message(project):
    out, plot = _asst()._tool_generate_plot(
        {"plot_type": "curve", "keyword": "NopeXYZ"}, project_root=project)
    assert plot is None
    assert "matched" in out.lower()


def test_guinier_by_keyword_autofits_without_rg(project):
    # no Rg/q_min passed → tool auto-fits so the fit line is drawn, not an error
    out, plot = _asst()._tool_generate_plot(
        {"plot_type": "guinier", "keyword": "Run5_r005"}, project_root=project)
    assert plot is not None, out
