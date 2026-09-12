"""
watchdog/app.py — Auto Watch (port 5110)
========================================
Platform liveness monitoring and the sole notification gateway to Slack.

Subscribes to the event bus, translates platform events into messages, applies
notification policy (quiet hours, snooze, throttle), and sends via webhook.

Run:  python watchdog/app.py          (from the activated venv)
Open: http://localhost:5110
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time as _time
import psutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import (Flask, jsonify, request, render_template, Response,
                   send_from_directory, redirect)

# Load .env if it exists (only SWAXS_SLACK_WEBHOOK_URL lives there; no dotenv dep needed)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.is_file():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _val = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _val.strip().strip('"').strip("'"))

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.watchdog.settings import load_settings, save_notify_settings, ValidationError  # noqa: E402
from src.watchdog.transport import WebhookTransport  # noqa: E402
from src.watchdog.policy import (  # noqa: E402
    should_send, set_snooze, clear_snooze, category_for_event, CATEGORIES,
)
from src.watchdog.messages import event_to_message  # noqa: E402
from src.watchdog.expectations import which_stage_is_overdue, format_stall_message  # noqa: E402
from src.watchdog.probes import probe_all  # noqa: E402
from src.watchdog.diagnose import diagnose_stall  # noqa: E402
from src.loop_naming import split_role, BKG_TAGS  # noqa: E402

app = Flask(__name__)

_project_root: str = os.environ.get("SWAXS_PROJECT", "")  # folder selected in the hub
_settings: dict = {}
_state: dict = {"snooze_until": None}
_transport: WebhookTransport | None = None
_sent_messages: list[dict] = []  # (timestamp, title, text) for the status page
_recent_events: list[dict] = []  # (type, timestamp) for stall detection

# The newest file.averaged per lane, kept outside the 100-event rolling window.
# A recipe cycle emits ~20 events (per-frame file.reduced dominates); a lane's
# single file.averaged event can be evicted from _recent_events within one
# more cycle, which made the subtract gate ("have_background"/"have_sample")
# and the reactor phase tracker forget a genuinely-completed average and
# report "still waiting" for something already on disk. This never evicts.
_last_averaged: dict = {"background": None, "sample": None}  # {recipe_id, timestamp} | None

# probe_all() does up to 6 blocking HTTP calls (2s timeout each). The SSE stream
# ticks every second, so we cache the result to avoid hammering every app — and
# so a slow/down app can never stall the dashboard stream for more than one refresh.
_PROBE_TTL_S = 5.0
_probe_cache: dict = {"ts": 0.0, "data": {}}

# One shared metrics snapshot, refreshed by a single background thread.
#
# _compute_metrics() re-reads and re-parses the whole manifest, stats every
# manifest file entry and its inputs, and globs + stats every .dat in both
# Reduction folders. It used to run once per SSE tick, PER CONNECTED CLIENT —
# 1 Hz x every open browser tab. On an overnight run that is tens of thousands
# of stat() calls a second, which made Auto Watch the heaviest process on the
# machine it exists to monitor and starved the apps it was watching. Now it is
# computed once every _METRICS_TTL_S regardless of how many dashboards are
# open, and every reader gets the same snapshot.
_METRICS_TTL_S = 3.0
_metrics_cache: dict = {"ts": 0.0, "data": None}


def _emit(msg: str, level: str = "info") -> None:
    """Log to stdout in the style of the hub."""
    prefix = {"ok": "✓", "warn": "⚠", "error": "✗"}.get(level, "ℹ")
    print(f"[Auto Watch] {prefix} {msg}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Event type → pipeline stage it marks the entry of.
_STAGE_EVENTS = {
    "reactor.run_start": "collect",
    "file.reduced": "reduce",
    "file.averaged": "average",
    "file.subtracted": "subtract",
    "fit.complete": "fit",
}
_STAGE_ORDER = ["collect", "reduce", "average", "subtract", "fit"]


def _current_stage() -> tuple[str, datetime | None]:
    """Return (stage_name, entered_at) for the most recent stage trigger."""
    # Snapshot the shared list: the bus thread may append/pop concurrently, and
    # iterating a reversed() view of a mutating list raises RuntimeError.
    events = list(_recent_events)
    if not events:
        return "idle", None
    for event in reversed(events):
        if event.get("type") in _STAGE_EVENTS:
            stage_name = _STAGE_EVENTS[event["type"]]
            try:
                ts = datetime.fromisoformat(event.get("timestamp", ""))
            except (ValueError, TypeError):
                ts = _now()
            return stage_name, ts
    return "idle", None


def _mtime(path) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except (OSError, TypeError, ValueError):
        return None


def _nanoparticle_analyses(manifest: dict) -> list:
    """Every nanoparticle fit record in the manifest, oldest first.

    `analyses` also holds the Data Analysis app's guinier/porod/peak/model
    records; only the analyzer's `nanoparticle` records carry a size, a PDI
    and a confidence, so everything that counts or plots FITS has to filter
    here rather than take len(analyses).
    """
    out = [e for e in (manifest.get("analyses", {}) or {}).values()
           if isinstance(e, dict) and e.get("type") == "nanoparticle"]
    out.sort(key=lambda e: str(e.get("updated_at") or ""))
    return out


#: How many of the most recent completed runs the per-run averages cover.
_CYCLE_RUNS_N = 12


def _run_cycle_times(manifest: dict) -> dict:
    """Average time each stage adds to ONE RUN of the loop, in seconds.

    Replaces the per-file elapsed-time metric, which was meaningless the
    moment anything stalled or was backfilled: it measured the wall-clock gap
    between one file's mtime and its input's, so a sample subtracted hours
    after it was averaged reported "subtract takes 5 h" — a fact about when
    the operator started the monitor, not about the pipeline.

    A RUN is one recipe_id: the reactor collects a background and a sample
    under the same id (src/loop_naming.py), the pipeline processes both, and
    the analyzer fits the result. Every boundary below is the completion of a
    stage for that one run, so the spans are the run's own timeline:

        reduce    last reduced frame   - first reduced frame  (the acquisition)
        average   last averaged file   - last reduced frame
        subtract  the subtracted file  - last averaged file
        fit       the analysis record  - the subtracted file

    Non-negative by construction — each boundary is downstream of the last —
    and a stall lands on ONE stage as a large span rather than poisoning the
    axis for all of them. Averaged over the most recent _CYCLE_RUNS_N runs so
    the numbers describe the campaign that is running, not the whole folder.

    Returns {stage: {avg_s, n}, ...} plus "_runs" (runs used) and
    "_total_s" (average end-to-end time for one run).
    """
    # ── group every manifest file by the run it belongs to ───────────────────
    runs: dict = {}

    def slot(rid: str) -> dict:
        return runs.setdefault(rid, {"reduced": [], "averaged": [],
                                      "subtracted": [], "fit": []})

    for entry in (manifest.get("files", {}) or {}).values():
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage")
        if stage not in ("reduced", "averaged", "subtracted"):
            continue
        kw = str(entry.get("keyword") or "")
        rid, role = split_role(kw)
        if not rid:
            rid, role = split_role(Path(str(entry.get("path") or "")).name)
        if not rid:
            continue
        # The SAMPLE lane defines the cycle: the background is collected during
        # the flush that precedes it and would date the run too early.
        if stage in ("reduced", "averaged") and role in BKG_TAGS:
            continue
        t = _mtime(entry.get("path"))
        if t is not None:
            slot(rid)[stage].append(t)

    for entry in _nanoparticle_analyses(manifest):
        rid, _role = split_role(Path(str(entry.get("file_path") or "")).name)
        if not rid:
            continue
        try:
            slot(rid)["fit"].append(
                datetime.fromisoformat(entry.get("updated_at", "")).timestamp())
        except (ValueError, TypeError):
            continue

    # ── one timeline per run, newest runs last ───────────────────────────────
    spans: dict = {"reduce": [], "average": [], "subtract": [], "fit": []}
    totals: list = []
    complete = []
    for rid, d in runs.items():
        if not d["reduced"]:
            continue
        complete.append((max(d["reduced"]), rid, d))
    complete.sort()

    for _t, _rid, d in complete[-_CYCLE_RUNS_N:]:
        red_first, red_last = min(d["reduced"]), max(d["reduced"])
        marks = [("reduce", red_first, red_last)]
        cursor = red_last
        if d["averaged"]:
            avg_last = max(d["averaged"])
            marks.append(("average", cursor, avg_last))
            cursor = max(cursor, avg_last)
        if d["subtracted"]:
            sub = max(d["subtracted"])
            marks.append(("subtract", cursor, sub))
            cursor = max(cursor, sub)
        if d["fit"]:
            marks.append(("fit", cursor, max(d["fit"])))

        run_total = 0.0
        for name, a, b in marks:
            dt = b - a
            if not (0 <= dt < 24 * 3600):     # clock skew / a run spanning a day
                continue
            spans[name].append(dt)
            run_total += dt
        if run_total > 0:
            totals.append(run_total)

    out: dict = {}
    for name, xs in spans.items():
        out[name] = ({"avg_s": round(sum(xs) / len(xs), 1), "n": len(xs)}
                     if xs else {"avg_s": None, "n": 0})
    out["_runs"] = len(totals)
    out["_total_s"] = round(sum(totals) / len(totals), 1) if totals else None
    return out


def _probe_age_s() -> float | None:
    """Seconds since the probe cache was last refreshed (None before the first)."""
    if not _probe_cache["ts"]:
        return None
    return round(_time.monotonic() - _probe_cache["ts"], 1)


def _probe_all_cached() -> dict:
    """Return the most recent probe snapshot, never blocking on the network.

    probe_all() does up to 6 serial HTTP calls; a down app costs the full 2 s
    timeout each (~8 s total when several are down). Doing that inline would
    stall the 1 Hz SSE tick — the exact "dashboard freezes then recovers"
    symptom. Instead a background thread refreshes the cache and request
    threads only ever read it. Empty dict until the first refresh completes.
    """
    return _probe_cache["data"] or {}


def _probe_refresh_loop() -> None:
    """Background thread: refresh the probe cache every _PROBE_TTL_S seconds."""
    while True:
        try:
            data = probe_all() or {}
            _probe_cache["data"] = data
            _probe_cache["ts"] = _time.monotonic()
        except Exception as exc:
            _emit(f"probe refresh error: {exc}", "warn")
        _time.sleep(_PROBE_TTL_S)


def _read_manifest() -> dict:
    """Safely read manifest.json from project root."""
    if not _project_root:
        return {}
    try:
        manifest_path = Path(_project_root) / "manifest.json"
        if not manifest_path.is_file():
            return {}
        return json.loads(manifest_path.read_text())
    except Exception as exc:
        _emit(f"manifest read error: {exc}", "warn")
        return {}


def _resource_metrics() -> dict:
    """Return CPU, memory, disk usage.

    Uses the non-blocking form of cpu_percent (measured against the previous
    call) so the 1 Hz SSE tick is never delayed by a 0.1 s sampling window.
    """
    try:
        return {
            "cpu": round(psutil.cpu_percent(interval=None), 1),
            "mem": round(psutil.virtual_memory().percent, 1),
            "disk": round(psutil.disk_usage("/").percent, 1),
        }
    except Exception:
        return {"cpu": 0, "mem": 0, "disk": 0}


# Prime the non-blocking cpu_percent counter so the first real read is meaningful.
try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass


def _throughput_last_24h() -> list[dict]:
    """Hourly count of reduced files over the real last 24 h, from files on
    disk (mtime) — not the in-memory event window, which is capped at 100
    entries and empties on restart (about five recipes, not a day). A count
    from disk is true regardless of when the watchdog process last started,
    and a quiet bucket after a burst reads as missing data, not zero.
    """
    now_dt = _now()
    now_ts = now_dt.timestamp()
    labels = [(now_dt - timedelta(hours=23 - i)).strftime("%H:00") for i in range(24)]
    buckets = [0] * 24

    if _project_root:
        root = Path(_project_root)
        for rel in ("1D/SAXS/Reduction/*.dat", "1D/WAXS/Reduction/*.dat"):
            for fp in root.glob(rel):
                try:
                    age_s = now_ts - fp.stat().st_mtime
                except OSError:
                    continue
                if 0 <= age_s < 24 * 3600:
                    buckets[23 - int(age_s // 3600)] += 1

    return [{"hour": labels[i], "count": buckets[i]} for i in range(24)]


# ── Cyclic loop view ──────────────────────────────────────────────────────────
# Reduce and average are each ONE app, not one per lane: the reactor collects
# background first (on the clean capillary, during the flush) and sample
# second (during synthesis), and the SAME reduce/average pipeline processes
# whichever lane is currently flowing through it. The background/sample split
# is shown on the REACTOR node (a two-step phase tracker), not by duplicating
# the reduce/average boxes. The two lanes only re-join at subtract, which
# needs BOTH averaged files, then fit+predict, which writes the next
# condition and hands control back to the reactor.
#
# Everything below only READS: bus events already received, the probe snapshot,
# and each app's own published numbers. Stall detection itself lives in
# src/watchdog/expectations.py and is called, not reimplemented.

#: Which apps belong to the autonomous loop, and the port each answers on.
LOOP_APPS = {
    "reactor": 5108,
    "reduction": 5102,
    "average": 5103,
    "background": 5104,
    "quality": 5105,
    "analyzer": 5107,
}

#: An event marks its node as "running" only while it is this fresh. Older than
#: that and the node is holding a result, not producing one.
_FRESH_S = 180.0

#: Only these payload fields are retained per event — enough to say which lane
#: and which recipe a file belongs to, without keeping whole recipes in memory.
_KEEP_DATA = ("file_path", "keyword", "recipe_id", "detector", "n_files",
              "size", "confidence")


def _event_lane_rid(event: dict) -> tuple[str | None, str | None]:
    """(lane, recipe_id) for a file event — ("background"|"sample", rid).

    The role tag is in the filename by convention (src/loop_naming.py), and the
    averaged/subtracted events carry the condition keyword directly. Returns
    (None, None) for anything that is not a loop file.
    """
    data = event.get("data") or {}
    for candidate in (data.get("keyword"), Path(str(data.get("file_path") or "")).name):
        rid, role = split_role(str(candidate or ""))
        if role:
            return ("background" if role in BKG_TAGS else "sample", rid)
    rid = str(data.get("recipe_id") or "") or None
    return (None, rid)


def _event_age_s(event: dict, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(event.get("timestamp", ""))).total_seconds()
    except (ValueError, TypeError):
        return None


def _gate_for(avg_status: dict, rid: str | None, lane: str) -> dict | None:
    """The average app's own gate entry for one lane of one recipe, or None.

    Matched on the group keyword the average app itself uses
    ({recipe_id}_{role}); we never recount frames on disk, so the number shown
    is the number the app is actually gating on.
    """
    gate = avg_status.get("gate") or {}
    expected = gate.get("expected")
    for group in gate.get("waiting") or []:
        g_lane, g_rid = _event_lane_rid({"data": {"keyword": group.get("keyword")}})
        if g_lane != lane:
            continue
        if rid and g_rid and g_rid != rid:
            continue
        return {"have": group.get("have"), "expected": expected,
                "keyword": group.get("keyword"), "detector": group.get("detector")}
    return None


def _loop_state(probes: dict) -> dict:
    """Per-node state for the cyclic pipeline view."""
    now = _now()
    events = list(_recent_events)
    reactor = probes.get("reactor") or {}
    analyzer = probes.get("analyzer") or {}
    statuses = probes.get("status") or {}
    avg_status = statuses.get("average") or {}

    # Which stage (if any) is late — asked of the existing detector, not re-derived.
    overdue_stage, overdue_s = None, 0.0
    try:
        result = which_stage_is_overdue(now, events)
        if result:
            overdue_stage, overdue_s = result[0], round(result[1])
    except Exception as exc:
        _emit(f"overdue check failed: {exc}", "warn")

    # Newest event of each type per lane, plus how many reduced frames we saw.
    # Frames are counted PER DETECTOR: in SWAXS mode both detectors emit
    # file.reduced for the same acquisition, and one merged count would read as
    # twice the frames — and would never line up with the averaging gate, which
    # is itself per detector.
    newest: dict = {}
    reduced_by_det: dict = {"background": {}, "sample": {}}
    reduced_rid: dict = {"background": None, "sample": None}
    reduced_seen: dict = {"background": set(), "sample": set()}
    for event in events:
        etype = event.get("type", "")
        if etype not in _STAGE_EVENTS and etype != "reactor.run_start":
            continue
        lane, rid = _event_lane_rid(event)
        newest[(etype, lane)] = event
        if etype == "file.reduced" and lane:
            # Count frames of the CURRENT recipe only: a new recipe_id restarts
            # the count, which is what "reduced 7 frames" has to mean.
            if reduced_rid[lane] != rid:
                reduced_rid[lane], reduced_by_det[lane] = rid, {}
                reduced_seen[lane] = set()
            # The reduction monitor can emit file.reduced twice for the same
            # path (overlapping poll cycles) — count each file once so the
            # number shown never exceeds what is actually on disk.
            path = str((event.get("data") or {}).get("file_path") or "")
            if path and path in reduced_seen[lane]:
                continue
            reduced_seen[lane].add(path)
            det = str((event.get("data") or {}).get("detector") or "?")
            reduced_by_det[lane][det] = reduced_by_det[lane].get(det, 0) + 1

    # file.skipped is not a stage-progress event (see diagnose.py Pattern G) —
    # counted separately from the _STAGE_EVENTS loop above, same per-lane reset
    # on recipe change as the reduced count.
    skipped_by_lane: dict = {"background": 0, "sample": 0}
    skipped_rid: dict = {"background": None, "sample": None}
    for event in events:
        if event.get("type") != "file.skipped":
            continue
        lane, rid = _event_lane_rid(event)
        if not lane:
            continue
        if skipped_rid[lane] != rid:
            skipped_rid[lane], skipped_by_lane[lane] = rid, 0
        skipped_by_lane[lane] += 1

    def _newest(etype: str, lane: str | None = None) -> dict | None:
        if lane is None:
            options = [newest.get((etype, k[1])) for k in newest if k[0] == etype]
            options = [o for o in options if o]
            if not options:
                return None
            return max(options, key=lambda e: e.get("timestamp", ""))
        return newest.get((etype, lane))

    # ── reactor ───────────────────────────────────────────────────────────────
    r_state = str(reactor.get("state") or "")
    last_collect = reactor.get("last_collect") or {}
    role = str(last_collect.get("role") or "")
    active_lane = ("background" if role in BKG_TAGS else "sample") if role else ""
    # last_collect wins: background is deliberately pre-tagged with the
    # UPCOMING recipe_id while it is collected during the flush of the
    # PREVIOUS recipe (src/reactor/controller.py::_begin_next) — current_recipe
    # does not update to the new recipe until the synthesis run actually
    # starts, well after that background collection (and its average) is
    # already done. Preferring current_recipe here made the phase tracker
    # attribute a finished background collection to the recipe that just
    # ended instead of the one it actually belongs to.
    active_rid = (last_collect.get("recipe_id")
                  or (reactor.get("current_recipe") or {}).get("recipe_id") or "")
    if not reactor:
        reactor_node = {"state": "unknown", "detail": "reactor not answering"}
    elif r_state in ("", "idle"):
        reactor_node = {"state": "idle", "detail": "no run"}
    elif reactor.get("supervising") is False:
        reactor_node = {"state": "stalled",
                        "detail": f"{r_state} but control loop is dead"}
    else:
        collecting = bool((reactor.get("spec") or {}).get("collecting"))
        detail = r_state
        if collecting and active_lane:
            detail = f"collecting {active_lane}"
        elif active_lane:
            detail = f"{r_state} · last collect {active_lane}"
        reactor_node = {"state": "running", "detail": detail, "lane": active_lane}
    reactor_node["recipe_id"] = active_rid

    # ── the two lanes ─────────────────────────────────────────────────────────
    lanes: dict = {}
    for lane in ("background", "sample"):
        ev_red = _newest("file.reduced", lane)
        # _last_averaged survives buffer rotation (see its declaration) — the
        # 100-event window alone is not enough once a recipe's per-frame
        # file.reduced events pile up behind its one file.averaged event.
        avg_rec = _last_averaged.get(lane)
        avg_rid = (avg_rec or {}).get("recipe_id")
        # Falls back to skipped_rid too: a recipe where every frame is
        # permanently skipped never fires a single file.reduced, so
        # reduced_rid/avg_rid alone would leave this lane misattributed to
        # whatever recipe_id it last saw (or none at all).
        rid = reduced_rid[lane] or avg_rid or skipped_rid[lane]
        age_red = _event_age_s(ev_red, now) if ev_red else None
        age_avg = (_event_age_s(avg_rec, now) if avg_rec else None)
        gate = _gate_for(avg_status, rid, lane)

        # REDUCE — per frame, as each file lands. Report the detector the gate is
        # waiting on when we know it, else the busiest one, and name it whenever
        # more than one detector is running so the number is never ambiguous.
        per_det = reduced_by_det[lane]
        det_shown = (gate or {}).get("detector")
        if det_shown not in per_det:
            det_shown = max(per_det, key=per_det.get) if per_det else None
        frames = per_det.get(det_shown, 0) if det_shown else 0
        suffix = f" · {det_shown}" if det_shown and len(per_det) > 1 else ""
        counted = f"reduced {frames} frames{suffix}"
        if overdue_stage == "reduce" and not frames:
            red_node = {"state": "stalled", "detail": "no reduced frame"}
        elif age_red is not None and age_red <= _FRESH_S:
            red_node = {"state": "running", "detail": counted}
        elif frames and (avg_rid and age_avg is not None and
                         (age_red is None or age_avg <= age_red)):
            red_node = {"state": "done", "detail": counted}
        elif frames:
            red_node = {"state": "waiting", "detail": counted}
        else:
            red_node = {"state": "idle", "detail": "no frames yet"}
        red_node["frames"] = frames
        red_node["detector"] = det_shown

        # AVERAGE — gated on the frame count matching the expected batch size.
        # The average app's own "waiting" counter is keyed by group keyword and
        # is never removed once a batch flushes (it just sits at 0) — so a
        # completed recipe can still show up as "0 / 10" long after its
        # file.averaged event fired. A completion event for THIS recipe+lane
        # always wins over that stale counter.
        have = gate.get("have") if gate else None
        expected = (gate or {}).get("expected") or avg_status.get("frames_per_average")
        already_averaged = bool(avg_rid) and avg_rid == rid
        # The average app's own gate counter never clears once a batch flushes
        # (see the comment above) — if the completion event says this batch is
        # full while the raw gate still reads empty, that mismatch IS the known
        # ghost-entry bug (src/watchdog/diagnose.py Pattern D), not a stall.
        ghost_gate = bool(already_averaged and expected and not have)
        if already_averaged and expected:
            have = expected
        avg_node = {"have": have, "expected": expected, "ghost_gate": ghost_gate}
        if already_averaged and age_avg is not None and age_avg <= _FRESH_S:
            avg_node.update(state="running", detail="averaging")
        elif already_averaged:
            avg_node.update(state="done", detail="averaged")
        elif gate and expected and have is not None and have < expected:
            avg_node.update(state="waiting",
                            detail=f"{have} / {expected} frames",
                            short=expected - have)
        elif overdue_stage == "average":
            avg_node.update(state="stalled", detail="no average written")
        elif frames:
            avg_node.update(state="waiting",
                            detail=(f"{frames} / {expected} frames" if expected
                                    else f"{frames} frames in"))
        else:
            avg_node.update(state="idle", detail="no frames yet")

        # Frames reduction has permanently given up on for the CURRENT recipe
        # (see reduction/app.py::_note_failure) — a definite reason the average
        # gate for this lane will never fill, not a guess from a timeout. See
        # diagnose.py Pattern G.
        red_node["skipped"] = skipped_by_lane[lane] if skipped_rid[lane] == rid else 0

        lanes[lane] = {
            "recipe_id": rid or "",
            "reduce": red_node,
            "average": avg_node,
            "averaged": bool(avg_rid),
            "averaged_rid": avg_rid,
        }

    # ── reactor phase tracker — background is collected, reduced and averaged
    # FIRST, then sample; reduce/average are one app each, so the split is
    # shown here rather than by duplicating their boxes per lane. "Done" must
    # be scoped to the CURRENT recipe: the newest file.averaged event for a
    # lane is often still the PREVIOUS recipe's (subtract/fit for it can still
    # be running after the reactor has already moved on), so checking "does an
    # averaged event exist at all" marked both phases done while the active
    # recipe's sample average gate was still sitting at 0/10 frames. ─────────
    bkg_done = bool(active_rid) and lanes["background"]["averaged_rid"] == active_rid
    smp_done = bool(active_rid) and lanes["sample"]["averaged_rid"] == active_rid
    lane_now = active_lane or (
        "sample" if (lanes["sample"]["recipe_id"] or lanes["sample"]["reduce"]["frames"])
        else "background")

    def _phase_state(lane: str, done: bool) -> str:
        if done:
            return "done"
        if lane_now == lane and r_state not in ("", "idle"):
            return "active"
        return "idle"

    reactor_node["phases"] = [
        {"lane": "background", "label": "Background",
         "state": _phase_state("background", bkg_done)},
        {"lane": "sample", "label": "Sample",
         "state": _phase_state("sample", smp_done)},
    ]

    # ── reduce / average — ONE app each. Whichever lane is currently flowing
    # through them (the reactor's active lane, falling back to whichever lane
    # actually has activity) is what the single node shows, tagged so it is
    # still obvious which phase it is. ────────────────────────────────────────
    reduce_node = dict(lanes[lane_now]["reduce"])
    reduce_node["lane"] = lane_now
    reduce_node["recipe_id"] = lanes[lane_now]["recipe_id"]
    average_node = dict(lanes[lane_now]["average"])
    average_node["lane"] = lane_now
    average_node["recipe_id"] = lanes[lane_now]["recipe_id"]

    # ── subtract — gated on BOTH lanes arriving ───────────────────────────────
    bkg_rid = lanes["background"]["averaged_rid"]
    smp_rid = lanes["sample"]["averaged_rid"]
    ev_sub = _newest("file.subtracted")
    age_sub = _event_age_s(ev_sub, now) if ev_sub else None
    sub_rid = _event_lane_rid(ev_sub)[1] if ev_sub else None
    have_pair = bool(bkg_rid) and bool(smp_rid) and bkg_rid == smp_rid
    missing = [name for name, rid_ in (("background", bkg_rid), ("sample", smp_rid))
               if not rid_]
    if ev_sub and age_sub is not None and age_sub <= _FRESH_S:
        sub_node = {"state": "running", "detail": "subtracting"}
    elif overdue_stage == "subtract":
        sub_node = {"state": "stalled", "detail": "no subtracted file"}
    elif missing and (bkg_rid or smp_rid):
        sub_node = {"state": "waiting",
                    "detail": f"waiting for {missing[0]} average",
                    "waiting_for": missing}
    elif have_pair and sub_rid != bkg_rid:
        sub_node = {"state": "waiting", "detail": "both averages in — pairing"}
    elif bkg_rid and smp_rid and bkg_rid != smp_rid:
        # Both lanes have an average, but of DIFFERENT conditions — subtraction
        # pairs on the shared recipe_id, so this is a wait, not a result.
        sub_node = {"state": "waiting",
                    "detail": f"averages disagree: bkg {bkg_rid} vs sample {smp_rid}"}
    elif ev_sub:
        sub_node = {"state": "done", "detail": "subtracted"}
    else:
        sub_node = {"state": "idle", "detail": "no averages yet"}
    sub_node.update(recipe_id=sub_rid or (bkg_rid or smp_rid or ""),
                    have_background=bool(bkg_rid), have_sample=bool(smp_rid))

    # ── fit + predict — one app: fits, then writes the next condition ─────────
    ev_fit = _newest("fit.complete")
    age_fit = _event_age_s(ev_fit, now) if ev_fit else None
    fit_data = (ev_fit or {}).get("data") or {}
    if ev_fit and age_fit is not None and age_fit <= _FRESH_S:
        fit_node = {"state": "running", "detail": "fitting"}
    elif overdue_stage == "fit":
        fit_node = {"state": "stalled", "detail": "no fit result"}
    elif ev_sub and (not ev_fit or (ev_fit.get("timestamp", "") <
                                    ev_sub.get("timestamp", ""))):
        fit_node = {"state": "waiting", "detail": "subtracted file not fitted"}
    elif ev_fit:
        size, conf = fit_data.get("size"), fit_data.get("confidence")
        detail = "fitted"
        if size is not None:
            detail = f"{float(size):.1f} nm"
            if conf is not None:
                detail += f" · conf {float(conf):.2f}"
        fit_node = {"state": "done", "detail": detail}
    else:
        fit_node = {"state": "idle", "detail": "nothing to fit"}
    fit_node["recipe_id"] = str(fit_data.get("recipe_id") or "")

    pending = analyzer.get("pending") or []
    campaign = str(analyzer.get("status") or "")
    return {
        "recipe_id": active_rid,
        "reactor": reactor_node,
        "reduce": reduce_node,
        "average": average_node,
        "subtract": sub_node,
        "fit": fit_node,
        "next_condition": {
            "campaign": campaign or ("—" if not analyzer else "idle"),
            "pending": pending[:3],
            "n_pending": len(pending),
            "n_evaluations": analyzer.get("n_evaluations"),
            "budget": analyzer.get("budget"),
        },
        "overdue": ({"stage": overdue_stage, "seconds": overdue_s}
                    if overdue_stage else None),
        "gate_live": bool(avg_status.get("gate")),
    }


def _health_row(probes: dict) -> list[dict]:
    """Loop apps that are CURRENTLY ACTIVE, one entry each.

    "Active" is each app's own notion of taking part in the loop: a monitor app
    is active while its monitor thread runs, the reactor while it has a run or
    auto-run armed, the analyzer while it answers (it watches the Subtracted
    folder whenever it is up). An app that is not taking part is omitted rather
    than shown grey — the row is meant to be scanned from across the room, so
    only what matters right now is on it.

    `ok` is false when the app is enrolled but unhealthy (a reactor whose
    control loop has died) or when the whole snapshot is too old to trust.
    """
    age = _probe_age_s()
    stale = age is None or age > _PROBE_TTL_S * 3
    monitors = probes.get("monitors", {}) or {}
    reactor = probes.get("reactor") or {}
    analyzer = probes.get("analyzer") or {}

    row: list[dict] = []

    def add(app: str, active: bool, ok: bool, note: str) -> None:
        if not active:
            return
        row.append({"app": app, "port": LOOP_APPS.get(app),
                    "ok": bool(ok) and not stale,
                    "note": "probe snapshot is stale" if stale else note})

    r_state = str(reactor.get("state") or "")
    add("reactor",
        active=bool(reactor) and (r_state not in ("", "idle") or bool(reactor.get("auto_run"))),
        ok=reactor.get("supervising") is not False,
        note=r_state or "up")
    for app in ("reduction", "average", "background", "quality"):
        add(app, active=bool(monitors.get(app)), ok=True, note="monitoring")
    add("analyzer", active=bool(analyzer), ok=True,
        note=str(analyzer.get("status") or "up"))
    return row


def _compute_metrics() -> dict:
    """Compute all metrics for the dashboard."""
    try:
        manifest = _read_manifest()
        current_stage, stage_time = _current_stage()

        # Per-run cycle time: how long each stage adds to ONE run of the loop,
        # averaged over recent runs. From manifest.json + disk mtimes, so it is
        # durable across a restart.
        cycle = _run_cycle_times(manifest)

        # App health. probe_all() returns {"monitors": {app: bool}, "analyzer": {...},
        # "reactor": {...}}. The monitor apps report a bool; analyzer/reactor are
        # considered up when they return a non-empty status dict.
        app_health = {}
        probes = _probe_all_cached()
        try:
            monitors = probes.get("monitors", {}) or {}
            app_health = {
                "reduction": bool(monitors.get("reduction")),
                "average": bool(monitors.get("average")),
                "background": bool(monitors.get("background")),
                "quality": bool(monitors.get("quality")),
                "analyzer": bool(probes.get("analyzer")),
                "reactor": bool(probes.get("reactor")),
            }
        except Exception as exc:
            _emit(f"probe error: {exc}", "warn")
            app_health = {}

        # Quality: count good vs bad from manifest
        quality_entries = manifest.get("quality", {}) or {}
        quality_scores = []
        good_count = 0
        bad_count = 0
        try:
            for entry in quality_entries.values():
                if isinstance(entry, dict):
                    score = entry.get("score", 50)
                    quality_scores.append(score)
                    if entry.get("verdict") == "good":
                        good_count += 1
                    elif entry.get("verdict") == "bad":
                        bad_count += 1
        except Exception as exc:
            _emit(f"quality parse error: {exc}", "warn")

        quality_trend = quality_scores[-50:] if quality_scores else []

        # Files per stage from manifest
        file_counts = {}
        try:
            for file_path, entry in (manifest.get("files", {}) or {}).items():
                if isinstance(entry, dict):
                    stage = entry.get("stage", "unknown")
                    file_counts[stage] = file_counts.get(stage, 0) + 1
        except Exception as exc:
            _emit(f"files parse error: {exc}", "warn")

        # Analysed profiles aren't tagged as a file stage — they live in the
        # analyses section. Count only the analyzer's nanoparticle fits: the
        # Data Analysis app also writes guinier/porod/peak/model records there,
        # and len(analyses) counted those too, so the funnel's last bar could
        # exceed the number of subtracted files it is drawn from.
        fits = _nanoparticle_analyses(manifest)
        analysed_count = len({e.get("file_path") for e in fits})

        # Run outcomes come from the nanoparticle fits in the analyses section
        # (reactor.runs only holds recipe/flow/timing — no size/pdi/confidence).
        # Already ordered by updated_at: the old sort key was results["seq"],
        # which the analyzer never writes (see its summary dict), so every run
        # sorted equal and the scatter's x-axis order was whatever order the
        # manifest dict happened to be in.
        runs = []
        try:
            for entry in fits:
                res = entry.get("results", {}) or {}
                diameter = res.get("diameter")
                if not isinstance(diameter, (int, float)):
                    # A fit that produced no size (failed/abandoned) must be
                    # OMITTED, not plotted as 0 nm — a row of points on the
                    # axis reads as "the loop is making 0 nm particles".
                    continue
                runs.append({
                    "recipe_id": res.get("name", entry.get("id", "")),
                    "size": float(diameter),                       # nm
                    "radius": res.get("radius"),
                    "pdi": res.get("pdi", 0) or 0,
                    "confidence": res.get("confidence", 0) or 0,
                })
        except Exception as exc:
            _emit(f"runs parse error: {exc}", "warn")

        try:
            loop = _loop_state(probes)
        except Exception as exc:
            _emit(f"loop state failed: {exc}", "warn")
            loop = {}
        probe_age = _probe_age_s()

        return {
            "pipeline": {
                "current_stage": current_stage,
                "stage_entered_at": stage_time.isoformat() if stage_time else None,
                "cycle": cycle,
            },
            "loop": loop,
            "health": _health_row(probes),
            "probe": {"age_s": probe_age,
                      # `is None` explicitly: an age of 0.0 is the freshest
                      # possible snapshot, and `or` would read it as missing.
                      "stale": probe_age is None or probe_age > _PROBE_TTL_S * 3},
            "throughput": _throughput_last_24h(),
            "quality": {
                "good": good_count,
                "bad": bad_count,
                "trend": quality_trend,
            },
            "files": {
                "reduced": file_counts.get("reduced", 0),
                "averaged": file_counts.get("averaged", 0),
                "subtracted": file_counts.get("subtracted", 0),
                "analysed": file_counts.get("analysed", 0) or analysed_count,
            },
            "runs": runs[-20:] if runs else [],
            "app_health": app_health,
            "resources": _resource_metrics(),
        }
    except Exception as exc:
        _emit(f"metrics computation failed: {exc}", "error")
        return {
            "pipeline": {"current_stage": "error", "stage_entered_at": None, "cycle": {}},
            "loop": {},
            "health": [],
            "probe": {"age_s": None, "stale": True},
            "throughput": [],
            "quality": {"good": 0, "bad": 0, "trend": []},
            "files": {"reduced": 0, "averaged": 0, "subtracted": 0, "analysed": 0},
            "runs": [],
            "app_health": {},
            "resources": {"cpu": 0, "mem": 0, "disk": 0},
        }


def _metrics_snapshot() -> dict:
    """The shared metrics snapshot. Computes it inline only on the very first
    call (before the refresh thread has produced one), so a dashboard opened
    the instant the app starts still gets real numbers rather than blank
    plots — every later read is free."""
    data = _metrics_cache["data"]
    if data is None:
        data = _compute_metrics()
        _metrics_cache["data"] = data
        _metrics_cache["ts"] = _time.monotonic()
    return data


def _metrics_refresh_loop() -> None:
    """Background thread: the ONLY caller of _compute_metrics in steady state."""
    while True:
        try:
            data = _compute_metrics()
            _metrics_cache["data"] = data
            _metrics_cache["ts"] = _time.monotonic()
        except Exception as exc:
            _emit(f"metrics refresh error: {exc}", "warn")
        _time.sleep(_METRICS_TTL_S)


def _config_path() -> Path:
    """Where this project's Auto Watch config lives, falling back to the app's
    own default when the project has none of its own.

    The per-project override is `<project_root>/watchdog_config.yml` — INSIDE
    the selected folder, alongside config.yml and manifest.json, the same
    convention every other app uses (`Path(_project_root) / "config.yml"`,
    e.g. calibration/app.py). It previously looked in
    `Path(_project_root).parent / "watchdog" / "config.yml"` — a sibling *of*
    the project folder, which is nothing in the documented layout, so the
    override never resolved and the Alerts page always wrote into the repo's
    own tracked watchdog/config.yml instead.
    """
    if _project_root:
        candidate = Path(_project_root) / "watchdog_config.yml"
        if candidate.is_file():
            return candidate
    return _HERE / "config.yml"


#: Set when the last config load failed, so the UI can say so instead of the
#: operator discovering it from the absence of messages.
_config_error: str = ""


def _load_config(project_root: str = "") -> dict:
    """Load config.yml, logging errors but never raising into the app.

    Loads unconditionally — a project root is NOT required. Auto Watch needs no
    project folder to probe apps or detect stalls, and _config_path() already
    falls back to the app's own config.yml. Returning {} when no project was
    set meant should_send() ran on its defaults instead: master switch on, all
    five categories on, quiet hours off. An operator who never picked a folder
    had their `slack_enabled: false` silently ignored.

    A malformed config FAILS CLOSED. It used to fall back to slack_enabled
    True with every category on, so a typo in config.yml overrode the
    operator's Stop and flooded Slack — the opposite of what a master off
    switch must do when it cannot be read.
    """
    global _config_error
    try:
        cfg = load_settings(_config_path())
        _config_error = ""
        return cfg
    except ValidationError as exc:
        _config_error = str(exc)
        _emit(f"config error — NOT sending until this is fixed: {exc}", "error")
        return {
            "quiet_hours": None,
            "summary": "off",
            "snooze_default_min": 30,
            "min_interval_s": 3,
            "slack_enabled": False,
            "categories": {c: False for c in CATEGORIES},
            "ai_fallback_enabled": False,
        }


#: Bus events survive a restart in manifest["events"] (a rolling 100-event
#: window, src/manifest.py). Stall detection reads _recent_events, which is
#: memory-only and empty at boot — so a restarted Auto Watch could not see a
#: stall that began BEFORE it started, which is the one case an operator
#: restarts it for: a stalled pipeline emits nothing, so the window never
#: refilled and the stall was never reported. Seed from the durable copy.
def _seed_recent_events() -> int:
    """Fill _recent_events from manifest["events"], oldest first. Returns how
    many were seeded. Only events the detector understands are kept."""
    events = _read_manifest().get("events") or []
    if not isinstance(events, list):
        return 0
    seeded = []
    for event in events[-100:]:
        if not isinstance(event, dict) or not event.get("timestamp"):
            continue
        data = event.get("data") or {}
        seeded.append({
            "type": event.get("type", ""),
            "timestamp": event.get("timestamp", ""),
            "data": {k: data[k] for k in _KEEP_DATA
                     if isinstance(data, dict) and k in data},
        })
    if not seeded:
        return 0
    # Prepend: anything the live bus already delivered is newer than the file.
    live = list(_recent_events)
    known = {(e.get("type"), e.get("timestamp")) for e in live}
    merged = [e for e in seeded if (e.get("type"), e.get("timestamp")) not in known] + live
    del _recent_events[:]
    _recent_events.extend(merged[-100:])
    return len(merged) - len(live)


# Initialize on startup
try:
    _settings = _load_config(_project_root)
    _transport = WebhookTransport(
        min_interval_s=_settings.get("min_interval_s", 3.0),
        timeout_s=6.0,
    )
    webhook_status = "configured" if _transport.webhook_url else "NOT configured"
    _emit(f"ready ({webhook_status})", "ok")
except Exception as exc:  # pragma: no cover
    _emit(f"startup failed: {exc}", "error")
    _transport = None

# ── Event bus and processing ──────────────────────────────────────────────────

def _on_bus_event(event: dict) -> None:
    """Process an incoming bus event: translate, apply policy, send if approved."""
    global _recent_events, _sent_messages, _last_averaged

    etype = event.get("type", "")
    data = event.get("data", {})

    # Record for stall detection (keep last 100 events). A trimmed copy of the
    # payload rides along so the dashboard can tell WHICH lane and WHICH recipe
    # a file belonged to; stall detection reads only type/timestamp.
    _recent_events.append({
        "type": etype,
        "timestamp": event.get("timestamp", ""),
        "data": {k: data[k] for k in _KEEP_DATA if isinstance(data, dict) and k in data},
    })
    if len(_recent_events) > 100:
        _recent_events.pop(0)

    if etype == "file.averaged":
        lane, rid = _event_lane_rid(event)
        if lane and rid:
            _last_averaged[lane] = {"recipe_id": rid,
                                     "timestamp": event.get("timestamp", "")}

    # Translate event to (title, text, level) if it's a notification event
    msg_tuple = event_to_message(etype, data)
    if msg_tuple:
        title, text, level = msg_tuple
        kind = "fault" if level == "fault" else "progress"
        category = category_for_event(etype)
        # quiet_hours in config.yml is written in the operator's local time.
        send, actual_level = should_send(kind, _now().astimezone(), _settings, _state, category)
        if send and _transport:
            _transport.send(title, text, actual_level)
            _sent_messages.append({
                "timestamp": _now().isoformat(),
                "title": title,
                "text": text,
                "level": actual_level,
            })
            if len(_sent_messages) > 50:
                _sent_messages.pop(0)


# Event bus subscription (graceful degradation)
try:
    from src.events import EventBusClient as _EventBusClient

    _bus = _EventBusClient("watchdog").connect(retry=True)
    _bus.on_event(_on_bus_event)
except Exception:
    _bus = None


# ── Stall detection monitor ───────────────────────────────────────────────────

def _stall_check_loop() -> None:
    """Periodically check if any pipeline stage is stalled."""
    import time

    # A stall persists for hours; re-alert on a backoff (5, 15, 60 min, then
    # hourly) instead of every tick, and start over when the stalled stage changes.
    _BACKOFF_S = (300, 900, 3600)
    last_stage = None
    last_alert_ts = 0.0
    n_alerts = 0

    while True:
        try:
            time.sleep(300)  # Check every 5 minutes
            result = which_stage_is_overdue(_now(), list(_recent_events))
            if not result:
                last_stage = None
                n_alerts = 0
                continue
            stage, overdue_s = result
            if stage != last_stage:
                last_stage, n_alerts, last_alert_ts = stage, 0, 0.0
            gap = _BACKOFF_S[min(n_alerts, len(_BACKOFF_S) - 1)]
            if n_alerts and time.monotonic() - last_alert_ts < gap:
                continue
            probes = probe_all()
            loop = _loop_state(probes)
            diag_title, diag_text, is_stall = diagnose_stall(
                stage, overdue_s, probes, loop,
                ai_fallback_enabled=_settings.get("ai_fallback_enabled", False),
            )
            if not is_stall:
                # A known non-stall shape (e.g. the reactor is still collecting,
                # or the average app's ghost-gate entry) — nothing to alert on.
                last_stage, n_alerts = None, 0
                continue
            if n_alerts:
                diag_title = f"{diag_title} (still stalled, alert #{n_alerts + 1})"
            n_alerts += 1
            last_alert_ts = time.monotonic()
            send, actual_level = should_send("fault", _now().astimezone(), _settings, _state, "stalls")
            if send and _transport:
                _transport.send(diag_title, diag_text, actual_level)
                _sent_messages.append({
                    "timestamp": _now().isoformat(),
                    "title": diag_title,
                    "text": diag_text,
                    "level": actual_level,
                })
                if len(_sent_messages) > 50:
                    _sent_messages.pop(0)
        except Exception as exc:
            _emit(f"stall check failed: {exc}", "warn")


_stall_thread: threading.Thread | None = None
_probe_thread: threading.Thread | None = None
_metrics_thread: threading.Thread | None = None

# Same guard every other app's monitor honours (see conftest.py): importing this
# module in a test must not spawn three loops that poll folders, probe six HTTP
# endpoints and render nothing anyone reads. Tests call _watch/_compute helpers
# directly; the daemons add only scheduling jitter and flakiness.
_NO_WATCH = os.environ.get("SWAXS_NO_WATCH", "").strip().lower() in ("1", "true", "yes")

if not _NO_WATCH:
    try:
        # Recover the event window BEFORE stall detection starts, so a stall
        # already in progress is visible on the first tick (_seed_recent_events).
        _n_seeded = _seed_recent_events()
        if _n_seeded:
            _emit(f"recovered {_n_seeded} bus event(s) from manifest.json — stall "
                  f"detection can see a stall that started before this restart", "ok")

        _stall_thread = threading.Thread(target=_stall_check_loop, daemon=True,
                                         name="watchdog-stall-check")
        _stall_thread.start()
        _probe_thread = threading.Thread(target=_probe_refresh_loop, daemon=True,
                                         name="watchdog-probe-refresh")
        _probe_thread.start()
        _metrics_thread = threading.Thread(target=_metrics_refresh_loop, daemon=True,
                                           name="watchdog-metrics-refresh")
        _metrics_thread.start()
    except Exception as exc:
        _emit(f"could not start background threads: {exc}", "warn")


# ── Flask routes ──────────────────────────────────────────────────────────────


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


#: Where a local plotly.min.js may already live. The analysis and assistant
#: apps vendor their own copy; Auto Watch serves whichever it finds instead of
#: adding a third 4.4 MB duplicate to the repo.
_VENDOR_DIRS = (
    _ROOT / "analysis" / "static" / "vendor",
    _ROOT / "assistant" / "static" / "vendor",
)
_VENDOR_CDN = {
    "plotly.min.js": "https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.26.0/plotly.min.js",
}


@app.route("/vendor/<path:name>", methods=["GET"])
def vendor(name: str):
    """Serve a vendored JS library from disk, falling back to the CDN.

    The dashboard used to load Plotly straight from cdnjs. On a beamline
    control PC with no outbound internet — or behind a proxy that blocks it —
    `Plotly` was simply undefined, every Plotly.react() call threw, and all
    four charts rendered blank while the rest of the page worked. Which is
    exactly what "some of the plots are not working" looks like.

    Serving the copy that is already in the repo makes the charts work
    offline; the redirect keeps today's behaviour on a machine that has
    internet but no vendored file.
    """
    safe = Path(name).name          # no traversal: basename only
    for d in _VENDOR_DIRS:
        candidate = d / safe
        if candidate.is_file():
            return send_from_directory(str(d), safe, max_age=86400)
    url = _VENDOR_CDN.get(safe)
    if url:
        _emit(f"no local {safe} — falling back to the CDN (charts need internet)", "warn")
        return redirect(url, code=302)
    return jsonify({"error": f"no vendored copy of {safe}"}), 404


@app.route("/api/settings", methods=["GET"])
def get_settings():
    snooze_until = _state.get("snooze_until")
    snoozed = snooze_until is not None and _now().timestamp() < snooze_until
    return jsonify({
        "quiet_hours": (
            f"{_settings['quiet_hours'][0][0]:02d}:{_settings['quiet_hours'][0][1]:02d}-"
            f"{_settings['quiet_hours'][1][0]:02d}:{_settings['quiet_hours'][1][1]:02d}"
            if _settings.get("quiet_hours")
            else None
        ),
        "summary": _settings.get("summary", "off"),
        "snoozed": snoozed,
        "snooze_until_ts": snooze_until,
        "webhook_configured": bool(_transport and _transport.webhook_url),
        "sent_messages": _sent_messages[-20:],  # Last 20 for the status page
        "slack_enabled": _settings.get("slack_enabled", True),
        "categories": _settings.get("categories", {c: True for c in CATEGORIES}),
        # Non-empty means config.yml could not be parsed and sending is held
        # OFF until it is fixed. The UI must show this — the symptom is
        # otherwise just an absence of messages, which looks like a quiet run.
        "config_error": _config_error,
        "delivery_error": (_transport.last_error if _transport else ""),
        "delivery_failures": (_transport.n_failed if _transport else 0),
    })


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Update the Slack master switch and/or category toggles.

    Persisted to config.yml via save_notify_settings() so the choice survives
    a restart. The safety-category exception (can't be false while sending
    is on) is enforced there, not here — one place, shared by load and save.
    """
    global _settings
    data = request.get_json() or {}
    if "slack_enabled" not in data and "categories" not in data:
        return jsonify({"ok": False, "error": "nothing to update"}), 400

    slack_enabled = data.get("slack_enabled")
    if slack_enabled is not None and not isinstance(slack_enabled, bool):
        return jsonify({"ok": False, "error": "slack_enabled must be a boolean"}), 400

    categories = data.get("categories")
    if categories is not None:
        if not isinstance(categories, dict) or not set(categories) <= set(CATEGORIES):
            return jsonify({"ok": False,
                            "error": f"categories must be a subset of {list(CATEGORIES)}"}), 400
        if not all(isinstance(v, bool) for v in categories.values()):
            return jsonify({"ok": False, "error": "category values must be booleans"}), 400

    try:
        result = save_notify_settings(_config_path(),
                                      slack_enabled=slack_enabled, categories=categories)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    _settings["slack_enabled"] = result["slack_enabled"]
    _settings["categories"] = result["categories"]
    return jsonify({"ok": True, **result})


@app.route("/api/test", methods=["POST"])
def test_notification():
    """Send a test message to Slack."""
    if not _transport or not _transport.webhook_url:
        return jsonify({"ok": False, "error": "webhook not configured"}), 400
    _transport.send(
        title="[TEST] Auto Watch",
        text="This is a test message from the Auto Watch app.",
        level="info",
    )
    return jsonify({"ok": True, "sent": True})


@app.route("/api/snooze", methods=["POST"])
def snooze_notifications():
    """Snooze progress messages for N minutes."""
    data = request.get_json() or {}
    duration_min = data.get("minutes", _settings.get("snooze_default_min", 30))
    try:
        duration_min = int(duration_min)
        if duration_min < 1:
            raise ValueError("must be >= 1")
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": f"invalid duration: {exc}"}), 400

    set_snooze(_state, duration_min, _now())
    return jsonify({
        "ok": True,
        "snoozed_until_ts": _state["snooze_until"],
        "duration_min": duration_min,
    })


@app.route("/api/set_project", methods=["POST"])
def set_project():
    """Set the project root and reload config."""
    global _project_root, _settings
    data = request.get_json() or {}
    project = str(data.get("path", "")).strip()
    if not project:
        return jsonify({"ok": False, "error": "path is required"}), 400
    _project_root = project
    os.environ["SWAXS_PROJECT"] = project
    _settings = _load_config(_project_root)
    clear_snooze(_state)
    # The manifest just became readable (or changed): recover its event window
    # so stall detection isn't blind until the pipeline next emits, and drop the
    # stale metrics snapshot so the dashboard doesn't show the old project's.
    n_seeded = _seed_recent_events()
    _metrics_cache["data"] = None
    _metrics_cache["ts"] = 0.0
    return jsonify({"ok": True, "project": project, "events_recovered": n_seeded})


@app.route("/api/metrics", methods=["GET"])
def metrics():
    """Return the live metrics snapshot (shared, refreshed in the background)."""
    return jsonify(_metrics_snapshot())


@app.route("/api/stream", methods=["GET"])
def stream_metrics():
    """Server-Sent Events stream of live metrics (1 Hz).

    Reads the shared snapshot rather than recomputing: the tick rate is a UI
    choice and must not multiply the cost of the underlying disk scan by the
    number of open tabs (see _metrics_cache).
    """
    def generate():
        try:
            last_sent = None
            while True:
                try:
                    snap = _metrics_snapshot()
                    stamp = _metrics_cache["ts"]
                    # Re-send only when the snapshot actually changed; a
                    # reconnecting client still gets one immediately.
                    if stamp != last_sent:
                        last_sent = stamp
                        yield f"data: {json.dumps(snap)}\n\n"
                    else:
                        yield ": keep-alive\n\n"
                except Exception as exc:
                    _emit(f"SSE tick error: {exc}", "warn")
                    yield f"data: {{}}\n\n"  # Send empty data on error
                _time.sleep(1.0)
        except GeneratorExit:
            pass
        except Exception as exc:
            _emit(f"SSE stream error: {exc}", "error")

    return Response(generate(), mimetype="text/event-stream")


# ── Shutdown ───────────────────────────────────────────────────────────────────

def shutdown():
    if _transport is not None:
        _transport.close()
    if _bus is not None:
        try:
            _bus.disconnect()
        except Exception:
            pass


import atexit as _atexit

_atexit.register(shutdown)

if __name__ == "__main__":
    app.run(host="localhost", port=5110, debug=False)
