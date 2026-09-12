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


def format_reactor_run_complete(data: dict) -> tuple[str, str, str]:
    """Translate reactor.run_complete event to (title, text, level)."""
    recipe_id = str(data.get("recipe_id") or "")
    reason = str(data.get("reason") or "?")
    duration_s = float(data.get("duration_s") or 0)
    analysis = data.get("analysis") or {}

    fields = [f"Stopped by: {reason}", f"Ran: {_format_duration(duration_s)}"]
    for key in ("size", "pdi", "confidence", "loss"):
        if analysis.get(key) is not None:
            fields.append(f"{key}: {_fmt(analysis[key])}")

    title = f"Run Complete — {recipe_id}" if recipe_id else "Run Complete"
    text = "\n".join(fields)
    return title, text, "info"


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


def event_to_message(event_type: str, data: dict) -> tuple[str, str, str] | None:
    """
    Convert a bus event to (title, text, level) or None if not a notification event.

    Handles:
    - reactor.run_start, reactor.run_complete, reactor.estop, reactor.safety, reactor.backend
    - fit.complete

    Returns None for events that should not generate a notification.
    """
    formatters = {
        "reactor.run_start": format_reactor_run_start,
        "reactor.run_complete": format_reactor_run_complete,
        "reactor.estop": format_reactor_estop,
        "reactor.safety": format_reactor_safety,
        "reactor.backend": format_reactor_backend,
        "fit.complete": format_fit_complete,
    }
    formatter = formatters.get(event_type)
    if formatter:
        try:
            return formatter(data)
        except Exception:
            return None
    return None
