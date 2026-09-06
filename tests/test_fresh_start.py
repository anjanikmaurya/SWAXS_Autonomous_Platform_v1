"""
tests/test_fresh_start.py — the platform starts FRESH on every stop/restart.

The operator asked that nothing from a previous run carry over: no monitor
auto-resume, no restoring the processed/batch/subtraction memos, no reusing the
Bayesian campaign. Resume is OFF by default and only returns under SWAXS_RESUME=1.
Aborting a campaign wipes the slate so the next start is a brand-new Target Run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.runstate import (save_state, load_state, save_monitor, load_monitor,  # noqa: E402
                          resume_disabled)


# ── the resume lever ─────────────────────────────────────────────────────────

def test_resume_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SWAXS_RESUME", raising=False)
    monkeypatch.delenv("SWAXS_NO_RESUME", raising=False)
    assert resume_disabled() is True


def test_swaxs_resume_re_enables(monkeypatch):
    monkeypatch.delenv("SWAXS_NO_RESUME", raising=False)
    monkeypatch.setenv("SWAXS_RESUME", "1")
    assert resume_disabled() is False


def test_no_resume_overrides_resume(monkeypatch):
    monkeypatch.setenv("SWAXS_RESUME", "1")
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")     # hard override wins
    assert resume_disabled() is True


def test_saved_state_is_not_read_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SWAXS_RESUME", raising=False)
    monkeypatch.delenv("SWAXS_NO_RESUME", raising=False)
    assert save_state(tmp_path, "thing", {"a": 1}) is True          # writing is fine
    assert load_state(tmp_path, "thing") is None                    # but not read back
    save_monitor(tmp_path, "reduction", True, {"interval": 10})
    assert load_monitor(tmp_path, "reduction") is None              # monitor not resumed
    # ...unless the operator opts in
    monkeypatch.setenv("SWAXS_RESUME", "1")
    assert (load_state(tmp_path, "thing") or {}).get("a") == 1
    assert load_monitor(tmp_path, "reduction") == {"interval": 10}


# ── analyzer: abort wipes the slate ───────────────────────────────────────────

def test_abort_resets_analyzer_to_a_clean_slate(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    import analyzer.app as az
    from src.optimizer import ParameterSpace, CampaignController

    az._project_root = str(tmp_path)
    sp = ParameterSpace.from_config(
        {"bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
                    "x_each": [0, 0.3], "x_sum_max": 0.9}})
    az._campaign = CampaignController(sp, target_size=5.0, tolerance=0.3,
                                      pdi_cap=0.2, budget=25, n_init=10, seed=1)
    az._campaign.start()
    az._campaign_id = "x"; az._run_tag = "Run4"; az._run_seq = 3
    az._pending["Run4_r001"] = {"T_reac": 240}
    az._pending_at["Run4_r001"] = 0.0
    az._handled["f.dat"] = (1, 2)
    save_state(str(tmp_path), az._CAMPAIGN_STATE, {"status": "running"})

    rv = az.app.test_client().post("/api/campaign/abort")
    assert rv.status_code == 200
    assert az._campaign is None
    assert az._pending == {} and az._pending_at == {}
    assert az._handled == {}
    assert az._run_tag == "" and az._run_seq == 0
    # the resume file is gone, so a restart cannot bring the campaign back
    assert not (tmp_path / ".swaxs_state" / f"{az._CAMPAIGN_STATE}.json").exists()
