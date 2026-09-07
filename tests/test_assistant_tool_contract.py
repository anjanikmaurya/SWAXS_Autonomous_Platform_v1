"""
tests/test_assistant_tool_contract.py — the assistant must never send Claude a
history that violates the tool_use/tool_result contract.

The observed failure (a real gateway 400):
    "messages.18: tool_use ids were found without tool_result blocks immediately
     after: toolu_… Each tool_use block must have a corresponding tool_result block
     in the next message."

Root cause: a response cut off by max_tokens (or a crash) could leave an assistant
`tool_use` persisted into session history with no matching `tool_result` after it.
Once saved, that corrupt history reloaded and 400-ed EVERY subsequent request in the
session. The fix repairs the outgoing messages (_sanitize_messages) and stops
persisting dangling tool_use turns (_drop_tool_use).
"""
from __future__ import annotations

from src.ai.assistant import (_sanitize_messages, _drop_tool_use,
                              _delta_is_balanced, _tool_use_ids, _tool_result_ids)


def _tu(id_):        # an assistant turn with one tool_use block
    return {"role": "assistant",
            "content": [{"type": "text", "text": "let me check"},
                        {"type": "tool_use", "id": id_, "name": "t", "input": {}}]}


def _tr(id_):        # the matching user tool_result turn
    return {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": id_, "content": "ok"}]}


def test_wellformed_history_is_unchanged_and_balanced():
    msgs = [{"role": "user", "content": "hi"}, _tu("a"), _tr("a"),
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
    out = _sanitize_messages(msgs)
    assert out == msgs, "a well-formed history must pass through unchanged"
    assert _delta_is_balanced(out)


def test_dangling_tool_use_before_user_text_is_repaired():
    """The exact bug: a tool_use with no result, followed by the next user prompt."""
    msgs = [{"role": "user", "content": "q1"}, _tu("x"),
            {"role": "user", "content": "q2"}]
    out = _sanitize_messages(msgs)
    assert _delta_is_balanced(out), "sanitize did not repair the contract"
    # The synthetic result sits in the SAME user turn, immediately after the tool_use,
    # so no two consecutive user messages are created.
    assert out[2]["role"] == "user"
    assert _tool_result_ids(out[2]["content"]) == {"x"}
    assert any(b.get("type") == "text" and b.get("text") == "q2"
               for b in out[2]["content"]), "the user's prompt text was lost"
    assert len([m for m in out if m["role"] == "user"]) == 2


def test_dangling_tool_use_at_end_of_history_gets_a_result_turn():
    msgs = [{"role": "user", "content": "q"}, _tu("z")]
    out = _sanitize_messages(msgs)
    assert _delta_is_balanced(out)
    assert out[-1]["role"] == "user" and _tool_result_ids(out[-1]["content"]) == {"z"}
    assert out[-1]["content"][0].get("is_error") is True


def test_partially_answered_multi_tool_turn_is_completed():
    """Two tool_use ids but the result turn only answers one — fill the gap."""
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "a", "name": "t", "input": {}},
                {"type": "tool_use", "id": "b", "name": "t", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}]
    out = _sanitize_messages(msgs)
    assert _delta_is_balanced(out)
    assert _tool_result_ids(out[2]["content"]) == {"a", "b"}


def test_sanitize_is_idempotent():
    msgs = [{"role": "user", "content": "q1"}, _tu("x"),
            {"role": "user", "content": "q2"}]
    once = _sanitize_messages(msgs)
    twice = _sanitize_messages(once)
    assert once == twice, "sanitize must be idempotent"


def test_drop_tool_use_removes_unanswered_calls():
    content = [{"type": "text", "text": "partial answer"},
               {"type": "tool_use", "id": "cut", "name": "t", "input": {}}]
    cleaned = _drop_tool_use(content)
    assert not _tool_use_ids(cleaned), "a final-turn tool_use must be stripped"
    assert any(b.get("type") == "text" for b in cleaned), "the text answer was lost"


def test_drop_tool_use_never_returns_empty():
    cleaned = _drop_tool_use([{"type": "tool_use", "id": "only", "name": "t", "input": {}}])
    assert cleaned, "must not return an empty content list (API rejects it)"
