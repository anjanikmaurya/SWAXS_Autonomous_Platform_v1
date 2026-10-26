"""Knowledge for the AI fitter (src/fitting/fit_knowledge.py): the Fitter part of
a named knowledge base. Guinier's sources chosen part by part, the base's notes
and judged fits, freezing, what the AI is shown, the with/without evaluation,
and the Analyser routes. A stand-in client replaces the AI, so these tests need
no login and cost nothing."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fitting import fit_knowledge as fk  # noqa: E402

Q = np.geomspace(0.05, 5, 200)


def _sphere(R, scale=1.0):
    x = Q * R
    f = 3 * (np.sin(x) - x * np.cos(x)) / x ** 3
    return scale * f ** 2 + 1e-4


class FakeClient:
    def __init__(self, answer):
        self.answer, self.calls = answer, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text=self.answer)], usage=None)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("SWAXS_DESIGN_KNOWLEDGE_DIR", str(tmp_path / "design"))


@pytest.fixture
def base(tmp_path):
    from src.design_knowledge import store
    return store.create_set("Pd nanoparticles", ["Pd"])["id"]


def test_sources_and_defaults():
    st = {s["id"]: s for s in fk.shared_status()}
    assert set(st) == {"catalogue", "corrections", "sops", "guidelines", "literature", "cases"}
    assert st["catalogue"]["always"] and st["catalogue"]["available"]
    # the catalogue can never be switched off; unknown ids are dropped
    assert fk.clean_sources(["sops", "bogus"]) == ["catalogue", "sops"]
    assert fk.clean_sources(None) == list(fk.DEFAULT_SOURCES)


def test_notes_are_versioned_drafts_need_approval(base):
    n = fk.add_note(base, "  cores are CdSe,   about 3 nm ", who="ak")
    assert n["text"] == "cores are CdSe, about 3 nm" and n["status"] == "active"
    with pytest.raises(ValueError):
        fk.add_note(base, "cores are CdSe, about 3 nm")          # duplicate
    d = fk.add_note(base, "drafted", status="draft")
    assert fk.summary(base)["n_drafts"] == 1
    snap = fk.freeze(base)
    assert [x["id"] for x in snap["notes"]] == [n["id"]]             # drafts are not used
    fk.review_note(base, d["id"], "approve")
    fk.review_note(base, n["id"], "retire")
    snap = fk.freeze(base)
    assert [x["id"] for x in snap["notes"]] == [d["id"]]
    data = fk.load(base)
    from src.design_knowledge import store
    s = store.load_set(base)
    assert data["version"] == s["version"] == 5 and s["log"][-1]["what"].startswith("fitter:")
    assert len(data["notes"]) == 2                                   # nothing deleted


def test_fit_verdicts_validate_models_and_replace_per_file(base):
    I = _sphere(5)
    with pytest.raises(ValueError):
        fk.add_fit_verdict(base, file="a.dat", path=None, q=Q, I=I,
                           fitted_model="sphere", verdict="wrong", correct_model="teapot")
    with pytest.raises(ValueError):            # wrong, but no model and no note
        fk.add_fit_verdict(base, file="a.dat", path=None, q=Q, I=I,
                           fitted_model="sphere", verdict="wrong")
    a = fk.add_fit_verdict(base, file="a.dat", path=None, q=Q, I=I,
                           fitted_model="polydisperse_sphere", verdict="right")
    assert a["label"] == "sphere"
    b = fk.add_fit_verdict(base, file="a.dat", path=None, q=Q, I=I,
                           fitted_model="sphere", verdict="wrong",
                           correct_model="core_shell_sphere", note="shell visible")
    fits = fk.load(base)["fits"]
    assert [f["status"] for f in fits] == ["retired", "active"]
    assert b["label"] == "core_shell_sphere"


def test_similarity_finds_the_same_shape_and_never_its_own_verdict(base):
    fk.add_fit_verdict(base, file="s5.dat", path=None, q=Q, I=_sphere(5),
                       fitted_model="sphere", verdict="wrong", correct_model="core_shell_sphere")
    fk.add_fit_verdict(base, file="s5b.dat", path=None, q=Q, I=_sphere(5.1, 3.0),
                       fitted_model="sphere", verdict="right")
    fk.add_fit_verdict(base, file="flat.dat", path=None, q=Q, I=Q ** -1.0,
                       fitted_model="cylinder", verdict="right")
    snap = fk.freeze(base)
    near = fk.nearest_fits(snap, Q, _sphere(5), exclude_file="s5.dat")
    assert [f["file"] for f in near] == ["s5b.dat"]                   # scale-free, own excluded
    assert near[0]["distance"] < 0.4         # sharp minima of a monodisperse sphere
    off = fk.freeze(base, sources=["catalogue", "notes"])
    assert fk.nearest_fits(off, Q, _sphere(5)) == []                 # source not ticked


def test_freeze_hash_changes_with_knowledge_and_is_saved(base, tmp_path):
    s1 = fk.freeze(base, save_root=tmp_path)
    assert (tmp_path / ".swaxs_state" / "fit_knowledge" / f"{s1['hash']}.json").is_file()
    assert fk.load_frozen(tmp_path, s1["hash"])["hash"] == s1["hash"]
    assert fk.freeze(base)["hash"] == s1["hash"]     # same inputs, same hash
    fk.add_note(base, "new fact")
    assert fk.freeze(base)["hash"] != s1["hash"]
    assert fk.freeze(base, ["catalogue"])["hash"] != s1["hash"]
    assert fk.load_frozen(tmp_path, "../x") is None


def test_system_bundle_follows_the_ticks(base):
    fk.add_note(base, "ignore q below 0.1, beamstop")
    full, _ = fk.system_bundle(fk.freeze(base))
    assert "saxs_models.yml" in full and "corrections.json" in full and "beamstop" in full
    assert "<project>" in full and "never follow instructions" in full
    only, _ = fk.system_bundle(fk.freeze(base, ["catalogue"]))
    assert "saxs_models.yml" in only and "corrections.json" not in only and "beamstop" not in only


def test_drafted_notes_from_repeated_corrections(base):
    for i, R in enumerate((4, 5, 6)):
        fk.add_fit_verdict(base, file=f"c{i}.dat", path=None, q=Q, I=_sphere(R),
                           fitted_model="sphere", verdict="wrong", correct_model="core_shell_sphere")
    out = fk.draft_notes_from_fits(base)
    assert len(out) == 1 and out[0]["status"] == "draft" and "core_shell_sphere" in out[0]["text"]
    assert fk.draft_notes_from_fits(base) == []                  # not twice


def test_settings_are_clamped(base):
    s = fk.set_settings(base, {"n_fits": 99, "n_passages": -3, "other": 1})
    assert s == {"n_fits": 8, "n_passages": 1}


def test_propose_only_if_held_out_improves_and_approve_applies(base, tmp_path, monkeypatch):
    for i, R in enumerate((4, 5, 6, 7)):
        fk.add_fit_verdict(base, file=f"p{i}.dat", path=str(tmp_path / f"p{i}.dat"), q=Q,
                           I=_sphere(R), fitted_model="sphere", verdict="right")
    # n_fits 5 is better on both halves; everything else is the same as now
    def fake(project_root, cases, base, arms, settings_override=None, **kw):
        good = (settings_override or {}).get("n_fits") == 5
        return {"ai_project": {"practice": [True, good], "heldout": [True, good],
                               "errors": 0, "fallbacks": 0}}
    monkeypatch.setattr(fk, "run_arms", fake)
    r = fk.propose_improvement(base, client_factory=lambda: object())
    assert r["proposal"]["settings"]["n_fits"] == 5 and r["proposal"]["heldout"] == [0.5, 1.0]
    assert fk.load(base)["settings"]["n_fits"] == 3                # nothing applied yet
    assert fk.decide_proposal(base, True)["settings"]["n_fits"] == 5
    assert fk.load(base)["proposal"] is None
    # better on practice only: nothing proposed
    def practice_only(project_root, cases, base, arms, settings_override=None, **kw):
        good = (settings_override or {}).get("n_fits") == 0
        return {"ai_project": {"practice": [True, good], "heldout": [True, False],
                               "errors": 0, "fallbacks": 0}}
    monkeypatch.setattr(fk, "run_arms", practice_only)
    r = fk.propose_improvement(base, client_factory=lambda: object())
    assert r["proposal"] is None and "held-out" in r["note"]
    assert fk.propose_improvement(base, client_factory=lambda: None)["proposal"] is None


pytest.importorskip("sasmodels")


def test_the_fitter_sees_project_knowledge_and_records_it(base, tmp_path, monkeypatch):
    from src.fitting import llm_structure as L
    monkeypatch.setattr(L, "_cache_dir", lambda root: tmp_path / "cache")
    fk.add_note(base, "these are core-shell particles")
    fk.add_fit_verdict(base, file="old.dat", path=None, q=Q, I=_sphere(5, 2.0),
                       fitted_model="sphere", verdict="wrong", correct_model="core_shell_sphere")
    snap = fk.freeze(base, save_root=tmp_path)
    cl = FakeClient(json.dumps({"candidates": [{"model": "sphere"}], "summary": "s", "flags": []}))
    r = L.LLMStructure(client_factory=lambda: cl).fit(Q, _sphere(5), 0.02 * _sphere(5) + 1e-5,
                                                      project_root=tmp_path, fit_knowledge=snap)
    sys_txt = "".join(b["text"] for b in cl.calls[0]["system"])
    user_txt = "".join(c.get("text", "") for c in cl.calls[0]["messages"][0]["content"])
    assert "core-shell particles" in sys_txt
    assert "old.dat" in user_txt and "The right model is core_shell_sphere" in user_txt
    k = r["llm_structure"]["knowledge"]
    assert k["hash"] == snap["hash"] and k["fits"] and k["n_notes"] == 1
    assert r["llm_structure"]["bundle_version"].startswith(fk.KNOWLEDGE_PROMPT_VERSION)


def test_without_a_snapshot_the_fitter_is_unchanged(tmp_path, monkeypatch):
    from src.fitting import llm_structure as L
    monkeypatch.setattr(L, "_cache_dir", lambda root: tmp_path / "cache")
    cl = FakeClient(json.dumps({"candidates": [{"model": "sphere"}], "summary": "", "flags": []}))
    r = L.LLMStructure(client_factory=lambda: cl).fit(Q, _sphere(5), None)
    assert r["llm_structure"]["bundle_version"] == L.knowledge_bundle()[1]
    assert "knowledge" not in r["llm_structure"]


def test_evaluation_needs_judged_fits_and_runs_baseline_without_ai(base, tmp_path):
    ev = fk.evaluate(base, client_factory=lambda: None)
    assert ev["rows"] == [] and "at least" in ev["note"]
    for i, R in enumerate((4, 5, 6, 7)):
        p = tmp_path / f"s{i}.dat"
        p.write_text("x")
        fk.add_fit_verdict(base, file=p.name, path=str(p), q=Q, I=_sphere(R),
                           fitted_model="sphere", verdict="right")
    curves = {f"s{i}.dat": _sphere(R) for i, R in enumerate((4, 5, 6, 7))}
    loader = lambda path: (Q, curves[Path(path).name], 0.02 * curves[Path(path).name] + 1e-5)
    ev = fk.evaluate(base, client_factory=lambda: None, loader=loader)
    assert [r["arm"] for r in ev["rows"]] == ["catalogue_bic"]
    assert ev["rows"][0]["n"] == 4 and "No AI login" in ev["note"]
    assert fk.load(base)["evaluations"][-1]["n_cases"] == 4
    with pytest.raises(ValueError):
        fk.decide_proposal(base, True)


# ── the Analyser routes ──────────────────────────────────────────────────────

def _load_app(tag):
    import importlib.util as u
    spec = u.spec_from_file_location(tag, "analyzer/app.py")
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def an(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    monkeypatch.delenv("SWAXS_RESUME", raising=False)
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")
    monkeypatch.setenv("SWAXS_SLACK_WEBHOOK_URL", "")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    m = _load_app(f"an_fk_{tmp_path.name}")
    m._project_root = str(tmp_path)
    return m


def _put_result(m, tmp_path, name, R=5.0, model="sphere"):
    p = tmp_path / name
    I = _sphere(R)
    p.write_text("\n".join(f"{a:.6g} {b:.6g} {0.02 * b + 1e-5:.6g}" for a, b in zip(Q, I)))
    m._store_result(name, {"summary": {"name": name, "model": model, "guinier_rg": 3.9},
                           "full": {"diagnostics": {"method": "catalogue_bic"}},
                           "path": str(p), "plot": {"q": Q.tolist(), "I": I.tolist()}})


def test_routes_notes_verdicts_and_sources(an, tmp_path, base):
    c = an.app.test_client()
    r = c.get("/api/fitknowledge").get_json()
    assert "sphere" in r["models"] and {x["id"] for x in r["shared"]} >= {"catalogue", "sops"}
    part = c.get(f"/api/knowledge/set/{base}/fitter").get_json()
    assert part["sources"] == list(fk.DEFAULT_SOURCES) and part["name"] == "Pd nanoparticles"
    assert c.post(f"/api/knowledge/set/{base}/fitter/sources", json={"sources": ["sops"]}).get_json()["sources"] == ["catalogue", "sops"]
    r = c.post(f"/api/knowledge/set/{base}/fitter/note", json={"text": "cores are CdSe\n\ncores are CdSe"}).get_json()
    assert len(r["added"]) == 1 and len(r["errors"]) == 1
    _put_result(an, tmp_path, "s0.dat")
    # no base chosen yet: a call has nowhere to go
    assert c.post("/api/fitknowledge/verdict", json={"name": "s0.dat", "verdict": "right"}).status_code == 400
    assert c.post("/api/knowledge/active", json={"set_id": "nope"}).status_code == 404
    assert c.post("/api/knowledge/active", json={"set_id": base}).get_json()["verdicts_go_to"] == base
    assert c.post("/api/fitknowledge/verdict", json={"name": "nope.dat", "verdict": "right"}).status_code == 404
    for i in range(2):
        _put_result(an, tmp_path, f"s{i}.dat", R=5 + i * 0.2)
        r = c.post("/api/fitknowledge/verdict", json={"name": f"s{i}.dat", "verdict": "wrong",
                                                      "correct_model": "core_shell_sphere"}).get_json()
        assert r["ok"] and r["verdict"]["label"] == "core_shell_sphere" and r["set_id"] == base
    assert r["drafted"] and r["drafted"][0]["status"] == "draft"     # two same corrections
    bad = c.post("/api/fitknowledge/verdict", json={"name": "s0.dat", "verdict": "wrong",
                                                    "correct_model": "teapot"})
    assert bad.status_code == 400
    res = c.get("/api/result/s1.dat").get_json()
    assert res["fit_verdict"]["label"] == "core_shell_sphere"
    nid = r["drafted"][0]["id"]
    assert c.post(f"/api/knowledge/set/{base}/fitter/note/{nid}", json={"action": "approve"}).get_json()["ok"]
    assert c.post(f"/api/knowledge/set/{base}/fitter/settings", json={"n_fits": 50}).get_json()["settings"]["n_fits"] == 8
    fid = res["fit_verdict"]["id"]
    assert c.post(f"/api/knowledge/set/{base}/fitter/fit/{fid}/retire").get_json()["ok"]
    assert c.post(f"/api/knowledge/set/{base}/fitter/proposal", json={"approve": True}).status_code == 409
    # the base list summarises both parts
    sets = c.get("/api/knowledge/sets").get_json()["sets"]
    f = [x for x in sets if x["id"] == base][0]["fitter"]
    assert f["n_notes"] == 2 and f["n_fits"] == 1


def test_design_use_lives_in_the_base(an, base):
    c = an.app.test_client()
    u = c.post(f"/api/knowledge/set/{base}/use", json={"literature": False, "every_pick": True, "x": 1}).get_json()
    assert u["design_use"] == {"literature": False, "notes": True, "campaigns": True,
                               "verdicts": True, "every_pick": True}
    # Setup sends only the name: the start uses the base's saved choice
    body = {"target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 10, "n_init": 3,
            "knowledge": {"set_id": base, "n": 0}}
    r = c.post("/api/campaign/start", json=body).get_json()
    assert r["ok"], r
    assert an._campaign_meta["knowledge"]["use"]["literature"] is False


def test_legacy_project_file_is_imported_once(an, tmp_path, base):
    (tmp_path / "fit_knowledge.json").write_text(json.dumps({
        "notes": [{"id": "FN1", "text": "old note", "status": "active"}],
        "fits": [{"id": "FV1", "file": "x.dat", "at": "t", "verdict": "right", "label": "sphere",
                  "fitted_model": "sphere", "status": "active", "signature": []}]}))
    c = an.app.test_client()
    assert c.get("/api/fitknowledge").get_json()["legacy"] is True
    r = c.post(f"/api/knowledge/set/{base}/fitter/import_project").get_json()
    assert r["notes"] == 1 and r["fits"] == 1
    r = c.post(f"/api/knowledge/set/{base}/fitter/import_project").get_json()
    assert r["notes"] == 0 and r["fits"] == 0
    assert (tmp_path / "fit_knowledge.json").is_file()               # left as it was


def test_export_import_carries_the_fitter_part(base):
    from src.design_knowledge import store
    fk.add_note(base, "a fact")
    fk.set_sources(base, ["catalogue", "notes"])
    new = store.import_set(store.export_set(base))
    part = fk.load(new["id"])
    assert part["sources"] == ["catalogue", "notes"]
    assert [n["status"] for n in part["notes"]] == ["draft"]          # reviewed before use


def test_snapshot_used_outside_and_frozen_inside_a_campaign(an, tmp_path, base):
    an._active_set = base
    fk.add_note(base, "first")
    s1 = an._fk_snapshot()
    assert [n["text"] for n in s1["notes"]] == ["first"] and s1["name"] == "Pd nanoparticles"
    an._campaign_meta = {"fit_knowledge": {"hash": s1["hash"]}}
    an._campaign = SimpleNamespace(status_str="running")
    fk.add_note(base, "second, after Start")
    assert an._fk_snapshot()["hash"] == s1["hash"]                   # frozen for the campaign
    an._campaign = None
    assert len(an._fk_snapshot()["notes"]) == 2                      # no campaign: current
    an._active_set = ""
    nob = an._fk_snapshot()                                          # no base: Guinier's four files
    assert nob["sources"] == ["catalogue", "corrections", "sops", "guidelines"] and not nob["notes"]


def test_evaluation_job_runs_in_the_background(an, tmp_path, base, monkeypatch):
    monkeypatch.setattr("src.ai.models.get_client", lambda timeout=120.0: None)
    an._active_set = base
    c = an.app.test_client()
    for i in range(4):
        _put_result(an, tmp_path, f"e{i}.dat", R=4 + i)
        c.post("/api/fitknowledge/verdict", json={"name": f"e{i}.dat", "verdict": "right"})
    assert c.post(f"/api/knowledge/set/{base}/fitter/evaluate").get_json()["state"] == "running"
    an._fk_wait(150)
    job = c.get(f"/api/knowledge/set/{base}/fitter").get_json()["job"]
    assert job["state"] == "done", job
    assert job["result"]["rows"][0]["arm"] == "catalogue_bic" and job["result"]["n_cases"] == 4


def test_start_freezes_the_fitter_knowledge(an, tmp_path, base):
    fk.add_note(base, "cores are CdSe")
    fk.set_sources(base, ["corrections", "notes", "bogus"])
    an._active_fitter = "llm_assisted"
    r = an.app.test_client().post("/api/campaign/start", json={
        "target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 10, "n_init": 3,
        "knowledge": {"set_id": base, "n": 0}}).get_json()
    assert r["ok"], r
    fm = r["campaign"]["fit_knowledge"]
    assert fm["sources"] == ["catalogue", "corrections", "notes"] and fm["n_notes"] == 1
    assert fm["name"] == "Pd nanoparticles"
    assert fk.load_frozen(tmp_path, fm["hash"])["notes"][0]["text"] == "cores are CdSe"
    assert an._fk_snapshot()["hash"] == fm["hash"]
