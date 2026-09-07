"""
tests/test_preflight_gate.py — Change 4 Part A: the tier -1 PRE-FLIGHT gate.

Reflexive data-sanity checks that must PASS (or be flagged unverifiable) before any
plot/number/interpretation. Each is a checkable state key with a NAMED REMEDY (the
gate quotes the `hint`). All deterministic and local — no API calls.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("yaml")
pytest.importorskip("scipy")
from src.analysis import guidelines as G   # noqa: E402


def _pf_entries(modality):
    return [e for e in G.load_guideline(modality)["doc"]["entries"] if e["tier"] == -1]


# ── the YAML still loads + graph-closes with tier -1 present ──────────────────
def test_both_yamls_valid_with_preflight_tier():
    assert len(_pf_entries("saxs")) == 11
    assert len(_pf_entries("waxs")) == 6
    # tier -1 is now legal
    G.validate_guideline(G.load_guideline("saxs")["doc"], "saxs")
    G.validate_guideline(G.load_guideline("waxs")["doc"], "waxs")


# ── every pre-flight check refuses on its failing condition, naming a remedy ──
def test_all_preflight_refuse_on_empty_state():
    pf = _pf_entries("saxs")
    res = G.gate_entries(pf, state={})
    assert res["proposable"] == []
    assert len(res["refused"]) == len(pf)
    for r in res["refused"]:
        assert r["tier"] == -1
        # the remedy (hint) is quoted back, not a bare refusal
        assert any("—" in reason and "requires" in reason for reason in r["reasons"])


@pytest.mark.parametrize("entry_id,flag", [
    ("pf_q_convention",          "q_convention_settled"),
    ("pf_geometry_provenance",   "geometry_ok"),
    ("pf_subtraction_sanity",    "subtraction_sane"),
    ("pf_radiation_damage",      "damage_checked"),
    ("pf_low_q_triage",          "low_q_triaged"),
    ("pf_high_q_tail",           "high_q_tail_excluded"),
    ("pf_detector_gaps",         "detector_gaps_checked"),
    ("pf_concentration_effects", "conc_effects_considered"),
    ("pf_uncertainties",         "uncertainties_present"),
])
def test_each_check_refuses_until_its_flag_is_true(entry_id, flag):
    entry = next(e for e in _pf_entries("saxs") if e["id"] == entry_id)
    # missing/False/None → refuse, naming the key
    for bad in ({}, {flag: False}, {flag: None}):
        res = G.gate_entries([entry], bad)
        assert res["proposable"] == [], f"{entry_id} passed with {bad}"
        assert flag in res["refused"][0]["reasons"][0]
    # True → proposable (silent pass)
    res = G.gate_entries([entry], {flag: True})
    assert res["proposable"] == [entry_id] and res["refused"] == []


def test_caveat_commitments_pass_when_acknowledged():
    for eid, flag in (("pf_monodispersity_caveat", "monodispersity_acknowledged"),
                      ("pf_plot_conventions", "plot_conventions_ok")):
        entry = next(e for e in _pf_entries("saxs") if e["id"] == eid)
        assert G.gate_entries([entry], {flag: True})["proposable"] == [eid]


# ── the log-plot trap: over-subtraction is caught in LINEAR values ────────────
def test_subtraction_negatives_caught_in_linear_values():
    """A curve whose over-subtracted region is invisible on a log axis (log just
    drops the non-positive points) must still be flagged from the LINEAR values."""
    q = np.linspace(0.02, 0.6, 200)
    I = 5000.0 / (1 + (q * 25) ** 2)          # smooth decay
    I[120:180] = -20.0                         # a real over-subtracted region
    res = G.subtraction_sanity(q, I)
    assert res["sane"] is False
    assert res["neg_fraction_linear"] > 0.05
    assert "negative" in res["reason"].lower()

    # a clean curve tending to zero passes
    clean = 5000.0 / (1 + (q * 25) ** 2)
    ok = G.subtraction_sanity(q, clean)
    assert ok["sane"] is True


# ── a one/two-point high-q "peak" is NOISE until proven ───────────────────────
def test_single_point_tail_spike_is_noise():
    q = np.linspace(0.05, 1.2, 300)
    I = 200.0 / (1 + (q * 15) ** 2) + 0.5
    I[-1] *= 60                                # one hot pixel in the tail
    assert G.classify_tail_peak(q, I)["classification"] == "noise"
    # two adjacent hot points → still noise (need >= 3 above local noise)
    I2 = 200.0 / (1 + (q * 15) ** 2) + 0.5
    I2[-2:] *= 40
    assert G.classify_tail_peak(q, I2)["classification"] == "noise"


def test_resolved_multipoint_tail_bump_is_a_candidate():
    q = np.linspace(0.05, 1.2, 300)
    base = 50.0 / (1 + (q * 15) ** 2) + 0.2
    # a broad, resolution-consistent bump spanning ~10 points near the tail
    bump = 30.0 * np.exp(-0.5 * ((q - 1.05) / 0.03) ** 2)
    res = G.classify_tail_peak(q, base + bump, sigma=np.full_like(q, 0.5))
    assert res["classification"] == "candidate_peak"
    assert res["n_points_above_noise"] >= 3


# ── WAXS pre-flight subset also gates ─────────────────────────────────────────
def test_waxs_preflight_refuses_on_empty_state():
    res = G.gate_entries(_pf_entries("waxs"), state={})
    assert res["proposable"] == []
    assert {r["id"] for r in res["refused"]} >= {
        "pf_q_convention", "pf_geometry_provenance", "pf_detector_gaps"}
