"""
tests/test_email_notify.py — email notifications (the no-approval-needed channel).

Same hard requirement as Slack: a dead mail server must never delay or break a
run. The send path is verified against a real in-process SMTP server so the
message content and threading headers are checked end to end.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from src.notify import email_notify as E
from src.notify import EmailNotifier, MultiNotifier, ALERT, PROGRESS, SESSION

ENVS = (E.ENV_HOST, E.ENV_PORT, E.ENV_USER, E.ENV_PASS, E.ENV_FROM, E.ENV_TO)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ENVS:
        monkeypatch.delenv(v, raising=False)


# ── a minimal SMTP server that records what it receives ──────────────────────
class CaptureSMTP(threading.Thread):
    """Speaks just enough SMTP to accept one message per connection."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages: list[str] = []
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                self.sock.settimeout(0.5)
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                self._serve(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _serve(self, conn):
        conn.settimeout(3.0)
        f = conn.makefile("rwb")
        f.write(b"220 test ESMTP\r\n"); f.flush()
        data_mode, body = False, []
        while True:
            line = f.readline()
            if not line:
                return
            if data_mode:
                if line.strip() == b".":
                    self.messages.append(b"".join(body).decode(errors="replace"))
                    f.write(b"250 OK\r\n"); f.flush()
                    data_mode, body = False, []
                    continue
                body.append(line)
                continue
            cmd = line.decode(errors="replace").strip().upper()
            if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                f.write(b"250-test\r\n250 SIZE 10240000\r\n")
            elif cmd.startswith(("MAIL", "RCPT", "NOOP", "RSET")):
                f.write(b"250 OK\r\n")
            elif cmd.startswith("DATA"):
                f.write(b"354 send it\r\n"); data_mode = True
            elif cmd.startswith("QUIT"):
                f.write(b"221 bye\r\n"); f.flush(); return
            else:
                f.write(b"250 OK\r\n")
            f.flush()

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass


@pytest.fixture
def smtp():
    s = CaptureSMTP(); s.start()
    yield s
    s.stop()


def _notifier(monkeypatch, smtp, **cfg):
    monkeypatch.setenv(E.ENV_HOST, "127.0.0.1")
    monkeypatch.setenv(E.ENV_PORT, str(smtp.port))
    monkeypatch.setenv(E.ENV_TO, "beamline@example.edu")
    monkeypatch.setenv(E.ENV_FROM, "swaxs@example.edu")
    base = {"enabled": True, "min_interval_s": 0.0, "mode": "plain"}
    base.update(cfg)
    return EmailNotifier(base)


def _wait(smtp, n=1, timeout=4.0):
    t0 = time.time()
    while len(smtp.messages) < n and time.time() - t0 < timeout:
        time.sleep(0.05)
    return smtp.messages


# ── configuration ─────────────────────────────────────────────────────────────
def test_disabled_without_host_or_recipients():
    assert EmailNotifier({"enabled": True}).enabled is False


def test_reports_what_is_missing(monkeypatch):
    logs = []
    monkeypatch.setenv(E.ENV_HOST, "smtp.example.edu")     # but no recipients
    n = EmailNotifier({"enabled": True}, log=lambda m, t="info": logs.append(m))
    assert n.enabled is False
    assert any(E.ENV_TO in m for m in logs), logs


def test_mode_and_port_are_inferred(monkeypatch):
    monkeypatch.setenv(E.ENV_HOST, "smtp.example.edu")
    monkeypatch.setenv(E.ENV_TO, "a@b.edu")
    plain = EmailNotifier({})
    assert (plain.mode, plain.port) == ("plain", 25), "no auth → plain relay"
    monkeypatch.setenv(E.ENV_USER, "u")
    monkeypatch.setenv(E.ENV_PASS, "p")
    auth = EmailNotifier({})
    assert (auth.mode, auth.port) == ("starttls", 587), "auth → submission port"


def test_explicit_mode_wins(monkeypatch):
    monkeypatch.setenv(E.ENV_HOST, "h"); monkeypatch.setenv(E.ENV_TO, "a@b.edu")
    n = EmailNotifier({"mode": "ssl"})
    assert n.mode == "ssl" and n.port == 465


def test_multiple_recipients_are_parsed(monkeypatch):
    monkeypatch.setenv(E.ENV_HOST, "h")
    monkeypatch.setenv(E.ENV_TO, "a@b.edu, c@d.edu ,")
    assert EmailNotifier({}).to == ["a@b.edu", "c@d.edu"]


# ── the send path, end to end ─────────────────────────────────────────────────
def test_message_is_delivered_with_readable_subject_and_body(monkeypatch, smtp):
    n = _notifier(monkeypatch, smtp)
    n.recipe_applied("r001", {"T_reac": 243.5, "F_tot": 80, "x_TOP": 0.15}, 600.0)
    msgs = _wait(smtp)
    n.close()
    assert msgs, "no message reached the SMTP server"
    m = msgs[0]
    assert "Subject: [SWAXS] Recipe applied" in m
    assert "T_reac" in m and "243.5" in m
    assert "10m 0s" in m, "duration not humanised"
    assert "recipe: r001" in m


def test_slack_markup_is_stripped_for_email(monkeypatch, smtp):
    n = _notifier(monkeypatch, smtp)
    n.notify(":rotating_light: *EMERGENCY STOP* — `r001`", tier=ALERT)
    m = _wait(smtp)[0]
    n.close()
    assert "*" not in m.split("Subject:")[1].splitlines()[0]
    assert ":rotating_light:" not in m
    assert "EMERGENCY STOP" in m


def test_alert_tier_is_marked_in_the_subject(monkeypatch, smtp):
    n = _notifier(monkeypatch, smtp)
    n.fault("SAFETY: over-temperature", "331.2°C > 320°C")
    m = _wait(smtp)[0]
    n.close()
    assert "[ALERT]" in m and "over-temperature" in m


def test_messages_for_one_recipe_share_a_mail_thread(monkeypatch, smtp):
    """References/In-Reply-To group a recipe's mails, mirroring Slack threading."""
    n = _notifier(monkeypatch, smtp)
    n.recipe_applied("r001", {"T_reac": 240}, 60.0)
    n.run_complete("r001", "duration elapsed", 61.0, result={"size": 4.1})
    msgs = _wait(smtp, 2)
    n.close()
    assert len(msgs) >= 2
    refs = [l for m in msgs for l in m.splitlines() if l.startswith("References:")]
    assert len(refs) >= 2 and refs[0] == refs[1], refs


def test_a_plot_can_be_attached(monkeypatch, smtp, tmp_path):
    png = tmp_path / "qc.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    n = _notifier(monkeypatch, smtp)
    n.upload_png(str(png), "r002 I(q) + fit", recipe_id="r002",
                 comment="low-confidence fit")
    m = _wait(smtp)[0]
    n.close()
    assert "qc.png" in m and "image/png" in m


# ── isolation: a broken mail server must not reach the caller ────────────────
def test_unreachable_server_never_raises(monkeypatch):
    monkeypatch.setenv(E.ENV_HOST, "127.0.0.1")
    monkeypatch.setenv(E.ENV_PORT, "9")            # discard port, nothing listens
    monkeypatch.setenv(E.ENV_TO, "a@b.edu")
    n = EmailNotifier({"enabled": True, "min_interval_s": 0.0, "mode": "plain",
                       "timeout_s": 1.0})
    n.notify("x", tier=ALERT)                      # must not raise
    time.sleep(1.5)
    assert n._worker.is_alive(), "worker died on a connection error"
    n.close()


def test_notify_does_not_block_on_a_slow_server(monkeypatch, smtp):
    n = _notifier(monkeypatch, smtp, timeout_s=1.0)
    t0 = time.time()
    for i in range(6):
        n.notify(f"m{i}")
    assert time.time() - t0 < 0.2, "notify() blocked on the network"
    n.close(timeout=0.5)


def test_tiers_filter_email_too(monkeypatch, smtp):
    n = _notifier(monkeypatch, smtp, tiers=[ALERT])
    n.notify("progress msg", tier=PROGRESS)
    n.notify("alert msg", tier=ALERT)
    msgs = _wait(smtp, 1)
    time.sleep(0.4)
    n.close()
    joined = "\n".join(msgs)
    assert "alert msg" in joined and "progress msg" not in joined


# ── runtime arm/disarm ────────────────────────────────────────────────────────
def test_email_can_be_armed_at_runtime(monkeypatch, smtp):
    n = _notifier(monkeypatch, smtp, enabled=False)
    assert n.configured and not n.enabled
    n.notify("dropped")
    time.sleep(0.3)
    assert smtp.messages == []
    ok, msg = n.enable()
    assert ok and "beamline@example.edu" in msg
    n.notify("sent now")
    assert _wait(smtp), "nothing sent after arming"
    n.close()


# ── probe ─────────────────────────────────────────────────────────────────────
def test_probe_finds_a_working_option(smtp, monkeypatch):
    monkeypatch.setattr(E, "PROBE_CANDIDATES", [(smtp.port, "plain")])
    rows = E.probe("127.0.0.1", timeout=3.0)
    assert any(r["ok"] for r in rows), rows


def test_probe_reports_failures_without_raising(monkeypatch):
    monkeypatch.setattr(E, "PROBE_CANDIDATES", [(9, "plain")])
    rows = E.probe("127.0.0.1", timeout=1.0)
    assert rows and not rows[0]["ok"] and rows[0]["error"]


# ── the multiplexer ───────────────────────────────────────────────────────────
def test_multi_reports_configured_when_only_email_is_available(monkeypatch, smtp):
    monkeypatch.setenv(E.ENV_HOST, "127.0.0.1")
    monkeypatch.setenv(E.ENV_PORT, str(smtp.port))
    monkeypatch.setenv(E.ENV_TO, "a@b.edu")
    m = MultiNotifier({"email": {"mode": "plain", "min_interval_s": 0.0}})
    assert m.configured is True, "email alone should make the button usable"
    assert m.slack.configured is False
    ok, msg = m.enable()
    assert ok and "email" in msg
    m.notify("via multi", tier=SESSION)
    assert _wait(smtp), "MultiNotifier did not deliver over email"
    m.close()


def test_multi_enable_fails_clearly_with_no_channels():
    m = MultiNotifier({})
    ok, msg = m.enable()
    assert not ok and "no channel is configured" in msg
    m.close()


def test_multi_one_broken_channel_does_not_stop_the_other(monkeypatch, smtp):
    """A Slack outage must not suppress the email, and vice versa."""
    monkeypatch.setenv(E.ENV_HOST, "127.0.0.1")
    monkeypatch.setenv(E.ENV_PORT, str(smtp.port))
    monkeypatch.setenv(E.ENV_TO, "a@b.edu")
    m = MultiNotifier({"email": {"mode": "plain", "min_interval_s": 0.0}})
    m.enable()
    # a channel whose every method explodes
    class Broken:
        configured = True
        enabled = True
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("channel is down")
            return boom
    m.slack = Broken()
    m.notify("still delivered", tier=SESSION)     # must not raise
    assert _wait(smtp), "email suppressed by the broken channel"
    m.email.close()


def test_multi_status_exposes_both_channels(monkeypatch, smtp):
    monkeypatch.setenv(E.ENV_HOST, "127.0.0.1")
    monkeypatch.setenv(E.ENV_PORT, str(smtp.port))
    monkeypatch.setenv(E.ENV_TO, "a@b.edu")
    m = MultiNotifier({"email": {"mode": "plain"}})
    st = m.status()
    assert set(("enabled", "configured", "mode", "slack", "email")) <= set(st)
    assert st["email"]["configured"] is True
    m.close()
