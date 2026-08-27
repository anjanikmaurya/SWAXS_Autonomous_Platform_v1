"""
tests/test_background_auto_monitor.py — automated subtraction must actually subtract.

THE BUG THIS FILE EXISTS FOR
----------------------------
`src.reactor.intake.decide_intake()` returns "skip" | "wait" | "go". The
background monitor compared its result against the string "handle":

    if decide_intake(rp, sig, {}, _sub_lastsig) != "handle":
        _sub_lastsig[rp] = sig
        continue

"go" != "handle" is always true, so EVERY sample fell through to `continue`.
Automated subtraction could never produce a single file — and because the skip
was silent, the app logged "Auto-subtraction started" and then nothing at all.
Status stayed green, the folder stayed empty, and there was no error to search
for. The analyzer and reactor watchers handle the same three strings correctly;
only this one drifted.

Two classes of regression are locked down:
  1. BEHAVIOUR — a sample plus its blank in the watched folder produces exactly
     one subtracted file, not zero and not one per poll.
  2. OBSERVABILITY — the loop says what it is doing. A monitor that is waiting
     for a background, or that sees zero samples because a keyword filter
     excluded them, must be distinguishable from a dead one.
"""
from __future__ import annotations

import importlib.util as u
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "background" / "app.py"


def _write_dat(path: Path, particle: float = 1.0, n: int = 300):
    """A plausible averaged profile: solvent power law + optional form factor."""
    q = np.linspace(0.1, 5.0, n)
    I = 40 * q ** -2.2 + particle * 900 * np.exp(-(q * 4.0) ** 2 / 3) + 5
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("# q_nm-1  I  sigma\n")
        for a, b in zip(q, I):
            f.write(f"{a:.6e} {b:.6e} {np.sqrt(b) * 0.02:.6e}\n")
        f.write("\n# --- metadata ---\n# detector: saxs\n")


def _load(tag, tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")     # don't resume a saved monitor
    spec = u.spec_from_file_location(tag, str(APP))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


def _wait(pred, timeout=12.0, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def _msgs(m):
    return [l["msg"] for _, l in list(m._sub_log)]


# ── 1. the loop subtracts ────────────────────────────────────────────────────
def test_a_sample_and_its_blank_produce_one_subtracted_file(tmp_path, monkeypatch):
    avg = tmp_path / "1D" / "SAXS" / "Averaged"
    rid = "auto_20260729_010448_e5a0"
    _write_dat(avg / f"{rid}_bkg_batch001_30files_Average.dat", particle=0.0)
    _write_dat(avg / f"{rid}_sample_batch001_30files_Average.dat", particle=1.0)

    m = _load("bgmon_ok", tmp_path, monkeypatch)
    try:
        r = m.app.test_client().post("/api/monitor/start",
                                     json={"interval": 1, "saxs_avg_folder": str(avg)})
        assert (r.get_json() or {}).get("ok"), r.get_json()
        assert _wait(lambda: m._sub_status["subtracted"] >= 1), (
            "automated subtraction produced nothing:\n" + "\n".join(_msgs(m)))
        out = list((tmp_path / "1D" / "SAXS" / "Subtracted").rglob("*.dat"))
        assert len(out) == 1, [p.name for p in out]
        assert out[0].name.startswith(rid) and out[0].name.endswith("_sub.dat")
        assert m._sub_status["last"] == out[0].name
    finally:
        m._sub_monitoring = False


def test_a_subtracted_sample_is_not_redone_every_poll(tmp_path, monkeypatch):
    """The other half of the intake contract. Getting "go" on every poll would
    rewrite the same output continuously and inflate the campaign's count."""
    avg = tmp_path / "1D" / "SAXS" / "Averaged"
    _write_dat(avg / "auto_1_a_bkg_batch001_30files_Average.dat", 0.0)
    _write_dat(avg / "auto_1_a_sample_batch001_30files_Average.dat", 1.0)
    m = _load("bgmon_once", tmp_path, monkeypatch)
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "saxs_avg_folder": str(avg)})
        assert _wait(lambda: m._sub_status["subtracted"] >= 1)
        time.sleep(3.5)                                    # several more polls
        assert m._sub_status["subtracted"] == 1, "the sample was subtracted twice"
        assert len(list((tmp_path / "1D" / "SAXS" / "Subtracted").rglob("*.dat"))) == 1
    finally:
        m._sub_monitoring = False


def test_a_rewritten_average_is_subtracted_again(tmp_path, monkeypatch):
    """`_sub_done` remembers a SIGNATURE, not just a path, because the averager
    may correct a batch in place. Remembering only the path would silently keep
    the stale subtraction forever."""
    avg = tmp_path / "1D" / "SAXS" / "Averaged"
    sample = avg / "auto_1_a_sample_batch001_30files_Average.dat"
    _write_dat(avg / "auto_1_a_bkg_batch001_30files_Average.dat", 0.0)
    _write_dat(sample, 1.0)
    m = _load("bgmon_rewrite", tmp_path, monkeypatch)
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "saxs_avg_folder": str(avg)})
        assert _wait(lambda: m._sub_status["subtracted"] >= 1)
        time.sleep(1.1)                          # ensure a different mtime_ns
        _write_dat(sample, 1.0, n=420)           # same name, new content
        assert _wait(lambda: m._sub_status["subtracted"] >= 2), (
            "a corrected average was ignored:\n" + "\n".join(_msgs(m)))
    finally:
        m._sub_monitoring = False


def test_keyword_mode_works_without_background_tokens(tmp_path, monkeypatch):
    """A manual dataset whose blank is called 'reference' matches none of the
    built-in background tokens; the operator's keywords must drive the split."""
    avg = tmp_path / "1D" / "WAXS" / "Averaged"
    _write_dat(avg / "nylon_run3_batch001_30files_Average.dat", 1.0)
    _write_dat(avg / "reference_run3_batch001_30files_Average.dat", 0.0)
    m = _load("bgmon_kw", tmp_path, monkeypatch)
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "waxs_avg_folder": str(avg),
                                       "sample_keyword": "nylon",
                                       "bkg_keyword": "reference"})
        assert _wait(lambda: m._sub_status["subtracted"] >= 1), "\n".join(_msgs(m))
        out = [p.name for p in (tmp_path / "1D" / "WAXS" / "Subtracted").rglob("*.dat")]
        assert out == ["nylon_run3_batch001_30files_Average_sub.dat"], out
    finally:
        m._sub_monitoring = False


# ── 2. the loop is not silent ────────────────────────────────────────────────
def test_the_monitor_reports_what_it_can_see(tmp_path, monkeypatch):
    """The heartbeat that would have made the original bug obvious in seconds."""
    avg = tmp_path / "1D" / "SAXS" / "Averaged"
    _write_dat(avg / "auto_1_a_bkg_batch001_30files_Average.dat", 0.0)
    _write_dat(avg / "auto_1_a_sample_batch001_30files_Average.dat", 1.0)
    m = _load("bgmon_hb", tmp_path, monkeypatch)
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "saxs_avg_folder": str(avg)})
        assert _wait(lambda: any("1 sample(s), 1 background(s)" in s for s in _msgs(m))), \
            "no heartbeat describing the sample/background split:\n" + "\n".join(_msgs(m))
    finally:
        m._sub_monitoring = False


def test_waiting_for_a_blank_is_announced_once(tmp_path, monkeypatch):
    """In a campaign the sample average is written before the flush finishes, so
    'no background yet' is normal — but it must be visible, and it must not spam
    the log on every poll for hours."""
    avg = tmp_path / "1D" / "SAXS" / "Averaged"
    _write_dat(avg / "auto_A_sample_batch001_30files_Average.dat", 1.0)
    _write_dat(avg / "auto_B_bkg_batch001_30files_Average.dat", 0.0)   # wrong recipe
    m = _load("bgmon_wait", tmp_path, monkeypatch)
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "saxs_avg_folder": str(avg)})
        assert _wait(lambda: any("waiting for the blank of recipe 'auto_A'" in s
                                 for s in _msgs(m))), "\n".join(_msgs(m))
        time.sleep(3.0)
        n = sum(1 for s in _msgs(m) if "waiting for the blank" in s)
        assert n == 1, f"the same waiting notice was logged {n} times"
        assert m._sub_status["subtracted"] == 0, \
            "it subtracted another recipe's background"
    finally:
        m._sub_monitoring = False


def test_a_keyword_that_matches_nothing_is_flagged(tmp_path, monkeypatch):
    """A typo'd sample keyword produces zero work. That must read as a warning,
    not as a healthy idle loop."""
    avg = tmp_path / "1D" / "SAXS" / "Averaged"
    _write_dat(avg / "auto_1_a_sample_batch001_30files_Average.dat", 1.0)
    _write_dat(avg / "auto_1_a_bkg_batch001_30files_Average.dat", 0.0)
    m = _load("bgmon_typo", tmp_path, monkeypatch)
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "saxs_avg_folder": str(avg),
                                       "sample_keyword": "smaple"})
        assert _wait(lambda: any("0 sample(s)" in s for s in _msgs(m))), "\n".join(_msgs(m))
        warn = [l for _, l in list(m._sub_log)
                if "0 sample(s)" in l["msg"] and l["tag"] == "warn"]
        assert warn, "a keyword that matches nothing was reported as normal"
    finally:
        m._sub_monitoring = False


# ── 3. the intake contract itself ────────────────────────────────────────────
def test_every_watcher_uses_the_actual_intake_verbs():
    """The root cause was a made-up return value. Any watcher comparing against a
    string decide_intake never returns is silently dead, so assert it directly."""
    from src.reactor.intake import decide_intake
    verbs = {decide_intake("k", (1, 2), {}, {}),
             decide_intake("k", (1, 2), {}, {"k": (1, 2)}),
             decide_intake("k", (1, 2), {"k": (1, 2)}, {})}
    assert verbs == {"wait", "go", "skip"}, verbs

    import re
    call = re.compile(r"(?:=|if)\s*decide_intake\(")
    for app in ("background/app.py", "analyzer/app.py", "reactor/app.py"):
        src = (ROOT / app).read_text()
        sites = [mm.start() for mm in call.finditer(src)]
        assert sites, f"{app} no longer calls decide_intake"
        for i in sites:
            seg = src[i:i + 420]
            assert '"handle"' not in seg, f'{app} compares decide_intake to "handle"'
            assert '"wait"' in seg and ('"skip"' in seg or "_done" in seg), \
                f"{app} does not branch on the real intake verbs:\n{seg[:200]}"
