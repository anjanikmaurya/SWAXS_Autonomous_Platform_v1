"""
tests/test_watchdog_safety_path.py

The path a reactor E-stop takes from bus event to Slack, and the two places it
used to be lost silently (docs/audits/AUTO_WATCH_AUDIT.md W5, W6, W7, W8):

  W5  event_to_message wrapped every formatter in `except: return None`, so a
      reactor.estop with an unexpected payload produced NO message at all.
  W6  one queue with a 3 s throttle = 20 msgs/min, so a full 200-deep queue is
      ten minutes long; an E-stop behind a burst of progress messages arrived
      ten minutes late or was dropped on queue.Full.
  W7  delivery failures were logged and swallowed, while the app recorded the
      message as sent — a revoked webhook looked like a healthy send history.
  W8  close() set _alive False before queueing the sentinel, so the worker
      could exit without draining and lose the last message before shutdown.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.watchdog.messages import event_to_message, NEVER_DROP     # noqa: E402
from src.watchdog import transport as tr                           # noqa: E402


# ── W5: a safety event is never dropped by a formatting failure ─────────────
@pytest.mark.parametrize("etype", NEVER_DROP)
def test_a_safety_event_with_a_broken_payload_still_produces_a_fault(etype):
    """`failed_to_idle` arriving as a string makes format_reactor_estop's
    ", ".join(...) raise. That must not silence the E-stop."""
    out = event_to_message(etype, {"recipe_id": "Run9_r003",
                                    "failed_to_idle": 1234})     # not a list
    assert out is not None, f"{etype} must never be dropped"
    title, text, level = out
    assert level == "fault"
    assert "Run9_r003" in text or "Run9_r003" in title, \
        "the raw payload must ride along so the operator can act on it"


@pytest.mark.parametrize("etype", NEVER_DROP)
def test_a_safety_event_with_a_non_dict_payload_still_produces_a_fault(etype):
    out = event_to_message(etype, None)
    assert out is not None and out[2] == "fault"


def test_a_well_formed_estop_is_unchanged():
    title, text, level = event_to_message(
        "reactor.estop", {"recipe_id": "Run9_r003", "failed_to_idle": ["P2"]})
    assert level == "fault"
    assert "EMERGENCY STOP" in title and "P2" in text
    assert "could not be formatted" not in title


def test_a_non_safety_event_with_a_broken_payload_is_still_dropped():
    """Only safety events degrade; a malformed progress event stays silent
    rather than sending noise."""
    assert event_to_message("reactor.run_start", {"recipe": "not-a-dict"}) is None


def test_an_unknown_event_type_is_still_none():
    assert event_to_message("file.reduced", {"file_path": "/x/a.dat"}) is None


# ── W6/W7/W8: the transport ─────────────────────────────────────────────────
def _queue_only(monkeypatch):
    """A transport whose worker was NEVER started, so queue contents are
    deterministic: built with no webhook in the env (which skips the worker),
    then given a URL so send() still enqueues."""
    monkeypatch.delenv(tr.ENV_WEBHOOK, raising=False)
    t = tr.WebhookTransport(min_interval_s=0.0)
    assert t._worker is None, "no worker should exist without a webhook URL"
    t.webhook_url = "https://hooks.example/T/B/X"
    return t


def test_a_fault_is_dequeued_before_queued_info_messages(monkeypatch):
    """The ordering guarantee, tested on the selector itself so there is no
    race with the worker: an E-stop must not wait behind progress."""
    t = _queue_only(monkeypatch)
    for i in range(25):
        t.send(f"progress {i}", "…", "info")
    t.send("EMERGENCY STOP", "all pumps idle", "fault")

    first = t._next()

    assert first["title"] == "EMERGENCY STOP", \
        f"the fault must jump the info queue, got {first['title']}"
    assert t._next()["title"] == "progress 0", "then info resumes in order"


def test_a_fault_is_not_throttled(monkeypatch):
    """Faults bypass min_interval_s — the throttle is for message volume, not
    for emergencies."""
    monkeypatch.setenv(tr.ENV_WEBHOOK, "https://hooks.example/T/B/X")
    sent: list = []
    monkeypatch.setattr(tr, "_post",
                        lambda url, payload, timeout=6.0: (sent.append(payload), {"ok": True})[1])
    t = tr.WebhookTransport(min_interval_s=30.0, timeout_s=1.0)
    try:
        started = time.time()
        t.send("SAFETY: temp", "stale reading", "fault")
        t.send("EMERGENCY STOP", "tripped", "fault")
        deadline = started + 3.0
        while time.time() < deadline and len(sent) < 2:
            time.sleep(0.02)
        assert len(sent) == 2, "two faults must not be 30 s apart"
        assert time.time() - started < 3.0
    finally:
        t.close(timeout=1.0)


def test_an_overflowing_info_queue_sheds_info_and_never_a_fault(monkeypatch):
    """Under pressure the info queue is what gets dropped; the fault queue is
    independent, so a fault still has a slot."""
    t = _queue_only(monkeypatch)
    for i in range(400):                  # overflow the 200-deep info queue
        t.send(f"progress {i}", "…", "info")
    assert t.n_dropped >= 190, f"info should have been shed, dropped={t.n_dropped}"

    t.send("EMERGENCY STOP", "tripped", "fault")

    assert t._next()["title"] == "EMERGENCY STOP", \
        "a fault must still be first in line after an info flood"


def test_an_overflowing_fault_queue_keeps_the_newest_faults(monkeypatch):
    t = _queue_only(monkeypatch)
    for i in range(205):
        t.send(f"fault {i}", "…", "fault")
    # 200 capacity, 205 sent → the 5 oldest evicted, newest retained.
    assert t.n_dropped == 5
    assert t._next()["title"] == "fault 5"


def test_delivery_failures_are_recorded_not_swallowed(monkeypatch):
    """A revoked webhook must be visible, not just logged."""
    monkeypatch.setenv(tr.ENV_WEBHOOK, "https://hooks.example/dead")
    monkeypatch.setattr(tr, "_post",
                        lambda url, payload, timeout=6.0: {"ok": False, "error": "404 no_service"})
    t = tr.WebhookTransport(min_interval_s=0.0, timeout_s=1.0)
    try:
        assert t.last_error == "" and t.n_failed == 0
        t.send("Recipe Applied", "…", "info")
        deadline = time.time() + 3.0
        while time.time() < deadline and t.n_failed == 0:
            time.sleep(0.02)
        assert t.n_failed == 1, "a rejected message must be counted"
        assert "no_service" in t.last_error, "and the reason kept for the UI"
        assert t.n_sent == 0
    finally:
        t.close(timeout=1.0)


def test_a_recovered_webhook_clears_the_error(monkeypatch):
    monkeypatch.setenv(tr.ENV_WEBHOOK, "https://hooks.example/flaky")
    state = {"fail": True}

    def flaky(url, payload, timeout=6.0):
        return {"ok": False, "error": "500"} if state["fail"] else {"ok": True}

    monkeypatch.setattr(tr, "_post", flaky)
    t = tr.WebhookTransport(min_interval_s=0.0, timeout_s=1.0)
    try:
        t.send("a", "…", "info")
        deadline = time.time() + 3.0
        while time.time() < deadline and t.n_failed == 0:
            time.sleep(0.02)
        state["fail"] = False
        t.send("b", "…", "info")
        deadline = time.time() + 3.0
        while time.time() < deadline and t.n_sent == 0:
            time.sleep(0.02)
        assert t.n_sent == 1 and t.last_error == ""
    finally:
        t.close(timeout=1.0)


def test_close_drains_what_is_already_queued(monkeypatch):
    """The last message before a shutdown is usually the reason for it."""
    monkeypatch.setenv(tr.ENV_WEBHOOK, "https://hooks.example/T/B/X")
    sent: list = []
    monkeypatch.setattr(tr, "_post",
                        lambda url, payload, timeout=6.0: (sent.append(payload), {"ok": True})[1])
    t = tr.WebhookTransport(min_interval_s=0.0, timeout_s=1.0)
    t.send("EMERGENCY STOP", "tripped", "fault")
    t.send("Run Complete", "…", "info")
    t.close(timeout=3.0)
    titles = [p["title"] for p in sent]
    assert "EMERGENCY STOP" in titles, f"close() dropped the fault: {titles}"


def test_no_webhook_configured_is_a_silent_no_op(monkeypatch):
    monkeypatch.delenv(tr.ENV_WEBHOOK, raising=False)
    t = tr.WebhookTransport(min_interval_s=0.0)
    t.send("EMERGENCY STOP", "tripped", "fault")   # must not raise
    assert t.n_sent == 0 and t.n_failed == 0
