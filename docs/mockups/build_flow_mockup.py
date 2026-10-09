"""
docs/mockups/build_flow_mockup.py — the Synthesis page, runnable without the app.

Takes the REAL page (reactor/templates/index.html, the left column + three tab
layout adopted in October 2026), inlines its partials and the shared stylesheet,
and swaps the backend for a small in-page simulator (_fake_reactor_backend.js)
that answers the same /api/* calls and streams status frames, using the settings
in reactor/config.yml. Open the result in any browser to try the page with no
Flask, no pumps and no SPEC.

Because it is built from the real template, it can never drift from the app.

    python docs/mockups/build_flow_mockup.py      →  docs/mockups/synthesis_flow_mockup.html
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TPL = ROOT / "reactor" / "templates"
OUT = Path(__file__).with_name("synthesis_flow_mockup.html")
SPEED = 20            # simulator clock speed-up only; every duration shown is the real one
PROJECT = "/Users/akmaurya/Desktop/Data_local/Auto_Run"


def reactor_settings() -> dict:
    """The settings the real app starts with, straight from reactor/config.yml."""
    cfg = yaml.safe_load((ROOT / "reactor" / "config.yml").read_text(encoding="utf-8"))
    pick = lambda d, *ks: {k: d.get(k) for k in ks}
    spec = cfg.get("spec", {})
    return {
        "project": PROJECT, "speed": SPEED, "backend": spec.get("backend", "mock"),
        "pumps": {n: pick(p, "sensor_min", "max_flow", "max_pressure", "calibration_factor", "sensor")
                  for n, p in cfg["pumps"].items()},
        "bounds": cfg["bounds"], "safety": cfg["safety"],
        "temperature": pick(cfg["temperature"], "tolerance", "stable_hold", "timeout", "mock_ramp", "cooldown_c"),
        "spec": pick(spec, "exposure_s", "frames", "sample_tag", "bkg_tag", "spec_lead_s", "data_dir"),
        "arming": cfg["arming"], "run": pick(cfg["run"], "default_duration", "min_dwell_s"),
        "flush": pick(cfg["flush"], "rate", "duration", "blank_rinse_s", "pump"),
        "folders": cfg["folders"],
    }


def main() -> None:
    page = (TPL / "index.html").read_text(encoding="utf-8")
    part = lambda n: (TPL / n).read_text(encoding="utf-8")
    tokens = (ROOT / "assets" / "icons" / "swaxs-tokens.css").read_text(encoding="utf-8")
    fake = Path(__file__).with_name("_fake_reactor_backend.js").read_text(encoding="utf-8")

    html = page.replace('<link rel="stylesheet" href="/static/swaxs-tokens.css">', f"<style>{tokens}</style>", 1)
    html = re.sub(r'\s*<link rel="icon" href="/app-icon">[^\n]*', "", html, count=1)
    for name in ("_theme_boot.html", "_icon_sprite.svg", "_app_mark.svg"):
        html = html.replace('{%% include "%s" %%}' % name, part(name))
    # the simulator must be in place before any page script calls fetch / EventSource
    html = html.replace('{% include "_icon_helpers.html" %}',
                        f"<script>window.__REACTOR_CFG={json.dumps(reactor_settings())};</script>\n"
                        f"<script>{fake}</script>\n" + part("_icon_helpers.html"), 1)
    assert "{%" not in html, "an include was left unresolved"
    html = html.replace("<title>Autonomous Synthesis · SWAXS</title>",
                        "<title>Autonomous Synthesis · simulated (mockup)</title>", 1)
    ribbon = ('<div style="background:#1d4ed8;color:#fff;font-size:13px;text-align:center;padding:3px 8px;flex:none">'
              f"Mockup · simulated data, no hardware · real page and reactor/config.yml settings · clock ×{SPEED}</div>")
    html = html.replace('<body class="swaxs-shell">', '<body class="swaxs-shell">\n' + ribbon, 1)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(html)//1024} kB)")


if __name__ == "__main__":
    main()
