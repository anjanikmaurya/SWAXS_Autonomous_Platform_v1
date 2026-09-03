#!/usr/bin/env python3
"""
tools/notify_test.py — verify the notifications without a beamtime run.

Covers BOTH channels: Slack (needs a workspace-app credential) and email (needs
no permission at all, and can deliver into Slack via a channel-email address).

    python tools/notify_test.py --check              # config/env only, sends NOTHING
    python tools/notify_test.py --demo --dry-run     # print the messages, no network
    python tools/notify_test.py --ping               # send ONE test message
    python tools/notify_test.py --demo               # send a full simulated campaign
    python tools/notify_test.py --fault              # send only the alert-tier messages

--demo posts what a real two-condition campaign looks like, including a threaded
fit result and a low-confidence fit with a QC plot attached, so you can judge the
formatting and volume before trusting it overnight.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml                                                        # noqa: E402
from src.notify import slack as S                                  # noqa: E402
from src.notify.slack import SlackNotifier                         # noqa: E402
from src.notify import email_notify as E                           # noqa: E402
from src.notify import MultiNotifier, smtp_probe                   # noqa: E402

OK, BAD, WARN = "\033[92m✓\033[0m", "\033[91m✗\033[0m", "\033[93m!\033[0m"


def load_notify_cfg() -> dict:
    p = _ROOT / "reactor" / "config.yml"
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"{BAD} could not read {p}: {exc}")
        return {}
    return cfg.get("notify") or {}


def load_slack_cfg() -> dict:
    return load_notify_cfg().get("slack") or {}


def check_email(probe_smtp: bool = True) -> bool:
    """Report the email transport AND probe which SMTP options actually work."""
    print("\n── Email notifications ──────────────────────────────────────")
    cfg = load_notify_cfg().get("email") or {}
    host = os.environ.get(E.ENV_HOST, "").strip()
    to = [a for a in os.environ.get(E.ENV_TO, "").split(",") if a.strip()]
    user = os.environ.get(E.ENV_USER, "").strip()
    print(f"{OK if cfg.get('enabled') else WARN} config notify.email.enabled: "
          f"{bool(cfg.get('enabled'))}   (the app's button can arm it anyway)")
    print(f"{OK if host else BAD} {E.ENV_HOST}: {host or '(not set)'}")
    print(f"{OK if to else BAD} {E.ENV_TO}: {', '.join(to) or '(not set)'}")
    print(f"{OK if user else WARN} {E.ENV_USER}: {user or '(none — unauthenticated relay)'}")
    if not host or not to:
        print(f"{BAD} email is not configured. Add to .env:")
        print(f"     {E.ENV_HOST}=smtp.yourinstitution.edu")
        print(f"     {E.ENV_TO}=you@example.edu")
        print(f"     {E.ENV_USER}=you@example.edu        # if auth is required")
        print(f"     {E.ENV_PASS}=app-password")
        return False

    n = E.EmailNotifier({**cfg, "enabled": True})
    print(f"{OK} would send as {n.sender} via {n.host}:{n.port} ({n.mode})")

    if probe_smtp:
        print("\n   probing SMTP options from THIS machine (a few seconds)…")
        rows = smtp_probe(host)
        any_ok = False
        for r in rows:
            mark = OK if r["ok"] else BAD
            auth = " (auth required)" if r["needs_auth"] else ""
            err = f"  {r['error']}" if r["error"] and not r["ok"] else ""
            print(f"   {mark} {r['host']}:{r['port']} {r['mode']}{auth}{err}")
            any_ok = any_ok or r["ok"]
        if not any_ok:
            print(f"   {BAD} no SMTP option worked — outbound mail may be blocked "
                  f"from this host. Ask IT which relay/port is permitted.")
        else:
            print(f"   {OK} at least one option works — put that host/port in .env")
        n.close()
        # Configured but unreachable is NOT usable — say so rather than implying
        # notifications will work.
        return any_ok
    n.close()
    return bool(host and to)


def check() -> bool:
    """Report exactly why notifications are or aren't going to work."""
    print("── Slack notification check ─────────────────────────────────")
    cfg = load_slack_cfg()
    enabled = bool(cfg.get("enabled"))
    print(f"{OK if enabled else BAD} reactor/config.yml → notify.slack.enabled: {enabled}")
    if not enabled:
        print("     → set it to true in reactor/config.yml")

    token = os.environ.get(S.ENV_TOKEN, "").strip()
    hook = os.environ.get(S.ENV_WEBHOOK, "").strip()
    if token:
        print(f"{OK} {S.ENV_TOKEN} is set ({token[:9]}…{token[-4:]}) → bot mode "
              f"(threading + plot uploads)")
    else:
        print(f"{WARN} {S.ENV_TOKEN} not set")
    if hook:
        print(f"{OK} {S.ENV_WEBHOOK} is set (…{hook[-12:]}) → webhook mode (flat messages)")
    else:
        print(f"{WARN} {S.ENV_WEBHOOK} not set")
    if not token and not hook:
        print(f"{BAD} no credentials — notifications are OFF. Export one:")
        print(f"     export {S.ENV_TOKEN}=xoxb-…       # preferred")
        print(f"     export {S.ENV_WEBHOOK}=https://hooks.slack.com/services/…")
        return False

    chan = cfg.get("channel", "")
    if token and not chan:
        print(f"{BAD} notify.slack.channel is empty — required in bot mode")
        return False
    print(f"{OK} channel: {chan or '(from the webhook)'}")
    print(f"{OK} tiers: {', '.join(cfg.get('tiers') or ['alert', 'progress', 'session'])}")
    print(f"{OK} thread_per_recipe: {cfg.get('thread_per_recipe', True)}"
          + ("" if token else "  (forced off — webhooks cannot thread)"))

    n = SlackNotifier({**cfg, "enabled": True})
    ready = n.enabled
    n.close()
    print(f"\n{OK if ready else BAD} notifier would be "
          f"{'ACTIVE' if ready else 'INACTIVE'} at reactor startup")
    return ready


def _qc_png() -> str:
    """A throwaway I(q) plot so the upload path can be exercised."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        q = np.logspace(-2, 0.4, 300)
        I = 1e4 / (1 + (q * 6.0) ** 4) + 30
        fig, ax = plt.subplots(figsize=(4.6, 3.4), dpi=110)
        ax.loglog(q, I, lw=1.1, label="subtracted I(q)")
        ax.loglog(q, I * 1.15, lw=1.1, ls="--", label="fit")
        ax.set_xlabel("q (nm$^{-1}$)"); ax.set_ylabel("I (a.u.)")
        ax.set_title("slack_test — synthetic QC plot", fontsize=8)
        ax.legend(fontsize=7); fig.tight_layout()
        out = Path(__file__).with_name("_slack_test_qc.png")
        fig.savefig(out); plt.close(fig)
        return str(out)
    except Exception as exc:
        print(f"{WARN} could not build the demo plot ({exc}) — skipping the upload test")
        return ""


def make_multi(cfg: dict, dry_run: bool):
    """A MultiNotifier with the transports stubbed out when --dry-run."""
    if dry_run:
        def fake(url, payload, token="", timeout=6.0):
            where = "webhook" if "hooks.slack.com" in url else url.rsplit("/", 1)[-1]
            thread = "  ↳ THREADED" if payload.get("thread_ts") else ""
            print(f"\n[slack {where}]{thread}")
            for line in str(payload.get("text", "")).splitlines():
                print(f"   {line}")
            return {"ok": True, "ts": f"{time.time():.4f}"}
        S._post = fake
        os.environ.setdefault(S.ENV_TOKEN, "xoxb-dry-run")
        os.environ.setdefault(E.ENV_HOST, "smtp.dry-run.invalid")
        os.environ.setdefault(E.ENV_TO, "you@example.edu")
    return MultiNotifier(cfg)


def make_notifier(cfg: dict, dry_run: bool) -> SlackNotifier:
    if dry_run:
        # Replace the transport: print instead of POST. Nothing leaves the machine.
        def fake(url, payload, token="", timeout=6.0):
            where = "webhook" if "hooks.slack.com" in url else url.rsplit("/", 1)[-1]
            thread = "  ↳ THREADED" if payload.get("thread_ts") else ""
            print(f"\n[{where}]{thread}")
            for line in str(payload.get("text", "")).splitlines():
                print(f"   {line}")
            return {"ok": True, "ts": f"{time.time():.4f}"}
        S._post = fake
        os.environ.setdefault(S.ENV_TOKEN, "xoxb-dry-run")
    return SlackNotifier({**cfg, "enabled": True, "min_interval_s": 0.0,
                          "dry_run": dry_run})


def demo(n: SlackNotifier, with_faults: bool = True) -> None:
    """A realistic two-condition campaign."""
    n.session_start(os.environ.get("SWAXS_REACTOR_BACKEND", "mock"),
                    os.environ.get("SWAXS_PROJECT", "(no project selected)"))
    time.sleep(0.6)

    # condition 1 — a good result
    n.recipe_applied("slacktest_r001",
                     {"T_reac": 243.5, "F_tot": 80, "x_ODE": 0.20,
                      "x_TOP": 0.15, "x_oley": 0.10}, 600.0)
    time.sleep(0.8)
    n.run_complete("slacktest_r001", "duration elapsed", 601.0)
    time.sleep(0.6)
    n.notify(":bar_chart: *Fit result* — `slacktest_r001`", tier=S.PROGRESS,
             recipe_id="slacktest_r001",
             fields={"size (nm)": 4.12, "PDI": 0.023, "confidence": 0.91,
                     "loss": 0.115, "file": "slacktest_r001_sample_SAXS_subtracted.dat"})
    time.sleep(0.8)

    # condition 2 — a suspect fit, escalated with the plot attached
    n.recipe_applied("slacktest_r002",
                     {"T_reac": 291.0, "F_tot": 115, "x_ODE": 0.28,
                      "x_TOP": 0.04, "x_oley": 0.02}, 600.0)
    time.sleep(0.8)
    n.notify(":mag: *Fit LOW CONFIDENCE* — `slacktest_r002`", tier=S.ALERT,
             recipe_id="slacktest_r002",
             fields={"size (nm)": 9.81, "PDI": 0.48, "confidence": 0.08,
                     "file": "slacktest_r002_sample_SAXS_subtracted.dat"})
    time.sleep(0.6)
    png = _qc_png()
    if png:
        n.upload_png(png, "slacktest_r002 I(q) + fit", recipe_id="slacktest_r002",
                     comment="low-confidence fit (0.08) — R=9.81 nm, PDI=0.48")
        time.sleep(1.5)

    if with_faults:
        faults(n)
    n.session_end(2, best={"size": 4.12, "loss": 0.115}, elapsed_s=1265.0)
    time.sleep(0.8)


def faults(n: SlackNotifier) -> None:
    """Only the alert tier — what would wake you up."""
    n.fault("SAFETY: over-temperature", "reactor 331.2°C exceeds T_max 320°C — "
                                        "all pumps idled")
    time.sleep(0.6)
    n.fault("EMERGENCY STOP", "pumps that did NOT idle: top — check them immediately")
    time.sleep(0.6)
    n.fault("SAFETY: temperature reading stale",
            "no successful read for 47s — the over-temperature interlock is blind")
    time.sleep(0.6)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="config/env only, send nothing")
    ap.add_argument("--ping", action="store_true", help="send one test message")
    ap.add_argument("--demo", action="store_true", help="send a simulated campaign")
    ap.add_argument("--fault", action="store_true", help="send only the alert-tier messages")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the messages instead of sending them")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the SMTP connection probe (faster --check)")
    a = ap.parse_args()

    if not any((a.check, a.ping, a.demo, a.fault)):
        a.check = True                       # safe default: never send unasked

    slack_ok = check()
    email_ok = check_email(probe_smtp=not a.no_probe)
    ok = slack_ok or email_ok
    print(f"\n{'─'*60}\n{OK if ok else BAD} "
          f"{'at least one channel is usable' if ok else 'NO channel is usable yet'}")
    if a.check and not (a.ping or a.demo or a.fault):
        return 0 if ok else 1
    if not ok and not a.dry_run:
        print(f"\n{BAD} not sending — fix the items above, or add --dry-run")
        return 1

    # A test must not be silenced by the configured tiers.
    nc = load_notify_cfg()
    tiers = [S.ALERT, S.PROGRESS, S.SESSION]
    cfg = {"slack": {**(nc.get("slack") or {}), "tiers": tiers,
                     "enabled": True, "min_interval_s": 0.0,
                     "dry_run": a.dry_run},
           "email": {**(nc.get("email") or {}), "tiers": tiers,
                     "enabled": True, "min_interval_s": 0.0,
                     "dry_run": a.dry_run}}
    n = make_multi(cfg, a.dry_run)
    print(f"\n── {'DRY RUN — nothing is sent' if a.dry_run else f'sending ({n.mode} mode)'} "
          f"────────────────")
    try:
        if a.ping:
            n.notify(":wave: *SWAXS Slack test* — if you can read this, "
                     "notifications are working.", tier=S.SESSION)
            time.sleep(1.0)
        if a.fault:
            faults(n)
        if a.demo:
            demo(n, with_faults=not a.fault)
    finally:
        n.close(timeout=8.0)
    print(f"\n{OK} done"
          + ("" if a.dry_run else " — check the channel now"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
