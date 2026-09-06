"""
tests/test_analyzer_audit.py — analyzer/optimizer findings from the Sept 2026
robustness audit.

  #3  CampaignController.tell() stores recipe_id as a top-level history field, so
      the loss can be joined back to a recipe and reported (it was always None).
      _last_loss_for() finds it.
  #4  _snapshot_handled() copies the intake memo under a lock, so persisting it
      cannot raise "dictionary changed size during iteration" against the watcher.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.optimizer import ParameterSpace, CampaignController   # noqa: E402
import analyzer.app as az                                      # noqa: E402

_CFG = {"bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
                   "x_each": [0, 0.3], "x_sum_max": 0.9}}


# ── #3: recipe_id survives into the campaign history + loss lookup ────────────

def test_tell_stores_recipe_id_in_history():
    sp = ParameterSpace.from_config(_CFG)
    c = CampaignController(sp, target_size=5.0, tolerance=0.3, pdi_cap=0.2,
                           budget=25, n_init=10, seed=1)
    c.start()
    p = {"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1}
    rec = c.tell(p, 5.0, 0.1, confidence=0.9, recipe_id="r042")
    assert rec["recipe_id"] == "r042"
    assert c.history[-1]["recipe_id"] == "r042"
    # recipe_id must NOT leak into params (params replay into the GP)
    assert "recipe_id" not in rec["params"]


def test_last_loss_for_finds_the_recipe(monkeypatch):
    sp = ParameterSpace.from_config(_CFG)
    c = CampaignController(sp, target_size=5.0, tolerance=0.3, pdi_cap=0.2,
                           budget=25, n_init=10, seed=1)
    c.start()
    p = {"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1}
    c.tell(p, 4.0, 0.1, confidence=0.9, recipe_id="r001")
    c.tell(p, 5.0, 0.1, confidence=0.9, recipe_id="r002")
    monkeypatch.setattr(az, "_campaign", c)
    # each recipe's own loss, not the latest / not None
    assert az._last_loss_for("r001") == round(c.history[0]["loss"], 4)
    assert az._last_loss_for("r002") == round(c.history[1]["loss"], 4)
    assert az._last_loss_for("nope") is None
    assert az._last_loss_for("") is None


# ── #4: intake-memo snapshot is lock-guarded ──────────────────────────────────

def test_snapshot_handled_is_safe_under_concurrent_mutation():
    az._handled.clear()
    for i in range(200):
        az._handled[f"f{i}"] = (i, i)

    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            with az._intake_lock:
                az._handled[f"x{i % 300}"] = (i, i)
                az._handled.pop(f"x{(i + 1) % 300}", None)
            i += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(500):
            snap = az._snapshot_handled()          # must never raise
            assert isinstance(snap, dict)
    finally:
        stop.set(); t.join(timeout=2.0)
