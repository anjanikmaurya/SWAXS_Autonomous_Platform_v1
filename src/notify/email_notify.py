"""
src/notify/email_notify.py — email notifications for unattended runs.

Exists because installing a Slack app needs workspace-admin approval, which can
block for days. Email needs no permission, pushes to a phone, and can ALSO
deliver into Slack via a channel-email address
(``#channel → Settings → Integrations → Send emails to this channel``), so the
same transport covers both.

Same contract as the Slack notifier: tiered, fire-and-forget on a worker thread,
every error swallowed. A mail server being down must never disturb the reactor.

Transport is deliberately flexible because beamline hosts differ:
  • auth + STARTTLS (port 587) — typical institutional SMTP
  • auth + SSL (port 465)
  • no auth (port 25) — a local/site relay
``probe()`` reports which of these actually works from the machine, instead of
making you guess.

Credentials come from the environment, never config.yml:
    SWAXS_SMTP_HOST, SWAXS_SMTP_PORT, SWAXS_SMTP_USER, SWAXS_SMTP_PASSWORD,
    SWAXS_SMTP_FROM, SWAXS_NOTIFY_EMAIL   (comma-separated recipients)
"""
from __future__ import annotations

import logging
import os
import queue
import smtplib
import socket
import ssl
import threading
import time
from email.message import EmailMessage

logger = logging.getLogger(__name__)

ENV_HOST = "SWAXS_SMTP_HOST"
ENV_PORT = "SWAXS_SMTP_PORT"
ENV_USER = "SWAXS_SMTP_USER"
ENV_PASS = "SWAXS_SMTP_PASSWORD"
ENV_FROM = "SWAXS_SMTP_FROM"
ENV_TO = "SWAXS_NOTIFY_EMAIL"

ALERT, PROGRESS, SESSION = "alert", "progress", "session"

#: (port, mode) combinations tried by probe(), most likely first
PROBE_CANDIDATES = [
    (587, "starttls"),
    (465, "ssl"),
    (25, "plain"),
    (1025, "plain"),          # local dev/debug servers
]


def _connect(host: str, port: int, mode: str, user: str = "", password: str = "",
             timeout: float = 10.0):
    """Open an SMTP connection. ``mode``: starttls | ssl | plain."""
    if mode == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=timeout,
                             context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(host, port, timeout=timeout)
        s.ehlo()
        if mode == "starttls":
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
    if user and password:
        s.login(user, password)
    return s


def probe(host: str = "", timeout: float = 6.0) -> list[dict]:
    """Try each transport option and report what works from THIS machine.

    Returns one dict per attempt: {port, mode, host, ok, needs_auth, error}.
    Purely diagnostic — used by tools/notify_test.py --check.
    """
    user = os.environ.get(ENV_USER, "").strip()
    password = os.environ.get(ENV_PASS, "").strip()
    hosts = [h for h in (host, os.environ.get(ENV_HOST, "").strip(),
                         "localhost") if h]
    seen, results = set(), []
    for h in hosts:
        for port, mode in PROBE_CANDIDATES:
            key = (h, port, mode)
            if key in seen:
                continue
            seen.add(key)
            row = {"host": h, "port": port, "mode": mode, "ok": False,
                   "needs_auth": False, "error": ""}
            try:
                s = _connect(h, port, mode, timeout=timeout)   # no auth first
                try:
                    # can we actually send unauthenticated?
                    code, _ = s.docmd("NOOP")
                    row["ok"] = code in (250, 220)
                finally:
                    try:
                        s.quit()
                    except Exception:
                        pass
            except smtplib.SMTPAuthenticationError as exc:
                row["needs_auth"] = True
                row["error"] = str(exc)[:120]
            except (OSError, socket.timeout, smtplib.SMTPException) as exc:
                row["error"] = f"{exc.__class__.__name__}: {str(exc)[:100]}"
            if not row["ok"] and user and password and not row["error"].startswith("OSError"):
                # retry the same option WITH credentials
                try:
                    s = _connect(h, port, mode, user, password, timeout=timeout)
                    try:
                        s.quit()
                    except Exception:
                        pass
                    row.update(ok=True, needs_auth=True, error="")
                except Exception as exc:
                    row["error"] = f"{exc.__class__.__name__}: {str(exc)[:100]}"
            results.append(row)
    return results


class EmailNotifier:
    """Tiered email notifications that cannot break the reactor."""

    def __init__(self, cfg: dict | None = None, log=None):
        cfg = dict(cfg or {})
        self.enabled = bool(cfg.get("enabled", False))
        self.tiers = {str(t).lower() for t in (cfg.get("tiers")
                                               or [ALERT, PROGRESS, SESSION])}
        self.subject_prefix = str(cfg.get("subject_prefix", "[SWAXS]"))
        self.timeout = float(cfg.get("timeout_s", 15.0))
        self.min_interval_s = float(cfg.get("min_interval_s", 1.0))
        self.dry_run = bool(cfg.get("dry_run", False))
        self._log = log or (lambda msg, tag="info": None)

        self.host = os.environ.get(ENV_HOST, "").strip()
        self.port = int(os.environ.get(ENV_PORT, "0") or 0)
        self.user = os.environ.get(ENV_USER, "").strip()
        self.password = os.environ.get(ENV_PASS, "").strip()
        self.sender = (os.environ.get(ENV_FROM, "").strip()
                       or self.user or "swaxs-reactor@localhost")
        self.to = [a.strip() for a in os.environ.get(ENV_TO, "").split(",")
                   if a.strip()]
        # explicit mode, else inferred from the port
        self.mode = str(cfg.get("mode", "") or "").lower()
        if not self.mode:
            self.mode = {465: "ssl", 587: "starttls"}.get(self.port, "starttls"
                                                          if self.user else "plain")
        if not self.port:
            self.port = 465 if self.mode == "ssl" else (587 if self.user else 25)

        self.configured = bool(self.host and self.to)
        if self.enabled and not self.configured:
            missing = [n for n, v in ((ENV_HOST, self.host), (ENV_TO, self.to)) if not v]
            self._log(f"⚠ email notifications requested but {', '.join(missing)} "
                      f"not set — email is OFF", "warn")
            self.enabled = False

        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._alive = True
        self._last_send = 0.0
        self._worker = None
        if self.enabled:
            self._ensure_worker()

    # ── runtime on/off ────────────────────────────────────────────────────────
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._alive = True
            self._worker = threading.Thread(target=self._run, daemon=True,
                                            name="email-notifier")
            self._worker.start()

    def enable(self) -> tuple[bool, str]:
        if not self.configured:
            return False, (f"email not configured — set {ENV_HOST} and {ENV_TO} "
                           f"in .env and restart")
        self.enabled = True
        self._ensure_worker()
        return True, f"email notifications ON → {', '.join(self.to)}"

    def disable(self) -> tuple[bool, str]:
        self.enabled = False
        return True, "email notifications OFF"

    def status(self) -> dict:
        return {"enabled": self.enabled, "configured": self.configured,
                "host": self.host, "port": self.port, "mode": self.mode,
                "auth": bool(self.user), "to": list(self.to),
                "tiers": sorted(self.tiers), "queued": self._q.qsize()}

    # ── public API (never blocks, never raises) ───────────────────────────────
    def notify(self, text: str, *, tier: str = PROGRESS, recipe_id: str = "",
               start_thread: bool = False, fields: dict | None = None,
               attach: str = "") -> None:
        if not self.enabled or tier not in self.tiers:
            return
        try:
            self._q.put_nowait({"text": text, "tier": tier, "recipe_id": recipe_id,
                                "fields": fields or {}, "attach": attach})
        except queue.Full:
            logger.warning("email queue full — dropped a notification")

    def close(self, timeout: float = 3.0) -> None:
        self._alive = False
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        if self._worker is not None:
            self._worker.join(timeout=timeout)

    # ── worker ────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while self._alive:
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._send(item)
            except Exception:
                logger.exception("email send failed")

    def _throttle(self) -> None:
        gap = time.time() - self._last_send
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last_send = time.time()

    def _build(self, item: dict) -> EmailMessage:
        # strip Slack markup so the plain-text mail reads cleanly
        title = item["text"].replace("*", "").replace("`", "")
        for emoji in (":rotating_light:", ":test_tube:", ":white_check_mark:",
                      ":bar_chart:", ":mag:", ":arrow_forward:",
                      ":checkered_flag:", ":bell:", ":gear:", ":wave:"):
            title = title.replace(emoji, "").strip()
        first = title.splitlines()[0].strip()

        tag = {"alert": "ALERT", "session": "SESSION"}.get(item["tier"], "")
        subject = " ".join(x for x in (self.subject_prefix, f"[{tag}]" if tag else "",
                                       first) if x)

        body = [first]
        rest = title.splitlines()[1:]
        if rest:
            body += rest
        if item["fields"]:
            body.append("")
            width = max(len(str(k)) for k in item["fields"])
            for k, v in item["fields"].items():
                body.append(f"  {str(k).ljust(width)} : {v}")
        if item["recipe_id"]:
            body += ["", f"recipe: {item['recipe_id']}"]
        body += ["", "— SWAXS autonomous platform (SSRL BL1-5)"]

        msg = EmailMessage()
        msg["Subject"] = subject[:180]
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        # Group a recipe's messages into one mail thread, mirroring Slack threading.
        if item["recipe_id"]:
            ref = f"<swaxs-{item['recipe_id']}@swaxs.local>"
            msg["References"] = ref
            msg["In-Reply-To"] = ref
        msg.set_content("\n".join(body))

        att = item.get("attach") or ""
        if att:
            try:
                from pathlib import Path
                p = Path(att)
                if p.is_file():
                    msg.add_attachment(p.read_bytes(), maintype="image",
                                       subtype="png", filename=p.name)
            except Exception:
                logger.warning("could not attach %s", att)
        return msg

    def _send(self, item: dict) -> None:
        self._throttle()
        msg = self._build(item)
        if self.dry_run:
            print(f"\n[email → {msg['To']}]\n   Subject: {msg['Subject']}")
            for line in msg.get_content().splitlines():
                print(f"   {line}")
            return
        s = _connect(self.host, self.port, self.mode, self.user, self.password,
                     timeout=self.timeout)
        try:
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass

    # ── formatters (same surface as the Slack notifier) ───────────────────────
    def session_start(self, backend: str, project: str = "") -> None:
        self.notify(f"Autonomous session started (backend {backend})", tier=SESSION,
                    fields={"project": project} if project else None)

    def session_end(self, n_runs: int, best: dict | None = None,
                    elapsed_s: float = 0.0) -> None:
        f = {"runs completed": n_runs, "elapsed": _dur(elapsed_s)}
        if best:
            f["best size"] = f"{best.get('size')} nm"
            f["best loss"] = best.get("loss")
        self.notify("Autonomous session finished", tier=SESSION, fields=f)

    def recipe_applied(self, recipe_id: str, params: dict, duration_s: float) -> None:
        f = {k: v for k, v in params.items()
             if k in ("T_reac", "F_tot", "x_ODE", "x_TOP", "x_oley")}
        f["run duration"] = _dur(duration_s)
        self.notify(f"Recipe applied — {recipe_id}", tier=PROGRESS,
                    recipe_id=recipe_id, fields=f)

    def run_complete(self, recipe_id: str, reason: str, duration_s: float,
                     result: dict | None = None) -> None:
        f = {"stopped by": reason, "ran": _dur(duration_s)}
        for k in ("size", "pdi", "confidence", "loss"):
            if (result or {}).get(k) is not None:
                f[k] = result[k]
        self.notify(f"Run complete — {recipe_id}", tier=PROGRESS,
                    recipe_id=recipe_id, fields=f)

    def fault(self, title: str, detail: str = "", recipe_id: str = "") -> None:
        self.notify(f"{title}\n{detail}" if detail else title, tier=ALERT,
                    recipe_id=recipe_id)

    def upload_png(self, path: str, title: str, recipe_id: str = "",
                   comment: str = "") -> None:
        """Email the plot as an attachment (the Slack-uploader equivalent)."""
        self.notify(title, tier=ALERT, recipe_id=recipe_id,
                    fields={"note": comment} if comment else None, attach=path)


def _dur(s: float) -> str:
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"
