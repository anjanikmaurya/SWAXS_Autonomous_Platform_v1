"""Regressions for the closed-loop items of docs/audits/PLATFORM_QUICK_AUDIT_2026_10.md.

Q9   an expired condition is withdrawn from Conditions/ and a late result
     replaces the recorded failure
Q10  the ML regressor fitter gives low confidence on noise and on railed sizes
Q17  withdraw_source also withdraws a recipe staged behind its blank flush
Q18  Continue counts asks from the highest issued r-number
Q19  after a restart, a profile for a pending condition is fitted; the expiry
     clock restarts
Q20  the analyzer follows the reactor's Conditions folder, or flags a mismatch
Q21  reactor sample/background tags are restricted to the parsers' names
Q33  ParameterSpace.valid() enforces the per-pump [sensor_min, max_flow]
Q34  the Pareto front ignores low-confidence fits
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import analyzer.app as az                                       # noqa: E402

PARAMS = {"T_reac": 240.0, "F_tot": 80.0, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1}


@pytest.fixture(autouse=True)
def _reset_analyzer(tmp_path, monkeypatch):
    monkeypatch.setattr(az, "_project_root", str(tmp_path))
    monkeypatch.setattr(az, "_sub_folder", "1D/SAXS/Subtracted")
    monkeypatch.setattr(az, "_cond_folder", "1D/SAXS/Conditions")
    monkeypatch.setattr(az, "_cond_folder_explicit", False)
    monkeypatch.setattr(az, "_reactor_cond_cache", (0.0, "", ""))
    monkeypatch.setattr(az, "_cond_mismatch_warned", "")
    monkeypatch.setattr(az, "_results_folder", "1D/SAXS/Results")
    monkeypatch.setattr(az, "_gate_mode", "off")
    monkeypatch.setattr(az, "_campaign", None)
    monkeypatch.setattr(az, "_campaign_cfg", {})
    monkeypatch.setattr(az, "_campaign_meta", {})
    monkeypatch.setattr(az, "_campaign_id", "")
    monkeypatch.setattr(az, "_run_tag", "")
    monkeypatch.setattr(az, "_run_seq", 0)
    monkeypatch.setattr(az, "_pending", {})
    monkeypatch.setattr(az, "_pending_at", {})
    az._handled.clear(); az._lastsig.clear(); az._startup_present.clear()
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True, exist_ok=True)
    yield
    az._handled.clear(); az._lastsig.clear(); az._startup_present.clear()


def _start(target=5.0, budget=10):
    c = az.app.test_client()
    r = c.post("/api/campaign/start", json={"target_size": target, "tolerance": 0.3,
                                             "pdi_cap": 0.15, "budget": budget,
                                             "n_init": 4,
                                             "goal": "target-hit"}).get_json()
    assert r.get("ok"), r
    return c


def _logs():
    return " ".join(e[1]["msg"] for e in list(az._log))


# ── Q9 ────────────────────────────────────────────────────────────────────────
def test_q9_expiry_withdraws_the_file_and_a_late_result_replaces_the_failure(tmp_path):
    _start()
    cond = tmp_path / "1D" / "SAXS" / "Conditions"
    rid = next(iter(az._pending))
    assert (cond / f"{rid}.txt").is_file()
    az._pending_at[rid] = time.time() - 10 * az._PENDING_TIMEOUT_S
    az._expire_pending()

    # withdrawn: out of Conditions/, into done/ with a NOT RUN note
    assert not (cond / f"{rid}.txt").exists()
    body = (cond / "done" / f"{rid}.txt").read_text()
    assert "NOT RUN" in body and "expired" in body
    rec = [h for h in az._campaign.history if h["recipe_id"] == rid][0]
    assert rec["size"] is None and rec.get("expired")
    nxt = [k for k in az._pending if k != rid]
    assert nxt, "a replacement condition must be proposed"
    n_hist = len(az._campaign.history)

    # the late result is recorded in place of the failure, budget unchanged
    az._feed_campaign(f"{rid}_sample_batch001_1files_Average_sub.dat",
                      {"size": {"radius": 7.0}, "pdi": 0.1, "confidence": 0.9})
    assert len(az._campaign.history) == n_hist
    rec = [h for h in az._campaign.history if h["recipe_id"] == rid][0]
    assert rec["size"] == 7.0 and rec.get("late") and not rec.get("expired")
    assert az._campaign.best["recipe_id"] == rid
    assert list(az._pending) == nxt, "the replacement stays pending"
    assert "late result" in _logs()


def test_q9_replace_failed_only_touches_expired_records():
    from src.optimizer import CampaignController, ParameterSpace
    from src.reactor import load_config
    camp = CampaignController(ParameterSpace.from_config(load_config()),
                              target_size=5.0, tolerance=0.3, pdi_cap=0.15,
                              budget=5, n_init=2).start()
    camp.tell(PARAMS, None, None, 0.0, recipe_id="a")      # measured but unsized
    assert camp.replace_failed("a", 5.0, 0.1, 0.9) is None
    rec = camp.tell(PARAMS, None, None, 0.0, recipe_id="b")
    rec["expired"] = True
    new = camp.replace_failed("b", 5.05, 0.05, 0.9)
    assert new is not None and len(camp.history) == 2
    assert camp.status_str == "converged"                  # a confident in-spec hit
    assert camp.replace_failed("b", 4.0, 0.1, 0.9) is None   # only once


# ── Q10 ───────────────────────────────────────────────────────────────────────
def _ml():
    import src.fitting as F
    return F.get("ml_regressor")


def test_q10_ml_regressor_low_confidence_on_flat_noise():
    rng = np.random.default_rng(1)
    q = np.linspace(0.08, 3.5, 400)
    for sd in (0.02, 0.1, 0.3):
        I = np.abs(1.0 + sd * rng.standard_normal(q.size))
        res = _ml().fit(q, I)
        assert res["confidence"] < 0.5, (sd, res["confidence"], res["diagnostics"])


def test_q10_ml_regressor_flags_a_30nm_sphere_as_railed():
    from src.analysis.nanoparticle import model_intensity
    rng = np.random.default_rng(2)
    q = np.linspace(0.08, 3.5, 400)
    I = np.asarray(model_intensity(q, 30.0, 0.1, scale=1.0, bkg=0.0, dist="schulz")) * 1e3
    I = I * (1 + 0.02 * rng.standard_normal(q.size))
    res = _ml().fit(q, I)
    assert res["diagnostics"]["railed"] is True
    assert res["confidence"] < 0.5


def test_q10_ml_regressor_still_confident_on_a_clean_in_range_sphere():
    from src.analysis.nanoparticle import model_intensity
    rng = np.random.default_rng(3)
    q = np.linspace(0.08, 3.5, 400)
    I = np.asarray(model_intensity(q, 5.0, 0.1, scale=1.0, bkg=0.0, dist="schulz")) * 1e3
    I = I * (1 + 0.02 * rng.standard_normal(q.size))
    res = _ml().fit(q, I)
    assert res["diagnostics"]["railed"] is False
    assert abs(res["size"]["radius"] - 5.0) < 1.0
    assert res["confidence"] > 0.5


# ── Q17 ───────────────────────────────────────────────────────────────────────
def _duck_reactor(queue, pending):
    from src.reactor.sequence import SequenceMixin
    d = types.SimpleNamespace()
    d.queue = deque(queue)
    d._pending = pending
    d._bkg_recipe_id = pending[0].recipe_id if pending else ""
    d._lock = threading.RLock()
    d.logs = []
    d._log = lambda msg, *a, **k: d.logs.append(msg)
    d._release_pending = types.MethodType(SequenceMixin._release_pending, d)
    return d


def _rec(rid, src):
    return types.SimpleNamespace(recipe_id=rid, source=src)


def test_q17_withdraw_source_also_withdraws_the_staged_recipe():
    from src.reactor.controller import ReactorController
    d = _duck_reactor([(_rec("b", "folder:b.txt"), {})],
                      (_rec("a", "folder:a.txt"), {"top": 1.0}))
    gone = ReactorController.withdraw_source(d, "folder:a.txt", reason="aborted")
    assert gone == ["a"]
    assert d._pending is None and d._bkg_recipe_id == ""
    assert [r.recipe_id for r, _ in d.queue] == ["b"]


def test_q17_withdraw_source_leaves_a_different_staged_recipe_alone():
    from src.reactor.controller import ReactorController
    staged = (_rec("a", "folder:a.txt"), {})
    d = _duck_reactor([(_rec("b", "folder:b.txt"), {})], staged)
    assert ReactorController.withdraw_source(d, "folder:b.txt") == ["b"]
    assert d._pending is staged and d._bkg_recipe_id == "a"
    assert list(d.queue) == []


# ── Q18 ───────────────────────────────────────────────────────────────────────
def _fit_record(fit_dir, rid, cid, written):
    stem = f"{rid}_sample_20260101_000000"
    fit_dir.mkdir(parents=True, exist_ok=True)
    (fit_dir / f"fit_{stem}.dat").write_text(
        f"# Written         : {written}\n# Radius (nm)     : 4.0\n# PDI             : 0.1\n"
        f"# Confidence      : 0.9\n# Campaign ID     : {cid}\n1 2 0.1 2\n",
        encoding="utf-8")


def _feedback(root, rid):
    d = root / "reactor" / "feedback"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.done.json").write_text(json.dumps({"recipe": PARAMS}), encoding="utf-8")


def _campaign_record(root, cid="cid9", tag="Run9", n_init=10):
    d = root / "1D" / "SAXS" / "Results"; d.mkdir(parents=True, exist_ok=True)
    (d / f"campaign_{cid}.json").write_text(json.dumps({
        "campaign_id": cid, "target_size": 5.0, "tolerance": 0.3, "pdi_cap": 0.15,
        "budget": 25, "n_init": n_init, "run_no": 9, "run_tag": tag,
        "status": "running"}), encoding="utf-8")


def test_q18_continue_counts_asks_from_the_highest_r_number(tmp_path):
    fit = tmp_path / "1D" / "SAXS" / "Results" / "Fit"
    for i in (1, 2, 3):
        rid = f"Run9_r{i:03d}"
        _feedback(tmp_path, rid); _fit_record(fit, rid, "cid9", f"2026-09-01T00:0{i}:00")
    # r004..r006 were issued but expired / withdrawn: they sit in done/
    done = tmp_path / "1D" / "SAXS" / "Conditions" / "done"; done.mkdir(parents=True)
    for i in (4, 5, 6):
        (done / f"Run9_r{i:03d}.txt").write_text("x", encoding="utf-8")
    _campaign_record(tmp_path)
    az._continue_run()
    # six asks were already made; the next proposal is the 7th
    assert az._campaign._n_asked == 7
    assert list(az._pending) == ["Run9_r007"]
    # and it is NOT the seed already handed out as r004..r006
    seeds = az._campaign._seeds
    assert az._pending["Run9_r007"] == seeds[6]


# ── Q19 ───────────────────────────────────────────────────────────────────────
def test_q19_a_profile_for_a_pending_condition_is_fit_even_if_present_at_boot(tmp_path, monkeypatch):
    _start()
    rid = next(iter(az._pending))
    sub = tmp_path / "1D" / "SAXS" / "Subtracted"; sub.mkdir(parents=True)
    pending_prof = sub / f"{rid}_sample_batch001_1files_Average_sub.dat"
    old_prof = sub / "Run77_r001_sample_batch001_1files_Average_sub.dat"
    for p in (pending_prof, old_prof):
        p.write_text("# h\n1 2 0.1\n", encoding="utf-8")
    az._seed_handled_at_boot()           # both landed while the app was down
    assert str(pending_prof) in az._startup_present
    calls = []
    monkeypatch.setattr(az, "_analyze_file", lambda p, **kw: calls.append(str(p)))
    az._watch_once(); az._watch_once()
    assert str(pending_prof) in calls
    assert str(old_prof) not in calls, "history stays frozen"


def test_q19_restore_restarts_the_expiry_clock(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_RESUME", "1")
    monkeypatch.delenv("SWAXS_NO_RESUME", raising=False)
    _start()
    rid = next(iter(az._pending))
    az._pending_at[rid] = time.time() - 5 * az._PENDING_TIMEOUT_S   # long downtime
    az._save_campaign()
    monkeypatch.setattr(az, "_campaign", None)
    monkeypatch.setattr(az, "_pending", {})
    monkeypatch.setattr(az, "_pending_at", {})
    az._restore_campaign()
    assert az._campaign is not None and rid in az._pending
    assert time.time() - az._pending_at[rid] < 60
    az._expire_pending()
    assert rid in az._pending, "a restored condition must not expire on the first poll"


# ── Q20 ───────────────────────────────────────────────────────────────────────
def test_q20_analyzer_follows_the_reactors_saved_conditions_folder(tmp_path):
    (tmp_path / "Conds2").mkdir()
    (tmp_path / "reactor_settings.json").write_text(
        json.dumps({"recipes_folder": "Conds2"}), encoding="utf-8")
    assert az._resolve_cond() == tmp_path / "Conds2"
    assert az._cond_folder_check()["mismatch"] is False


def test_q20_an_explicit_different_folder_is_flagged(tmp_path):
    c = az.app.test_client()
    r = c.post("/api/campaign/folder", json={"folder": "Elsewhere"}).get_json()
    assert r["mismatch"] is True
    assert r["reactor"].endswith("Conditions")
    assert "MISMATCH" in _logs()
    _start()
    st = c.get("/api/campaign").get_json()
    assert st["conditions_folder_mismatch"] is True
    assert st["conditions_folder"].endswith("Elsewhere")


# ── Q21 ───────────────────────────────────────────────────────────────────────
def _spec_duck():
    d = types.SimpleNamespace(_lock=threading.RLock(), logs=[],
                              _spec_exposure=1.0, _spec_frames=1, _spec_lead=0.0,
                              _spec_sample_tag="sample", _spec_bkg_tag="bkg",
                              _spec_data_dir="")
    d._log = lambda msg, *a, **k: d.logs.append(msg)
    d.spec_lock_reason = lambda: ""
    return d


def test_q21_unknown_tags_are_refused_with_a_clear_message():
    from src.reactor.collection import CollectionMixin
    d = _spec_duck()
    ok, msg = CollectionMixin.set_spec_settings(d, {"sample_tag": "mysample",
                                                    "exposure_s": "5"})
    assert not ok and "mysample" in msg and "sample" in msg
    assert d._spec_sample_tag == "sample" and d._spec_exposure == 1.0   # nothing applied
    ok, msg = CollectionMixin.set_spec_settings(d, {"bkg_tag": "sample"})
    assert not ok and "background keyword" in msg


def test_q21_known_tags_are_accepted_and_parse():
    from src.reactor.collection import CollectionMixin
    from src.loop_naming import split_role, is_background
    d = _spec_duck()
    ok, _ = CollectionMixin.set_spec_settings(d, {"sample_tag": "smp", "bkg_tag": "Blank"})
    assert ok and (d._spec_sample_tag, d._spec_bkg_tag) == ("smp", "blank")
    assert split_role("Run1_r001_smp_0001.raw") == ("Run1_r001", "smp")
    assert not is_background("Run1_r001_smp_0001.raw")
    assert is_background("Run1_r001_blank_0001.raw")


# ── Q33 ───────────────────────────────────────────────────────────────────────
def test_q33_valid_enforces_per_pump_limits():
    from src.optimizer import ParameterSpace
    cfg = {"bounds": {"T_reac": [180, 300], "F_tot": [40, 120]},
           "pumps": {"ode_dilution": {"sensor_min": 5.0, "max_flow": 50.0},
                     "top": {"sensor_min": 0.0, "max_flow": 10.0},
                     "oleylamine": {"sensor_min": 0.0, "max_flow": 50.0},
                     "pd_top_precursor": {"sensor_min": 0.0, "max_flow": 1000.0}},
           "safety": {"per_pump_max": 1000.0}}
    sp = ParameterSpace.from_config(cfg)
    base = {"T_reac": 240.0, "F_tot": 80.0, "x_ODE": 0.0, "x_TOP": 0.1, "x_oley": 0.1}
    assert sp.valid(base)                                   # ODE off is allowed
    assert not sp.valid({**base, "x_ODE": 0.02})            # 1.6 < sensor_min 5
    assert sp.valid({**base, "x_ODE": 0.1})                 # 8 in [5, 50]
    assert not sp.valid({**base, "x_TOP": 0.2})             # 16 > max_flow 10
    # every Sobol point the optimizer can propose passes the reactor's own rule
    from src.reactor.recipe import Recipe, recipe_to_setpoints
    for p in sp.sobol(40, seed=3):
        recipe_to_setpoints(Recipe.from_dict({**p, "recipe_id": "t"}), cfg,
                            validate_first=False)          # raises if rejected


def test_q33_analyzer_space_uses_the_reactors_live_limits(tmp_path):
    (tmp_path / "reactor_limits.json").write_text(json.dumps(
        {"limits": {"top": {"sensor_min": 0.0, "max_flow": 5.0}}}), encoding="utf-8")
    from src.optimizer import ParameterSpace
    sp = ParameterSpace.from_config(az._space_cfg())
    assert sp.pump_limits["x_TOP"] == (0.0, 5.0)
    assert not sp.valid({"T_reac": 240.0, "F_tot": 80.0, "x_ODE": 0.1,
                         "x_TOP": 0.1, "x_oley": 0.1})


# ── Q34 ───────────────────────────────────────────────────────────────────────
def test_q34_pareto_front_ignores_low_confidence_fits():
    import src.optimizer.methods as M
    from src.optimizer import ParameterSpace
    from src.reactor import load_config
    camp = M.make("pareto_ehvi", ParameterSpace.from_config(load_config()),
                  target_size=5.0, tolerance=1.0, pdi_cap=0.3, budget=10, n_init=4,
                  size_lo=4.0, size_hi=6.0, stop_on_hit=False)
    camp.start()
    camp.tell(PARAMS, 5.0, 0.1, 0.9, recipe_id="good")
    camp.tell(PARAMS, 4.5, 0.01, 0.2, recipe_id="noisy")   # would dominate
    pts = camp._front_points()
    assert len(pts) == 1
