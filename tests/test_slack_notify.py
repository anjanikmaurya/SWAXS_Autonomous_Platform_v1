"""
tests/test_slack_notify.py — Slack notifications must be useful AND harmless.

The hard requirement is isolation: a Slack outage, a bad token, a hung socket or
a rate limit must never delay or break a run. Everything else (tiers, threading,
formatting) is secondary.

No network is touched — the transport function is monkeypatched.
"""
from __future__ import annotations

import time

import pytest

from src.notify import slack as S
from src.notify import SlackNotifier, ALERT, PROGRESS, SESSION


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(S.ENV_TOKEN, raising=False)
    monkeypatch.delenv(S.ENV_WEBHOOK, raising=False)


def _bot(monkeypatch, sent, **cfg):
    monkeypatch.setenv(S.ENV_TOKEN, "xoxb-test")
    monkeypatch.setattr(S, "_post",
                        lambda url, payload, token="", timeout=6.0:
                        (sent.append({"url": url, **payload}), {"ok": True, "ts": "111.2"})[1])
    base = {"enabled": True, "channel": "#c", "min_interval_s": 0.0}
    base.update(cfg)
    return SlackNotifier(base)


def _drain(n=None, timeout=2.0):
    time.sleep(0.25)          # let the worker thread flush


# ── disabled / unconfigured ───────────────────────────────────────────────────
def test_disabled_by_default():
    assert SlackNotifier({}).enabled is False


def test_enabled_without_credentials_turns_itself_off():
    logs = []
    n = SlackNotifier({"enabled": True}, log=lambda m, t="info": logs.append((t, m)))
    assert n.enabled is False
    assert any("neither" in m for _, m in logs), logs


def test_notify_on_a_disabled_notifier_is_a_noop():
    SlackNotifier({}).notify("hello")          # must not raise


# ── isolation: Slack failures cannot reach the caller ─────────────────────────
def test_transport_exception_never_propagates(monkeypatch):
    monkeypatch.setenv(S.ENV_TOKEN, "xoxb-test")

    def boom(*a, **k):
        raise ConnectionError("slack is down")

    monkeypatch.setattr(S, "_post", boom)
    n = SlackNotifier({"enabled": True, "channel": "#c", "min_interval_s": 0.0})
    n.notify("x", tier=ALERT)                  # must not raise
    _drain()
    assert n._worker.is_alive(), "worker died on a transport error"
    n.notify("y", tier=ALERT)                  # still accepting work
    _drain()
    n.close()


def test_notify_returns_immediately_even_if_slack_hangs(monkeypatch):
    """The caller is the control loop's event callback — it must not block."""
    monkeypatch.setenv(S.ENV_TOKEN, "xoxb-test")
    monkeypatch.setattr(S, "_post",
                        lambda *a, **k: (time.sleep(5.0), {"ok": True})[1])
    n = SlackNotifier({"enabled": True, "channel": "#c", "min_interval_s": 0.0})
    t0 = time.time()
    for i in range(5):
        n.notify(f"m{i}")
    assert time.time() - t0 < 0.2, "notify() blocked on the network"
    n.close(timeout=0.1)


def test_a_full_queue_drops_rather_than_blocks(monkeypatch):
    monkeypatch.setenv(S.ENV_TOKEN, "xoxb-test")
    monkeypatch.setattr(S, "_post",
                        lambda *a, **k: (time.sleep(0.5), {"ok": True})[1])
    n = SlackNotifier({"enabled": True, "channel": "#c", "min_interval_s": 0.0})
    t0 = time.time()
    for i in range(400):                        # queue maxsize is 200
        n.notify(f"m{i}")
    assert time.time() - t0 < 1.0, "notify() blocked when the queue filled"
    n.close(timeout=0.1)


def test_slack_rejection_is_logged_not_raised(monkeypatch):
    monkeypatch.setenv(S.ENV_TOKEN, "xoxb-bad")
    monkeypatch.setattr(S, "_post",
                        lambda *a, **k: {"ok": False, "error": "invalid_auth"})
    logs = []
    n = SlackNotifier({"enabled": True, "channel": "#c", "min_interval_s": 0.0},
                      log=lambda m, t="info": logs.append((t, m)))
    n.notify("x")
    _drain()
    assert any("invalid_auth" in m for _, m in logs), logs
    n.close()


# ── transport selection ───────────────────────────────────────────────────────
def test_bot_token_preferred_over_webhook(monkeypatch):
    monkeypatch.setenv(S.ENV_TOKEN, "xoxb-t")
    monkeypatch.setenv(S.ENV_WEBHOOK, "https://hooks.slack.com/x")
    n = SlackNotifier({"enabled": True, "channel": "#c"})
    assert n.mode == "bot"
    n.close()


def test_webhook_mode_disables_threading(monkeypatch):
    monkeypatch.setenv(S.ENV_WEBHOOK, "https://hooks.slack.com/x")
    logs = []
    n = SlackNotifier({"enabled": True, "thread_per_recipe": True},
                      log=lambda m, t="info": logs.append((t, m)))
    assert n.mode == "webhook" and n.thread_per_recipe is False
    assert any("cannot thread" in m for _, m in logs)
    n.close()


def test_webhook_posts_to_the_hook_url(monkeypatch):
    sent = []
    monkeypatch.setenv(S.ENV_WEBHOOK, "https://hooks.slack.com/HOOK")
    monkeypatch.setattr(S, "_post",
                        lambda url, payload, token="", timeout=6.0:
                        (sent.append({"url": url, **payload}), {"ok": True})[1])
    n = SlackNotifier({"enabled": True, "min_interval_s": 0.0})
    n.notify("hello")
    _drain()
    n.close()
    assert sent and sent[0]["url"].endswith("/HOOK")
    assert "hello" in sent[0]["text"]


# ── tiers ─────────────────────────────────────────────────────────────────────
def test_only_configured_tiers_are_sent(monkeypatch):
    sent = []
    n = _bot(monkeypatch, sent, tiers=[ALERT])
    n.notify("a", tier=ALERT)
    n.notify("p", tier=PROGRESS)
    n.notify("s", tier=SESSION)
    _drain()
    n.close()
    texts = " ".join(m["text"] for m in sent)
    assert "a" in texts and "p" not in texts.replace("swaxs", "")


def test_mention_is_added_only_to_alerts(monkeypatch):
    sent = []
    n = _bot(monkeypatch, sent, mention_on_alert="<!channel>")
    n.notify("boom", tier=ALERT)
    n.notify("fine", tier=PROGRESS)
    _drain()
    n.close()
    by_text = {m["text"].split()[-1]: m["text"] for m in sent}
    assert any("<!channel>" in t for t in by_text.values())
    assert not any("<!channel>" in t for t in by_text.values() if "fine" in t)


# ── threading per recipe ──────────────────────────────────────────────────────
def test_updates_are_threaded_under_the_recipe_parent(monkeypatch):
    sent = []
    n = _bot(monkeypatch, sent)
    n.recipe_applied("r1", {"T_reac": 240, "F_tot": 80, "x_TOP": 0.15}, 600.0)
    _drain()
    n.run_complete("r1", "duration elapsed", 601.0,
                   result={"size": 4.1, "pdi": 0.02, "loss": 0.11})
    _drain()
    n.close()
    assert len(sent) == 2, sent
    assert "thread_ts" not in sent[0], "the parent must not be threaded"
    assert sent[1].get("thread_ts") == "111.2", "update was not threaded"


def test_separate_recipes_get_separate_threads(monkeypatch):
    sent = []
    n = _bot(monkeypatch, sent)
    n.recipe_applied("r1", {"T_reac": 240}, 10.0)
    _drain()
    n.recipe_applied("r2", {"T_reac": 250}, 10.0)
    _drain()
    n.run_complete("r1", "done", 10.0)
    _drain()
    n.close()
    assert n._threads.keys() == {"r1", "r2"}


# ── message content ───────────────────────────────────────────────────────────
def test_recipe_message_carries_the_conditions(monkeypatch):
    sent = []
    n = _bot(monkeypatch, sent)
    n.recipe_applied("r7", {"T_reac": 243.5, "F_tot": 80, "x_TOP": 0.15,
                            "x_ODE": 0.2, "x_oley": 0.1, "irrelevant": 9}, 600.0)
    _drain()
    n.close()
    t = sent[0]["text"]
    assert "r7" in t and "T_reac" in t and "243.5" in t
    assert "irrelevant" not in t, "unrelated recipe keys should be filtered out"
    assert "10m 0s" in t, "run duration not humanised"


def test_run_complete_reports_the_fitted_result(monkeypatch):
    sent = []
    n = _bot(monkeypatch, sent)
    n.run_complete("r1", "duration elapsed", 600.0,
                   result={"size": 4.123456, "pdi": 0.0234, "confidence": 0.91,
                           "loss": 0.1149})
    _drain()
    n.close()
    t = sent[0]["text"]
    assert "4.123" in t and "0.0234" in t and "0.91" in t
    assert "duration elapsed" in t


def test_fault_message_is_an_alert_with_the_detail(monkeypatch):
    sent = []
    n = _bot(monkeypatch, sent)
    n.fault("SAFETY: over-temperature", "331.2°C > T_max 320°C")
    _drain()
    n.close()
    assert "over-temperature" in sent[0]["text"] and "331.2" in sent[0]["text"]


def test_duration_formatting():
    assert S._dur(45) == "45s"
    assert S._dur(600) == "10m 0s"
    assert S._dur(3720) == "1h 2m"


# ── secrets ───────────────────────────────────────────────────────────────────
def test_credentials_are_read_from_env_not_config(monkeypatch):
    """A token in config.yml would end up in git."""
    n = SlackNotifier({"enabled": True, "bot_token": "xoxb-in-config",
                       "webhook": "https://hooks.slack.com/in-config"})
    assert n.enabled is False, "credentials in config must NOT enable Slack"
    assert n.token == "" and n.webhook == ""


def test_token_is_not_included_in_the_message_payload(monkeypatch):
    sent = []
    n = _bot(monkeypatch, sent)
    n.notify("hello", tier=PROGRESS)
    _drain()
    n.close()
    assert "xoxb" not in str(sent), "token leaked into the payload"
