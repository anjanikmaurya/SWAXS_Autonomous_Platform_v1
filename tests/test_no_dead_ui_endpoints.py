"""
tests/test_no_dead_ui_endpoints.py

Notifications moved from the reactor app to Auto Watch. reactor/app.py stopped
serving /api/slack and stopped importing src.notify — but the card and its
JavaScript stayed behind, and the way it failed was quiet and dangerous:

    api('/api/slack').then(renderSlack).catch(()=>{})

The GET 404'd, .json() threw on the HTML error page, the empty .catch
swallowed it, and renderSlack never ran. So the card kept the enabled-looking
"🔔 Notify me on Slack" button and the "Arm Slack notifications once the
measurement is running, so recipes, results and any fault reach you while
you're away" text that were hard-coded in the HTML. Clicking it 404'd into
another empty catch and silently changed nothing.

An operator could arm notifications, read the reassuring text, and leave for
the night with nothing armed. That is the failure this file exists to prevent:
not dead code as untidiness, but a UI making a promise no backend keeps.

So: every fetch() path in an app's own template must be a route that app
actually serves.
"""
from __future__ import annotations

import importlib.util as u
import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")

APPS = ["calibration", "reduction", "average", "background", "quality",
        "analysis", "analyzer", "reactor", "assistant", "watchdog", "hub"]

#: Any string literal that looks like a call to this app's own API. Covers
#: api('/api/x'), fetch('/api/x', …), fetch(`/api/x/${id}`) and EventSource.
_CALL = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/]*)""")

#: Paths built at runtime from a variable, which a static scan cannot resolve.
_SKIP = ("/api/",)

#: Block comments, in both syntaxes these templates use.
_BLOCK = re.compile(r"/\*.*?\*/|<!--.*?-->", re.S)


def strip_comments(text: str) -> str:
    """Remove comments before scanning for API calls.

    Not fussiness: the comment explaining why a route was REMOVED naturally
    quotes that route, so a scanner that cannot tell code from prose reports
    the explanation as the defect and pushes the next person to delete the
    explanation. `//` is only stripped at the start of a line, because the
    templates are full of http:// URLs.
    """
    out = _BLOCK.sub("", text)
    return "\n".join(l for l in out.splitlines()
                     if not l.strip().startswith("//"))


def _served(app) -> set[str]:
    out = set()
    for rule in app.url_map.iter_rules():
        # /api/app/<id>/log -> /api/app, so a templated path matches its prefix
        out.add(str(rule.rule))
        out.add(str(rule.rule).split("<")[0].rstrip("/"))
    return out


@pytest.fixture(scope="module")
def modules():
    loaded = {}
    for app_id in APPS:
        spec = u.spec_from_file_location(f"deadui_{app_id}", f"{app_id}/app.py")
        mod = u.module_from_spec(spec)
        sys.modules[f"deadui_{app_id}"] = mod
        spec.loader.exec_module(mod)
        loaded[app_id] = mod
    return loaded


@pytest.mark.parametrize("app_id", APPS)
def test_every_api_call_in_the_template_has_a_route(app_id, modules):
    tpl = _ROOT / app_id / "templates" / "index.html"
    if not tpl.is_file():
        pytest.skip(f"{app_id} has no template")
    served = _served(modules[app_id].app)
    called = {m for m in _CALL.findall(strip_comments(tpl.read_text()))
              if m not in _SKIP}

    dead = []
    for path in sorted(called):
        if path in served:
            continue
        # a templated route: /api/app/x/log matched by the /api/app prefix
        if any(path.startswith(s) and s for s in served if s.count("/") >= 2):
            continue
        dead.append(path)

    assert not dead, (
        f"{app_id}/templates/index.html calls {dead}, which {app_id}/app.py "
        f"does not serve. A fetch to a missing route 404s, .json() throws, and "
        f"a .catch(()=>{{}}) hides it — leaving whatever the HTML hard-coded "
        f"on screen as though it were live state.")


# ── the specific regression ─────────────────────────────────────────────────
def test_the_reactor_does_not_offer_to_arm_notifications():
    """It cannot: the routes are gone. Offering anyway is worse than offering
    nothing, because the operator acts on it and then leaves."""
    body = strip_comments(
        (_ROOT / "reactor" / "templates" / "index.html").read_text())
    assert "/api/slack" not in body
    for gone in ("toggleSlack(", "testSlack(", "renderSlack("):
        assert gone not in body, f"{gone} survived the move to Auto Watch"


def test_the_reactor_carries_no_notification_ui_at_all():
    """This assertion is the inverse of the one it replaced, deliberately.

    The dead card was first REPLACED by a pointer card — "notifications live in
    Auto Watch, here is the link" — on the reasoning that deleting it outright
    leaves the operator with no answer to "how do I get told about this run?".
    The operator disagreed, and was right: notifications are one feature with
    one owner, and a second place that talks about them is a second place that
    can drift out of date, contradict the first, or be mistaken for a second
    switch that also needs arming. Auto Watch is the whole answer.

    So the reactor's template must not mention notifications in any form —
    not a route, not a handler, not a link, not a sentence."""
    tpl = (_ROOT / "reactor" / "templates" / "index.html").read_text()
    body = strip_comments(tpl)
    for gone in ("/api/slack", "Slack", "slack",
                 "Auto Watch", "5110", "Notify", "notification"):
        assert gone not in body, (
            f"{gone!r} is back in the reactor UI; notifications belong to "
            f"Auto Watch alone")


def test_auto_watch_honours_the_deep_link():
    """Auto Watch's own nav writes #alerts into the URL, so a reloaded or
    bookmarked Alerts page must come back to Alerts. Without hash handling it
    lands on the overview and the operator has to know to click through."""
    tpl = (_ROOT / "watchdog" / "templates" / "index.html").read_text()
    assert "applyHashView" in tpl
    assert "hashchange" in tpl
    assert "_VIEWS" in tpl, "an unknown hash must not reach switchView"


def test_the_reactor_still_does_not_import_the_notify_package():
    """CLAUDE.md records this: src/notify is legacy, kept only for
    tools/notify_test.py. An app importing it again means notifications have
    started leaking back out of Auto Watch."""
    src = (_ROOT / "reactor" / "app.py").read_text()
    assert "src.notify" not in src
