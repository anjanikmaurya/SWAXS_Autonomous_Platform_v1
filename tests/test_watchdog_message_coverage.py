"""
tests/test_watchdog_message_coverage.py

Two complaints, one audit.

1. "Stopped by: SAXS measurement complete — /Users/…/Run21_r016_…Average.dat /
   Ran: 50s". In an autonomous campaign almost every run ends because the
   measurement finished, so "Stopped by" read as though something had
   interrupted the rig — and the absolute path pushed the reason off the edge
   of a phone screen.

2. "it is not sending all the messages". It was not. event_to_message() had
   formatters for six event types; the bus carries more than that, and two of
   the unhandled ones are the pipeline telling you a condition has been lost
   for good:

       average.skipped   a full batch consumed, no average written — terminal
       file.skipped      reduction gave up on a frame; the gate cannot fill
       reactor.vent      published since the controller was written

   All three returned None and nothing was sent. Meanwhile "campaign" sat in
   CATEGORIES and on the Alerts page with no event mapped to it, so the toggle
   did nothing either way (audit item W10).

This file pins the wording and, more importantly, asserts that every event
type the platform actually publishes has been CONSIDERED — so the next new
event cannot be silently unroutable.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.watchdog.messages import event_to_message          # noqa: E402
from src.watchdog.policy import (CATEGORIES,                # noqa: E402
                                 category_for_event, should_send)


# ── 1. the run-complete wording ─────────────────────────────────────────────
_DONE = {"recipe_id": "Run21_r016", "duration_s": 50,
         "reason": ("SAXS measurement complete — /Users/akmaurya/Desktop/"
                    "Data_local/Auto_Run/1D/SAXS/Averaged/"
                    "Run21_r016_sample_batch001_10files_Average.dat")}


def test_a_normal_finish_is_not_described_as_being_stopped():
    title, text, _ = event_to_message("reactor.run_complete", _DONE)
    assert "Stopped" not in text and "Stopped" not in title
    assert text.startswith("Ended: SAXS measurement complete")


def test_the_file_is_shown_by_name_not_by_absolute_path():
    """The full path is four lines on a phone and tells the operator nothing
    they cannot get from the recipe id."""
    _t, text, _ = event_to_message("reactor.run_complete", _DONE)
    assert "Run21_r016_sample_batch001_10files_Average.dat" in text
    assert "/Users/" not in text


@pytest.mark.parametrize("reason", ["aborted", "arm timeout",
                                    "volume limit exceeded"])
def test_an_early_stop_is_still_called_out(reason):
    """Removing "Stopped by" must not cost the distinction that matters: this
    run did NOT finish, and the operator should see that in the title."""
    title, text, _ = event_to_message(
        "reactor.run_complete",
        {"recipe_id": "Run21_r017", "duration_s": 900, "reason": reason})
    assert "Stopped Early" in title
    assert reason in text


@pytest.mark.parametrize("reason", ["SAXS measurement complete",
                                    "duration elapsed",
                                    "next condition available"])
def test_every_normal_end_reason_reads_as_normal(reason):
    title, _text, _ = event_to_message(
        "reactor.run_complete", {"recipe_id": "r1", "reason": reason})
    assert title == "Run Complete — r1"


def test_a_missing_reason_does_not_produce_a_scary_message():
    title, text, _ = event_to_message("reactor.run_complete", {"recipe_id": "r1"})
    assert "Stopped Early" not in title and "?" not in text


# ── 2. the events that sent nothing ─────────────────────────────────────────
def test_a_dropped_batch_is_reported_as_a_fault():
    """Terminal: the frames are consumed, so this condition can never yield a
    subtracted profile. Run20 lost r006 this way in silence."""
    title, text, level = event_to_message("average.skipped", {
        "keyword": "Run20_r006_bkg", "detector": "saxs", "batch": 1,
        "n_files": 10, "reason": "all 10 frames are unusable (10× I ≤ 0)"})
    assert level == "fault"
    assert "Run20_r006_bkg" in title
    assert "I ≤ 0" in text and "will not be retried" in text


def test_a_permanently_skipped_frame_is_reported_but_not_as_a_fault():
    """Faults bypass the transport throttle. One bad CSV can make reduction
    give up frame after frame, and a burst of un-throttled faults would get
    rate-limited by Slack, taking genuinely urgent messages with it."""
    _t, text, level = event_to_message("file.skipped", {
        "file_path": "/x/y/Run20_r006_bkg_scan1_0003.raw",
        "keyword": "Run20_r006_bkg", "detector": "saxs", "n_failures": 3})
    assert level == "info"
    assert "scan1_0003.raw" in text and "/x/y/" not in text


def test_venting_after_an_estop_is_a_fault():
    _t, text, level = event_to_message("reactor.vent", {"estop_latched": True})
    assert level == "fault" and "E-stop" in text


def test_venting_normally_is_not():
    _t, _x, level = event_to_message("reactor.vent", {"estop_latched": False})
    assert level == "info"


# ── the campaign category is no longer dead ─────────────────────────────────
def test_every_category_has_at_least_one_event():
    """"campaign" was offered on the Alerts page with nothing mapped to it, so
    the toggle changed nothing whichever way it was set (W10). A category the
    operator can turn off must be able to turn something off."""
    from src.watchdog.policy import _EVENT_CATEGORY
    used = set(_EVENT_CATEGORY.values())
    # "stalls" is applied at the diagnose_stall call site, not via an event.
    used.add("stalls")
    missing = [c for c in CATEGORIES if c not in used]
    assert not missing, f"categories nothing can ever populate: {missing}"


def test_turning_campaign_off_silences_a_dropped_batch():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    cfg = {"slack_enabled": True, "categories": {"campaign": False}}
    send, _ = should_send("fault", now, cfg, {},
                          category_for_event("average.skipped"))
    assert send is False


def test_turning_campaign_off_does_not_silence_safety():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    cfg = {"slack_enabled": True,
           "categories": {"campaign": False, "safety": False}}
    send, _ = should_send("fault", now, cfg, {},
                          category_for_event("reactor.estop"))
    assert send is True, "safety must only be silenceable by the master switch"


# ── nothing published may be silently unroutable ────────────────────────────
def _published_event_types() -> set[str]:
    """Every event type the platform publishes, from both call styles:
    publish("x.y", …) and the controller's self._event("x.y", …)."""
    found = set()
    pat = re.compile(r"""(?:publish|_event)\(\s*["']([a-z_]+\.[a-z_]+)["']""")
    for p in list((_ROOT / "src").rglob("*.py")) + list(_ROOT.glob("*/app.py")):
        if "__pycache__" in str(p):
            continue
        found.update(pat.findall(p.read_text(errors="ignore")))
    return found


#: Published, deliberately NOT notified — high-frequency progress events that
#: would mean a Slack message per frame. The dashboard shows these; Slack
#: should not. Listed explicitly so the decision is recorded rather than
#: implied by absence.
_NOT_NOTIFIED = {
    "file.reduced", "file.averaged", "file.subtracted", "file.stitched",
    "file.classified", "watch.new_raw", "analysis.complete", "ai.hint",
    "reactor.ready", "reactor.spec_collect",
    # Bus plumbing: every app emits this on (re)connect, so notifying on it
    # would page the operator whenever a socket blipped.
    "app.connected",
}


def test_every_published_event_is_either_notified_or_explicitly_not():
    published = _published_event_types()
    assert published, "the scanner found no events — it has stopped working"
    unrouted = sorted(t for t in published
                      if t not in _NOT_NOTIFIED
                      and event_to_message(t, {}) is None)
    assert not unrouted, (
        f"these events are published but produce no Slack message and are not "
        f"in _NOT_NOTIFIED: {unrouted}. Add a formatter, or add them to the "
        f"list with a reason — silence should be a decision, not an oversight.")


def test_the_not_notified_list_has_no_stale_entries():
    """An entry for an event nobody publishes any more is a note about code
    that no longer exists, and it hides the next real gap."""
    published = _published_event_types()
    stale = sorted(t for t in _NOT_NOTIFIED if t not in published)
    assert not stale, f"_NOT_NOTIFIED lists unpublished events: {stale}"
