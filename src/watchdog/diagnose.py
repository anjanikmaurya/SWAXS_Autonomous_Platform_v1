"""
src/watchdog/diagnose.py — Translate stall detection + loop state into diagnostic
messages.

Layer 1 (deterministic, always runs): given the overdue stage, probe results,
and the per-node loop state that watchdog/app.py::_loop_state already computes
for the dashboard (recipe_id, lane, frame gate, reactor state), match one of
seven known stall shapes (patterns A-G) and report the numbers behind the
diagnosis. If nothing matches, say "unrecognised pattern" and dump every
number rather than guessing.

Layer 2 (LLM fallback, opt-in, unrecognised case only): reads the tail of the
stalled app's log and asks for a likely cause. Advisory only, same contract as
src/ai/loop_advice.py — JSON out, small token budget, neutral (no-op) on any
failure. It can never change the matched pattern or the is_stall verdict; it
only appends a paragraph labeled "AI reading of the log:".
"""
from __future__ import annotations

import json
from pathlib import Path

from src.watchdog.expectations import STAGE_TIMEOUTS
from src.watchdog.messages import _format_duration
from src.watchdog.probes import MONITOR_APPS, ANALYZER_PORT, REACTOR_PORT

# repo_root/src/watchdog/diagnose.py, so parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Hard bound on the Layer-2 LLM call. It runs on Auto Watch's stall-check
#: loop, so it must fail fast rather than delay the alert (see _ai_log_reading).
_AI_TIMEOUT_S = 15.0

# Which app produces the event a given overdue stage is waiting for.
_STAGE_PRODUCERS = {
    "reduce": ("reduction", MONITOR_APPS["reduction"], "2D → 1D reduction"),
    "average": ("average", MONITOR_APPS["average"], "averaging & stitching"),
    "subtract": ("background", MONITOR_APPS["background"], "background subtraction"),
    "fit": ("analyzer", ANALYZER_PORT, "auto-fit & size measurement"),
}

# Stage whose last event we measure "time since upstream last produced
# anything" from — see which_stage_is_overdue(): overdue_s is how far PAST the
# timeout we are, counted from the last event of the stage before this one.
_UPSTREAM_STAGE = {"reduce": "collect", "average": "reduce",
                    "subtract": "average", "fit": "subtract"}
_UPSTREAM_LABEL = {"collect": "the reactor run start", "reduce": "reduction",
                    "average": "averaging", "subtract": "subtraction"}


def _upstream_elapsed_s(stage: str, overdue_s: float) -> float:
    """Seconds since the upstream stage's event — see _UPSTREAM_STAGE above."""
    return overdue_s + STAGE_TIMEOUTS.get(stage, 1800)


def _gate_str(avg: dict) -> str:
    have, expected = avg.get("have"), avg.get("expected")
    if not expected:
        return "no batch gate active"
    return f"{have or 0} of {expected} frames"


def _facts_block(stage: str, overdue_s: float, loop: dict, recipe_id: str,
                  lane: str, gate_line: str, pattern_line: str) -> str:
    """The five things every stall message must report, as one block."""
    upstream = _UPSTREAM_STAGE.get(stage)
    elapsed = _upstream_elapsed_s(stage, overdue_s)
    reactor = loop.get("reactor") or {}
    return "\n".join([
        f"recipe_id: {recipe_id or '?'} · lane: {lane or '?'}",
        f"gate: {gate_line}",
        f"{_UPSTREAM_LABEL.get(upstream, upstream or '?')} last produced "
        f"something {_format_duration(elapsed)} ago",
        f"reactor: {reactor.get('state', 'unknown')} "
        f"({reactor.get('detail', '') or 'no detail'})",
        pattern_line,
    ])


def _tail_log(app: str, n: int = 50) -> str:
    """Last ``n`` lines of logs/<app>.log, or "" if it can't be read."""
    path = _REPO_ROOT / "logs" / f"{app}.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _ai_log_reading(app: str, stage: str, facts_text: str) -> dict | None:
    """One structured, advisory-only LLM call over the stalled app's log tail.

    Mirrors src/ai/loop_advice.py's contract: JSON out, small token budget,
    returns None (never raises) on missing credentials, no log, or bad output.
    """
    log_tail = _tail_log(app)
    if not log_tail:
        return None
    try:
        from src.ai.loop_advice import _ask_json
    except Exception:
        return None
    out = _ask_json(
        system=(
            "You are reading the tail of a SAXS pipeline app's log to help diagnose "
            "why it appears stalled. You do not know for certain that it is broken. "
            "Keys: likely_cause (one sentence), evidence (one sentence citing/"
            "paraphrasing something specific in the log), suggested_check (one "
            "sentence, a concrete next step for the operator)."
        ),
        user=json.dumps({"stalled_stage": stage, "app": app, "facts": facts_text,
                          "log_tail": log_tail[-8000:]}, default=str),
        max_tokens=400,
        # Bounded hard: this runs inline in Auto Watch's 5-minute stall-check
        # loop, so an unresponsive gateway must not hold up the alert it is
        # annotating (the SDK's own default is 10 minutes). On timeout
        # _ask_json returns None and the message goes out without the
        # paragraph — which is exactly the advisory-only contract.
        timeout_s=_AI_TIMEOUT_S,
    )
    if not isinstance(out, dict):
        return None
    return {
        "likely_cause": str(out.get("likely_cause", ""))[:400],
        "evidence": str(out.get("evidence", ""))[:400],
        "suggested_check": str(out.get("suggested_check", ""))[:400],
    }


def _append_ai_reading(text: str, app: str, stage: str, ai_fallback_enabled: bool) -> str:
    if not ai_fallback_enabled:
        return text
    ai = _ai_log_reading(app, stage, text)
    if not ai:
        return text
    return (
        f"{text}\n\n"
        f"AI reading of the log: {ai['likely_cause']} "
        f"(evidence: {ai['evidence']}; check: {ai['suggested_check']})"
    )


def _unrecognised(stage: str, overdue_s: float, probes: dict, loop: dict,
                   recipe_id: str, lane: str, gate_line: str,
                   note: str, ai_fallback_enabled: bool) -> tuple[str, str, bool]:
    producer_app, producer_port, producer_desc = _STAGE_PRODUCERS[stage]
    is_monitoring = (probes.get("monitors") or {}).get(producer_app)
    node = loop.get(stage) or {}
    pattern_line = (
        f"pattern: unrecognised — no known shape (A-F) matched; "
        f"{producer_app} monitoring: {is_monitoring}"
    )
    text = _facts_block(stage, overdue_s, loop, recipe_id, lane, gate_line, pattern_line)
    text = "\n".join([
        note,
        f"node: state={node.get('state')!r} detail={node.get('detail')!r}",
        text,
    ])
    text = _append_ai_reading(text, producer_app, stage, ai_fallback_enabled)
    title = f"Unrecognised pattern — {producer_desc} stalled"
    return title, text, True


def _diagnose_average(stage: str, overdue_s: float, probes: dict, loop: dict,
                       ai_fallback_enabled: bool) -> tuple[str, str, bool]:
    avg = loop.get("average") or {}
    red = loop.get("reduce") or {}
    reactor = loop.get("reactor") or {}
    recipe_id = str(avg.get("recipe_id") or loop.get("recipe_id") or "")
    lane = str(avg.get("lane") or "")
    gate_line = _gate_str(avg)
    have, expected = avg.get("have"), avg.get("expected")
    have_n = have or 0

    # D — the known average/app.py `_avg_pending` ghost entry. _loop_state
    # already corrects have -> expected once a matching file.averaged event is
    # seen, so this flag means the batch is actually done — the raw gate read
    # have=0 only because its per-keyword counter never clears on flush.
    if avg.get("ghost_gate"):
        pattern_line = (f"pattern: D (average gate ghost entry) — this is the known "
                         f"average/app.py _avg_pending bug, not a stall")
        text = "\n".join([
            f"Gate showed 0/{expected} for {recipe_id or '?'} · {lane or '?'}, but a "
            f"matching file.averaged already fired for this recipe.",
            "Known bug: the average app's per-keyword 'waiting' counter is never "
            "cleared once a batch flushes, so it just sits at 0. This is that ghost "
            "entry, not a stall — no action needed.",
            _facts_block(stage, overdue_s, loop, recipe_id, lane, gate_line, pattern_line),
        ])
        return "Pattern D — average gate ghost entry (not a stall)", text, False

    gate_full = bool(expected) and have_n >= expected

    # G — reduction has permanently skipped one or more frames for this lane's
    # recipe (reduction/app.py::_note_failure hit its strike limit) while the
    # gate is still short: a definite reason the batch will never fill, not a
    # guess from a timeout — checked ahead of E/A/B/C so it always wins when
    # it applies.
    skipped = red.get("skipped") or 0
    if skipped and not gate_full:
        plural = "s" if skipped != 1 else ""
        pattern_line = (f"pattern: G (reduction permanently skipped frames) — "
                         f"check the reduction app, port {MONITOR_APPS['reduction']}")
        text = "\n".join([
            f"reduction permanently skipped {skipped} frame{plural} for "
            f"{recipe_id or '?'} {lane or '?'} — the average gate will never "
            f"reach {expected if expected is not None else '?'}.",
            f"Check: reduction app (port {MONITOR_APPS['reduction']}) — "
            f"logs/reduction.log names the skipped file(s).",
            _facts_block(stage, overdue_s, loop, recipe_id, lane, gate_line, pattern_line),
        ])
        return (f"Pattern G — reduction permanently skipped {skipped} "
                f"frame{plural}, gate stuck at {gate_line}"), text, True

    # E — gate full, but no averaged output: genuine average-app failure.
    if gate_full and avg.get("state") == "stalled":
        pattern_line = (f"pattern: E (average app stalled with a full gate) — "
                         f"check the average app's log, port {MONITOR_APPS['average']}")
        text = "\n".join([
            f"Gate is full ({gate_line}) for {recipe_id or '?'} · {lane or '?'}, but no "
            f"file.averaged event followed.",
            f"Check: average app (port {MONITOR_APPS['average']}) — this needs the log.",
            _facts_block(stage, overdue_s, loop, recipe_id, lane, gate_line, pattern_line),
        ])
        return f"Pattern E — average app stalled, gate full ({gate_line})", text, True

    if gate_full:
        # Full but _loop_state did not call it stalled — a timing mismatch
        # between the timeout-based detector and the gate. Not one of A-F.
        return _unrecognised(
            stage, overdue_s, probes, loop, recipe_id, lane, gate_line,
            note=(f"Gate reads full ({gate_line}) but the averaging node is not "
                  f"flagged stalled (state={avg.get('state')!r}) — timing mismatch "
                  f"with the timeout-based detector."),
            ai_fallback_enabled=ai_fallback_enabled,
        )

    # From here down: gate not full.
    reactor_collecting = (reactor.get("state") == "running"
                           and str(reactor.get("detail") or "").startswith("collecting"))

    # A — gate not full, reactor still collecting: NOT a stall.
    if reactor_collecting:
        remaining = (expected - have_n) if expected else None
        pattern_line = "pattern: A (reactor still collecting) — not a stall"
        if remaining is not None:
            headline = (f"Waiting for {remaining} more frame{'s' if remaining != 1 else ''}, "
                        f"reactor is collecting.")
        else:
            headline = "Reactor is still collecting; more frames are coming."
        text = "\n".join([
            headline,
            _facts_block(stage, overdue_s, loop, recipe_id, lane, gate_line, pattern_line),
        ])
        return "Pattern A — gate not full, reactor collecting (not a stall)", text, False

    reduce_producing = red.get("state") == "running"

    # C — gate not full, reduction produced nothing recently: reduction stopped.
    if not reduce_producing:
        pattern_line = (f"pattern: C (reduction stopped) — check the reduction app, "
                         f"port {MONITOR_APPS['reduction']}")
        text = "\n".join([
            f"Reduction has not produced a new frame recently (state="
            f"{red.get('state')!r}, detail={red.get('detail')!r}) while the gate is "
            f"still short ({gate_line}).",
            f"Check: reduction app (port {MONITOR_APPS['reduction']}).",
            _facts_block(stage, overdue_s, loop, recipe_id, lane, gate_line, pattern_line),
        ])
        return f"Pattern C — reduction stopped, gate stuck at {gate_line}", text, True

    # B — gate not full, reactor idle/ready, reduction otherwise fine: real
    # stall, the remaining frames are never coming.
    pattern_line = (f"pattern: B (reactor not collecting) — check the reactor app, "
                     f"port {REACTOR_PORT}")
    text = "\n".join([
        f"Reduction is keeping up, but the reactor is {reactor.get('state', 'unknown')} "
        f"(not collecting) — the remaining frames for {recipe_id or '?'} are never coming.",
        f"Check: reactor app (port {REACTOR_PORT}).",
        _facts_block(stage, overdue_s, loop, recipe_id, lane, gate_line, pattern_line),
    ])
    return f"Pattern B — reactor stopped collecting, gate stuck at {gate_line}", text, True


def _diagnose_subtract(stage: str, overdue_s: float, probes: dict, loop: dict,
                        ai_fallback_enabled: bool) -> tuple[str, str, bool]:
    sub = loop.get("subtract") or {}
    recipe_id = str(sub.get("recipe_id") or loop.get("recipe_id") or "")
    have_bkg, have_smp = bool(sub.get("have_background")), bool(sub.get("have_sample"))
    gate_line = f"background {'✓' if have_bkg else '✗'} · sample {'✓' if have_smp else '✗'}"

    # F — both lanes averaged, no subtracted file: subtraction stopped.
    if have_bkg and have_smp:
        pattern_line = (f"pattern: F (subtraction stopped) — check the background app, "
                         f"port {MONITOR_APPS['background']}")
        text = "\n".join([
            f"Both lanes are averaged for {recipe_id or '?'}, but no file.subtracted "
            f"event followed.",
            f"Check: background app (port {MONITOR_APPS['background']}).",
            _facts_block(stage, overdue_s, loop, recipe_id, "background+sample",
                         gate_line, pattern_line),
        ])
        return "Pattern F — both lanes averaged, no subtracted file", text, True

    missing = "sample" if have_bkg else ("background" if have_smp else "background and sample")
    return _unrecognised(
        stage, overdue_s, probes, loop, recipe_id, "background+sample", gate_line,
        note=f"Subtraction can't start yet — the {missing} average isn't on disk.",
        ai_fallback_enabled=ai_fallback_enabled,
    )


def diagnose_stall(
    stage: str,
    overdue_s: float,
    probes: dict,
    loop: dict,
    *,
    ai_fallback_enabled: bool = False,
) -> tuple[str, str, bool]:
    """
    Given a stalled stage, probe results, and the dashboard's per-node loop
    state, return (title, text, is_stall) with a diagnostic message.

    Parameters:
        stage: Which stage is overdue ("reduce", "average", "subtract", "fit")
        overdue_s: How long it's been stuck (seconds), per which_stage_is_overdue
        probes: Result dict from probes.probe_all()
        loop: Result dict from watchdog/app.py::_loop_state(probes) — the same
            per-lane/gate/reactor numbers the dashboard already shows
        ai_fallback_enabled: Layer 2 switch (watchdog/config.yml
            diagnosis.ai_fallback_enabled). Only consulted for the
            unrecognised-pattern case; never changes is_stall or the title.

    Returns:
        (title, text, is_stall). is_stall is False for the two known
        non-stall shapes (A: reactor still collecting; D: the average app's
        ghost-gate entry) — callers should not alert on those.
    """
    if stage not in _STAGE_PRODUCERS:
        return "Stalled", f"Stalled at unknown stage: {stage}", True

    if stage == "average":
        return _diagnose_average(stage, overdue_s, probes, loop, ai_fallback_enabled)
    if stage == "subtract":
        return _diagnose_subtract(stage, overdue_s, probes, loop, ai_fallback_enabled)

    # "reduce" and "fit" have no named pattern (A-F) yet.
    producer_app, _, _ = _STAGE_PRODUCERS[stage]
    node = loop.get(stage) or {}
    recipe_id = str(node.get("recipe_id") or loop.get("recipe_id") or "")
    lane = str(node.get("lane") or "")
    return _unrecognised(
        stage, overdue_s, probes, loop, recipe_id, lane, "no batch gate at this stage",
        note=f"No named pattern covers the '{stage}' stage yet.",
        ai_fallback_enabled=ai_fallback_enabled,
    )
