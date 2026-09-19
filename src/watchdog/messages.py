"""
src/watchdog/messages.py — Translate platform bus events into Slack messages.

Pure functions converting reactor and analyzer events into (title, text, level)
tuples suitable for the Slack webhook. No I/O, no side effects, unit-testable.

The old reactor app sent messages directly via SlackNotifier; now the watchdog
subscribes to the same events from the bus and formats them here.
"""
from __future__ import annotations


def _fmt(v) -> str:
    """Format a value for display."""
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _format_duration(s: float) -> str:
    """Format duration in seconds as human-readable."""
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def format_reactor_run_start(data: dict) -> tuple[str, str, str]:
    """Translate reactor.run_start event to (title, text, level)."""
    recipe_id = str(data.get("recipe_id") or "")
    recipe = data.get("recipe") or {}
    duration_s = float(data.get("duration_s") or 0)

    params = {
        k: v for k, v in recipe.items()
        if k in ("T_reac", "F_tot", "x_ODE", "x_TOP", "x_oley")
    }
    fields = " • ".join(f"{_fmt(k)}: {_fmt(v)}" for k, v in params.items())
    text = f"Duration: {_format_duration(duration_s)}"
    if fields:
        text += f"\n{fields}"

    title = f"Recipe Applied — {recipe_id}" if recipe_id else "Recipe Applied"
    return title, text, "info"


#: Run-end reasons that are the loop working as designed. Everything else
#: ended the run earlier than planned and the operator should be able to see
#: that at a glance, from the phone, without decoding reason strings.
_NORMAL_END = ("saxs measurement complete", "duration elapsed",
               "next condition available", "ended")


def _split_reason(reason: str) -> tuple[str, str]:
    """('SAXS measurement complete', 'Run21_r016_..._Average.dat').

    The reactor appends the triggering file to the reason with an em dash
    (controller._run_reason). Slack then showed the whole absolute path inline,
    which pushed the actual reason off the edge on a phone.
    """
    head, _, tail = reason.partition(" — ")
    detail = tail.strip()
    if detail and ("/" in detail or "\\" in detail):
        detail = detail.replace("\\", "/").rsplit("/", 1)[-1]
    return head.strip(), detail


def format_reactor_run_complete(data: dict) -> tuple[str, str, str]:
    """Translate reactor.run_complete event to (title, text, level).

    "Stopped by: …" was wrong for the case that happens most. In an autonomous
    campaign almost every run ends because the measurement finished, and
    reading "Stopped by" on the phone at 2am suggests something interrupted the
    rig. The label now reflects which of the two actually happened, because the
    distinction — did this finish, or did it stop early? — is the whole reason
    the message is worth sending.
    """
    recipe_id = str(data.get("recipe_id") or "")
    reason = str(data.get("reason") or "").strip() or "ended"
    duration_s = float(data.get("duration_s") or 0)
    analysis = data.get("analysis") or {}

    head, detail = _split_reason(reason)
    normal = head.lower() in _NORMAL_END

    fields = [f"{'Ended' if normal else '⚠ Stopped early'}: {head}"]
    if detail:
        fields.append(f"File: {detail}")
    fields.append(f"Ran: {_format_duration(duration_s)}")
    for key in ("size", "pdi", "confidence", "loss"):
        if analysis.get(key) is not None:
            fields.append(f"{key}: {_fmt(analysis[key])}")

    if recipe_id:
        title = (f"Run Complete — {recipe_id}" if normal
                 else f"Run Stopped Early — {recipe_id}")
    else:
        title = "Run Complete" if normal else "Run Stopped Early"
    return title, "\n".join(fields), "info"


def format_reactor_estop(data: dict) -> tuple[str, str, str]:
    """Translate reactor.estop event to (title, text, level)."""
    recipe_id = str(data.get("recipe_id") or "")
    failed = data.get("failed_to_idle") or []

    if failed:
        detail = f"Pumps that did NOT idle: {', '.join(failed)}\nCheck them immediately"
    else:
        detail = "All pumps idle"

    title = f"EMERGENCY STOP — {recipe_id}" if recipe_id else "EMERGENCY STOP"
    return title, detail, "fault"


def format_reactor_safety(data: dict) -> tuple[str, str, str]:
    """Translate reactor.safety event to (title, text, level)."""
    recipe_id = str(data.get("recipe_id") or "")
    check = str(data.get("check", "fault"))
    detail = str(data.get("detail") or "")

    title = f"SAFETY: {check}"
    if recipe_id:
        title = f"{title} — {recipe_id}"
    return title, detail, "fault"


def format_reactor_backend(data: dict) -> tuple[str, str, str]:
    """Translate reactor.backend event to (title, text, level)."""
    backend = str(data.get("backend") or "?")
    return "Backend", f"Switched to {backend}", "info"


def format_fit_complete(data: dict) -> tuple[str, str, str]:
    """Translate fit.complete event to (title, text, level)."""
    recipe_id = str(data.get("recipe_id") or "")
    confidence = data.get("confidence")
    suspect = bool(data.get("suspect"))
    plot_png = str(data.get("plot_png") or "")
    file = str(data.get("file") or "")

    fields = []
    for label, key in (("size (nm)", "size"), ("PDI", "pdi"),
                       ("confidence", "confidence"), ("loss", "loss")):
        if data.get(key) is not None:
            fields.append(f"{label}: {_fmt(data[key])}")
    if file:
        fields.append(f"file: {file}")

    if suspect:
        title = f"Fit LOW CONFIDENCE — {recipe_id or file}"
    else:
        title = f"Fit Result — {recipe_id or file}"

    text = "\n".join(fields)
    if suspect and plot_png:
        text += f"\nPlot: {plot_png}"

    level = "fault" if suspect else "info"
    return title, text, level


def format_average_skipped(data: dict) -> tuple[str, str, str]:
    """Translate average.skipped — a full batch consumed with no average out.

    This is terminal and was completely silent: the average app dropped ten
    frames, the condition could never produce a subtracted profile, and an
    operator away from the beamline heard nothing. Run20 lost r006 that way and
    the campaign kept going as though nothing had happened.

    A fault, not progress. It is not recoverable by waiting — the frames are
    consumed — so it is the kind of thing worth waking someone for.
    """
    kw = str(data.get("keyword") or "?")
    det = str(data.get("detector") or "?").upper()
    n = data.get("n_files")
    reason = str(data.get("reason") or "no reason recorded")
    text = (f"{n or '?'} frames for {kw} [{det}] were consumed without "
            f"producing an average.\nReason: {reason}\n"
            f"These frames will not be retried, so this condition will never "
            f"produce a subtracted profile.")
    return f"Averaging DROPPED a batch — {kw}", text, "fault"


def format_file_skipped(data: dict) -> tuple[str, str, str]:
    """Translate file.skipped — reduction gave up on a frame for good.

    Also silent until now. One skipped frame means the average gate for that
    lane can never fill, which the watchdog already understands well enough to
    diagnose (diagnose.py Pattern G) but never actually told anyone about
    unless a stall check happened to fire later.
    """
    path = str(data.get("file_path") or "")
    name = path.replace("\\", "/").rsplit("/", 1)[-1] or "?"
    kw = str(data.get("keyword") or "?")
    det = str(data.get("detector") or "?").upper()
    n = data.get("n_failures")
    text = (f"{name} [{det}] failed {n or '?'}× and will not be retried.\n"
            f"The average gate for {kw} is now one frame short and cannot fill "
            f"on its own.")
    # Deliberately info, not fault. One bad CSV can make reduction give up on
    # frame after frame, and faults bypass the transport's throttle entirely —
    # a per-frame fault would push a burst at Slack, get rate-limited, and take
    # genuinely urgent messages down with it. The CONSEQUENCE of these skips
    # (the batch being dropped, the stage stalling) is reported as a fault by
    # average.skipped and by the stall diagnosis; the individual frame is news,
    # not an emergency.
    return f"Frame permanently skipped — {kw}", text, "info"


def format_reactor_vent(data: dict) -> tuple[str, str, str]:
    """Translate reactor.vent. Published since the controller was written and
    never formatted, so venting — which may follow an E-stop — was invisible."""
    latched = bool(data.get("estop_latched"))
    text = ("Vented while an E-stop was latched." if latched
            else "All lines vented.")
    return "Reactor vented", text, ("fault" if latched else "info")


#: Events whose message must go out even if formatting it fails. These are the
#: reactor's safety events; a formatting bug must never be the reason an
#: operator is not told the rig tripped.
#: Not reactor.vent. It is routed to the "safety" CATEGORY (so quiet hours and
#: the category toggles cannot suppress a vent that follows an E-stop), but
#: NEVER_DROP means something stricter: on a formatter failure, send the raw
#: payload as a FAULT rather than nothing. A routine vent is not a fault, and
#: promoting every one to fault-level to satisfy that rule would page the
#: operator for normal operation.
NEVER_DROP = ("reactor.estop", "reactor.safety")


def _degraded(event_type: str, data: dict, exc: Exception) -> tuple[str, str, str]:
    """A crude but complete fault message for a safety event whose formatter
    raised — the raw payload beats silence."""
    try:
        raw = repr(data)[:600]
    except Exception:
        raw = "<unrepresentable payload>"
    title = f"SAFETY EVENT — {event_type} (message could not be formatted)"
    text = (f"Auto Watch could not format this event ({type(exc).__name__}: {exc}). "
            f"Raw payload below — treat it as a fault and check the reactor.\n\n{raw}")
    return title, text, "fault"


def event_to_message(event_type: str, data: dict) -> tuple[str, str, str] | None:
    """
    Convert a bus event to (title, text, level) or None if not a notification event.

    Handles:
    - reactor.run_start, reactor.run_complete, reactor.estop, reactor.safety, reactor.backend
    - fit.complete

    Returns None for events that should not generate a notification — but NEVER
    for an event in NEVER_DROP. Swallowing a formatter exception into None meant
    a `reactor.estop` whose payload had an unexpected shape (say `failed_to_idle`
    arriving as a string, so `", ".join(...)` raised) produced no Slack message
    at all: the single most important message the platform sends, lost to a
    formatting bug. Those now degrade to the raw payload instead.
    """
    formatters = {
        "reactor.run_start": format_reactor_run_start,
        "reactor.run_complete": format_reactor_run_complete,
        "reactor.estop": format_reactor_estop,
        "reactor.safety": format_reactor_safety,
        "reactor.backend": format_reactor_backend,
        "reactor.vent": format_reactor_vent,
        "fit.complete": format_fit_complete,
        # Pipeline failures. Both are terminal for the condition they hit, and
        # both were published for months with no formatter, so event_to_message
        # returned None and nothing was ever sent.
        "average.skipped": format_average_skipped,
        "file.skipped": format_file_skipped,
    }
    formatter = formatters.get(event_type)
    if formatter:
        try:
            out = formatter(data if isinstance(data, dict) else {})
        except Exception as exc:
            return _degraded(event_type, data, exc) if event_type in NEVER_DROP else None
        # A formatter that returns something unusable is the same failure.
        if not (isinstance(out, tuple) and len(out) == 3):
            if event_type in NEVER_DROP:
                return _degraded(event_type, data, ValueError("formatter returned "
                                                               f"{type(out).__name__}"))
            return None
        return out
    return None
