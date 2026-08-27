"""
tests/test_analyzer_ui.py — the analyzer UI must stay usable after 3000 recipes.

Two problems are locked down here.

1. LAG. The SSE stream used to re-send every summary it had ever produced, once a
   second, and the page rebuilt its entire <tbody> from that payload. At a few
   thousand profiles that is ~0.8 MB and a full re-layout every second. The
   stream is now sequence-based: the first frame is a bounded snapshot, later
   frames carry only rows the client has not seen.

2. THE PLOT. It was a fixed 720x300 canvas, log-log only, with two tick labels
   and one arc() call per point. The redesign needs `sigma` in the plot payload
   (error bars + a proper (data-model)/sigma residual strip) and a set of scale
   buttons in the page.
"""
from __future__ import annotations

import importlib.util as u
import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "analyzer" / "templates" / "index.html"


def _load(tag="az_ui"):
    spec = u.spec_from_file_location(tag, str(ROOT / "analyzer" / "app.py"))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


def _fake_entry(mod, name, seq=None, sigma=True):
    """Insert a result through the real store, without running a fit."""
    q = np.linspace(0.05, 3.0, 40)
    I = 1e3 * q ** -2
    return mod._store_result(name, {
        "summary": {"name": name, "radius": 4.0, "pdi": 0.1,
                    "confidence": 0.9, "phase": None},
        "full": {"llm": {}},
        "plot": {"q": q.tolist(), "I": I.tolist(), "model": I.tolist(),
                 "sigma": (I * 0.05).tolist() if sigma else None},
    })


# ── the plot payload ─────────────────────────────────────────────────────────
def test_plot_payload_carries_sigma(tmp_path, monkeypatch):
    """Without sigma the residual strip can only show a relative %, and error
    bars are impossible — the fit can look fine while being far outside the
    counting statistics."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    m = _load("az_sig")
    src = (ROOT / "analyzer" / "app.py").read_text()
    block = src[src.index('"plot": {'):src.index('"plot": {') + 260]
    assert '"sigma"' in block, "plot payload must include sigma"

    _fake_entry(m, "a.dat")
    r = m.app.test_client().get("/api/result/a.dat").get_json()
    assert r["plot"]["sigma"] is not None
    assert len(r["plot"]["sigma"]) == len(r["plot"]["q"])


def test_sigma_is_masked_the_same_way_as_q_and_I():
    """q/I are filtered to finite positive values; a sigma array of the original
    length would silently misalign the error bars with the points."""
    src = (ROOT / "analyzer" / "app.py").read_text()
    i = src.index("m = np.isfinite(q) & np.isfinite(I)")
    seg = src[i:i + 700]
    assert "sig = sig[m] if sig.shape == m.shape else None" in seg, \
        "sigma must be masked with the same boolean mask, or dropped"


# ── the stream is incremental ────────────────────────────────────────────────
def test_first_frame_is_a_bounded_snapshot_then_only_new_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    m = _load("az_stream")
    for i in range(1, 51):
        _fake_entry(m, f"p{i:03d}.dat")

    gen = m.app.view_functions["api_stream"]().response
    it = iter(gen)
    first = json.loads(next(it).split("data: ", 1)[1])
    assert first["reset"] is True
    assert first["total"] == 50
    assert len(first["results"]) == 50
    assert first["seq"] == 50
    assert first["results"][-1]["name"] == "p050.dat", "newest row must come last"

    # nothing new -> an essentially empty frame
    _fake_entry(m, "p051.dat")
    second = json.loads(next(it).split("data: ", 1)[1])
    assert second["reset"] is False
    assert [r["name"] for r in second["results"]] == ["p051.dat"], \
        "the stream re-sent rows the client already had"
    assert second["total"] == 51
    it.close()


def test_snapshot_is_capped_so_a_long_campaign_still_loads_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    m = _load("az_cap")
    n = m._MAX_RESULTS + 120
    for i in range(1, n + 1):
        _fake_entry(m, f"q{i:05d}.dat")
    assert len(m._results) == m._MAX_RESULTS, "result store is unbounded"

    gen = m.app.view_functions["api_stream"]().response
    frame = next(iter(gen))
    payload = json.loads(frame.split("data: ", 1)[1])
    assert len(payload["results"]) <= m._SNAPSHOT
    assert len(frame.encode()) < 200_000, f"first frame is {len(frame)} B"
    gen.close()


def test_steady_state_frame_is_tiny(tmp_path, monkeypatch):
    """The regression this whole change exists to prevent."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    m = _load("az_tiny")
    for i in range(1, 601):
        _fake_entry(m, f"r{i:04d}.dat")
    gen = m.app.view_functions["api_stream"]().response
    it = iter(gen)
    next(it)                                   # snapshot
    quiet = next(it)                           # nothing happened since
    assert len(quiet.encode()) < 1500, f"idle frame is {len(quiet)} B"
    it.close()


# ── the page ─────────────────────────────────────────────────────────────────
def test_all_five_scales_are_offered():
    html = TPL.read_text()
    for s in ("loglog", "semilogy", "semilogx", "linear", "guinier"):
        assert f'data-scale="{s}"' in html, f"missing the {s} scale button"
    assert 'id="resid"' in html and 'id="showErr"' in html


def test_every_insertion_goes_through_the_capped_store():
    """A second insertion path would reintroduce the unbounded store."""
    src = (ROOT / "analyzer" / "app.py").read_text()
    body = src[src.index("def _store_result"):]
    body = body[body.index("\ndef ", 10):]          # everything after the helper
    assert "_results[" not in body.replace("_results[name]", ""), \
        "results are inserted somewhere other than _store_result()"


def test_plot_is_no_longer_a_fixed_size_canvas():
    """A hard-coded width/height on the canvas is what made it blurry on a
    retina screen and unable to use the available width."""
    html = TPL.read_text()
    canvas = re.search(r'<canvas id="plot"[^>]*>', html).group(0)
    assert "width=" not in canvas and "height=" not in canvas, canvas
    assert "devicePixelRatio" in html, "renderer must be DPR-aware"


def test_the_table_is_no_longer_rebuilt_wholesale():
    """The render path must patch rows in place. A `.map(...).join('')` into
    innerHTML once a second is what made a long campaign crawl."""
    html = TPL.read_text()
    body = html[html.index("const MAX_ROWS"):html.index("function applyFilter")]
    # the antipattern, precisely: build every row into one string, assign once
    assert ".join('')" not in body and '.join("")' not in body, \
        "the table is still rebuilt from a joined string"
    assert "tb.innerHTML = rs" not in body and "tb.innerHTML=rs" not in body
    assert "insertBefore" in body and "MAX_ROWS" in body
    assert "innerHTML = rowHTML" in body or "innerHTML=rowHTML" in body


def test_the_stream_reconnects_instead_of_dying_silently():
    """An EventSource opened once with no error handler leaves the page showing
    stale numbers after an analyzer restart — the worst failure for an
    unattended overnight run."""
    html = TPL.read_text()
    assert "onerror" in html and "setTimeout(connect" in html, "no reconnect"
    assert "Math.pow(2" in html, "reconnect has no backoff"
    assert "no data for" in html, "a silent-but-open stream is not reported"


def test_fetches_cannot_throw_into_the_page():
    body = TPL.read_text()
    api = body[body.index("async function api("):body.index("let _sel")]
    assert "try{" in api and "catch" in api, "api() can still throw"
    assert "r.ok" in api, "a non-200 is not detected"


def test_the_status_strip_reports_the_whole_loop():
    html = TPL.read_text()
    for pid in ("p_conn", "p_camp", "p_count", "p_best", "p_watch"):
        assert f'id="{pid}"' in html, f"status strip is missing {pid}"


def test_operator_conveniences_are_present():
    html = TPL.read_text()
    assert 'id="filter"' in html and "applyFilter" in html, "no name filter"
    assert "exportCsv" in html, "no CSV export"
    assert 'class="sortable"' in html, "columns are not sortable"
    assert "ArrowDown" in html, "cannot step through fits from the keyboard"
    assert 'id="spark"' in html, "no inline convergence sparkline"


def test_abort_asks_first():
    """Aborting mid-run wastes whatever is in the reactor; it should not be a
    single misplaced click."""
    html = TPL.read_text()
    fn = html[html.index("async function abortCampaign"):]
    assert "confirm(" in fn[:400], "abort has no confirmation"


def test_scale_choice_is_remembered():
    html = TPL.read_text()
    assert "swaxs-an-scale" in html, "the operator's scale choice must persist"


def test_the_row_list_is_height_contained():
    """250 rows in an uncontained table make the page thousands of pixels tall;
    the scrolling cost reads as lag even once the payload is small."""
    html = TPL.read_text()
    assert 'class="tablewrap"' in html and "max-height" in html
    assert 'id="rowsnote"' in html, "no 'showing N of M' note"



def test_parameter_space_panel_sits_under_the_fit_plot():
    """It was in the narrow left column, where an 11-inch-wide matplotlib figure
    is unreadable. It belongs in the wide column, below the fit."""
    html = TPL.read_text()
    fit = html.index('id="plot"')
    ps = html.index('class="card pspace"')
    table = html.index('id="rows"')
    assert ps > fit, "the parameter-space card is still above the fit plot"
    assert ps > table, "the parameter-space card is still in the left column"


def test_parameter_space_figures_can_be_enlarged():
    html = TPL.read_text()
    assert 'id="lightbox"' in html and "zoom-in" in html
    assert "position:fixed" in html[html.index("#lightbox{"):
                                    html.index("#lightbox{") + 120], \
        "the lightbox would scroll away with the page"
