"""The "Open pyFAI-calib2" button: opens the GUI with the right inputs, detached,
and touches nothing else on the platform.

The real GUI needs Qt and a display, so these tests stand in a tiny fake GUI for
the pyFAI-calib2 command. Everything else (helper process, crash watch, .raw to
CBF conversion, argument building, the Flask route) is the real code.
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

import numpy as np
import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.preprocess import calib  # noqa: E402

SAXS = (1043, 981)


def _fake_gui(monkeypatch, code):
    """Replace the pyFAI-calib2 launcher with ``python -c <code>``."""
    monkeypatch.setattr(calib, "_calib2_launcher",
                        lambda: ([sys.executable, "-c", code], "test"))


def _raw(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    np.random.poisson(50, SAXS).astype(np.int32).tofile(path)
    return path


def _app(tmp_path):
    spec = importlib.util.spec_from_file_location("cal_launch_app", ROOT / "calibration/app.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m._project_root = str(tmp_path)
    return m


def _kill(pid):
    try:
        psutil.Process(pid).kill()
    except psutil.Error:
        pass


# ── inputs ──────────────────────────────────────────────────────────────────
def test_calibrant_names_are_pyfai_names():
    pf = pytest.importorskip("pyFAI.calibrant")
    for name in calib.CALIBRANTS:
        assert name in pf.CALIBRANT_FACTORY, f"{name} is not a pyFAI calibrant"
    # the old default was not a pyFAI name and opened the GUI with no calibrant
    assert calib.resolve_calibrant("AgBehenate") == "AgBh"
    assert calib.resolve_calibrant("") == "AgBh"


def test_command_is_accepted_by_pyfai_calib2_parser(tmp_path, monkeypatch):
    c2 = pytest.importorskip("pyFAI.app.calib2")
    cmd = calib.build_calib2_command(tmp_path / "x.cbf", "AgBehenate", 12.0, pixel_um=172.0)
    monkeypatch.setattr(sys, "argv", cmd)
    o = c2.parse_options()
    assert o.spacing == "AgBh" and o.energy == 12.0
    assert c2.parse_pixel_size(o.pixel)[0] == pytest.approx(172e-6)
    assert o.args == [str(tmp_path / "x.cbf")]


def test_launcher_prefers_the_platform_interpreter():
    pytest.importorskip("pyFAI.app.calib2")
    argv, _ = calib._calib2_launcher()
    assert argv[0] == sys.executable, "a pyFAI-calib2 on PATH may be another env without Qt"


def test_energy_must_be_positive(tmp_path):
    img = tmp_path / "a.cbf"; img.write_bytes(b"x")
    ok, msg, _ = calib.launch_calib2(img, "AgBh", 0)
    assert not ok and "energy" in msg


# ── launch behaviour ───────────────────────────────────────────────────────
def test_gui_is_detached_and_reported_open(tmp_path, monkeypatch):
    marker = tmp_path / "pid.txt"
    _fake_gui(monkeypatch, f"import os,time;open({str(marker)!r},'w').write(str(os.getpid()));time.sleep(60)")
    img = tmp_path / "a.cbf"; img.write_bytes(b"x")
    ok, msg, cmd = calib.launch_calib2(img, "AgBh", 12, workdir=tmp_path / "poni", watch_s=1.0)
    assert ok, msg
    pid = int(marker.read_text())
    try:
        assert psutil.pid_exists(pid)
        mine = {p.pid for p in psutil.Process().children(recursive=True)}
        assert pid not in mine, "GUI is still a child: stopping the app would close it"
        if os.name != "nt":
            assert os.getsid(pid) != os.getsid(0), "GUI shares the app's session"
        assert psutil.Process(pid).cwd() == str(tmp_path / "poni")
    finally:
        _kill(pid)


def test_gui_that_dies_is_reported_with_its_error(tmp_path, monkeypatch):
    _fake_gui(monkeypatch, "import sys;sys.stderr.write('ImportError: No Qt wrapper found\\n');sys.exit(1)")
    img = tmp_path / "a.cbf"; img.write_bytes(b"x")
    t0 = time.time()
    ok, msg, _ = calib.launch_calib2(img, "AgBh", 12, watch_s=5.0)
    assert not ok and "PySide6" in msg and "rc=1" in msg
    assert time.time() - t0 < 4, "a crash should be reported at once, not after the full watch"


def test_gui_does_not_inherit_headless_settings(tmp_path, monkeypatch):
    out = tmp_path / "env.txt"
    _fake_gui(monkeypatch, "import os,time;open(%r,'w').write(repr((os.environ.get('MPLBACKEND'),"
                           "os.environ.get('QT_QPA_PLATFORM'))));time.sleep(30)" % str(out))
    monkeypatch.setenv("MPLBACKEND", "Agg"); monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    img = tmp_path / "a.cbf"; img.write_bytes(b"x")
    ok, _, _ = calib.launch_calib2(img, "AgBh", 12, watch_s=1.0)
    assert ok and out.read_text() == "(None, None)"
    for p in psutil.process_iter(["cmdline"]):
        if str(out) in " ".join(p.info["cmdline"] or []):
            _kill(p.pid)


# ── the Flask route ────────────────────────────────────────────────────────
def test_route_converts_raw_and_touches_nothing_else(tmp_path, monkeypatch):
    m = _app(tmp_path)
    raw = _raw(tmp_path / "2D" / "calib" / "AgBh_0001.raw")
    (tmp_path / "manifest.json").write_text("{}")
    before = {p: p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()}
    seen = {}

    def fake_launch(img, calibrant, energy, pixel_um=None, workdir=None, **kw):
        seen.update(img=img, calibrant=calibrant, energy=energy, pixel=pixel_um, workdir=workdir)
        return True, "opened", "cmd"
    monkeypatch.setattr(m, "launch_calib2", fake_launch)

    r = m.app.test_client().post("/api/calibrate/launch", json={
        "cbf": str(raw), "calibrant": "AgBh", "energy_keV": 12, "pixel_um": 172})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"], j
    cbf = Path(seen["img"])
    assert cbf.suffix == ".cbf" and cbf.is_file(), "pyFAI cannot read the headerless .raw"
    fabio = pytest.importorskip("fabio")
    assert fabio.open(str(cbf)).data.shape == SAXS
    assert seen["energy"] == 12 and seen["pixel"] == 172 and seen["calibrant"] == "AgBh"
    assert Path(seen["workdir"]) == tmp_path / "poni"
    # only the new CBF appeared; no .poni, config or manifest change
    after = {p: p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()}
    assert set(after) - set(before) == {cbf}
    assert all(after[p] == t for p, t in before.items())


def test_route_rejects_missing_inputs(tmp_path):
    c = _app(tmp_path).app.test_client()
    assert c.post("/api/calibrate/launch", json={}).status_code == 400
    assert c.post("/api/calibrate/launch", json={"cbf": "/no/such.cbf", "energy_keV": 12}).status_code == 400
    img = tmp_path / "a.cbf"; img.write_bytes(b"x")
    r = c.post("/api/calibrate/launch", json={"cbf": str(img), "energy_keV": ""})
    assert r.status_code == 400 and "energy" in r.get_json()["error"]


def _kill_fakes(marker):
    """Kill only the fake GUIs this test started: `python -c <code>` processes."""
    for p in psutil.process_iter(["cmdline"]):
        cl = p.info["cmdline"] or []
        if len(cl) > 2 and cl[1] == "-c" and marker in cl[2] and "pytest" not in " ".join(cl):
            _kill(p.pid)


# ── the detector model (Oct 2026: a bare --pixel crashed pyFAI-calib2 on start) ──
def _cbf(path, shape):
    fabio = pytest.importorskip("fabio")
    fabio.cbfimage.CbfImage(data=np.zeros(shape, dtype=np.int32)).write(str(path))
    return path


def test_detector_is_named_from_the_image_shape(tmp_path):
    pytest.importorskip("pyFAI")
    assert calib.detector_for_image(_cbf(tmp_path / "s.cbf", (1043, 981)), 172) == "pilatus1m"
    assert calib.detector_for_image(_cbf(tmp_path / "w.cbf", (195, 487)), 172) == "pilatus100k"
    assert calib.detector_for_image(_cbf(tmp_path / "x.cbf", (100, 120))) is None
    assert calib.detector_for_image(tmp_path / "s.cbf", 75) is None      # pixel size disagrees
    assert calib.detector_for_image(tmp_path / "missing.cbf") is None


def test_launch_names_the_detector_and_never_a_bare_pixel(tmp_path, monkeypatch):
    pytest.importorskip("pyFAI")
    out = tmp_path / "argv.txt"
    _fake_gui(monkeypatch, "import sys,time;open(%r,'w').write(' '.join(sys.argv[1:]));time.sleep(30)" % str(out))
    img = _cbf(tmp_path / "AgBh.cbf", (1043, 981))
    ok, msg, cmd = calib.launch_calib2(img, "AgBh", 12, pixel_um=172, watch_s=1.0)
    try:
        assert ok and "pilatus1m" in msg
        assert "--detector pilatus1m" in cmd and "--pixel" not in cmd
    finally:
        _kill_fakes(str(out))
    odd = _cbf(tmp_path / "odd.cbf", (100, 120))
    _fake_gui(monkeypatch, "import time;time.sleep(30)")
    ok, msg, cmd = calib.launch_calib2(odd, "AgBh", 12, pixel_um=172, watch_s=1.0)
    _kill_fakes("time.sleep(30)")
    assert ok and "--pixel" not in cmd and "--detector" not in cmd and "choose the detector" in msg


def test_pyfai_accepts_the_detector_option_and_knows_its_shape(monkeypatch):
    c2 = pytest.importorskip("pyFAI.app.calib2")
    from pyFAI.detectors import detector_factory
    monkeypatch.setattr(sys, "argv", ["pyFAI-calib2", "--calibrant", "AgBh", "--energy", "12",
                                      "--detector", "pilatus1m", "x.cbf"])
    o = c2.parse_options()
    assert o.detector_name == "pilatus1m"
    d = detector_factory(o.detector_name)
    assert tuple(d.max_shape) == (1043, 981) and abs(d.pixel1 - 172e-6) < 1e-9


# ── Detector menu (replaces the Pixel field) ─────────────────────────────────
def test_detector_list_is_every_pyfai_model_with_its_size():
    pytest.importorskip("pyFAI")
    dets = calib.list_detectors()
    by = {d["id"]: d for d in dets}
    assert len(dets) > 50, "the menu should offer pyFAI's whole catalogue"
    assert by["pilatus1m"]["shape"] == [1043, 981] and by["pilatus1m"]["pixel_um"] == 172.0
    assert by["pilatus100k"]["shape"] == [195, 487]
    assert all(d["shape"] and d["manufacturer"] for d in dets)
    assert len({d["name"] for d in dets}) == len(dets), "aliases must be merged into one entry"


def test_a_detector_that_does_not_fit_the_image_is_refused(tmp_path, monkeypatch):
    pytest.importorskip("pyFAI")
    img = _cbf(tmp_path / "saxs.cbf", (1043, 981))
    assert calib.check_detector(img, "pilatus1m") == (True, "")
    ok, why = calib.check_detector(img, "pilatus2m")
    assert not ok and "1043 × 981" in why and "1679 × 1475" in why
    assert not calib.check_detector(img, "no_such_detector")[0]
    binned = _cbf(tmp_path / "bin.cbf", (1043 // 1, 981 // 1))
    assert calib.check_detector(binned, "pilatus1m")[0]
    _fake_gui(monkeypatch, "import time;time.sleep(30)")
    ok, msg, cmd = calib.launch_calib2(img, "AgBh", 12, detector="pilatus2m", watch_s=1.0)
    assert not ok and "Pick the detector" in msg and cmd == ""        # nothing was started


def test_the_chosen_detector_is_passed_to_pyfai(tmp_path, monkeypatch):
    pytest.importorskip("pyFAI")
    img = _cbf(tmp_path / "waxs.cbf", (195, 487))
    _fake_gui(monkeypatch, "import time;time.sleep(30)")
    ok, msg, cmd = calib.launch_calib2(img, "AgBh", 12, detector="pilatus100k", watch_s=1.0)
    _kill_fakes("time.sleep(30)")
    assert ok and "--detector pilatus100k" in cmd and "--pixel" not in cmd


def test_route_and_page_offer_the_detector_menu(tmp_path, monkeypatch):
    pytest.importorskip("pyFAI")
    m = _app(tmp_path)
    j = m.app.test_client().get("/api/detectors").get_json()
    assert any(d["id"] == "pilatus1m" for d in j["detectors"])
    html = (ROOT / "calibration/templates/index.html").read_text()
    assert 'id="detsel"' in html and 'value="auto"' in html
    assert 'id="pixel"' not in html, "the Pixel field is replaced by the Detector menu"
    assert 'detector:$("detsel").value' in html
