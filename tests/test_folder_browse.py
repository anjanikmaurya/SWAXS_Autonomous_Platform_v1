"""Every folder field has a Browse… button, and every app can serve it.

* src/folder_browse.list_dirs: the listing behind the shared picker.
* each app that shows a folder field answers GET /api/browse in the shape the
  shared picker (swaxsBrowse in _icon_helpers.html) reads.
* policy: a text input for a folder never ships without a Browse… next to it.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.folder_browse import list_dirs  # noqa: E402

# input ids that hold a folder, per app (hub uses its own project picker)
FOLDER_FIELDS = {
    "analysis": ["folder"],
    "analyzer": ["folder"],
    "reactor": ["condFolder"],
    "quality": ["saxs-folder", "waxs-folder"],
    "assistant": ["projectRoot"],
    "average": ["saxs-dir", "waxs-dir", "out-dir", "raw-saxs-dir", "raw-waxs-dir",
                "aa-saxs-dir", "aa-waxs-dir", "aa-out-dir"],
    "background": ["sam-folder", "bkg-folder", "out-folder", "auto-saxs-folder",
                   "auto-waxs-folder"],
}


def test_list_dirs_resolves_relative_paths_inside_the_project(tmp_path):
    (tmp_path / "1D" / "SAXS" / "Subtracted").mkdir(parents=True)
    (tmp_path / "1D" / "SAXS" / "Averaged").mkdir()
    (tmp_path / "1D" / "SAXS" / ".hidden").mkdir()
    (tmp_path / "1D" / "SAXS" / "a.dat").write_text("x")
    d = list_dirs("1D/SAXS", str(tmp_path))
    assert d["current"] == str(tmp_path / "1D" / "SAXS")
    assert d["dirs"] == ["Averaged", "Subtracted"]          # folders only, no hidden
    assert d["parent"] == str(tmp_path / "1D")


def test_list_dirs_falls_back_to_the_nearest_existing_parent(tmp_path):
    d = list_dirs(str(tmp_path / "not" / "yet" / "made"), str(tmp_path))
    assert d["current"] == str(tmp_path)
    assert not (tmp_path / "not").exists(), "browsing must never create folders"


def test_list_dirs_blank_opens_the_project(tmp_path):
    assert list_dirs("", str(tmp_path))["current"] == str(tmp_path)


def _load(app_id):
    spec = importlib.util.spec_from_file_location(f"fb_{app_id}", ROOT / app_id / "app.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("app_id", sorted(FOLDER_FIELDS))
def test_every_app_with_a_folder_field_serves_browse(app_id, tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_SLACK_WEBHOOK_URL", "")
    (tmp_path / "sub").mkdir()
    m = _load(app_id)
    r = m.app.test_client().get(f"/api/browse?path={tmp_path}")
    assert r.status_code == 200
    j = r.get_json()
    # the shared picker reads current/parent/dirs (average/background/reduction
    # have their own pickers and may add more keys; the core ones must be there)
    assert "sub" in (j.get("dirs") or [d.get("name") for d in j.get("entries", [])
                                        if isinstance(d, dict)])


@pytest.mark.parametrize("app_id", sorted(FOLDER_FIELDS))
def test_no_folder_field_without_browse(app_id):
    html = (ROOT / app_id / "templates" / "index.html").read_text()
    for fid in FOLDER_FIELDS[app_id]:
        m = re.search(r'<input[^>]*\bid="%s"' % re.escape(fid), html)
        assert m, f"{app_id}: no #{fid}"
        tail = html[m.end(): m.end() + 600]
        assert re.search(r"(openBrowser|swaxsBrowse)\('%s'" % re.escape(fid), tail), \
            f"{app_id}: #{fid} has no Browse… button beside it"
        assert "Browse…" in tail


def test_shared_picker_is_in_every_app():
    for app_id in FOLDER_FIELDS:
        helpers = (ROOT / app_id / "templates" / "_icon_helpers.html")
        if helpers.exists():                                # generated file
            assert "function swaxsBrowse(" in helpers.read_text()
    src = (ROOT / "tools" / "build_icon_sprite.py").read_text()
    assert "function swaxsBrowse(" in src
    # names from the server are inserted as text, never parsed as HTML
    assert "createTextNode(n)" in src
