"""
tests/test_reduction_reprocess_is_optional.py

Restarting the reduction app re-reduced the whole folder.

`_already_reduced()` was supposed to prevent that, but it compares the .raw
mtime with its .dat — so anything that re-stamps the raw files (the SFTP pull,
a two-laptop sync, a restore from backup, `cp` without `-p`) makes the entire
back-catalogue look newer than its own output. The check then says "new" for
every frame and the folder is reduced from scratch, oldest first, with the live
frames of the run the operator just started queued behind it. There was no way
to say no.

So reducing what is already in the folder is now opt-in
(`reprocess_existing`), and these tests pin the three things that must not
regress:

  1. the default skips existing work even when every mtime says "new";
  2. the opt-in really does redo it — clearing the processed set alone was not
     enough, because the mtime check would still have skipped the frames;
  3. a restart never inherits the opt-in.
"""
from __future__ import annotations

import importlib.util as u
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")

from src.backlog import CRASH_GAP_WINDOW_S            # noqa: E402


@pytest.fixture(scope="module")
def red():
    spec = u.spec_from_file_location("red_reproc", _ROOT / "reduction" / "app.py")
    mod = u.module_from_spec(spec)
    sys.modules["red_reproc"] = mod
    spec.loader.exec_module(mod)
    return mod


def _folder(tmp_path, n=5, *, with_dat=True, age_s=3600.0):
    """A project folder holding `n` frames, every .raw stamped NEWER than its
    .dat — the exact state an SFTP pull or a laptop sync leaves behind."""
    raws = []
    out = tmp_path / "1D"
    (out / "SAXS" / "Reduction").mkdir(parents=True)
    now = time.time()
    for i in range(n):
        r = tmp_path / "2D" / "SAXS" / f"scan_{i:03d}.raw"
        r.parent.mkdir(parents=True, exist_ok=True)
        r.write_bytes(b"x")
        os.utime(r, (now - age_s, now - age_s))
        raws.append(r)
        if with_dat:
            d = out / "SAXS" / "Reduction" / f"scan_{i:03d}_SAXS.dat"
            d.write_text("# q I\n")
            # .dat OLDER than its .raw: _already_reduced() alone says "new".
            os.utime(d, (now - age_s - 60, now - age_s - 60))
    return raws, out


# ── 1. the default ──────────────────────────────────────────────────────────
def test_the_mtime_check_alone_is_fooled_by_a_restamped_raw(red, tmp_path):
    """The premise. If this ever starts passing as `True`, the bug this guards
    against has been fixed elsewhere and the test below needs rewriting."""
    raws, out = _folder(tmp_path)
    assert red._already_reduced(raws[0], out, ("", "")) is False, (
        "a .raw newer than its .dat reads as unreduced — that is the hole")


def test_seeding_claims_the_existing_folder_even_when_mtimes_say_new(red, tmp_path,
                                                                     monkeypatch):
    raws, out = _folder(tmp_path)
    monkeypatch.setattr(red.reduction_core, "find_new_raw_files",
                        lambda cfg, seen: (list(raws), []))
    red._processed_files.clear()

    note = red._seed_processed_from_disk({}, out)

    assert len(red._processed_files) == len(raws), \
        "a fresh Start must not re-reduce frames that already have output"
    assert "new run, not a re-run" in note, "the skip has to be said out loud"


def test_a_frame_from_just_before_a_crash_is_still_reduced(red, tmp_path,
                                                           monkeypatch):
    """Skipping the back-catalogue must not swallow the one frame that landed
    seconds before the process died — that frame has no output and is young."""
    raws, out = _folder(tmp_path, n=3, with_dat=True)
    fresh = tmp_path / "2D" / "SAXS" / "scan_099.raw"
    fresh.write_bytes(b"x")                      # no .dat, mtime = now
    monkeypatch.setattr(red.reduction_core, "find_new_raw_files",
                        lambda cfg, seen: (list(raws) + [fresh], []))
    red._processed_files.clear()

    red._seed_processed_from_disk({}, out)

    assert str(fresh) not in red._processed_files
    assert all(str(r) in red._processed_files for r in raws)


def test_an_old_frame_with_no_output_is_history_not_backlog(red, tmp_path,
                                                            monkeypatch):
    raws, out = _folder(tmp_path, n=0)
    old = tmp_path / "2D" / "SAXS" / "ancient.raw"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"x")
    stamp = time.time() - CRASH_GAP_WINDOW_S - 60
    os.utime(old, (stamp, stamp))
    monkeypatch.setattr(red.reduction_core, "find_new_raw_files",
                        lambda cfg, seen: ([old], []))
    red._processed_files.clear()

    red._seed_processed_from_disk({}, out)
    assert str(old) in red._processed_files


def test_seeding_never_raises_when_the_folder_cannot_be_listed(red, tmp_path,
                                                               monkeypatch):
    """Bookkeeping must not be able to stop a beamtime."""
    def boom(cfg, seen):
        raise OSError("mount went away")
    monkeypatch.setattr(red.reduction_core, "find_new_raw_files", boom)
    assert red._seed_processed_from_disk({}, tmp_path) == ""


# ── 2. the opt-in ───────────────────────────────────────────────────────────
def test_the_optin_is_wired_past_both_defences(red):
    """Clearing `_processed_files` is only half of it: with the set empty the
    loop's `_already_reduced` filter would still drop every frame that has a
    newer .dat. `reprocess` has to bypass that filter too, or the checkbox
    silently does nothing on a normally-stamped folder."""
    src = (_ROOT / "reduction" / "app.py").read_text()
    assert "reprocess_existing" in src
    assert "(lambda f: False) if reprocess" in src, \
        "the newer-.dat filter is not bypassed when reprocessing was asked for"


# ── 3. a restart is not a re-run request ────────────────────────────────────
def test_a_restart_does_not_inherit_the_optin(red, monkeypatch):
    """The saved monitor params are replayed verbatim on boot. If the last run
    was started with the box ticked, a 3 a.m. crash would otherwise reduce the
    whole folder again while the beamline is still producing frames."""
    monkeypatch.setattr(red, "load_monitor",
                        lambda root, app: {"config": {"data_directory": "/x"},
                                           "interval": 5,
                                           "reprocess_existing": True})
    monkeypatch.setattr(red.time, "sleep", lambda *_: None)

    sent = {}

    class _RV:
        status_code = 200
        def get_json(self, silent=False):
            return {"ok": True}

    class _Client:
        def post(self, path, json=None):
            sent.update(json or {})
            return _RV()

    monkeypatch.setattr(red.app, "test_client", lambda: _Client())
    red._boot_resume_monitor()

    assert sent.get("reprocess_existing") is False, \
        "a restart continues a run; it does not decide to redo the folder"
    assert sent.get("interval") == 5, "the rest of the saved settings must survive"
