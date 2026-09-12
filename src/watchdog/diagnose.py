"""
src/watchdog/diagnose.py — Translate stall detection + probes into diagnostic messages.

Pure: given the overdue stage and probe results, return a message explaining
what's dead and what the fix might be.
"""
from __future__ import annotations


def diagnose_stall(
    stage: str,
    overdue_s: float,
    probes: dict,
) -> tuple[str, str]:
    """
    Given a stalled stage and probe results, return (title, text) with
    a diagnostic message.

    Parameters:
        stage: Which stage is overdue ("collect", "reduce", "average", "subtract", "fit")
        overdue_s: How long it's been stuck (seconds)
        probes: Result dict from probes.probe_all()

    Returns:
        (title, text) suitable for a fault-level Slack message.
    """
    elapsed_min = int(overdue_s / 60)

    # Map stage (the event we're waiting for) to the app that produces it
    # stage="reduce" means we're waiting for file.reduced (produced by reduction app)
    # stage="average" means we're waiting for file.averaged (produced by average app)
    # etc.
    stage_producers = {
        "reduce": ("reduction", 5102, "2D → 1D reduction"),
        "average": ("average", 5103, "averaging & stitching"),
        "subtract": ("background", 5104, "background subtraction"),
        "fit": ("analyzer", 5107, "auto-fit & size measurement"),
    }

    if stage not in stage_producers:
        return "Stalled", f"Stalled at unknown stage: {stage}"

    producer_app, producer_port, producer_desc = stage_producers[stage]
    monitors = probes.get("monitors", {})
    is_monitoring = monitors.get(producer_app, False)

    title = f"Stalled — waiting for {producer_desc}"

    if not is_monitoring:
        text = (
            f"{producer_app.title()} is not monitoring (port :{producer_port})\n"
            f"No events for {elapsed_min}+ minutes\n"
            f"Check: Is the app running? Is its watch folder configured?"
        )
    else:
        text = (
            f"{producer_app.title()} is running but has not produced output\n"
            f"No events for {elapsed_min}+ minutes\n"
            f"Check: Input files in the pipeline, error logs in the app"
        )

    return title, text
