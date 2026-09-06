"""
tests/test_analysis_guidelines.py — Change 3 (analysis ladder) tests
====================================================================
Covers the four required behaviours plus the six YAML-review changes:

  (a) the gate REFUSES a higher-tier (tier-3) entry when a precondition fails
  (b) an entry with unmet `consumes` is never proposed
  (c) tier-1 runs in ONE local pass with ZERO model calls
  (d) the router sends a Bragg-peak curve to WAXS and a smooth decay to SAXS

Plus: both YAMLs valid + graph closed, in-code window clamping (#4), MW withheld
without concentration (#3), refusal messages name the precondition + remedy (#6),
and the tool re-verifies modality per curve and surfaces a switch (#5).

All deterministic and local — no API calls (that is the point of tier-0-2).
"""

from __future__ import annotations

import sys
import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("yaml")

from src.analysis import guidelines as G


# ── synthetic curves ───────────────────────────────────────────────────────────
def _saxs_curve(n=800):
    """Smooth Guinier->power-law decay, no peaks."""
    q = np.linspace(0.05, 6.0, n)
    I = 100.0 / (1 + (q * 3.0) ** 2) ** 2 + 0.01
    return q, I, np.sqrt(np.abs(I)) * 0.01


def _waxs_curve(n=800):
    """Weak baseline + three sharp Bragg peaks."""
    q = np.linspace(0.05, 6.0, n)
    I = 0.5 * np.exp(-q)
    for q0 in (2.0, 3.0, 4.2):
        I = I + 5.0 * np.exp(-0.5 * ((q - q0) / 0.02) ** 2)
    return q, I, np.sqrt(np.abs(I) + 1e-6) * 0.01


# ── YAML validity + graph closure ───────────────────────────────────────────────
def test_both_yamls_valid_and_graph_closed():
    for mod in ("saxs", "waxs"):
        d = G.load_guideline(mod)              # raises on any schema/graph problem
        assert d["doc"]["entries"]
        # my_notes must be empty and nothing marked `tested` (operator-only fields)
        for e in d["doc"]["entries"]:
            assert e["my_notes"] == "", f"{e['id']} has non-empty my_notes"
            assert e["confidence"] in ("documented", "untested"), e["id"]


def test_validate_rejects_dangling_consume():
    bad = {"entries": [{
        "id": "x", "tier": 3, "modality": "saxs", "package": "p",
        "observes": [], "requires": [], "inapplicable_when": [],
        "provides": [], "consumes": ["nonexistent_key"],
        "initial_guesses": {}, "my_notes": "", "confidence": "documented",
        "cost": "cheap", "invocable": False,
    }]}
    with pytest.raises(ValueError):
        G.validate_guideline(bad, "saxs")


# ── (d) router ───────────────────────────────────────────────────────────────────
def test_router_sends_smooth_decay_to_saxs():
    q, I, s = _saxs_curve()
    r = G.route_modality(q, I, s)
    assert r["modality"] == "saxs"
    assert r["bragg_present"] is False


def test_router_sends_bragg_peaks_to_waxs():
    q, I, s = _waxs_curve()
    r = G.route_modality(q, I, s)
    assert r["modality"] == "waxs"
    assert r["bragg_present"] is True
    assert r["n_sharp_peaks"] >= 2


# ── (c) tier-1 is ONE local pass with zero model calls ───────────────────────────
def test_tier1_single_pass_no_model_dependency(monkeypatch):
    # Make ANY attempt to talk to a model blow up: if tier-1 tried, this fails.
    monkeypatch.setitem(sys.modules, "anthropic", None)
    q, I, s = _saxs_curve()
    out = G.run_tier1(q, I, s, context={"background_ok": True})
    st = out["state"]
    # a single call produced every tier-1 quantity — the model did not sequence them
    for key in ("Rg", "I0", "guinier_R2", "kratky_peak_qRg",
                "powerlaw_exponent", "porod_volume",
                "q_usable_min", "q_usable_max"):
        assert key in st, f"missing {key}"
    assert isinstance(st["Rg"], float) and st["Rg"] > 0


# ── (#4) fitted windows clamped to q_usable, loud on escape ──────────────────────
def test_tier1_windows_stay_inside_usable():
    q, I, s = _saxs_curve()
    out = G.run_tier1(q, I, s, context={"background_ok": True})
    st = out["state"]
    lo, hi = st["q_usable_min"], st["q_usable_max"]
    assert lo <= st["guinier_qmin"] <= st["guinier_qmax"] <= hi + 1e-9
    assert lo - 1e-9 <= st["powerlaw_qmin"] <= st["powerlaw_qmax"] <= hi + 1e-9
    assert not out["warnings"]           # a clean curve should not trip the loud path


def test_tier1_loud_when_guinier_would_escape_usable(monkeypatch):
    # Force guinier_fit to return a window BELOW q_usable_min (beamstop region);
    # tier-1 must refuse the Rg loudly rather than silently accept it.
    import src.analysis.core as core
    q, I, s = _saxs_curve()

    def fake_guinier(qq, ii, ss, q_min=None, q_max=None, auto_range=True):
        return {"Rg": 5.0, "I0": 100.0, "R2": 0.999,
                "q_range": [1e-4, 0.2], "qRg_max": 1.0}  # 1e-4 << q_usable_min
    monkeypatch.setattr(core, "guinier_fit", fake_guinier)
    out = G.run_tier1(q, I, s, context={"background_ok": True})
    assert "Rg" not in out["state"]
    assert any("escaped usable range" in w for w in out["warnings"])


# ── (a) gate refuses a tier-3 entry when a precondition fails ────────────────────
def _tier3_fixture_entry():
    # tier-3 ab-initio-style step; not shipped in the YAML (tiers 0-2 only), built
    # here to prove the gate guards tier-3 exactly as it guards tier 2.
    return {
        "id": "t3_dammif", "tier": 3, "modality": "saxs", "package": "ATSAS DAMMIF",
        "observes": [], "consumes": ["Dmax"],
        "requires": [{"key": "azimuthal_isotropy",
                      "hint": "verify isotropy from the 2D/cake pattern first"}],
        "inapplicable_when": [{"key": "bragg_present",
                               "hint": "crystalline peaks present — ab initio shape is meaningless"}],
        "provides": ["envelope"], "initial_guesses": {}, "my_notes": "",
        "confidence": "documented", "cost": "expensive", "invocable": False,
    }


def test_gate_refuses_tier3_when_precondition_fails():
    entry = _tier3_fixture_entry()
    # precondition fails: isotropy unknown AND Dmax not produced
    res = G.gate_entries([entry], state={})
    assert res["proposable"] == []
    assert len(res["refused"]) == 1
    refusal = res["refused"][0]
    assert refusal["id"] == "t3_dammif"
    joined = " ".join(refusal["reasons"])
    assert "azimuthal_isotropy" in joined       # names the failed precondition
    assert "Dmax" in joined                       # names the unmet consume


def test_gate_admits_tier3_when_all_satisfied():
    entry = _tier3_fixture_entry()
    ok = {"Dmax": 8.0, "azimuthal_isotropy": True, "bragg_present": False}
    res = G.gate_entries([entry], ok)
    assert res["proposable"] == ["t3_dammif"] and res["refused"] == []


def test_gate_inapplicable_when_triggers_on_bragg():
    entry = _tier3_fixture_entry()
    st = {"Dmax": 8.0, "azimuthal_isotropy": True, "bragg_present": True}
    res = G.gate_entries([entry], st)
    assert res["proposable"] == []
    assert any("bragg_present" in r for r in res["refused"][0]["reasons"])


# ── (b) an entry with unmet consumes is never proposed ───────────────────────────
def test_entry_with_unmet_consumes_not_proposed():
    entries = G.load_guideline("saxs")["doc"]["entries"]
    # nothing produced yet: t1_guinier consumes q_usable_* -> must be refused
    res = G.gate_entries(entries, state={})
    assert "t1_guinier" not in res["proposable"]
    guin = next(r for r in res["refused"] if r["id"] == "t1_guinier")
    assert any("q_usable_min" in x or "q_usable_max" in x for x in guin["reasons"])
    # once q_usable is produced AND background_ok holds, it becomes proposable
    res2 = G.gate_entries(entries, {"q_usable_min": 0.05, "q_usable_max": 3.0,
                                    "background_ok": True})
    assert "t1_guinier" in res2["proposable"]


# ── (#3) MW is withheld without concentration ────────────────────────────────────
def test_mw_withheld_without_concentration():
    q, I, s = _saxs_curve()
    # no concentration in context
    out = G.run_tier1(q, I, s, context={"background_ok": True})
    assert out["summary"].get("mw_estimate_kda") is None
    assert "mw_estimate" not in out["state"]
    assert "porod_volume" in out["state"]        # volume still available
    # with concentration, MW is offered and t1_mw_estimate can be proposed
    out2 = G.run_tier1(q, I, s, context={"background_ok": True, "concentration": 2.5})
    assert out2["state"].get("mw_estimate") is not None
    res = G.gate_entries(G.load_guideline("saxs")["doc"]["entries"], out2["state"])
    assert "t1_mw_estimate" in res["proposable"]


# ── (#6) refusal messages are actionable ─────────────────────────────────────────
def test_pr_refusal_names_precondition_and_remedy():
    q, I, s = _saxs_curve()
    st = G.run_tier1(q, I, s, context={"background_ok": True})["state"]
    res = G.gate_entries(G.load_guideline("saxs")["doc"]["entries"], st)
    pr = next(r for r in res["refused"] if r["id"] == "t2_pr_local")
    text = " ".join(pr["reasons"])
    assert "azimuthal_isotropy" in text and "2D" in text          # precondition + how to satisfy
    assert "interparticle_free" in text and "dilution" in text


# ── (#5) tool re-verifies modality per curve and surfaces a switch ───────────────
def test_tool_reroutes_and_switches_modality(monkeypatch, tmp_path):
    import tempfile
    from src.ai.assistant import SWAXSAssistant
    import src.ai.assistant as A

    a = SWAXSAssistant(ai_knowledge_dir=tempfile.mkdtemp(), user_id="u1")
    assert a._get_modality("u1") == "saxs"           # default

    qw, Iw, sw = _waxs_curve()
    monkeypatch.setattr(A, "_load_dat", lambda p: (qw, Iw, sw))
    monkeypatch.setattr(A, "_load_manifest_cached", lambda p: {
        "files": {"s.dat": {"stage": "subtracted", "detector": "SAXS",
                            "path": str(tmp_path / "s.dat")}},
        "ai_memory": {"user_context": {}},
    })
    out, _ = a._tool_analysis_tier1({"keyword": "s", "detector": "SAXS"},
                                    project_root=str(tmp_path), user_id="u1")
    import json
    payload = json.loads(out)
    assert payload["modality"] == "waxs"
    assert "modality_switch" in payload             # surfaced, not silent
    assert a._get_modality("u1") == "waxs"          # stored modality switched
