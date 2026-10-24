"""
tests/test_ai_design.py: the AI next-recipe levels (src/optimizer/ai_design.py)
with a scripted stand-in AI, so no login is needed.

What must hold whatever the AI answers:
  * Co-pilot can only pick from the shortlist and must cite; otherwise the
    optimiser's own pick is used and the fallback is recorded.
  * Pilot's recipe must be complete, finite, inside the search bounds, pump
    windows, hard caps, working range and trust region, and new; one retry with
    the reason, then the optimiser's pick.
  * Advisor never picks; its ranges are applied only inside the search bounds
    (auto) or wait for the operator (ask), and can be undone.
  * No AI login, an exception or a non-JSON answer: the loop keeps going on the
    optimiser, nothing is blocked.
"""
from __future__ import annotations

import json

import pytest

from src.optimizer import methods as M
from src.optimizer import ParameterSpace
from src.optimizer.ai_design import AIDesign
from src.reactor import load_config
from src.simulator.ground_truth import truth_from_recipe


def _space():
    return ParameterSpace.from_config(load_config())


class _Resp:
    def __init__(self, text):
        self.content = [type("B", (), {"text": text})()]


class FakeAI:
    """Answers from a list (the last one repeats); records every prompt."""
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []
        self.messages = self

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        assert kw.get("temperature") == 0
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return _Resp(a if isinstance(a, str) else json.dumps(a))


KNOW = {"set_id": "pd", "version": 3,
        "cards": [{"id": "L1", "param": "T_reac", "effect": "hotter, larger", "range": [240, 280],
                   "source": "Smith 2021"}]}


def _ctl(level, ai, n_init=3, **kw):
    inner = M.make("gp_ei", _space(), target_size=2.5, tolerance=0.3, pdi_cap=0.1,
                   budget=20, n_init=n_init)
    kw.setdefault("review_every", 5 if level == "ai_pilot" else 0)
    c = AIDesign(inner, level, knowledge=KNOW, client_factory=lambda: ai,
                 caps={"T_max": 320, "F_tot_max": 150}, **kw)
    return c.start()


def _feed(c, n, rid0=0):
    for j in range(n):
        p = c.ask()
        t = truth_from_recipe(p, seed_key=f"r{rid0 + j}")
        c.label_last(f"r{rid0 + j}")
        c.tell(p, t["R_nm"], t["pdi"], 0.9, recipe_id=f"r{rid0 + j}")


def test_cold_start_is_never_the_ais():
    ai = FakeAI({"choice": 1, "reason": "x", "cited": ["data"]})
    c = _ctl("ai_copilot", ai)
    _feed(c, 3)
    assert ai.prompts == [] and c.decisions == []


def test_copilot_picks_from_the_shortlist_and_cites():
    ai = FakeAI({"choice": 2, "reason": "card L1 says hotter is larger", "cited": ["L1", "nope"],
                 "expected": {"diameter_nm": 5.0, "pdi": 0.08}})
    c = _ctl("ai_copilot", ai)
    _feed(c, 3)
    st = c.prepare_pick()
    assert len(st["shortlist_params"]) == 5
    assert "<data>" in ai.prompts[0] if ai.prompts else True
    res = c.decide(st)
    assert res["ok"] and res["choice"] == 2 and res["cited"] == ["L1"], "unknown ids are dropped"
    p = c.commit_pick(st, res)
    assert p == st["shortlist_params"][1]
    d = c.decisions[-1]
    assert d["by"] == "AI Co-pilot" and not d["same_as_optimiser"] and d["knowledge_version"] == 3
    assert "Smith 2021" in ai.prompts[0] and '"L1"' in ai.prompts[0], "approved cards are in the context"


@pytest.mark.parametrize("bad, why", [
    ({"choice": 9, "cited": ["data"]}, "not on the shortlist"),
    ({"choice": "one", "cited": ["data"]}, "not a shortlist number"),
    ({"choice": 1, "cited": []}, "cited"),
    ("I think number 2", "format"),
    (RuntimeError("gateway down"), "AI call failed"),
])
def test_copilot_falls_back_to_the_optimisers_pick(bad, why):
    c = _ctl("ai_copilot", FakeAI(bad))
    _feed(c, 3)
    st = c.prepare_pick()
    res = c.decide(st)
    assert not res["ok"] and why in res["why"]
    p = c.commit_pick(st, res)
    assert p == st["shortlist_params"][0] and c.decisions[-1]["by"] == "optimiser (fallback)"
    assert c.n_ai_fallbacks == 1


def test_no_login_runs_as_the_optimiser():
    c = _ctl("ai_copilot", None)
    _feed(c, 8)
    assert len(c.history) == 8
    assert all(d["by"] == "optimiser (fallback)" and d["fallback"] == "no AI login"
               for d in c.decisions)


def test_pilot_safety_every_unsafe_recipe_is_refused():
    sp = _space()
    c = _ctl("ai_pilot", FakeAI({"recipe": {}, "cited": ["data"]}))
    _feed(c, 3)
    st = c.prepare_pick()
    best = st["best"]
    ok = dict(best)
    lo_t, hi_t = st["trust_region"]["T_reac"]
    ok["T_reac"] = best["T_reac"] + 3 if best["T_reac"] + 3 <= hi_t else best["T_reac"] - 3
    cases = {
        "missing": {k: v for k, v in ok.items() if k != "F_tot"},
        "nan": {**ok, "x_TOP": float("nan")},
        "hot": {**ok, "T_reac": 999},
        "box": {**ok, "x_ODE": 0.31},
        "trust": {**ok, "T_reac": st["trust_region"]["T_reac"][0] - 5},
        "repeat": dict(c.history[0]["params"]),
    }
    for name, rec in cases.items():
        res = c._check_pilot(st, {"recipe": rec, "cited": ["data"]})
        assert not res["ok"], name
    assert c._check_pilot(st, {"recipe": ok, "cited": ["data"]})["ok"]
    assert sp.valid(ok)


def test_pilot_gets_one_retry_with_the_reason_then_falls_back():
    good_later = None
    c = _ctl("ai_pilot", FakeAI({"recipe": {"T_reac": 999, "F_tot": 60, "x_ODE": .1, "x_TOP": .1,
                                            "x_oley": .1}, "cited": ["data"]}))
    _feed(c, 3)
    st = c.prepare_pick()
    res = c.decide(st)
    ai = c.client_factory()
    assert not res["ok"] and len(ai.prompts) == 2 and "refused" in ai.prompts[1]
    p = c.commit_pick(st, res)
    assert p == st["shortlist_params"][0]
    assert good_later is None


def test_pilot_needs_reviews():
    with pytest.raises(ValueError):
        _ctl("ai_pilot", None, review_every=0)


def test_advisor_never_picks_and_applies_only_safe_ranges():
    sp = _space()
    tlo, thi = sp.bounds["T_reac"]
    safe = {"note": "PDI is stuck; card L1 suggests 240 to 280.", "bounds": {"T_reac": [240, 280]},
            "cited": ["L1"]}
    c = _ctl("ai_advisor", FakeAI(safe), review_every=4)
    _feed(c, 4)
    assert all(d["kind"] == "review" for d in c.decisions) or c.decisions == []
    _feed(c, 1, 4)                     # the review runs before the 5th pick
    rv = [d for d in c.decisions if d["kind"] == "review"]
    assert rv and rv[0]["action"] == "applied"
    assert c.work_bounds["T_reac"] == (240.0, 280.0)
    # new proposals respect the working range; the surrogate keeps the full space
    for _ in range(3):
        p = c.inner.ask()
        assert 240 <= p["T_reac"] <= 280
        c.inner._n_asked -= 1
    assert c.undo() and c.work_bounds is None


@pytest.mark.parametrize("bounds, why", [
    ({"T_reac": [100, 200]}, "outside the search range"),
    ({"T_reac": [250, 251]}, "narrower than 10"),
    ({"pressure": [1, 2]}, "not a recipe setting"),
])
def test_advisor_unsafe_ranges_go_back_once_then_are_dropped(bounds, why):
    ai = FakeAI({"note": "n", "bounds": bounds, "cited": ["data"]})
    c = _ctl("ai_advisor", ai, review_every=3)
    _feed(c, 3)
    st = c.prepare_review()
    res = c.decide(st)
    assert not res["ok"] and why in res["why"] and len(ai.prompts) == 2
    d = c.commit_review(st, res)
    assert d["action"] == "dropped" and c.work_bounds is None


def test_advisor_ask_me_first_waits_for_the_operator():
    c = _ctl("ai_advisor", FakeAI({"note": "narrow T", "bounds": {"T_reac": [240, 290]},
                                   "cited": ["L1"]}), review_every=3, apply="ask")
    _feed(c, 4)
    assert c.pending_suggestion and c.work_bounds is None
    assert c.resolve_pending(True) and c.work_bounds["T_reac"] == (240.0, 290.0)
    assert not c.resolve_pending(True)


def test_starting_recipes_only_means_no_cards_for_the_ai():
    ai = FakeAI({"choice": 1, "reason": "r", "cited": ["L1", "data"]})
    c = _ctl("ai_copilot", ai, every_pick=False)
    _feed(c, 4)
    assert "Smith 2021" not in ai.prompts[0]
    assert c.decisions[-1]["cited"] == ["data"]


def test_same_state_same_prompt_is_cached():
    ai = FakeAI({"choice": 1, "reason": "r", "cited": ["data"]})
    c = _ctl("ai_copilot", ai)
    _feed(c, 3)
    st = c.prepare_pick()
    c.decide(st), c.decide(st)
    assert len(ai.prompts) == 1


def test_state_survives_a_restart():
    c = _ctl("ai_advisor", FakeAI({"note": "n", "bounds": {"T_reac": [240, 290]}, "cited": ["data"]}),
             review_every=3)
    _feed(c, 4)
    st = json.loads(json.dumps(c.to_state()))
    c2 = _ctl("ai_advisor", None, review_every=3)
    c2.from_state(st)
    assert c2.work_bounds["T_reac"] == (240.0, 290.0) and c2.decisions == c.decisions
    assert c2.reviews_done == 1


def test_commit_is_dropped_if_the_campaign_moved_on():
    c = _ctl("ai_copilot", FakeAI({"choice": 1, "reason": "r", "cited": ["data"]}))
    _feed(c, 3)
    st = c.prepare_pick()
    res = c.decide(st)
    c.inner._n_asked += 1                # something else consumed a proposal meanwhile
    assert c.commit_pick(st, res) is None


def test_ai_levels_race_on_the_simulator():
    from src.optimizer.study import run_study
    res = run_study(_space(), ["ai_copilot"], {"target_size": 4.0, "tolerance": 0.3,
                                               "pdi_cap": 0.15, "budget": 8, "n_init": 4},
                    ai_client_factory=lambda: None)
    run = res["ai_copilot"]["repeats"][0]
    assert run["n"] == 8 and "no AI login" in res["ai_copilot"]["label"]


# ── in the Analyser: async picks, routes, persistence ─────────────────────────
import importlib.util as _u
import sys as _sys
import threading as _th
import time as _time

from src.optimizer import ai_design as _aid


def _app(tmp_path, monkeypatch, tag, ai, resume=False):
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")
    monkeypatch.setenv("SWAXS_DESIGN_KNOWLEDGE_DIR", str(tmp_path / "design"))
    if resume:
        monkeypatch.setenv("SWAXS_RESUME", "1")
    else:
        monkeypatch.delenv("SWAXS_RESUME", raising=False)
    monkeypatch.setattr(_aid, "_client", lambda: ai)
    spec = _u.spec_from_file_location(tag, "analyzer/app.py")
    m = _u.module_from_spec(spec)
    _sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


BODY = {"target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 12, "n_init": 2}


def _feed_app(a, k):
    for _ in range(k):
        a._ai_wait()
        rid = list(a._pending)[0]
        p = a._pending[rid]
        t = truth_from_recipe(p, seed_key=rid)
        a._feed_campaign(f"{rid}_s_SAXS_subtracted.dat",
                         {"size": {"radius": t["R_nm"]}, "pdi": t["pdi"], "confidence": 0.9})
    a._ai_wait()


def test_pilot_start_needs_confirmation_and_reviews(tmp_path, monkeypatch):
    a = _app(tmp_path, monkeypatch, "an_ai_1", None)
    cl = a.app.test_client()
    r = cl.post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_pilot", "review_every": 5}})
    assert r.status_code == 400 and "confirm" in r.get_json()["error"]
    r = cl.post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_pilot", "review_every": 0,
                                                             "confirmed": True}})
    assert r.status_code == 400 and "review" in r.get_json()["error"]
    r = cl.post("/api/campaign/start", json={**BODY, "goal": "benchmark", "ai": {"level": "ai_copilot"}})
    assert r.status_code == 400
    assert a._campaign is None


def test_copilot_in_the_app_picks_in_the_background(tmp_path, monkeypatch):
    ai = FakeAI({"choice": 2, "reason": "data says hotter", "cited": ["data"]})
    a = _app(tmp_path, monkeypatch, "an_ai_2", ai)
    cl = a.app.test_client()
    assert cl.post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_copilot"}}).get_json()["ok"]
    _feed_app(a, 3)                          # 2 cold-start runs, then AI picks
    st = cl.get("/api/campaign").get_json()
    assert st["ai"]["level"] == "ai_copilot" and st["ai"]["n_picks"] >= 1
    d = st["ai"]["decisions"][0]
    assert d["by"] == "AI Co-pilot" and d["recipe_id"] and d["choice"] == 2
    assert len(a._pending) == 1, "exactly one condition in flight"
    rec = next(h for h in a._campaign.history if h.get("recipe_id") == d["recipe_id"])
    assert rec.get("decided_by") == "AI Co-pilot"


def test_the_ai_call_does_not_hold_the_campaign_lock(tmp_path, monkeypatch):
    class Slow(FakeAI):
        def create(self, **kw):
            _time.sleep(0.8)
            return super().create(**kw)
    a = _app(tmp_path, monkeypatch, "an_ai_3", Slow({"choice": 1, "reason": "r", "cited": ["data"]}))
    cl = a.app.test_client()
    cl.post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_copilot"}})
    _feed_app(a, 1)
    rid = list(a._pending)[0]
    a._feed_campaign(f"{rid}_s_SAXS_subtracted.dat", {"size": {"radius": 2.0}, "pdi": 0.1, "confidence": 0.9})
    t0 = _time.time()
    st = cl.get("/api/campaign").get_json()
    assert _time.time() - t0 < 0.5, "status answered while the AI was thinking"
    assert st["ai_busy"]["pick"] is True
    a._ai_wait()
    assert len(a._pending) == 1


def test_pause_while_the_ai_thinks_drops_its_pick(tmp_path, monkeypatch):
    class Slow(FakeAI):
        def create(self, **kw):
            _time.sleep(0.5)
            return super().create(**kw)
    a = _app(tmp_path, monkeypatch, "an_ai_4", Slow({"choice": 1, "reason": "r", "cited": ["data"]}))
    cl = a.app.test_client()
    cl.post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_copilot"}})
    _feed_app(a, 1)
    rid = list(a._pending)[0]
    a._feed_campaign(f"{rid}_s_SAXS_subtracted.dat", {"size": {"radius": 2.0}, "pdi": 0.1, "confidence": 0.9})
    cl.post("/api/campaign/pause", json={"paused": True})
    a._ai_wait()
    assert not a._pending, "paused: the AI's pick is not issued"
    cl.post("/api/campaign/pause", json={"paused": False})
    a._ai_wait()
    assert len(a._pending) == 1


def test_advisor_ask_first_routes_and_undo(tmp_path, monkeypatch):
    ai = FakeAI({"note": "narrow T", "bounds": {"T_reac": [240, 290]}, "cited": ["data"]})
    a = _app(tmp_path, monkeypatch, "an_ai_5", ai)
    cl = a.app.test_client()
    cl.post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_advisor", "review_every": 5,
                                                         "apply": "ask"}})
    _feed_app(a, 5)
    st = cl.get("/api/campaign").get_json()
    assert st["ai"]["pending_suggestion"]["bounds"] == {"T_reac": [240.0, 290.0]}
    assert cl.post("/api/campaign/ai/suggestion", json={"accept": True}).get_json()["ok"]
    assert a._campaign.work_bounds["T_reac"] == (240.0, 290.0)
    assert cl.post("/api/campaign/ai/suggestion", json={"accept": True}).status_code == 409
    assert cl.post("/api/campaign/ai/undo").get_json()["ok"]
    assert a._campaign.work_bounds is None


def test_ai_state_survives_a_restart(tmp_path, monkeypatch):
    ai = FakeAI({"note": "narrow T", "bounds": {"T_reac": [240, 290]}, "cited": ["data"]})
    a1 = _app(tmp_path, monkeypatch, "an_ai_6a", ai, resume=True)
    a1.app.test_client().post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_advisor",
                                                                           "review_every": 5}})
    _feed_app(a1, 5)
    assert a1._campaign.work_bounds
    a2 = _app(tmp_path, monkeypatch, "an_ai_6b", None, resume=True)
    a2._restore_campaign()
    assert a2._is_ai(a2._campaign) and a2._campaign.level == "ai_advisor"
    assert a2._campaign.work_bounds["T_reac"] == (240.0, 290.0)
    assert a2._campaign.decisions == a1._campaign.decisions


# ── model, thinking and the connection test ──────────────────────────────────
def test_thinking_drops_temperature_and_sets_a_budget():
    kw = _aid._call_kwargs("us.anthropic.claude-opus-5", "deep")
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert "temperature" not in kw and kw["max_tokens"] > 8000
    assert _aid._call_kwargs("m", "off")["temperature"] == 0


def test_chosen_model_and_thinking_are_used_and_recorded():
    seen = {}

    class Spy(FakeAI):
        def create(self, **kw):
            seen.update(kw)
            if "thinking" in kw:
                kw = {**kw, "temperature": 0}      # FakeAI asserts temperature 0
            return super().create(**kw)
    c = _ctl("ai_copilot", Spy({"choice": 1, "reason": "r", "cited": ["data"]}),
             model="us.anthropic.claude-haiku-4-5-20251001-v1:0", thinking="standard")
    _feed(c, 4)
    assert seen["model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0" and seen["thinking"]["budget_tokens"] == 2000
    d = c.decisions[-1]
    assert d["model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0" and d["thinking"] == "standard"


def test_connection_test_reports_and_never_shows_a_secret():
    ok = _aid.test_connection("us.anthropic.claude-sonnet-4-6", client_factory=lambda: FakeAI('{"ok": true}'))
    assert ok["ok"] and ok["ms"] >= 0
    bad = _aid.test_connection("x", client_factory=lambda: FakeAI(
        RuntimeError("401 invalid key sk-ant-REALSECRET123456 for Bearer abc.def")))
    assert not bad["ok"] and "REALSECRET" not in bad["error"] and "abc.def" not in bad["error"]
    none = _aid.test_connection("x", client_factory=lambda: None)
    assert not none["ok"] and "no AI login" in none["error"]


def test_routes_validate_model_and_thinking(tmp_path, monkeypatch):
    a = _app(tmp_path, monkeypatch, "an_ai_7", FakeAI('{"ok": true}'))
    cl = a.app.test_client()
    st = cl.get("/api/ai/status").get_json()
    assert st["available"] and {"off", "standard", "deep"} <= set(st["thinking"])
    assert any(m["id"] == "us.anthropic.claude-opus-5" for m in st["models"])
    r = cl.post("/api/ai/test", json={"model": "us.anthropic.claude-opus-5"}).get_json()
    assert r["ok"] and r["model"] == "us.anthropic.claude-opus-5"
    assert cl.post("/api/ai/test", json={"model": "gpt-9"}).status_code == 400
    r = cl.post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_copilot", "model": "gpt-9"}})
    assert r.status_code == 400 and "model" in r.get_json()["error"]
    assert cl.post("/api/campaign/start", json={**BODY, "ai": {"level": "ai_copilot",
                   "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "thinking": "standard"}}).get_json()["ok"]
    assert a._campaign.model == "us.anthropic.claude-haiku-4-5-20251001-v1:0" and a._campaign.thinking == "standard"
    assert a._campaign_meta["ai"]["model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_the_analyser_uses_guiniers_login_and_gateway_model(tmp_path, monkeypatch):
    """The same ~/.claude/settings.json the assistant reads gives the Analyser its
    login and model; with nothing set the default is a gateway id, never a bare
    'claude-...' id that the SLAC Bedrock gateway rejects."""
    import json as _j
    from src.ai import models as MD
    for k in ("ANTHROPIC_MODEL", "SWAXS_DESIGN_MODEL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(MD, "_ROOT", tmp_path)                 # no .env
    monkeypatch.setenv("HOME", str(tmp_path))
    assert MD.default_model("SWAXS_DESIGN_MODEL") == MD.GATEWAY_DEFAULT
    assert MD.GATEWAY_DEFAULT.startswith("us.anthropic.")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(_j.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://gw.example", "ANTHROPIC_AUTH_TOKEN": "tok-123",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-5"}}))
    assert _aid._model() == "us.anthropic.claude-sonnet-5"
    s = MD.login_summary()
    assert s["login"] == "gateway token" and s["gateway"] and "tok-123" not in str(s)
    assert all(m.startswith("us.anthropic.") for m, _ in MD.MODELS)
    assert [m for m, _ in _aid.MODELS][0] == "" and len(_aid.MODELS) == len(MD.MODELS) + 1
