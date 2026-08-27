"""
src/notify — outbound notifications for unattended (autonomous) operation.

Two transports, identical surfaces, fanned out by MultiNotifier:
  • Slack  — needs a workspace-app credential (often admin-approved)
  • email  — needs no permission; can also deliver INTO Slack via a
             channel-email address (#channel → Integrations → Send emails here)

Everything here is fire-and-forget: a notification failure can never delay or
break a run.
"""
from __future__ import annotations

from .slack import (SlackNotifier, ALERT, PROGRESS, SESSION,
                    ENV_TOKEN, ENV_WEBHOOK)
from .email_notify import (EmailNotifier, probe as smtp_probe,
                           ENV_HOST, ENV_PORT, ENV_USER, ENV_PASS,
                           ENV_FROM, ENV_TO)
from .multi import MultiNotifier

__all__ = ["SlackNotifier", "EmailNotifier", "MultiNotifier", "smtp_probe",
           "ALERT", "PROGRESS", "SESSION",
           "ENV_TOKEN", "ENV_WEBHOOK",
           "ENV_HOST", "ENV_PORT", "ENV_USER", "ENV_PASS", "ENV_FROM", "ENV_TO"]
