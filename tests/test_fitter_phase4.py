"""Structure fitter, Phase 4 (docs/design/LLM_FITTER_PLAN.md): the AI proposes,
physics fits, statistics decide. A stand-in client replaces the AI, so these
tests need no login and cost nothing."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("sasmodels")

from src.fitting import llm_structure as L  # noqa: E402
from src.analysis.model_fit import load_catalogue  # noqa: E402

Q = np.geomspace(0.05, 5, 220)
CAT = load_catalogue()


def _curve(name, **p):
    from sasmodels.core import load_model
    from sasmodels import data as smd, direct_model as dm
    c = dm.DirectModel(smd.empty_data1D(Q * 0.1), load_model(name))
    I = np.asarray(c(scale=0.01, background=0.001, **p), float)
    s = 0.02 * I + 2e-4
    return np.abs(I + np.random.default_rng(5).normal(0, s)), s


@pytest.fixture(scope="module")
def cylinder():
    return _curve("cylinder", radius=20, length=300, radius_pd=0.1, radius_pd_n=25)


class FakeClient:
    """Answers with fixed texts in order; records what it was sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        text = self.answers.pop(0) if self.answers else "{}"
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=80,
                                                     cache_read_input_tokens=900,
                                                     cache_creation_input_tokens=0))


def _answer(*cands, summary="rods", flags=()):
    return json.dumps({"candidates": list(cands), "summary": summary, "flags": list(flags)})


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_cache_dir", lambda root: tmp_path / "cache")
    monkeypatch.setenv("SWAXS_TRACES_DIR", str(tmp_path / "traces"))


def test_validate_keeps_only_catalogue_models_and_clips_starts():
    out = L.validate({"candidates": [
        {"model": "cylinder", "start": {"radius": 1e6, "length": 30, "bogus": 1}},
        {"model": "teapot"},
        {"model": "sphere", "structure_factor": "not_a_factor"},
        {"model": "mass_fractal", "structure_factor": "hardsphere"},
        {"model": "sphere", "structure_factor": "hardsphere", "reason": "x"},
        {"model": "cylinder"},                                   # duplicate
    ]}, CAT)
    assert [(c["model"], c["structure_factor"]) for c in out] == \
        [("cylinder", None), ("sphere", "hardsphere")]
    assert out[0]["start"] == {"radius": 100.0, "length": 30.0}  # clipped to bounds
    assert L.validate("not a dict", CAT) == [] and L.validate(None, CAT) == []


def test_the_ai_proposal_is_fitted_and_statistics_pick_the_winner(cylinder):
    fake = FakeClient(_answer({"model": "cylinder", "start": {"radius": 2, "length": 30},
                               "reason": "q^-1 region"},
                              {"model": "ellipsoid", "reason": "alternative"},
                              summary="Rod-like particles.", flags=["polydisperse"]))
    r = L.LLMStructure(lambda: fake).fit(Q, *cylinder)
    assert r["model"] == "cylinder"
    assert r["model_params"]["radius"] == pytest.approx(2.0, rel=0.05)
    info = r["llm_structure"]
    assert info["status"] == "ai" and info["rounds"] == 1 and not info["cached"]
    assert {c["model"] for c in info["proposed"]} == {"cylinder", "ellipsoid", "sphere"}
    assert r["llm_assist"] == {"summary": "Rod-like particles.", "flags": ["polydisperse"]}
    assert r["diagnostics"]["method"] == "llm_assisted"
    sent = fake.calls[0]
    assert sent["system"][1]["cache_control"] == {"type": "ephemeral"}   # bundle is cached
    assert "saxs_models.yml" in sent["system"][1]["text"]
    assert any(b["type"] == "image" for b in sent["messages"][0]["content"])


def test_a_wrong_ai_proposal_is_overruled_by_the_sphere_baseline():
    I, s = _curve("sphere", radius=50, radius_pd=0.08, radius_pd_n=35)
    fake = FakeClient(_answer({"model": "cylinder"}), "{}")
    r = L.LLMStructure(lambda: fake).fit(Q, I, s)
    assert r["model"] == "sphere"                                # BIC, not the AI, decides


def test_second_round_only_when_the_winner_still_misses():
    I, s = _curve("mass_fractal", radius=5, fractal_dim_mass=2.2, cutoff_length=400)
    fake = FakeClient(_answer({"model": "core_shell_sphere"}),
                      _answer({"model": "mass_fractal", "reason": "power-law residuals"}))
    r = L.LLMStructure(lambda: fake).fit(Q, I, s)
    assert r["llm_structure"]["rounds"] == 2 and len(fake.calls) == 2
    assert r["model"] == "mass_fractal"
    assert "already_fitted" in fake.calls[1]["messages"][0]["content"][-1]["text"]


def test_results_are_cached_so_reanalysis_costs_nothing(cylinder):
    fake = FakeClient(_answer({"model": "cylinder"}))
    L.LLMStructure(lambda: fake).fit(Q, *cylinder)
    fake2 = FakeClient()
    r = L.LLMStructure(lambda: fake2).fit(Q, *cylinder)
    assert r["llm_structure"]["cached"] is True and fake2.calls == []
    assert r["model"] == "cylinder"


@pytest.mark.parametrize("client,status", [
    (lambda: None, "fallback_no_ai"),
    (lambda: FakeClient("this is not json"), "fallback_invalid_answer"),
    (lambda: FakeClient(_answer({"model": "teapot"})), "fallback_invalid_answer"),
])
def test_no_ai_or_a_bad_answer_falls_back_to_catalogue_bic(cylinder, client, status):
    r = L.LLMStructure(client).fit(Q, *cylinder)
    assert r["llm_structure"]["status"] == status
    assert r["model"].split("@")[0] == "cylinder"                 # statistics still find it


def test_an_ai_exception_falls_back(cylinder):
    def boom(**kw):
        raise TimeoutError("gateway")
    fake = SimpleNamespace(messages=SimpleNamespace(create=boom))
    r = L.LLMStructure(lambda: fake).fit(Q, *cylinder)
    assert r["llm_structure"]["status"] == "fallback_ai_error"
    assert "gateway" in r["llm_structure"]["error"]


def test_every_decision_is_traced(tmp_path, cylinder):
    L.LLMStructure(lambda: FakeClient(_answer({"model": "cylinder"}))).fit(Q, *cylinder)
    rec = json.loads(next((tmp_path / "traces").glob("*.jsonl")).read_text().splitlines()[-1])
    assert rec["kind"] == "fitter" and rec["winner"] == "cylinder"
    assert "cylinder" in rec["proposed"] and rec["bundle_version"]


def test_bundle_version_changes_with_the_knowledge():
    _, v1 = L.knowledge_bundle()
    _, v2 = L.knowledge_bundle(include_guidelines=False)
    assert v1 != v2 and len(v1) == 12


def test_context_reaches_the_ai(tmp_path, cylinder):
    root = tmp_path / "proj"
    sub = root / "1D" / "SAXS" / "Subtracted"
    sub.mkdir(parents=True)
    f = sub / "r003_sample_sub.dat"
    f.write_text("")
    (root / "chemistry.yml").write_text("product: gold nanorods\nexpected_shape: rods\n")
    fake = FakeClient(_answer({"model": "cylinder"}))
    L.LLMStructure(lambda: fake).fit(Q, *cylinder, path=str(f), project_root=str(root))
    text = fake.calls[0]["messages"][0]["content"][-1]["text"]
    assert "gold nanorods" in text and "r003" in text


def test_registered_under_the_same_menu_id():
    import src.fitting as F
    assert isinstance(F.get("llm_assisted"), L.LLMStructure)
