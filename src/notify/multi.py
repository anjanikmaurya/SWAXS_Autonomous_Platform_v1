"""
src/notify/multi.py — fan one notification out to every configured channel.

The app holds ONE object and doesn't care how many transports exist. Slack and
email have deliberately identical surfaces, so this just forwards and collects.

Failure isolation is preserved: each forward is individually guarded, so a broken
transport cannot stop the others, and nothing raises into the caller.
"""
from __future__ import annotations

import logging

from .slack import SlackNotifier, ALERT, PROGRESS, SESSION
from .email_notify import EmailNotifier

logger = logging.getLogger(__name__)

_FORWARD = ("notify", "session_start", "session_end", "recipe_applied",
            "run_complete", "fault", "upload_png")


class MultiNotifier:
    """Slack + email behind one interface."""

    def __init__(self, cfg: dict | None = None, log=None):
        cfg = cfg or {}
        self.slack = SlackNotifier(cfg.get("slack", {}), log=log)
        self.email = EmailNotifier(cfg.get("email", {}), log=log)
        self._log = log or (lambda msg, tag="info": None)

    # ── channel set ───────────────────────────────────────────────────────────
    @property
    def channels(self) -> dict:
        return {"slack": self.slack, "email": self.email}

    @property
    def configured(self) -> bool:
        """True if ANY channel has credentials — i.e. the button can do something."""
        return any(c.configured for c in self.channels.values())

    @property
    def enabled(self) -> bool:
        return any(c.enabled for c in self.channels.values())

    @property
    def mode(self) -> str:
        """Human summary of the live transports, for the UI."""
        parts = []
        if self.slack.enabled:
            parts.append(f"slack:{self.slack.mode}")
        if self.email.enabled:
            parts.append("email")
        if parts:
            return " + ".join(parts)
        ready = [n for n, c in self.channels.items() if c.configured]
        return ("ready: " + ", ".join(ready)) if ready else ""

    # ── runtime on/off ────────────────────────────────────────────────────────
    def enable(self, which: str = "all") -> tuple[bool, str]:
        """Arm one channel or every configured one. Returns (ok, message)."""
        targets = (self.channels if which in ("all", "", None)
                   else {which: self.channels.get(which)})
        oks, msgs = [], []
        for name, ch in targets.items():
            if ch is None:
                return False, f"unknown channel '{which}'"
            if not ch.configured:
                continue                      # skip unconfigured, don't fail the lot
            ok, msg = ch.enable()
            oks.append(ok)
            msgs.append(f"{name}: {msg}")
        if not oks:
            return False, ("no channel is configured — set a Slack credential or "
                           "SWAXS_SMTP_HOST + SWAXS_NOTIFY_EMAIL in .env")
        return any(oks), "; ".join(msgs)

    def disable(self, which: str = "all") -> tuple[bool, str]:
        targets = (self.channels if which in ("all", "", None)
                   else {which: self.channels.get(which)})
        msgs = []
        for name, ch in targets.items():
            if ch is None:
                return False, f"unknown channel '{which}'"
            ch.disable()
            msgs.append(name)
        return True, f"notifications OFF ({', '.join(msgs)})"

    def status(self) -> dict:
        return {"enabled": self.enabled, "configured": self.configured,
                "mode": self.mode,
                "slack": self.slack.status(), "email": self.email.status()}

    def close(self) -> None:
        for ch in self.channels.values():
            try:
                ch.close()
            except Exception:
                pass


def _make_forwarder(name: str):
    def fwd(self, *a, **kw):
        for chan_name, ch in self.channels.items():
            try:
                getattr(ch, name)(*a, **kw)
            except Exception:
                # One broken transport must not stop the others, and must not
                # reach the caller (which is the reactor's event callback).
                logger.exception("%s.%s failed", chan_name, name)
    fwd.__name__ = name
    return fwd


for _n in _FORWARD:
    setattr(MultiNotifier, _n, _make_forwarder(_n))

__all__ = ["MultiNotifier", "ALERT", "PROGRESS", "SESSION"]
