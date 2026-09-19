"""
tests/test_watchdog_footprint.py

Auto Watch must not cost the apps it watches.

It has form here. _compute_metrics() re-reads the manifest, stats every
manifest entry and its inputs, and globs + stats every .dat in both Reduction
folders — and it used to run once per SSE tick PER CONNECTED BROWSER TAB. That
made the monitoring app the heaviest process on the machine and starved the
pipeline it was monitoring. It was moved behind a 3 s shared cache.

Two more of the same shape were left, and this file pins both fixes:

  * the 24 h throughput histogram is bucketed by HOUR, but the disk walk
    feeding it ran on the 3 s metrics tick — twenty full directory scans a
    minute, against the directory reduction is actively writing into, to
    redraw a chart that cannot change more than once an hour;

  * _stall_check_loop called probe_all() directly instead of reading the probe
    cache built for exactly that purpose, adding six blocking HTTP calls every
    five minutes — one of which takes the reactor controller's lock — and
    diagnosing from a different snapshot than the dashboard was showing.

Also asserts the read-only property: Auto Watch may GET from the other apps
and POST only to the Slack webhook. A monitoring tool that can change the rig
is a monitoring tool that can break the rig.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_APP = (_ROOT / "watchdog" / "app.py").read_text()
_PROBES = (_ROOT / "src" / "watchdog" / "probes.py").read_text()


# ── the disk walk is not on the 3 s tick ────────────────────────────────────
def test_the_throughput_scan_is_cached_far_longer_than_the_metrics_tick():
    import importlib.util as u
    import os
    os.environ.setdefault("SWAXS_NO_WATCH", "1")
    os.environ.setdefault("SWAXS_NO_BUS", "1")
    spec = u.spec_from_file_location("wd_fp", _ROOT / "watchdog" / "app.py")
    mod = u.module_from_spec(spec)
    sys.modules["wd_fp"] = mod
    spec.loader.exec_module(mod)

    assert mod._THROUGHPUT_TTL_S >= 10 * mod._METRICS_TTL_S, (
        "the disk walk is running at (or near) the metrics tick rate again")


def test_the_scan_really_is_skipped_while_the_cache_is_warm(monkeypatch):
    import importlib.util as u
    spec = u.spec_from_file_location("wd_fp2", _ROOT / "watchdog" / "app.py")
    mod = u.module_from_spec(spec)
    sys.modules["wd_fp2"] = mod
    spec.loader.exec_module(mod)

    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return [{"hour": "00:00", "count": 0}]

    monkeypatch.setattr(mod, "_throughput_scan", counted)
    mod._throughput_cache["ts"], mod._throughput_cache["data"] = 0.0, None

    for _ in range(20):                      # a full minute of metrics ticks
        mod._throughput_last_24h()
    assert calls["n"] == 1, (
        f"the directory was walked {calls['n']}× for one hourly chart")


def test_the_cache_does_expire():
    """A stuck cache would freeze the chart, which is its own kind of lie."""
    import importlib.util as u
    spec = u.spec_from_file_location("wd_fp3", _ROOT / "watchdog" / "app.py")
    mod = u.module_from_spec(spec)
    sys.modules["wd_fp3"] = mod
    spec.loader.exec_module(mod)
    assert "_THROUGHPUT_TTL_S" in _APP
    src = _APP.split("def _throughput_last_24h")[1].split("def _throughput_scan")[0]
    assert "_THROUGHPUT_TTL_S" in src and "_throughput_scan()" in src


# ── nothing probes the live apps outside the refresh thread ─────────────────
def test_only_the_refresh_thread_calls_probe_all():
    """Every other caller must read _probe_all_cached(). A direct call is six
    blocking HTTP round trips against running apps, one of them taking the
    reactor controller's lock."""
    # Parsed, not grepped: the docstring on _probe_all_cached explains what
    # probe_all costs, and a text scan reported that prose as a call site.
    import ast
    tree = ast.parse(_APP)
    direct = [n.lineno for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name) and n.func.id == "probe_all"]
    assert len(direct) == 1, (
        f"probe_all() is called from {len(direct)} places (lines {direct}); "
        f"only the refresh loop may call it — everything else reads the cache")

    loop = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_probe_refresh_loop")
    assert loop.lineno < direct[0] <= (loop.end_lineno or direct[0]), \
        "the one call to probe_all() is not the refresh loop's"


def test_the_probes_have_short_timeouts():
    """A hung app must not hold the watchdog's threads open indefinitely."""
    timeouts = [float(m) for m in re.findall(r"timeout_s:\s*float\s*=\s*([\d.]+)",
                                             _PROBES)]
    assert timeouts, "probe timeouts are no longer declared"
    assert all(t <= 5.0 for t in timeouts), f"probe timeouts too long: {timeouts}"


# ── read-only with respect to every other app ───────────────────────────────
def test_auto_watch_never_posts_to_another_app():
    """It may GET status from the pipeline apps and POST only to the Slack
    webhook. Anything else means the monitor can change what it monitors."""
    posts = re.findall(r"""Request\(\s*([^\n]*?)\s*,?\s*.*?method\s*=\s*["']POST["']""",
                       _PROBES, re.S)
    assert not posts, f"probes.py issues a POST: {posts}"
    for verb in ("data=", "json="):
        assert verb not in _PROBES, (
            f"probes.py sends a body ({verb}) — probes must be reads")


def test_the_only_outbound_post_is_the_webhook():
    transport = (_ROOT / "src" / "watchdog" / "transport.py").read_text()
    urls = re.findall(r"https?://[^\s\"')]+", transport)
    bad = [u for u in urls if "slack.com" not in u and "example" not in u]
    assert not bad, f"transport.py talks to something other than Slack: {bad}"


def test_the_stall_loop_does_not_run_more_often_than_every_few_minutes():
    body = _APP.split("def _stall_check_loop")[1].split("\ndef ")[0]
    sleeps = [float(s) for s in re.findall(r"time\.sleep\(([\d.]+)\)", body)]
    assert sleeps and min(sleeps) >= 60, (
        f"the stall check loop woke up every {min(sleeps) if sleeps else '?'}s")
