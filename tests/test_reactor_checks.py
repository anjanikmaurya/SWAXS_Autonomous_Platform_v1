"""The Hardware test checklist (src/reactor/checks.py): read only, honest, helpful."""
from __future__ import annotations

import copy
import importlib.util
import re
import time
from pathlib import Path

import yaml

from src.reactor import checks
from src.reactor.controller import ReactorController

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path):
    cfg = yaml.safe_load(open("reactor/config.yml"))
    cfg["spec"]["data_dir"] = str(tmp_path / "proj")
    cfg["spec"]["simulator"].update(enabled=False)
    return cfg


def _status(tmp_path):
    ctl = ReactorController(_cfg(tmp_path), backend="mock")
    try:
        time.sleep(0.6)                                   # let the loop take a reading
        return ctl.status(), ctl.cfg
    finally:
        ctl.shutdown()


def test_all_checks_pass_in_simulation_and_say_so(tmp_path):
    st, cfg = _status(tmp_path)
    res = checks.run_all(st, cfg)
    assert [r["id"] for r in res] == [c.id for c in checks.CHECKS]
    assert all(r["ok"] for r in res), res
    assert any("simulated" in str(r["value"]) for r in res)


def test_each_failure_names_the_problem_and_a_fix(tmp_path):
    st, cfg = _status(tmp_path)
    bad = copy.deepcopy(st)
    name = next(iter(bad["pumps"]))
    del bad["pumps"][name]
    r = checks.run_check("pumps_present", bad, cfg)
    assert not r.ok and name in r.value and "cable" in r.hint
    bad = copy.deepcopy(st); bad["pumps"][name]["fault"] = True
    assert not checks.run_check("pumps_healthy", bad, cfg).ok
    bad = copy.deepcopy(st); bad["pumps"][name]["pressure"] = 99999.0
    r = checks.run_check("pressure", bad, cfg)
    assert not r.ok and "blockage" in r.hint
    bad = copy.deepcopy(st); bad["temperature"]["source"] = "unwired"
    r = checks.run_check("temperature", bad, cfg)
    assert not r.ok and "interlock" in r.hint
    bad = copy.deepcopy(st); bad["temperature"].update(source="beamline", stale=True, age_s=42)
    assert not checks.run_check("temperature", bad, cfg).ok
    bad = copy.deepcopy(st); bad["temperature"]["i0"] = None
    assert not checks.run_check("beamline", bad, cfg).ok


def test_a_broken_check_reports_instead_of_raising(tmp_path):
    r = checks.run_check("pressure", {"pumps": {"x": None}}, {})
    assert r.ok is False and "failed" in r.hint


def test_checks_never_command_hardware():
    src = (ROOT / "src/reactor/checks.py").read_text()
    for call in ("set_pump_flow", "idle_all", "zero_pumps", "estop", "collect", "set_temperature",
                 "close_shutter", "open_shutter", "_cmd"):
        assert not re.search(r"\.%s\s*\(" % call, src), f"checks.py must be read only, calls .{call}()"
    assert not re.search(r"^\s*(import|from)\s+(serial|requests|epics)", src, re.M)


def test_api_runs_the_checks_and_page_has_the_button(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_SLACK_WEBHOOK_URL", "")
    spec = importlib.util.spec_from_file_location("reactor_app_checks", ROOT / "reactor/app.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    try:
        c = m.app.test_client()
        assert len(c.get("/api/checks").get_json()["checks"]) == len(checks.CHECKS)
        j = c.post("/api/checks/run", json={"id": "all"}).get_json()
        assert j["ok"] and len(j["results"]) == len(checks.CHECKS)
        assert c.post("/api/checks/run", json={"id": "nope"}).status_code == 400
    finally:
        try:
            m._ctrl.shutdown()
        except Exception:
            pass
    html = (ROOT / "reactor/templates/index.html").read_text()
    assert 'id="runChecksBtn"' in html and "function runChecks(" in html
