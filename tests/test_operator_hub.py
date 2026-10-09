"""The operator is set once, in the hub, and reaches every app's provenance."""
from __future__ import annotations

import importlib.util as _u
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _hub(monkeypatch, tmp_path):
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    spec = _u.spec_from_file_location("hub_operator_t", ROOT / "hub" / "app.py")
    h = _u.module_from_spec(spec); spec.loader.exec_module(h)
    from src import operator_id
    monkeypatch.setattr(operator_id, "OPERATOR_FILE", tmp_path / "op.txt")
    return h


def test_hub_sets_operator_into_project_env_and_file(monkeypatch, tmp_path):
    h = _hub(monkeypatch, tmp_path)
    proj = tmp_path / "proj"; proj.mkdir()
    monkeypatch.setattr(h, "_project_root", str(proj))
    c = h.app.test_client()
    r = c.post("/api/operator", json={"operator": "  AKM   lab  "}).get_json()
    assert r["operator"] == "AKM lab"
    assert c.get("/api/operator").get_json()["operator"] == "AKM lab"
    import os
    assert os.environ.get("SWAXS_USER_ID") == "AKM lab"          # apps launched from now on
    from src.operator_id import from_manifest, load_saved
    assert from_manifest(proj) == "AKM lab"                       # apps already running
    assert load_saved() == "AKM lab"                              # survives a hub restart


def test_an_app_resolves_the_hubs_operator_from_the_project(monkeypatch, tmp_path):
    from src import operator_id
    proj = tmp_path / "p"; proj.mkdir()
    monkeypatch.setenv("SWAXS_USER_ID", "env-person")
    operator_id.write_to_project(proj, "hub-person")
    assert operator_id.current_operator(proj) == "hub-person"           # hub wins over env
    assert operator_id.current_operator(proj, explicit="caller") == "caller"
    assert operator_id.current_operator(tmp_path / "none") == "env-person"


def test_reduction_takes_the_operator_from_the_hub(monkeypatch, tmp_path):
    from src import operator_id
    proj = tmp_path / "r"; proj.mkdir()
    operator_id.write_to_project(proj, "AKM")
    monkeypatch.setenv("SWAXS_PROJECT", str(proj))
    import importlib
    red = importlib.import_module("reduction.app")
    assert red._current_user(None) == "AKM"
    assert red._current_user("") == "AKM"


def test_links_between_apps_carry_the_theme():
    boot = (ROOT / "hub" / "templates" / "_theme_boot.html").read_text()
    helpers = (ROOT / "hub" / "templates" / "_icon_helpers.html").read_text()
    assert "get('theme')" in boot and "searchParams.set('theme'" in helpers
