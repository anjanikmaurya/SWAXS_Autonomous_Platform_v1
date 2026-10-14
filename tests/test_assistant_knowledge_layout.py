"""The assistant's knowledge layout (src/ai/layout.py) and answer traces
(src/ai/traces.py)."""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ai import layout, traces  # noqa: E402
from src.ai.layout import KnowledgeLayout, migrate  # noqa: E402


# ── layout ────────────────────────────────────────────────────────────────────

def _sources():
    return yaml.safe_load((ROOT / "ai_knowledge" / "sources.yml").read_text())["sources"]


def test_every_listed_source_exists():
    for s in _sources():
        p = s["path"]
        if any(ch in p for ch in "<~*"):
            continue
        assert (ROOT / p).exists(), f"sources.yml lists a missing path: {p}"
        for extra in s.get("also", []):
            assert (ROOT / extra).exists(), extra


def test_every_layout_path_is_listed_in_sources():
    L = KnowledgeLayout(ROOT / "ai_knowledge")
    listed = {s["path"] for s in _sources()}
    for p in (L.system_prompt, L.corrections, L.sops, L.cases_dir, L.evals_dir,
              L.index_dir, *(L.reference(c) for c in layout.REFERENCE_COLLECTIONS)):
        assert str(p.relative_to(ROOT)) in listed, p


def test_repo_uses_the_layout_and_no_legacy_files_remain():
    kb = ROOT / "ai_knowledge"
    for legacy in ("corrections.json", "literature", "beamline", "user_papers",
                   "vector_db", "ingestion_log.json", "group/sops.json"):
        assert not (kb / legacy).exists(), f"legacy path still present: {legacy}"
    assert (kb / "rules" / "corrections.json").is_file()
    assert (kb / "rules" / "system_prompt.md").is_file()


def test_system_prompt_comes_from_the_file():
    import src.ai.assistant as m
    text = (ROOT / "ai_knowledge" / "rules" / "system_prompt.md").read_text()
    assert m._SYSTEM_BASE == text
    assert "{app_id}" in text and text.startswith("You are Guinier")


def test_migrate_moves_legacy_files_once_and_fixes_index_paths(tmp_path):
    kb = tmp_path / "ai_knowledge"
    (kb / "literature").mkdir(parents=True)
    (kb / "literature" / "paper.pdf").write_text("x")
    (kb / "group").mkdir()
    (kb / "group" / "sops.json").write_text("[]")
    (kb / "corrections.json").write_text("[]")
    (kb / "vector_db").mkdir()
    win = "C:\\Users\\a\\SWAXS\\ai_knowledge\\literature\\b.pdf"
    (kb / "ingestion_log.json").write_text(json.dumps({"literature": {
        "/Users/a/SWAXS/ai_knowledge/literature/paper.pdf": {"hash": "h"},
        win: {"hash": "w"}}}))
    acts = migrate(kb)
    L = KnowledgeLayout(kb)
    assert (L.reference("literature") / "paper.pdf").is_file()
    assert L.sops.is_file() and L.corrections.is_file() and L.vector_db.is_dir()
    assert not (kb / "group").exists() and not (kb / "corrections.json").exists()
    keys = set(json.loads(L.ingestion_log.read_text())["literature"])
    assert keys == {"/Users/a/SWAXS/ai_knowledge/reference/literature/paper.pdf",
                    "C:\\Users\\a\\SWAXS\\ai_knowledge\\reference\\literature\\b.pdf"}
    assert acts and migrate(kb) == []                     # idempotent


def test_migrate_never_overwrites_the_new_location(tmp_path):
    kb = tmp_path / "ai_knowledge"
    L = KnowledgeLayout(kb)
    L.rules_dir.mkdir(parents=True)
    L.corrections.write_text('[{"id": "corr_0001"}]')
    (kb / "corrections.json").write_text("[]")
    acts = migrate(kb)
    assert json.loads(L.corrections.read_text()) == [{"id": "corr_0001"}]
    assert (kb / "corrections.json").exists()             # left for a human to look at
    assert any("kept legacy" in a for a in acts)


# ── traces ────────────────────────────────────────────────────────────────────

def _assistant(tmp_path, monkeypatch):
    import src.ai.memory as memmod
    from src.ai.assistant import SWAXSAssistant
    monkeypatch.setattr(memmod, "_USER_ROOT_DIR", tmp_path / "users")
    monkeypatch.setenv("SWAXS_TRACES_DIR", str(tmp_path / "traces"))
    a = SWAXSAssistant(ai_knowledge_dir=str(tmp_path / "kb"), user_id="anj")
    monkeypatch.setattr(a, "_get_knowledge_base", lambda: None)
    return a


def _read(tmp_path):
    files = list((tmp_path / "traces").glob("*.jsonl"))
    assert len(files) == 1
    return [json.loads(l) for l in files[0].read_text().splitlines()]


def test_a_failed_turn_is_traced_with_its_outcome_and_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-test-SECRET-123")
    a = _assistant(tmp_path, monkeypatch)
    monkeypatch.setattr(a, "_get_client", lambda: None)
    out = a.chat("What is my Rg?", app_id="analysis")
    assert "No AI credentials" in out["text"]
    (rec,) = _read(tmp_path)
    assert rec["outcome"] == "no_credentials" and rec["app_id"] == "analysis"
    assert rec["question"] == "What is my Rg?" and rec["answer"].startswith("No AI")
    assert "SECRET" not in (tmp_path / "traces").joinpath(
        next((tmp_path / "traces").glob("*.jsonl")).name).read_text()


def test_an_exception_is_traced_and_chat_still_answers(tmp_path, monkeypatch):
    a = _assistant(tmp_path, monkeypatch)

    def boom(*a_, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(a, "_chat_impl", boom)
    out = a.chat("hi")
    assert "went wrong" in out["text"]
    (rec,) = _read(tmp_path)
    assert rec["outcome"] == "error" and "kaboom" in rec["error"]


def test_prompt_parts_and_missing_index_are_recorded(tmp_path, monkeypatch):
    (tmp_path / "analysis").mkdir()                       # kb_dir.parent holds the apps
    (tmp_path / "analysis" / "knowledge.md").write_text("# Analysis\nGuinier help.")
    a = _assistant(tmp_path, monkeypatch)
    traces.start(question="q")
    static, _dyn = a._build_system_prompt("q", "anj", None, "analysis")
    path = traces.finish({"text": "a"})
    rec = json.loads(path.read_text().splitlines()[-1])
    parts = [p["part"] for p in rec["prompt_parts"]]
    assert parts[0] == "system_prompt"
    assert "knowledge.md:analysis" in parts and "analysis_guidelines" in parts
    assert rec["retrieval"].startswith("index unavailable")


def test_traces_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("SWAXS_ASSISTANT_TRACES", "0")
    traces.start(question="q")
    assert traces.finish({"text": "a"}) is None
    assert not (tmp_path / "traces").exists()
