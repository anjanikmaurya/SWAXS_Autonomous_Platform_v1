"""
tests/test_hub_lifecycle.py — closing an app must actually close it.

WHAT WAS WRONG
--------------
The hub managed sub-apps with `Popen.terminate()` and a bare port check:

  * **Stop did nothing it had not spawned.** `_stop_app` returned "Not running"
    whenever its own Popen handle was gone — so closing an app that a PREVIOUS
    hub run had started did nothing at all, while that process kept polling the
    project folder and writing files.
  * **Stop did not wait for the port**, so an immediate restart could race the
    socket teardown and fail to bind.
  * **Start refused instead of recovering**: "Port 5003 is already in use — free
    that port and retry", with no way to free it from the UI.
  * **A hub SIGKILL stranded every child.** atexit cannot run, nothing was
    recorded on disk, so the next hub found nine taken ports and a UI that said
    "Stopped".
  * **Ctrl-C relied on atexit from a signal handler** and often left the apps up.

These tests spawn REAL processes on REAL ports. They are slower than the rest of
the suite on purpose: the bug lived precisely in the gap between what the process
table said and what was actually running.
"""
from __future__ import annotations

import importlib.util as u
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import proc_lifecycle as pl                      # noqa: E402

psutil = pytest.importorskip("psutil")

#: A cheap app to spawn — the quality gate boots fast and touches nothing.
APP_ID = "quality"


def _hub(tag="hubtest"):
    spec = u.spec_from_file_location(tag, str(ROOT / "hub" / "app.py"))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


def _wait(pred, timeout=25.0, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def hub():
    h = _hub()
    yield h
    try:
        for a in h.APPS:
            h._stop_app(a["id"])
    except Exception:
        pass


@pytest.fixture
def meta(hub):
    m = hub._app_by_id(APP_ID)
    if m is None:
        pytest.skip(f"{APP_ID} is not registered in apps.yml")
    if pl.port_in_use(m["port"]):
        pytest.skip(f"port {m['port']} is already busy on this machine")
    return m


# ── the pure helpers ─────────────────────────────────────────────────────────
def test_only_our_own_processes_are_recognised():
    """The safety property. Killing by port number alone would be a foot-gun on a
    shared workstation, so identification must be strict."""
    class P:
        def __init__(self, c): self._c = c
        def cmdline(self): return self._c

    entry, root = "background/app.py", "/opt/swaxs"
    assert pl.is_our_app(P(["python", "background/app.py"]), entry, root)
    assert pl.is_our_app(P(["/usr/bin/python3", "/opt/swaxs/background/app.py"]), entry, root)
    # not ours
    assert not pl.is_our_app(P(["postgres", "-D", "/var/lib/pg"]), entry, root)
    assert not pl.is_our_app(P(["python", "manage.py", "runserver"]), entry, root)
    assert not pl.is_our_app(P(["python", "/opt/swaxs/tools/notify_test.py"]), entry, root)
    assert not pl.is_our_app(P([]), entry, root)
    assert not pl.is_our_app(P(["python", "analyzer/app.py"]), entry, root), \
        "a DIFFERENT app of ours must not be claimed by this app's entry"


def test_wait_port_free_returns_promptly_when_free():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t0 = time.time()
    assert pl.wait_port_free(port, timeout=3.0)
    assert time.time() - t0 < 1.0


def test_log_rotation_keeps_the_previous_traceback(tmp_path):
    """Opening the log "w" on start destroyed the traceback that explained the
    crash the operator was reacting to (platform audit O17)."""
    log = tmp_path / "app.log"
    log.write_text("first run: Traceback ...")
    pl.rotate_log(log)
    assert (tmp_path / "app.log.1").read_text().startswith("first run")
    log.write_text("second run")
    pl.rotate_log(log)
    assert (tmp_path / "app.log.1").read_text() == "second run"
    assert (tmp_path / "app.log.2").read_text().startswith("first run")
    pl.rotate_log(tmp_path / "absent.log")          # must not raise


def test_reclaim_refuses_when_the_owner_cannot_be_identified(monkeypatch):
    """No psutil, or an OS that hides the owner → refuse loudly rather than guess
    and kill something at random."""
    monkeypatch.setattr(pl, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(pl, "listeners", lambda p: [])
    freed, msg, killed = pl.reclaim_port(5999, "background/app.py", ROOT)
    assert freed is False and killed == []
    assert "could not be identified" in msg


# ── real processes ───────────────────────────────────────────────────────────
@pytest.mark.slow
def test_stop_frees_the_port_and_clears_the_registry(hub, meta):
    port = meta["port"]
    ok, msg = hub._start_app(APP_ID)
    assert ok, msg
    assert _wait(lambda: pl.port_in_use(port)), "the app never bound its port"
    assert pl.read_children(hub._CHILDREN_FILE).get(APP_ID, {}).get("pid")

    ok, msg = hub._stop_app(APP_ID)
    assert ok, msg
    assert not pl.port_in_use(port), "stop returned before the port was released"
    assert APP_ID not in pl.read_children(hub._CHILDREN_FILE)


@pytest.mark.slow
def test_stopping_an_app_the_hub_never_spawned_still_kills_it(hub, meta):
    """The headline bug: an orphan from a previous hub run was reported as
    "Not running" and left alive, polling folders and holding the port."""
    port = meta["port"]
    entry = ROOT / meta["entry"]
    orphan = subprocess.Popen([sys.executable, str(entry)], cwd=str(ROOT),
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                              **pl.popen_kwargs())
    try:
        assert _wait(lambda: pl.port_in_use(port)), "the orphan never bound"
        assert hub._is_running(APP_ID) is False, "the hub should not own this process"

        ok, msg = hub._stop_app(APP_ID)
        assert ok, msg
        assert "reclaim" in msg.lower(), f"stop did not report the reclaim: {msg}"
        assert _wait(lambda: orphan.poll() is not None, timeout=10), "orphan survived"
        assert not pl.port_in_use(port)
    finally:
        if orphan.poll() is None:
            pl.kill_tree(orphan)


@pytest.mark.slow
def test_start_reclaims_a_port_held_by_our_orphan(hub, meta):
    """"Start fresh" has to work from the UI, without a terminal."""
    port = meta["port"]
    orphan = subprocess.Popen([sys.executable, str(ROOT / meta["entry"])],
                              cwd=str(ROOT), stdout=subprocess.DEVNULL,
                              stderr=subprocess.STDOUT, **pl.popen_kwargs())
    try:
        assert _wait(lambda: pl.port_in_use(port))
        old = orphan.pid
        ok, msg = hub._start_app(APP_ID)
        assert ok, msg
        assert "reclaim" in msg.lower(), msg
        assert _wait(lambda: orphan.poll() is not None), "the old process survived"
        new = pl.read_children(hub._CHILDREN_FILE)[APP_ID]["pid"]
        assert new != old, "a fresh process was not started"
        assert _wait(lambda: pl.port_in_use(port)), "the fresh app never bound"
    finally:
        if orphan.poll() is None:
            pl.kill_tree(orphan)
        hub._stop_app(APP_ID)


@pytest.mark.slow
def test_a_foreign_process_on_the_port_is_never_killed(hub, meta):
    """A database or another user's server on one of our ports must be reported,
    not terminated."""
    port = meta["port"]
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(64)     # a small backlog can be exhausted by probing
    try:
        ok, msg = hub._start_app(APP_ID)
        assert ok is False
        assert "not part of this platform" in msg or "could not be identified" in msg, msg
        # still bound: we did not touch it
        assert pl.port_in_use(port)
    finally:
        srv.close()


@pytest.mark.slow
def test_there_is_no_restart_endpoint(hub, meta):
    """The Restart button was removed at the operator's request — the card keeps
    to Start / Stop / Open. The endpoint outlived it and was unreachable from
    any UI, so it went too. Stop-then-Start is the supported sequence."""
    c = hub.app.test_client()
    assert c.post(f"/api/restart/{APP_ID}").status_code == 404


@pytest.mark.slow
def test_stop_all_leaves_nothing_running(hub, meta):
    c = hub.app.test_client()
    c.post(f"/api/start/{APP_ID}")
    assert _wait(lambda: pl.port_in_use(meta["port"]))
    j = c.post("/api/stop_all").get_json()
    assert j["ok"], j
    assert not j["stuck"]
    assert not pl.port_in_use(meta["port"])
    assert pl.read_children(hub._CHILDREN_FILE) == {}


@pytest.mark.slow
def test_a_new_hub_reaps_the_children_of_a_SIGKILLED_one(tmp_path, meta):
    """The scenario the child registry exists for. A hub that is SIGKILLed cannot
    run atexit; without a record on disk its apps run on invisibly forever."""
    runner = tmp_path / "run_hub.py"
    runner.write_text(
        "import os,sys\n"
        f"os.chdir({str(ROOT)!r}); sys.path.insert(0,{str(ROOT)!r})\n"
        "import importlib.util as u\n"
        "spec=u.spec_from_file_location('hubm','hub/app.py')\n"
        "h=u.module_from_spec(spec); sys.modules['hubm']=h; spec.loader.exec_module(h)\n"
        "h._reap_previous_run()\n"
        f"print(h._start_app({APP_ID!r}), flush=True)\n"
        "import time\n"
        "while True: time.sleep(0.2)\n")

    port = meta["port"]
    ch = ROOT / "logs" / "hub_children.json"
    first = subprocess.Popen([sys.executable, str(runner)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                             **pl.popen_kwargs())
    orphan_pid = None
    try:
        assert _wait(lambda: pl.port_in_use(port)), "the first hub never started the app"
        orphan_pid = pl.read_children(ch)[APP_ID]["pid"]

        os.kill(first.pid, signal.SIGKILL)          # atexit cannot run
        first.wait(timeout=10)
        assert _wait(lambda: pl.port_in_use(port), timeout=3), \
            "the app should still be running — that is the orphan"
        assert pl.read_children(ch)[APP_ID]["pid"] == orphan_pid, \
            "the registry must survive a SIGKILL"

        second = subprocess.Popen([sys.executable, str(runner)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                  **pl.popen_kwargs())
        try:
            assert _wait(lambda: pl.read_children(ch).get(APP_ID, {}).get("pid")
                         not in (None, orphan_pid), timeout=30), \
                "the new hub did not replace the orphan"
            assert not psutil.pid_exists(orphan_pid) or \
                not pl.is_our_app(psutil.Process(orphan_pid), meta["entry"], ROOT), \
                "the orphaned app is still running"
            assert _wait(lambda: pl.port_in_use(port), timeout=30), \
                "the freshly started app is not listening"
        finally:
            os.kill(second.pid, signal.SIGTERM)
            second.wait(timeout=20)
    finally:
        if first.poll() is None:
            pl.kill_tree(first)
        if orphan_pid and psutil.pid_exists(orphan_pid):
            try:
                pl.kill_tree(psutil.Process(orphan_pid))
            except Exception:
                pass
        pl.write_children(ch, {})

    assert _wait(lambda: not pl.port_in_use(port), timeout=10), \
        "closing the hub left the app running"


@pytest.mark.slow
def test_closing_the_hub_closes_its_apps(tmp_path, meta):
    """SIGTERM/Ctrl-C must take the children with it. `sys.exit(0)` in a signal
    handler relied on atexit, which does not run reliably while Flask's request
    threads are alive."""
    runner = tmp_path / "run_hub2.py"
    runner.write_text(
        "import os,sys\n"
        f"os.chdir({str(ROOT)!r}); sys.path.insert(0,{str(ROOT)!r})\n"
        "import importlib.util as u\n"
        "spec=u.spec_from_file_location('hubm','hub/app.py')\n"
        "h=u.module_from_spec(spec); sys.modules['hubm']=h; spec.loader.exec_module(h)\n"
        f"h._start_app({APP_ID!r})\n"
        "import time\n"
        "while True: time.sleep(0.2)\n")
    port = meta["port"]
    proc = subprocess.Popen([sys.executable, str(runner)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            **pl.popen_kwargs())
    try:
        assert _wait(lambda: pl.port_in_use(port)), "the app never started"
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=25)
        assert _wait(lambda: not pl.port_in_use(port), timeout=15), \
            "the app outlived the hub"
    finally:
        if proc.poll() is None:
            pl.kill_tree(proc)
        pl.write_children(ROOT / "logs" / "hub_children.json", {})


# ── the macOS AirPlay regression ─────────────────────────────────────────────
# A port check based on the process table refused to start the hub because
# `ControlCenter` (AirPlay Receiver) listens on port 5000 — even though Flask had
# been binding 127.0.0.1:5000 alongside it perfectly happily for months. The rule
# that came out of it: ask the kernel whether WE can bind, never infer from who
# else is listening.
def test_availability_is_decided_by_a_real_bind_not_by_the_process_table():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert pl.can_bind(port) and not pl.port_in_use(port)

    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", port))
    held.listen(8)
    try:
        assert not pl.can_bind(port), "a live listener must block the bind"
        assert pl.port_in_use(port)
    finally:
        held.close()
    assert pl.wait_port_free(port, timeout=3.0)


def test_a_listener_on_another_interface_does_not_block_us():
    """The AirPlay shape: something is listening on this port number, but not in a
    way that stops us binding loopback."""
    port = None
    other = None
    for cand in ("127.0.0.2", "127.0.1.1"):
        try:
            s = socket.socket()
            s.bind((cand, 0))
            port = s.getsockname()[1]
            s.close()
            other = socket.socket()
            other.bind((cand, port))
            other.listen(8)
            break
        except OSError:
            other = None
    if other is None:
        pytest.skip("no secondary loopback address available on this machine")
    try:
        assert pl.can_bind(port), "a listener on another address must not block us"
        assert not pl.port_in_use(port)
        # and it must not appear as a holder we would try to kill
        assert pl.listeners(port) == [], \
            "a non-loopback listener was reported as holding the port"
    finally:
        other.close()


def test_can_bind_matches_werkzeug_on_a_lingering_connection():
    """After stopping an app, a connection can sit in TIME_WAIT. werkzeug sets
    SO_REUSEADDR and binds fine, so our probe must too — otherwise a restart is
    refused for no reason."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(8)
    cli = socket.socket()
    cli.connect(("127.0.0.1", port))
    conn, _ = srv.accept()
    cli.close(); conn.close(); srv.close()          # leaves TIME_WAIT behind
    assert pl.can_bind(port), "a TIME_WAIT connection must not look like a busy port"


def test_a_zombie_counts_as_gone():
    """`psutil.wait_procs` reports a zombie as alive until its real parent reaps
    it. An orphan we kill is not our child, so waiting on it burned the whole
    grace period — a hub restart took 8 s instead of 0.3 s."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    # deliberately do NOT wait()/poll() — reaping it would remove the zombie
    deadline = time.time() + 5
    proc = psutil.Process(p.pid)
    while time.time() < deadline and proc.status() != psutil.STATUS_ZOMBIE:
        time.sleep(0.05)
    if proc.status() != psutil.STATUS_ZOMBIE:
        p.wait()
        pytest.skip("could not produce a zombie on this platform")
    try:
        assert pl._gone(proc) is True
        assert pl.kill_tree(proc) == "already-gone"
        assert pl._wait_gone([proc], 0.2) is True
    finally:
        p.wait()


def test_the_hub_port_is_configurable():
    """The escape hatch quoted in the error message has to exist."""
    src = (ROOT / "hub" / "app.py").read_text()
    assert 'os.environ.get("SWAXS_HUB_PORT"' in src
    assert "app.run(debug=False, port=_HUB_PORT" in src
    # and the failure path must name it
    assert "SWAXS_HUB_PORT=5100" in src
    if sys.platform == "darwin" or True:
        assert "AirPlay" in src, "the macOS cause is not explained anywhere"


# ── the card keeps to three controls ─────────────────────────────────────────
def test_each_app_card_offers_exactly_start_stop_open():
    """No Restart button. Stop then Start is the whole workflow — Stop waits for
    the port to be released and Start reclaims it if anything is left holding it,
    so the two in sequence already give a clean restart."""
    html = (ROOT / "hub" / "templates" / "index.html").read_text()
    footer = html[html.index('<div class="card-footer">'):]
    footer = footer[:footer.index("</div>")]
    assert "startApp(" in footer and "stopApp(" in footer and "btn-open" in footer
    assert "restartApp(" not in footer, "the card grew a Restart button again"
    assert footer.count("<button") == 2, "expected exactly two buttons plus the Open link"
    assert "Restart" not in html, "a Restart control is still referenced somewhere"


@pytest.mark.slow
def test_stop_then_start_gives_a_clean_process(hub, meta):
    """The sequence the two buttons perform, without the endpoint that used to
    wrap it. This is what has to work now that Restart is gone."""
    port = meta["port"]
    c = hub.app.test_client()
    assert c.post(f"/api/start/{APP_ID}").get_json()["ok"]
    assert _wait(lambda: pl.port_in_use(port))
    first = pl.read_children(hub._CHILDREN_FILE)[APP_ID]["pid"]

    assert c.post(f"/api/stop/{APP_ID}").get_json()["ok"]
    assert not pl.port_in_use(port), "Stop must release the port before Start runs"

    assert c.post(f"/api/start/{APP_ID}").get_json()["ok"]
    assert _wait(lambda: pl.port_in_use(port)), "Start after Stop did not bind"
    second = pl.read_children(hub._CHILDREN_FILE)[APP_ID]["pid"]
    assert second != first, "Start reused the old process"
