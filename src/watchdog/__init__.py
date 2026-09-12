"""
src/watchdog — Platform liveness monitoring and notifications.

Auto Watch is the sole publisher of notifications to Slack. It subscribes to the
event bus, translates platform events into messages, applies notification policy
(quiet hours, snooze, throttle), and sends via a webhook to Slack's Workflow
Builder trigger.

Unlike the reactor's old notification layer, Auto Watch has no I/O dependencies on
Flask app startup and degrades gracefully when the webhook is unset or unreachable.
"""
