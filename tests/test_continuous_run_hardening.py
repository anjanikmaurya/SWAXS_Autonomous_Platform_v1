"""
tests/test_continuous_run_hardening.py — the defects that break a multi-day run.

Each test here reproduces a filed defect from docs/audits/OPEN_DEFECTS.md that
only shows up after a restart, a race, or a transient read failure — i.e. at
3am, unattended, with nobody watching. Written to FAIL against the old code.

Plan and rationale: docs/CONTINUOUS_RUN_HARDENING_PLAN.md
"""
from __future__ import annotations

import importlib.util as u
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(tag: str, rel: str, tmp_path=None, monkeypatch=None):
    """Import an app module fresh — the closest thing to a process restart.

    SWAXS_NO_RESUME is essential: reduction and average both start a
    _boot_resume daemon thread at import time.
    """
    if monkeypatch is not None:
        monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
        monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    spec = u.spec_from_file_location(tag, str(ROOT / rel))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


# ── N2: /api/run must not race the monitor ───────────────────────────────────
def test_run_is_refused_while_the_monitor_is_active(tmp_path, monkeypatch):
    """Both paths share one cached pyFAI AzimuthalIntegrator (documented
    single-threaded) and both write-then-replace() the same .part, so an
    interleave publishes a TRUNCATED .dat that looks complete to every
    downstream size/mtime check. Refusing is honest and removes the interleave.
    """
    m = _load("red_n2", "reduction/app.py", tmp_path, monkeypatch)
    m._monitoring = True                      # pretend the monitor is running

    class _T:
        def is_alive(self): return True
    m._monitor_thread = _T()

    r = m.app.test_client().post("/api/run", json={"data_directory": str(tmp_path)})
    assert r.status_code == 409, \
        "a one-shot run was accepted while the monitor was active"
    body = r.get_json()
    assert body.get("ok") is False and "monitor" in (body.get("error") or "").lower()


# ── N1: a restart must not re-reduce the whole experiment ────────────────────
def test_the_processed_set_survives_a_restart(tmp_path, monkeypatch):
    """_processed_files was memory-only and find_new_raw_files filters on
    nothing else, so after a restart EVERY .raw in the tree is "new": every
    .dat rewritten, thousands of manifest writes under the cross-process lock,
    and live frames starved until the backlog clears."""
    m1 = _load("red_n1a", "reduction/app.py", tmp_path, monkeypatch)
    seen = {str(tmp_path / "2D" / "SAXS" / f"s_{i:04d}.raw") for i in range(5)}
    m1._processed_files.update(seen)
    m1._save_processed()                      # what the monitor does each cycle

    m2 = _load("red_n1b", "reduction/app.py", tmp_path, monkeypatch)
    assert m2._processed_files == seen, \
        "the processed set was lost on restart — the whole experiment re-reduces"


def test_a_reduced_file_is_not_reprocessed_even_without_saved_state(tmp_path,
                                                                    monkeypatch):
    """Belt and braces for when the state file is missing or the project moved:
    a .raw whose .dat already exists and is NEWER is already reduced."""
    m = _load("red_n1c", "reduction/app.py", tmp_path, monkeypatch)
    raw = tmp_path / "2D" / "SAXS" / "s_0001.raw"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x")
    dat = tmp_path / "1D" / "SAXS" / "Reduction" / "s_0001_SAXS.dat"
    dat.parent.mkdir(parents=True)
    time.sleep(0.01)
    dat.write_text("# reduced\n")             # newer than the .raw
    assert m._already_reduced(raw, tmp_path / "1D"), \
        "an already-reduced frame was not recognised"
    # a .raw modified AFTER its .dat is genuinely new again
    time.sleep(0.01)
    raw.write_bytes(b"xy")
    assert not m._already_reduced(raw, tmp_path / "1D"), \
        "a re-acquired frame was wrongly skipped"


# ── N4: batch membership must be identities, not a count ─────────────────────
def test_a_missing_frame_does_not_shift_every_later_batch(tmp_path, monkeypatch):
    """Batch state was a COUNT and read_folder silently skips unreadable files,
    so one transient read failure shortened the group and shifted every
    subsequent slice: a frame silently reused in the next batch, another
    dropped, and if the count ever outran the group the keyword stopped
    averaging entirely — permanently, with nothing logged."""
    m = _load("avg_n4", "average/app.py", tmp_path, monkeypatch)
    key = ("SAXS", "sampleA")
    names = [f"sampleA_{i:03d}_SAXS.dat" for i in range(6)]

    m._mark_consumed(key, names[:3])                 # first batch done
    assert m._batch_number(key) == 2, "next batch should be #2"

    # Frame 1 becomes unreadable, so this cycle's group is missing it entirely.
    grp_now = [n for n in names if n != names[1]]
    todo = m._unconsumed(key, grp_now)
    assert names[3:] == todo, (
        "the next batch must be the frames not yet consumed, regardless of a "
        f"missing earlier frame — got {todo}")
    # and nothing already averaged comes back round a second time
    assert not set(todo) & set(names[:3]), "a consumed frame was reused"


# ── N3: a restart must not re-average the night ──────────────────────────────
def test_batch_state_survives_a_restart_and_a_boot_resume(tmp_path, monkeypatch):
    """monitor_start zeroed _avg_batch_state on EVERY start, including the boot
    resume, so a restart re-averaged everything: each batch file overwritten and
    one file.averaged per batch — which the reactor treats as
    measurement-complete and acts on."""
    m1 = _load("avg_n3a", "average/app.py", tmp_path, monkeypatch)
    key = ("SAXS", "sampleA")
    m1._mark_consumed(key, ["sampleA_000_SAXS.dat", "sampleA_001_SAXS.dat"])
    m1._save_batch_state()

    m2 = _load("avg_n3b", "average/app.py", tmp_path, monkeypatch)
    m2._load_batch_state()
    assert m2._unconsumed(key, ["sampleA_000_SAXS.dat", "sampleA_001_SAXS.dat",
                                "sampleA_002_SAXS.dat"]) == ["sampleA_002_SAXS.dat"], \
        "consumed frames were forgotten — the night re-averages"
    assert m2._batch_number(key) == 2, "batch numbering rewound and would overwrite"


def test_the_resume_path_keeps_batch_state_but_a_fresh_start_clears_it(tmp_path,
                                                                       monkeypatch):
    """The boot resume replays the saved body through the SAME endpoint the
    operator's Start button uses. Only the operator's start should reset."""
    m = _load("avg_n3c", "average/app.py", tmp_path, monkeypatch)
    key = ("SAXS", "sampleA")
    body = {"dets": ["SAXS"], "saxs_folder": str(tmp_path), "interval": 3600,
            "n_per_batch": 2}
    c = m.app.test_client()

    m._mark_consumed(key, ["a.dat", "b.dat"])
    m._save_batch_state()

    c.post("/api/monitor/start", json={**body, "resume": True})
    assert m._unconsumed(key, ["a.dat", "c.dat"]) == ["c.dat"], \
        "a RESUME wrongly cleared the batch state — the night re-averages"
    c.post("/api/monitor/stop")

    c.post("/api/monitor/start", json=body)          # operator pressed Start
    assert m._unconsumed(key, ["a.dat", "c.dat"]) == ["a.dat", "c.dat"], \
        "a fresh operator start should begin from scratch"
    c.post("/api/monitor/stop")
