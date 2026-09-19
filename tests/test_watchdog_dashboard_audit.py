"""
tests/test_watchdog_dashboard_audit.py

An audit of the Auto Watch dashboard as a whole: does it say true things, in
the right order, and does it cost the apps it watches anything?

What the audit turned up, and what each test here now holds in place:

  * `app_health` — a per-app up/down bool computed on every 3 s metrics tick
    and read by nothing. The health strip renders `health`, a richer row built
    by _health_row() from the same probes. Two sources for "is this app alive"
    is how they end up disagreeing, so the dead one was deleted rather than
    wired up.

  * `quality` — good/bad counts computed every tick and also never displayed,
    except this one was worth showing. The Quality Gate sits between subtract
    and analyse and was the single pipeline stage absent from the dashboard,
    so a gate quietly flagging every profile looked identical to a gate that
    was not running. It is now a funnel bar, present only once the gate has
    graded something (it is optional; a permanent zero bar teaches people to
    ignore the chart).

  * the stage order is defined TWICE — _STAGE_EVENTS/_STAGE_ORDER in
    watchdog/app.py and stage_markers in src/watchdog/expectations.py. They
    agree today. Nothing made them.

  * stopping Auto Watch said nothing. The hub stops apps with SIGTERM, which
    skips atexit, so the operator's last Slack message would be an ordinary
    one from minutes earlier — and the thing that had stopped was the only
    thing that would have told them.
"""
from __future__ import annotations

import importlib.util as u
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")

_TPL = (_ROOT / "watchdog" / "templates" / "index.html").read_text()
_APP_SRC = (_ROOT / "watchdog" / "app.py").read_text()


@pytest.fixture(scope="module")
def wd():
    spec = u.spec_from_file_location("wd_audit", _ROOT / "watchdog" / "app.py")
    mod = u.module_from_spec(spec)
    sys.modules["wd_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── the stages are in the real pipeline order, in both definitions ──────────
def test_the_two_stage_maps_agree(wd):
    """A stage added to one and not the other means the banner and the stall
    detector disagree about what the loop is doing."""
    from src.watchdog import expectations
    src = (_ROOT / "src" / "watchdog" / "expectations.py").read_text()
    for etype, stage in wd._STAGE_EVENTS.items():
        assert f'"{etype}": "{stage}"' in src, (
            f"{etype}→{stage} is in watchdog/app.py but not in expectations.py")
    assert expectations is not None


def test_the_stage_order_is_the_real_pipeline_order(wd):
    assert wd._STAGE_ORDER == ["collect", "reduce", "average", "subtract", "fit"]


def test_the_loop_ring_renders_the_same_stages_in_the_same_order(wd):
    """The ring is hand-written HTML; the order it shows is the claim the
    dashboard makes about the loop."""
    order = []
    for stage in wd._STAGE_ORDER:
        marker = f'data-stage="{stage}"'
        assert marker in _TPL, f"the ring has no node for the {stage!r} stage"
        order.append(_TPL.index(marker))
    assert order == sorted(order), (
        "the ring's nodes are not in _STAGE_ORDER order — the dashboard is "
        "drawing the pipeline out of sequence")


def test_the_funnel_is_in_pipeline_order_and_includes_the_quality_gate():
    block = _TPL.split("Pipeline funnel: how many files")[1][:2200]
    for earlier, later in (("'Reduced'", "'Averaged'"),
                           ("'Averaged'", "'Subtracted'"),
                           ("'Subtracted'", "'Quality-passed'"),
                           ("'Quality-passed'", "'Analysed'")):
        assert block.index(earlier) < block.index(later), \
            f"{later} is drawn before {earlier}"


def test_the_quality_bar_only_appears_once_the_gate_has_graded_something():
    block = _TPL.split("Pipeline funnel: how many files")[1][:2200]
    assert "qGood + qBad > 0" in block, (
        "the Quality bar is unconditional — every run that never enables the "
        "optional gate would show a permanent zero bar")


# ── nothing is computed for nobody ──────────────────────────────────────────
def test_the_dead_app_health_payload_is_gone(wd):
    assert "app_health" not in wd._compute_metrics(), \
        "app_health is computed again and still read by nothing"


def test_every_metrics_key_is_actually_consumed_by_the_page(wd):
    """The cheapest performance win available to a monitoring app is to stop
    computing what it does not show — this ran 20x a minute."""
    keys = set(wd._compute_metrics().keys())
    unused = sorted(k for k in keys if f"_metrics.{k}" not in _TPL)
    assert not unused, (
        f"_compute_metrics returns {unused}, which the dashboard never reads. "
        f"Either show it or stop computing it every tick.")


# ── the shutdown notice ─────────────────────────────────────────────────────
def test_stopping_sends_a_final_slack_message(wd, monkeypatch):
    sent = []

    class _T:
        webhook_url = "https://hooks.slack.com/x"
        def send(self, title, text, level):
            sent.append((title, text, level))
        def close(self, timeout=2.0):
            pass

    monkeypatch.setattr(wd, "_transport", _T())
    monkeypatch.setattr(wd, "_farewell_sent", [False])
    monkeypatch.setattr(wd, "_NO_WATCH", False)
    wd.shutdown()

    assert len(sent) == 1, "stopping Auto Watch sent no farewell"
    title, text, level = sent[0]
    assert "stopped" in title.lower()
    assert "no further alerts" in text.lower(), \
        "the message must say what stopping MEANS, not just that it happened"


def test_the_farewell_is_a_fault_so_the_throttle_cannot_eat_it(wd, monkeypatch):
    """close() joins the worker briefly; an info message can sit behind the
    3 s throttle for longer than that and never leave the process."""
    _t, _x, level = wd._farewell_message()
    assert level == "fault"


def test_the_farewell_reports_an_unwatched_reactor(wd, monkeypatch):
    """The point of the message is that nothing is watching from now on. If a
    run is live when the watch stops, that is the part that matters."""
    monkeypatch.setitem(wd._metrics_cache, "data", {
        "loop": {"current_stage": "average", "recipe_id": "Run21_r016"},
        "reactor": {"state": "running"}})
    _t, text, _l = wd._farewell_message()
    assert "Run21_r016" in text
    assert "UNWATCHED" in text and "running" in text


def test_it_is_sent_once_however_shutdown_is_reached(wd, monkeypatch):
    sent = []

    class _T:
        webhook_url = "x"
        def send(self, *a):
            sent.append(a)
        def close(self, timeout=2.0):
            pass

    monkeypatch.setattr(wd, "_transport", _T())
    monkeypatch.setattr(wd, "_farewell_sent", [False])
    monkeypatch.setattr(wd, "_NO_WATCH", False)
    wd.shutdown()
    wd.shutdown()          # atexit after the signal handler already ran
    assert len(sent) == 1


def test_a_signal_handler_is_installed_because_atexit_is_not_enough():
    """The hub stops apps with terminate() (proc_lifecycle.kill_tree), and
    Python's default SIGTERM disposition does NOT run atexit handlers."""
    assert "signal.signal" in _APP_SRC.replace("_signal.signal", "signal.signal")
    assert "SIGTERM" in _APP_SRC


def test_the_drain_fits_inside_the_hubs_kill_grace(wd):
    """kill_tree gives 5 s before SIGKILL. A drain longer than that is killed
    mid-flight and the farewell is lost — the exact failure it exists to fix."""
    import re
    body = _APP_SRC.split("def shutdown(")[1].split("\ndef ")[0]
    timeouts = [float(t) for t in re.findall(r"close\(timeout=([\d.]+)\)", body)]
    assert timeouts, "shutdown() no longer bounds the drain"
    assert max(timeouts) < 5.0, f"drain of {max(timeouts)}s exceeds the 5s grace"


def test_the_farewell_ignores_quiet_hours_but_not_the_master_switch(wd, monkeypatch):
    """Quiet hours suppress routine chatter. "Your monitoring is gone" is the
    one message whose absence cannot be noticed — silence is what it warns
    about. The master switch still means off."""
    sent = []

    class _T:
        webhook_url = "x"
        def send(self, *a):
            sent.append(a)
        def close(self, timeout=2.0):
            pass

    monkeypatch.setattr(wd, "_transport", _T())
    monkeypatch.setattr(wd, "_settings",
                        {"slack_enabled": True,
                         "quiet_hours": ((0, 0), (23, 59)),
                         "categories": {c: False for c in
                                        ("safety", "stalls", "results",
                                         "progress", "campaign")}})
    monkeypatch.setattr(wd, "_farewell_sent", [False])
    monkeypatch.setattr(wd, "_NO_WATCH", False)
    wd.shutdown()
    assert len(sent) == 1, "quiet hours or a category toggle swallowed it"

    sent.clear()
    monkeypatch.setattr(wd, "_settings", {"slack_enabled": False})
    monkeypatch.setattr(wd, "_farewell_sent", [False])
    monkeypatch.setattr(wd, "_NO_WATCH", False)
    wd.shutdown()
    assert not sent, "the master switch must still mean off"
