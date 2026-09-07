"""
tests/test_corrections_channel.py — Change 4 Part B: durable, authoritative
operator corrections.

They must: persist to ai_knowledge/corrections.json (not ChromaDB); ride the STATIC
cached prefix (mtime-cached, effective next turn); OUTRANK package docs / knowledge.md;
surface conflicts (overrides); and support propose→confirm so the assistant can suggest
but only the operator authors an authoritative correction. No live API is used.
"""
from __future__ import annotations

import json
import tempfile

import pytest

from src.ai.memory import LayeredMemory
from src.ai import assistant as A
from src.ai.assistant import (_resolve_corrections_block, _ANALYSIS_LADDER_RULES,
                              _estimate_tokens, _STATIC_PREFIX_WARN_TOKENS)


def _mem(tmp):
    return LayeredMemory(ai_knowledge_dir=str(tmp), user_id="anj")


# ── store: add / list / propose / confirm / remove ────────────────────────────
def test_add_list_remove_roundtrip(tmp_path):
    m = _mem(tmp_path)
    e = m.add_correction("stale claim", "correct instead", scope="app:analysis")
    assert e["status"] == "active" and e["author"] == "anj" and e["scope"] == "app:analysis"
    assert e["id"] in [r["id"] for r in m.load_authoritative_corrections()]
    assert m.remove_correction(e["id"]) is True
    assert e["id"] not in [r["id"] for r in m.load_authoritative_corrections()]  # archived


def test_propose_is_quarantined_until_confirm(tmp_path):
    m = _mem(tmp_path)
    p = m.add_correction("x", "y", proposed=True)
    assert p["status"] == "proposed" and p["proposed_by_assistant"] is True
    # not in the active set
    assert p["id"] not in [r["id"] for r in
                           m.load_authoritative_corrections(include_proposed=False)]
    r = m.confirm_correction(p["id"])
    assert r and r["status"] == "active" and r["proposed_by_assistant"] is False
    assert p["id"] in [x["id"] for x in
                       m.load_authoritative_corrections(include_proposed=False)]


def test_bad_scope_collapses_to_global(tmp_path):
    m = _mem(tmp_path)
    e = m.add_correction("w", "r", scope="nonsense")
    assert e["scope"] == "global"


# ── injection: authoritative block, conflict surfaced, precedence declared ────
def test_block_surfaces_override_and_is_authoritative(tmp_path):
    m = _mem(tmp_path)
    m.add_correction("beamline 1-5 geometry is current",
                     "read geometry from the .poni files + Reduction app",
                     scope="doc:beamline_1_5",
                     overrides=["beamline_1_5/knowledge.md"])
    blk = _resolve_corrections_block(str(tmp_path), "analysis")
    assert "AUTHORITATIVE" in blk
    assert "OVERRIDES: beamline_1_5/knowledge.md" in blk
    assert "override" in blk.lower()  # instruction to say so, not apply silently


def test_proposed_render_separate_and_not_authoritative(tmp_path):
    m = _mem(tmp_path)
    m.add_correction("p-wrong", "p-right", proposed=True)
    blk = _resolve_corrections_block(str(tmp_path), "analysis")
    assert "NOT yet authoritative" in blk


def test_precedence_rule_puts_corrections_on_top():
    # the ladder rule declares the hierarchy the model must follow
    assert "operator corrections" in _ANALYSIS_LADDER_RULES.lower()
    idx = _ANALYSIS_LADDER_RULES.lower().index("operator corrections")
    assert idx < _ANALYSIS_LADDER_RULES.lower().index("package docs")


# ── app-scoping ───────────────────────────────────────────────────────────────
def test_app_scoped_correction_only_shows_for_that_app(tmp_path):
    m = _mem(tmp_path)
    m.add_correction("only-in-analysis", "fix", scope="app:analysis")
    assert _resolve_corrections_block(str(tmp_path), "analysis") is not None
    assert "only-in-analysis" in _resolve_corrections_block(str(tmp_path), "analysis")
    # a different app must not see it (and with nothing else, gets no block)
    assert _resolve_corrections_block(str(tmp_path), "reduction") is None


def test_no_corrections_no_block(tmp_path):
    assert _resolve_corrections_block(str(tmp_path), "analysis") is None


# ── mtime cache: hit on no change, rebuild after a write ──────────────────────
def test_block_mtime_cache_hit_then_rebuild(tmp_path):
    m = _mem(tmp_path)
    m.add_correction("first", "one", scope="global")
    b1 = _resolve_corrections_block(str(tmp_path), "analysis")
    b2 = _resolve_corrections_block(str(tmp_path), "analysis")
    assert b1 == b2                      # same file mtime → cache hit
    m.add_correction("second", "two", scope="global")   # changes the file
    b3 = _resolve_corrections_block(str(tmp_path), "analysis")
    assert "second" in b3 and "second" not in b1        # rebuilt next read


# ── the tool method (direct call, no API) ─────────────────────────────────────
def test_tool_manage_corrections_actions(tmp_path):
    a = A.SWAXSAssistant(ai_knowledge_dir=str(tmp_path), user_id="anj")
    out, _ = a._tool_manage_corrections(
        {"action": "add", "wrong": "old", "right": "new",
         "scope": "doc:beamline_1_5", "overrides": ["beamline_1_5/knowledge.md"]}, "anj")
    assert "authoritative correction" in out.lower() and "overrides" in out.lower()

    lst = json.loads(a._tool_manage_corrections({"action": "list"}, "anj")[0])
    assert lst[0]["scope"] == "doc:beamline_1_5"
    cid = lst[0]["id"]

    prop, _ = a._tool_manage_corrections(
        {"action": "propose", "wrong": "p", "right": "q"}, "anj")
    assert "not authoritative" in prop.lower()
    pid = json.loads(a._tool_manage_corrections({"action": "list"}, "anj")[0])
    pid = [r["id"] for r in pid if r["status"] == "proposed"][0]
    conf, _ = a._tool_manage_corrections({"action": "confirm", "id": pid}, "anj")
    assert "authoritative" in conf.lower()

    rem, _ = a._tool_manage_corrections({"action": "remove", "id": cid}, "anj")
    assert "archived" in rem.lower()

    # add requires both wrong+right
    bad, _ = a._tool_manage_corrections({"action": "add", "wrong": "only"}, "anj")
    assert "provide both" in bad.lower()


# ── token meter ───────────────────────────────────────────────────────────────
def test_token_meter_and_threshold():
    assert _estimate_tokens("x" * 4000) == 1000
    assert _estimate_tokens("y" * (4 * _STATIC_PREFIX_WARN_TOKENS)) >= _STATIC_PREFIX_WARN_TOKENS
