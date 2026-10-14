"""
tests/test_quick_audit_platform.py
==================================
Platform items of docs/audits/PLATFORM_QUICK_AUDIT_2026_10.md:
Q1 (assistant bind), Q2 (run_python runtime guard), Q22 (hub port), Q23
(cross-site POSTs), Q24 (bus liveness), Q25 (delivery status), Q26 (log
handle), Q35 (Windows kill tree), Q36 (launcher banners).
"""
from __future__ import annotations

import importlib.util as u
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")
os.environ["SWAXS_SLACK_WEBHOOK_URL"] = ""


def _load(tag, rel):
    spec = u.spec_from_file_location(tag, str(ROOT / rel))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


# ── Q1 ───────────────────────────────────────────────────────────────────────
def test_q1_assistant_binds_localhost_only():
    src = (ROOT / "assistant" / "app.py").read_text()
    assert 'host="0.0.0.0"' not in src
    assert 'app.run(host="127.0.0.1"' in src


# ── Q2 ───────────────────────────────────────────────────────────────────────
from src.ai.code_exec import run_user_code  # noqa: E402

_ESCAPES = {
    "shell via module attribute":
        "import matplotlib\nprint(matplotlib.subprocess.getstatusoutput('id'))",
    "read outside project":
        "import numpy as np\nprint(np.loadtxt('/etc/passwd', dtype=str, delimiter=':'))",
    "network via pandas":
        "import pandas as pd\nprint(pd.read_csv('http://127.0.0.1:9/x.csv'))",
    "write outside outputs":
        "import numpy as np\nnp.savetxt('{outside}', [1, 2])",
    "list outside project":
        "from pathlib import Path\nprint(list(Path('/etc').iterdir())[:3])",
}


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.mark.parametrize("static", [True, False], ids=["with-static", "runtime-only"])
@pytest.mark.parametrize("name", sorted(_ESCAPES))
def test_q2_escapes_are_blocked(name, static, project, tmp_path):
    pytest.importorskip("numpy")
    if "pandas" in _ESCAPES[name]:
        pytest.importorskip("pandas")
    if "matplotlib" in _ESCAPES[name]:
        pytest.importorskip("matplotlib")
    outside = tmp_path / "escaped.txt"
    code = _ESCAPES[name].replace("{outside}", outside.as_posix())
    res = run_user_code(code, project_root=project, _static_check=static)
    assert not res["ok"], f"{name} ran: {res}"
    assert "Blocked" in (res["error"] or ""), res["error"]
    assert not outside.exists()


def test_q2_plotting_and_project_io_still_work(project):
    pytest.importorskip("numpy")
    pytest.importorskip("matplotlib")
    (project / "a.dat").write_text("# q I s\n0.1 10 1\n0.2 5 1\n0.3 2 1\n")
    res = run_user_code(
        "import numpy as np\nimport matplotlib.pyplot as plt\n"
        "q, I, s = load_dat('a.dat')\n"
        "np.savetxt(OUTPUTS + '/copy.txt', I)\n"
        "print('n', len(q))\nplt.loglog(q, I)", project_root=project)
    assert res["ok"], res["error"]
    assert "n 3" in res["stdout"]
    assert res["figure"]
    assert (project / "assistant_outputs" / "copy.txt").exists()


def test_q2_message_describes_what_is_enforced():
    src = (ROOT / "src" / "ai" / "assistant.py").read_text()
    assert "no network, no file writes, no secret reads" not in src
    assert "not an OS" in src


# ── Q22 ──────────────────────────────────────────────────────────────────────
def test_q22_bus_url_follows_hub_port(monkeypatch):
    from src import events
    monkeypatch.delenv("SWAXS_HUB_URL", raising=False)
    monkeypatch.delenv("SWAXS_HUB_API", raising=False)
    monkeypatch.setenv("SWAXS_HUB_PORT", "5200")
    assert events.hub_ws_url() == "ws://localhost:5200/ws"
    assert events.hub_http_url() == "http://localhost:5200"
    assert events.EventBusClient("t")._url == "ws://localhost:5200/ws"
    monkeypatch.delenv("SWAXS_HUB_PORT")
    assert events.hub_ws_url() == "ws://localhost:5100/ws"
    monkeypatch.setenv("SWAXS_HUB_URL", "ws://example:1/ws")
    assert events.hub_ws_url() == "ws://example:1/ws"


def test_q22_hub_exports_its_port_to_children():
    src = (ROOT / "hub" / "app.py").read_text()
    assert 'env["SWAXS_HUB_PORT"] = str(_HUB_PORT)' in src


# ── Q23 ──────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def hub():
    return _load("hub_quick_audit", "hub/app.py")


def test_q23_hub_rejects_cross_site_posts(hub, tmp_path):
    c = hub.app.test_client()
    r = c.post("/api/set_project", json={"path": str(tmp_path)},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/stop_all", headers={"Referer": "http://evil.example/x"})
    assert r.status_code == 403
    # localhost origins and server-side calls (no Origin) still pass the guard
    for h in ({"Origin": "http://localhost:5100"}, {"Origin": "http://127.0.0.1:5100"},
              {"Origin": "http://[::1]:5100"}, {}):
        r = c.post("/api/set_project", json={"path": ""}, headers=h)
        assert r.status_code != 403, h
    # a non-JSON body no longer crashes the route (force=True dropped)
    r = c.post("/api/set_project", data="path=x",
               content_type="application/x-www-form-urlencoded")
    assert r.status_code < 500


def test_q23_assistant_guard_and_no_force_json():
    src = (ROOT / "assistant" / "app.py").read_text()
    assert "force=True" not in src
    assert "def _reject_cross_site" in src and "@app.before_request" in src


# ── Q24 / Q25 ────────────────────────────────────────────────────────────────
@pytest.fixture
def wd(monkeypatch):
    m = _load("wd_quick_audit", "watchdog/app.py")
    m._sent_messages.clear()
    m._bus_watch.update({"down_since": None, "alerted": False})
    m._settings = dict(m._settings or {}, slack_enabled=True)
    return m


def test_q24_bus_connected_reported(wd):
    c = wd.app.test_client()
    assert "bus_connected" in c.get("/api/health").get_json()
    assert "bus_connected" in c.get("/api/settings").get_json()


def test_q24_one_fault_after_60s_then_cleared(wd):
    wd._bus_watch_tick(now_mono=0.0, connected=False)
    wd._bus_watch_tick(now_mono=30.0, connected=False)
    assert not wd._sent_messages
    wd._bus_watch_tick(now_mono=61.0, connected=False)
    wd._bus_watch_tick(now_mono=200.0, connected=False)
    faults = [m for m in wd._sent_messages if m["level"] == "fault"]
    assert len(faults) == 1 and "event bus" in faults[0]["title"]
    wd._bus_watch_tick(now_mono=210.0, connected=True)
    assert wd._bus_watch == {"down_since": None, "alerted": False}
    assert "reconnected" in wd._sent_messages[-1]["title"]


def test_q25_history_status_not_configured(wd):
    from src.watchdog.transport import WebhookTransport
    t = WebhookTransport()
    assert t.webhook_url == ""
    wd._transport = t
    rec = wd._deliver("t", "x", "info")
    assert rec["status"] == "not configured"


def test_q25_transport_reports_queued_then_sent(monkeypatch):
    from src.watchdog import transport as tr
    monkeypatch.setattr(tr, "_post", lambda *a, **k: {"ok": True})
    monkeypatch.setenv(tr.ENV_WEBHOOK, "https://hooks.invalid/x")
    t = tr.WebhookTransport(min_interval_s=0.0)
    rec = {}
    assert t.send("t", "x", "fault", record=rec) == "queued"
    t.close(timeout=3.0)
    assert rec.get("status") == "sent"


# ── Q26 ──────────────────────────────────────────────────────────────────────
def test_q26_launch_closes_parent_log_handle():
    src = (ROOT / "hub" / "app.py").read_text()
    assert 'with open(log_path, "w", encoding="utf-8") as logf:' in src


# ── Q35 ──────────────────────────────────────────────────────────────────────
def test_q35_windows_signal_kills_children_first(monkeypatch):
    from src import proc_lifecycle as pl
    calls = []

    class _Kid:
        def __init__(self, n):
            self.n = n

        def kill(self):
            calls.append(("kill", self.n))

    class _PP:
        def __init__(self, pid):
            pass

        def children(self, recursive=False):
            assert recursive
            return [_Kid(1), _Kid(2)]

    class _Psutil:
        Process = _PP

    class _Proc:
        pid = 123

        def terminate(self):
            calls.append(("terminate", 0))

    monkeypatch.setattr(pl, "IS_WIN", True)
    monkeypatch.setattr(pl, "psutil", _Psutil)
    pl._signal_group(_Proc(), 15)
    assert calls == [("kill", 1), ("kill", 2), ("terminate", 0)]


# ── Q36 ──────────────────────────────────────────────────────────────────────
def test_q36_launcher_banners():
    bat = (ROOT / "start_platform.bat").read_text()
    sh = (ROOT / "start_platform.sh").read_text()
    assert "Vis ^& Avg" in bat and "Vis & Avg" not in bat
    assert "Project: !SWAXS_PROJECT!" in bat
    for txt in (bat, sh):
        assert "5107 Autonomous Analyser" in txt
        assert "Auto-Fit" not in txt
