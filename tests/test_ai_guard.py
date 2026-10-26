"""The AI fitter's circuit breaker and call budget (src/fitting/ai_guard.py) and
how the Analyser uses it: a broken, stuck or hallucinating AI stops costing
tokens by itself, and the design loop never waits more than the cap."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fitting.ai_guard import AIGuard  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_three_failures_in_a_row_pause_the_ai_then_one_trial():
    c = Clock()
    g = AIGuard(max_failures=3, cooldown_s=600, max_calls_per_hour=100, slow_s=150, clock=c)
    for st in ("fallback_ai_error", "fallback_invalid_answer"):
        g.record({"status": st, "rounds": 1})
    assert g.allow()[0] and g.state_change() is None
    g.record({"status": "fallback_ai_error", "rounds": 1})
    ok, why = g.allow()
    assert not ok and "3 AI fits in a row" in why and "10 min" in why
    assert g.state_change().startswith("paused") and g.state_change() is None   # once
    c.t += 601
    assert g.allow()[0] and g.state_change() == "resumed"
    g.record({"status": "fallback_invalid_answer", "rounds": 1})          # the trial fails
    assert not g.allow()[0] and "unusable answers" in g.allow()[1]
    c.t += 601
    g.record({"status": "ai", "rounds": 1})                               # a good fit clears it
    g.record({"status": "fallback_ai_error", "rounds": 1})
    assert g.allow()[0]


def test_a_slow_fit_counts_as_a_failure_and_a_cached_one_costs_nothing():
    c = Clock()
    g = AIGuard(max_failures=1, cooldown_s=60, max_calls_per_hour=2, slow_s=150, clock=c)
    g.record({"status": "ai", "rounds": 1, "cached": True}, seconds=1)
    assert g.status()["calls_last_hour"] == 0
    g.record({"status": "ai", "rounds": 2}, seconds=200)
    assert not g.allow()[0] and "too slow" in g.allow()[1]


def test_the_hourly_budget_stops_calls_until_the_hour_rolls_over():
    c = Clock()
    g = AIGuard(max_failures=99, cooldown_s=60, max_calls_per_hour=3, slow_s=999, clock=c)
    g.record({"status": "ai", "rounds": 2})
    g.record({"status": "ai", "rounds": 1})
    ok, why = g.allow()
    assert not ok and "hourly limit of 3" in why
    c.t += 3601
    assert g.allow()[0]


pytest.importorskip("scipy")


def _load(tag):
    import importlib.util as u
    spec = u.spec_from_file_location(tag, "analyzer/app.py")
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def an(tmp_path, monkeypatch):
    monkeypatch.delenv("SWAXS_RESUME", raising=False)
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")
    monkeypatch.setenv("SWAXS_SLACK_WEBHOOK_URL", "")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    m = _load(f"an_guard_{tmp_path.name}")
    m._project_root = str(tmp_path)
    m._BATCH_SETTLE_S = 60.0
    assert m.app.test_client().post("/api/campaign/start", json={
        "target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 20, "n_init": 3}).get_json()["ok"]
    m._active_fitter = "llm_assisted"
    return m


def _profile(tmp_path, name):
    import numpy as np
    sub = tmp_path / "1D" / "SAXS" / "Subtracted"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / name
    q = np.geomspace(0.05, 5, 80)
    f.write_text("\n".join(f"{a:.6g} {1 / (1 + (a * 3) ** 4):.6g} 0.01" for a in q))
    return f


def _fitters(an, monkeypatch, ai_status):
    calls = []

    def get(fid):
        def fit(q, I, sigma=None, **kw):
            calls.append(fid)
            ai = fid == "llm_assisted"
            return {"size": {"radius": 3.0}, "pdi": 0.07, "confidence": 0.6, "model": "sphere",
                    "fit": {"bic": 100.0},
                    "llm_structure": {"status": ai_status, "rounds": 1} if ai else {},
                    "llm_assist": {"summary": "", "flags": []}}
        return SimpleNamespace(fit=fit)
    monkeypatch.setattr(an.fitting, "get", get)
    return calls


def test_the_ai_refit_wait_is_capped_at_3_minutes(an):
    assert an._AI_REFIT_MAX_WAIT_S == 180.0


def test_a_hallucinating_ai_is_paused_and_costs_no_more_calls(an, tmp_path, monkeypatch):
    calls = _fitters(an, monkeypatch, "fallback_invalid_answer")
    for i in range(5):
        an._analyze_file(_profile(tmp_path, f"x{i}_SAXS_subtracted.dat"))
        an._ai_fit_wait(20)
    assert calls.count("llm_assisted") == 3                 # paused after 3 in a row
    assert calls.count("catalogue_bic") == 5                # every profile still fitted
    st = an.app.test_client().get("/api/campaign").get_json()["ai_fitter"]
    assert st["allowed"] is False and "unusable answers" in st["why"]


def test_a_refit_too_late_to_help_is_skipped(an, tmp_path, monkeypatch):
    calls = _fitters(an, monkeypatch, "ai")
    rid = next(iter(an._pending))
    an._told_recently.append(rid)                           # already learned from the quick fit
    an._queue_ai_fit(_profile(tmp_path, f"{rid}_SAXS_subtracted.dat"))
    an._ai_fit_wait(20)
    assert "llm_assisted" not in calls


def test_the_fitter_makes_no_retries_and_skips_round_two_after_the_deadline(monkeypatch):
    from src.fitting import llm_structure as L
    seen = {}

    class C:
        def with_options(self, **kw):
            seen.update(kw)
            return self
        messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(content=[SimpleNamespace(text="{}")], usage=None))
    L._ask(C(), "bundle", "curve", None)
    assert seen == {"max_retries": 0} and L.TIMEOUT_S <= 45 and L.DEADLINE_S <= 90
