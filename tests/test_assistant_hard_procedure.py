"""The assistant's read-mode HARD PROCEDURE must stay in the system prompt.

Operator request (2026-09): the assistant should follow a fixed, enforced
step-order for read-mode analysis (ground → resolve one file → validate →
compute → always plot → state assumptions), ask one question when ambiguous,
be proactive on reads, and keep answers short. These are a behavioural contract,
so guard the key phrases against silent removal.
"""
import importlib

m = importlib.import_module("src.ai.assistant")
P = m._SYSTEM_BASE


def test_hard_procedure_block_present():
    assert "HARD PROCEDURE" in P
    # the six ordered grounding steps
    for tag in ("G1", "G2", "G3", "G4", "G5", "G6"):
        assert tag in P, f"grounding step {tag} missing"


def test_core_grounding_requirements():
    assert "RESOLVE ONE FILE" in P
    assert "ALWAYS PLOT" in P
    assert "STATE ASSUMPTIONS" in P
    assert "never guess" in P.lower() or "never fabricate" in P.lower()
    assert "ASK ONE" in P.upper()          # one clarifying question when ambiguous


def test_per_task_orders_present():
    for task in ("Guinier", "p(r)", "Model"):
        assert task in P, f"per-task order for {task} missing"


def test_reads_are_proactive_writes_need_consent():
    assert "JUST DO IT" in P.upper()
    # writes still gated
    assert "add_note" in P and "flag_quality" in P
    assert "require a clear yes" in P.lower() or "need consent" in P.lower()


def test_strict_no_waxs_peak_rule_present():
    low = P.lower()
    assert "waxs" in low and "noise" in low
    # must forbid reporting waxs peaks / crystallinity / d-spacing
    assert "never report" in low or "do not report" in low
    assert "crystallinity" in low and "d-spacing" in low
    # the old peak-finding guidance must not encourage WAXS peaks anymore
    assert "sharp peaks ⇒ crystalline order" not in P


def test_answers_are_short_no_routine_footer():
    low = P.lower()
    assert "brief why" in low
    assert 'no routine' in low and 'next step' in low
