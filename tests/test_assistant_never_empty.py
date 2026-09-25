"""The assistant must NEVER return an empty answer.

An empty `text` renders as "no response" in the UI — the most embarrassing
failure mode, and one the operator hit often. chat() now forces a tool-less
summary call when the loop ends with no text, and falls back to a plain message
if even that is empty. Also: plot functions tolerate the extra kwargs the model
passes (the Porod branch used to raise on an unexpected `sigma`).
"""
import base64
import importlib

import numpy as np
import pytest

m = importlib.import_module("src.ai.assistant")


# ── fake anthropic client ─────────────────────────────────────────────────────
class _Block:
    def __init__(self, text=None, type="text"):
        self.type = type
        self.text = text


class _ToolBlock:
    def __init__(self, name="query_manifest", id="tu_1", inp=None):
        self.type = "tool_use"
        self.name = name
        self.id = id
        self.input = inp or {}


class _Resp:
    def __init__(self, content, stop="end_turn"):
        self.content = content
        self.stop_reason = stop
        self.usage = None


class _FakeMessages:
    def __init__(self, script):
        self.script = script
        self.n = 0

    def create(self, **kw):
        r = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        return r


class _FakeClient:
    def __init__(self, script):
        self.messages = _FakeMessages(script)


def _assistant(script):
    a = object.__new__(m.SWAXSAssistant)
    a._user_id = "u"
    a._model = "fake-model"
    a._get_client = lambda: _FakeClient(script)
    a._build_system_prompt = lambda **k: ("static", "")
    a._run_hints = lambda **k: []
    a._maybe_save_correction = lambda *a, **k: None
    return a


def test_empty_first_response_is_recovered():
    # round 1 returns NO text (would be "no response"); forced call returns text
    script = [_Resp([]), _Resp([_Block("Here is the summary.")])]
    out = _assistant(script).chat("hi", project_root=None)
    assert out["text"].strip() == "Here is the summary."


def test_totally_empty_falls_back_to_a_message():
    script = [_Resp([]), _Resp([])]           # every call empty
    out = _assistant(script).chat("hi", project_root=None)
    assert out["text"].strip(), "returned an empty answer (no response)"
    assert "didn't manage" in out["text"].lower() or "try again" in out["text"].lower()


def test_normal_text_answer_passes_through():
    script = [_Resp([_Block("Rg is 3.1 nm.")])]
    out = _assistant(script).chat("rg?", project_root=None)
    assert out["text"].strip() == "Rg is 3.1 nm."


def test_tool_use_with_wrong_stop_reason_is_still_executed():
    # Gateway quirk: a tool_use block arrives with stop_reason "end_turn" (not
    # "tool_use"). The loop must still run the tool and continue to a real answer,
    # not treat the round as final and return empty.
    script = [
        _Resp([_ToolBlock()], stop="end_turn"),      # tool_use, wrong stop_reason
        _Resp([_Block("Here is the final answer.")], stop="end_turn"),
    ]
    a = _assistant(script)
    a._dispatch_tool = lambda name, inp, **k: ("tool output", None)
    out = a.chat("analyse", project_root=None)
    assert out["text"].strip() == "Here is the final answer."
    assert out["tool_calls"], "the tool was not executed"


# ── plot kwarg tolerance ──────────────────────────────────────────────────────
def test_porod_and_curve_tolerate_extra_kwargs():
    from src.ai import plots
    q = np.linspace(0.05, 3.0, 120)
    I = 1e3 * q ** -4 + 1.0
    sig = np.sqrt(I)
    # these used to raise "unexpected keyword 'sigma'" / q_min / Rg
    for extra in ({"sigma": sig}, {"sigma": sig, "q_min": 0.1, "q_max": 2.0, "Rg": 3.1}):
        b = plots.generate_plot("porod", q=q, I=I, **extra)
        assert len(base64.b64decode(b)) > 1000
    b = plots.generate_plot("curve", q=q, I=I, sigma=sig)
    assert len(base64.b64decode(b)) > 1000
