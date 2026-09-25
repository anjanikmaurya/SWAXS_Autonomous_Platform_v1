"""Regressions for the assistant audit fixes (2026-09).

- units: _load_dat normalises Å⁻¹ → nm⁻¹ so the assistant recovers the same sizes
  as the analyzer (a q_A-1 curve otherwise gave 10×-too-small Rg/Dmax/radius).
- A1: PDF ingest sanitises the client filename (no path traversal).
- A2: an upload size cap is configured.
- A3: routes return a generic error, not raw str(exc).
"""
import importlib

import numpy as np

m = importlib.import_module("src.ai.assistant")


def _write_dat(path, unit_hdr, q):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# q({unit_hdr}) I sigma\n")
        for qi in q:
            f.write(f"{qi} 1.0 0.1\n")
    return str(path)


def test_load_dat_converts_angstrom_to_nm(tmp_path):
    q = np.linspace(0.005, 0.5, 12)                 # Å⁻¹ range
    p = _write_dat(tmp_path / "ang.dat", "q_A-1", q)
    qa, _, _ = m._load_dat(p)
    assert abs(qa.max() - q.max() * 10.0) < 1e-6, "Å⁻¹ q was not scaled to nm⁻¹"


def test_load_dat_leaves_nm_unchanged(tmp_path):
    q = np.linspace(0.05, 5.0, 12)                   # nm⁻¹ range
    p = _write_dat(tmp_path / "nm.dat", "q_nm^-1", q)
    qn, _, _ = m._load_dat(p)
    assert abs(qn.max() - q.max()) < 1e-6, "nm⁻¹ q was wrongly rescaled"


def test_angstrom_detection():
    assert m._q_is_angstrom(["# q(q_A-1) I sigma"]) is True
    assert m._q_is_angstrom(["# q(A^-1) I"]) is True
    assert m._q_is_angstrom(["# q(q_nm^-1) I sigma"]) is False
    assert m._q_is_angstrom([]) is False


# ── app-level fixes (A1/A2/A3) ────────────────────────────────────────────────
def test_a2_upload_size_cap_is_set():
    app_mod = importlib.import_module("assistant.app")
    assert app_mod.app.config.get("MAX_CONTENT_LENGTH"), "no MAX_CONTENT_LENGTH (A2)"


def test_a1_and_a3_are_in_place():
    src = open("assistant/app.py", encoding="utf-8").read()
    # A1: filename sanitised
    assert "secure_filename" in src, "PDF ingest does not sanitise the filename (A1)"
    # A3: no route returns raw str(exc); the _fail helper is used instead
    assert 'jsonify({"error": str(exc)})' not in src, "a route still leaks str(exc) (A3)"
    assert "def _fail(" in src
