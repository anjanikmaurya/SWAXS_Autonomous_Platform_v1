"""
src/notify — outbound notifications for unattended (autonomous) operation.

Currently Slack. Everything here is fire-and-forget: a notification failure can
never delay or break a run.
"""
from __future__ import annotations

from .slack import (SlackNotifier, ALERT, PROGRESS, SESSION,
                    ENV_TOKEN, ENV_WEBHOOK)

__all__ = ["SlackNotifier", "ALERT", "PROGRESS", "SESSION",
           "ENV_TOKEN", "ENV_WEBHOOK"]
