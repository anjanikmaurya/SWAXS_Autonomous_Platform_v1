"""
tests/test_replay.py: verdicts, harness settings, the replay of past campaigns
and the approve-only improvement loop (src/design_knowledge/replay.py).

Past campaigns here are made from the ground-truth simulator, so the replay
can be checked against something known.
"""
from __future__ import annotations

import importlib.util as _u
import sys as _sys

import pytest

from src.design_knowledge import replay as RP
from src.design_knowledge import store as S
from src.design_knowledge.campaigns import entry_from_campaign
from src.optimizer import ParameterSpace
from src.reactor import load_config
from src.simulator.ground_truth import truth_from_recipe, DEFAULTS


@pytest.fixture(autouse=True)
def kdir(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_DESIGN_KNOWLEDGE_DIR", str(tmp_path / "design"))


def _space():
    return ParameterSpace.from_config(load_config())


def _campaign(cid, n=14, seed=0, ended="2026-10-01"):
    sp = _space()
    runs = []
    for j, p in enumerate(sp.sobol(n, seed=seed)):
        t = truth_from_recipe(p, seed_key=f"{cid}_{j}")
        runs.append({"params": p, "size": t["R_nm"], "pdi": t["pdi"], "confidence": 0.9})
    e = entry_from_campaign({"campaign_id": cid, "run_tag": cid, "target_size": DEFAULTS["R_opt"],
                             "tolerance": 0.3, "pdi_cap": 0.15, "ended_at": ended}, runs, "/p")
    e["ended"] = ended
    return e


def _set_with(n_camps=2):
    s = S.create_set("Sim")
    for i in range(n_camps):
        S.add_campaign(s["id"], _campaign(f"Run{i + 1}", seed=i, ended=f"2026-10-0{i + 1}"))
    S.add_drafts(s["id"], [{"param": "T_reac", "effect": "near the optimum",
                            "range": [DEFAULTS["T_opt"] - 15, DEFAULTS["T_opt"] + 15], "kind": "literature"}])
    S.review(s["id"], "L1", "approve")
    return S.load_set(s["id"])


# ── verdicts and harness settings ────────────────────────────────────────────
def test_verdicts_count_at_once_and_one_per_recipe():
    s = S.create_set("v")
    v1 = S.add_verdict(s["id"], {"call": "good", "why": "PDI fell", "recipe_id": "Run3_r011"})
    assert v1["id"] == "V1" and v1["status"] == "approved"
    v2 = S.add_verdict(s["id"], {"call": "bad", "why": "changed my mind", "recipe_id": "Run3_r011"})
    vs = S.load_set(s["id"])["verdicts"]
    assert [v["status"] for v in vs] == ["retired", "approved"] and v2["id"] == "V2"
    with pytest.raises(ValueError):
        S.add_verdict(s["id"], {"call": "maybe"})
    assert S.retire_verdict(s["id"], "V2") and not S.retire_verdict(s["id"], "V2")


def test_harness_defaults_and_limits():
    s = S.create_set("h")
    assert S.harness(s) == S.HARNESS_DEFAULTS
    h = S.set_harness(s["id"], {"n_knowledge": 99, "shortlist_k": 1, "trust": "x", "evil": 1})
    assert h == {"n_knowledge": 10, "shortlist_k": 2, "trust": 0.25}


def test_freeze_carries_verdicts_and_harness():
    from src.design_knowledge.freeze import for_campaign
    s = _set_with(1)
    S.add_verdict(s["id"], {"call": "bad", "why": "aggregated", "recipe_id": "x", "campaign_id": "c"})
    out = for_campaign({"set_id": s["id"], "n": 0, "use": {"verdicts": True}}, _space())
    assert out["frozen"]["verdicts"][0]["why"] == "aggregated"
    assert out["frozen"]["harness"] == S.HARNESS_DEFAULTS
    out = for_campaign({"set_id": s["id"], "n": 0, "use": {"verdicts": False}}, _space())
    assert out["frozen"]["verdicts"] == []


# ── replay ───────────────────────────────────────────────────────────────────
def test_emulator_reproduces_the_measured_runs():
    sp = _space()
    e = _campaign("RunX", n=16)
    emu = RP.Emulator(e, sp)
    import numpy as np
    rng = np.random.default_rng(0)
    r0 = e["runs"][0]
    R, P = emu(r0["params"], rng)
    assert abs(R - r0["size"]) / r0["size"] < 0.1
    with pytest.raises(ValueError):
        RP.Emulator({"runs": e["runs"][:3]}, sp)


def test_evaluate_scores_every_arm_and_is_reproducible():
    s = _set_with(2)
    ev = RP.evaluate(s, _space(), repeats=2, budget=12)
    assert [r["arm"] for r in ev["summary"]] == list(RP.ARMS)
    assert ev["campaigns"] == ["Run1", "Run2"] and "Indicative" in ev["caveat"]
    for r in ev["summary"]:
        assert 1 <= r["runs_to_spec"] <= 13 and r["trials"] == 4
    assert RP.evaluate(s, _space(), repeats=2, budget=12)["summary"] == ev["summary"]


def test_campaigns_without_a_target_are_left_out_with_a_note():
    s = S.create_set("t")
    e = _campaign("RunOld")
    e.pop("tolerance")
    S.add_campaign(s["id"], e)
    ev = RP.evaluate(S.load_set(s["id"]), _space())
    assert ev["summary"] == [] and "tolerance" in ev["notes"][0] and "No usable" in ev["caveat"]


def test_ai_arm_without_login_runs_as_the_optimiser():
    s = _set_with(1)
    ev = RP.evaluate(s, _space(), arms=("ai_copilot",), repeats=1, budget=10,
                     client_factory=lambda: None)
    assert ev["summary"][0]["fallbacks"] > 0


def test_improvement_needs_two_campaigns_and_a_heldout_win(monkeypatch):
    sp = _space()
    assert not RP.propose_improvement(_set_with(1), sp)["ok"]
    s = _set_with(2)
    # practice says n_knowledge 5 is better; the held-out campaign disagrees: not proposed
    calls = []

    def fake_eval(s_, space, *, arms, repeats, h, held_ids, client_factory=None, budget=None):
        calls.append((h["n_knowledge"], tuple(held_ids)))
        held = held_ids == [s_["campaigns"][-1]["campaign_id"]]
        better = (h["n_knowledge"] == 5) != held          # wins on practice, loses held-out
        return {"summary": [{"arm": arms[0], "runs_to_spec": 4.0 if better else 6.0, "best_after_10": 1.0}]}
    monkeypatch.setattr(RP, "evaluate", fake_eval)
    out = RP.propose_improvement(s, sp)
    assert out["ok"] and out["proposal"] is None and "not on the held-out" in out["why"]
    # now it wins on both: proposed, but the set is NOT changed until approved
    monkeypatch.setattr(RP, "evaluate", lambda s_, space, **kw: {"summary": [{
        "arm": kw["arms"][0], "runs_to_spec": 3.0 if kw["h"]["n_knowledge"] == 5 else 6.0, "best_after_10": 1.0}]})
    out = RP.propose_improvement(s, sp)
    assert out["proposal"]["n_knowledge"] == 5
    assert S.harness(S.load_set(s["id"]))["n_knowledge"] == 3


# ── the Analyser routes ──────────────────────────────────────────────────────
def _app(tmp_path, monkeypatch, tag):
    pytest.importorskip("scipy")
    (tmp_path / "proj" / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path / "proj"))
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")
    monkeypatch.delenv("SWAXS_RESUME", raising=False)
    spec = _u.spec_from_file_location(tag, "analyzer/app.py")
    m = _u.module_from_spec(spec)
    _sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


def test_verdict_route_keeps_the_call_in_the_run_and_the_set(tmp_path, monkeypatch):
    a = _app(tmp_path, monkeypatch, "an_rp_1")
    cl = a.app.test_client()
    sid = cl.post("/api/knowledge/sets", json={"name": "Pd"}).get_json()["set"]["id"]
    cl.post("/api/campaign/start", json={"target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1,
                                         "budget": 10, "n_init": 3,
                                         "knowledge": {"set_id": sid, "n": 0}})
    rid = list(a._pending)[0]
    a._feed_campaign(f"{rid}_s_SAXS_subtracted.dat", {"size": {"radius": 2.2}, "pdi": 0.08, "confidence": 0.9})
    st = cl.get("/api/campaign").get_json()
    assert st["runs"][0]["recipe_id"] == rid and st["runs"][0]["by"] == "cold start (spread)"
    assert cl.post("/api/campaign/verdict", json={"recipe_id": rid, "call": "meh"}).status_code == 400
    assert cl.post("/api/campaign/verdict", json={"recipe_id": "nope", "call": "good"}).status_code == 404
    r = cl.post("/api/campaign/verdict", json={"recipe_id": rid, "call": "good", "why": "close to 5 nm"}).get_json()
    assert r["ok"] and r["kept_in_set"]
    assert a._campaign.history[0]["verdict"] == {"call": "good", "why": "close to 5 nm"}
    assert S.load_set(sid)["verdicts"][0]["recipe_id"] == rid


def test_evaluation_job_and_approval(tmp_path, monkeypatch):
    a = _app(tmp_path, monkeypatch, "an_rp_2")
    cl = a.app.test_client()
    s = _set_with(2)
    r = cl.post(f"/api/knowledge/set/{s['id']}/evaluate", json={"repeats": 1}).get_json()
    assert r["ok"]
    a._eval_wait()
    j = cl.get(f"/api/knowledge/set/{s['id']}/evaluation").get_json()
    assert j["state"] == "done" and j["result"]["summary"]
    assert S.load_set(s["id"])["evaluations"], "kept with the set"
    # a proposal is applied only on approval
    S.set_proposal(s["id"], {"proposal": {"n_knowledge": 5, "shortlist_k": 5, "trust": 0.25},
                             "why": "test"})
    assert cl.post(f"/api/knowledge/set/{s['id']}/proposal", json={"approve": True}).get_json()["ok"]
    full = S.load_set(s["id"])
    assert S.harness(full)["n_knowledge"] == 5 and full["proposal"] is None
    assert cl.post(f"/api/knowledge/set/{s['id']}/proposal", json={"approve": True}).status_code == 409
