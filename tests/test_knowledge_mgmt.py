"""
tests/test_knowledge_mgmt.py
============================
Knowledge-base management: list / remove operate on the ingestion log and work
even when ChromaDB is not installed (vector ops are best-effort, the log is the
source of truth for what's indexed). These guard the visualise/add/remove
capability for user-supplied literature.

Run:
    python tests/test_knowledge_mgmt.py
    uv run pytest tests/test_knowledge_mgmt.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.ai.knowledge import KnowledgeBase  # noqa: E402


def _kb_with_log(entries: dict) -> KnowledgeBase:
    d = Path(tempfile.mkdtemp())
    (d / "ingestion_log.json").write_text(json.dumps(entries))
    return KnowledgeBase(d)


def test_list_ingested_reads_log():
    kb = _kb_with_log({"user_papers": {
        "/abs/uz5008.pdf": {"hash": "x", "chunks": 12, "ingested_at": "2026-06-18T00:00:00"},
        "note:my fact":    {"hash": "y", "chunks": 1,  "ingested_at": "2026-06-18T00:00:00"},
    }})
    items = kb.list_ingested()
    names = sorted(i["name"] for i in items)
    assert names == ["note:my fact", "uz5008.pdf"]
    assert any(i["chunks"] == 12 for i in items)


def test_remove_source_by_name():
    kb = _kb_with_log({"user_papers": {
        "/abs/uz5008.pdf": {"hash": "x", "chunks": 12, "ingested_at": "t"},
        "/abs/other.pdf":  {"hash": "z", "chunks": 5,  "ingested_at": "t"},
    }})
    res = kb.remove_source("uz5008.pdf")
    assert res.get("removed") == ["uz5008.pdf"] and res.get("chunks") == 12
    remaining = [i["name"] for i in kb.list_ingested()]
    assert remaining == ["other.pdf"]
    # change persisted to the log file
    log = json.loads((Path(kb._log_path)).read_text())
    assert "/abs/uz5008.pdf" not in log["user_papers"]


def test_remove_missing_errors():
    kb = _kb_with_log({"user_papers": {}})
    assert "error" in kb.remove_source("nope")


def test_remove_note_by_title():
    kb = _kb_with_log({"user_papers": {
        "note:buffer matching tip": {"hash": "h", "chunks": 1, "ingested_at": "t"},
    }})
    res = kb.remove_source("buffer matching tip")
    assert res.get("removed") == ["buffer matching tip"]
    assert kb.list_ingested() == []


def test_group_sops_lifecycle_and_prompt():
    """Group SOPs add/list/remove and appear in the assembled prompt context."""
    import tempfile
    from src.ai.memory import LayeredMemory
    m = LayeredMemory(tempfile.mkdtemp(), user_id="tester")
    assert m.load_group_sops() == []
    e = m.add_group_sop("Buffer matching", "Match buffer from the same SEC run.")
    m.add_group_sop("Default model", "Start membrane fits with correlation_length.")
    titles = sorted(s["title"] for s in m.load_group_sops())
    assert titles == ["Buffer matching", "Default model"]

    # included in the prompt-format text
    txt = m.format_for_prompt(m.load_context())
    assert "Group SOPs & Conventions" in txt and "Buffer matching" in txt

    assert m.remove_group_sop("Default model") is True      # by title
    assert m.remove_group_sop(e["id"]) is True              # by id
    assert m.load_group_sops() == []
    assert m.remove_group_sop("nope") is False              # missing


def test_per_project_chat_history_persists():
    """Chat turns persist per project and survive a 'restart' (new instance)."""
    import tempfile
    from src.ai.memory import LayeredMemory
    proj = tempfile.mkdtemp()
    m = LayeredMemory(tempfile.mkdtemp(), user_id="tester")
    m.append_chat(proj, "user", "what is Rg?")
    m.append_chat(proj, "assistant", "About 6 nm.")
    assert [t["role"] for t in m.load_chat(proj)] == ["user", "assistant"]
    # a fresh instance (simulated restart) still loads it
    m2 = LayeredMemory(tempfile.mkdtemp(), user_id="tester")
    assert len(m2.load_chat(proj)) == 2
    m.clear_chat(proj)
    assert m.load_chat(proj) == []


def test_preferences_drive_audience_directive():
    """Saved audience/verbosity preferences appear as a prompt directive."""
    import tempfile
    from src.ai.assistant import SWAXSAssistant
    a = SWAXSAssistant(ai_knowledge_dir=tempfile.mkdtemp(), user_id="tester")
    a._tool_set_preferences({"audience": "student", "verbosity": "detailed"}, "tester")
    sp = a._build_system_prompt(message="x", user_id="tester",
                                project_root=None, app_id="assistant")
    assert "Audience is a STUDENT" in sp and "detailed" in sp
    a._tool_set_preferences({"audience": "expert", "verbosity": "concise"}, "tester")
    sp2 = a._build_system_prompt(message="x", user_id="tester",
                                 project_root=None, app_id="assistant")
    assert "Audience is EXPERT" in sp2 and "concise" in sp2


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
