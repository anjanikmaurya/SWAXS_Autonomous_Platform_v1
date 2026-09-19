"""
tests/test_auto_average_start_cost.py

"Start auto-averaging" sat unresponsive for a long time on a folder with
previous runs in it, while the subtraction app's identical button felt
instant. The operator's diagnosis was right: the first press was working
through the files already averaged. The detail that made it slow, though, was
not the counting.

`_seed_batch_state_from_disk` needs to know, for each .dat in the Reduction
folder, which group it belongs to and where it sits in the sequence. Both come
from the FILENAME. It was getting them from `read_folder`, which opens and
parses every .dat into q/I/sigma numpy arrays — and it ran synchronously
inside POST /api/monitor/start, so the button could not answer until the whole
back-catalogue had been read and thrown away.

Measured on 2000 frames of 1000 points (150 MB) on a fast local disk:
read_folder 1.90 s, list_folder_index 0.01 s. A real beamtime folder is larger
and often on a network mount, where the gap is minutes rather than seconds.

The subtraction app felt fast because its seed only globs and stats — it never
opens a file. These tests hold average to the same standard.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.plot_reduction import (list_folder_index, read_folder,   # noqa: E402
                                keyword_and_index, clear_read_cache)


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "Reduction"
    d.mkdir()
    q = np.geomspace(0.01, 1.0, 400)
    for i in range(60):
        np.savetxt(d / f"Run21_r{i // 10:03d}_sample_scan1_{i % 10:04d}_SAXS.dat",
                   np.c_[q, np.full_like(q, 2.0), np.full_like(q, 0.1)])
    return d


# ── it must agree with the parser it replaced ───────────────────────────────
def test_the_cheap_index_groups_and_orders_exactly_like_read_folder(folder):
    """If these ever disagree, a seed would mark the wrong frames consumed and
    the loop would either re-average or silently skip real data."""
    clear_read_cache()
    full = read_folder(folder)
    cheap = list_folder_index(folder)
    assert len(full) == len(cheap)
    for a, b in zip(full, cheap):
        assert (a["filename"], a["keyword"], a["scan_idx"]) == \
               (b["filename"], b["keyword"], b["scan_idx"])


@pytest.mark.parametrize("name,keyword,idx", [
    ("Run21_r003_sample_scan1_0007_SAXS.dat", "Run21_r003_sample_scan1", 7),
    ("buffer_0012.dat", "buffer", 12),
    ("PIP_TMC_batch001_10files_Average.dat", "PIP_TMC_batch001", 0),
    ("no_index_here.dat", "no_index_here", 0),
])
def test_the_filename_rules_are_unchanged(name, keyword, idx):
    """Factoring this out of read_folder must not have changed what it says."""
    assert keyword_and_index(name) == (keyword, idx)


def test_keywords_filter_the_same_way(folder):
    assert list_folder_index(folder, keywords=["r000"]) == \
        [e for e in list_folder_index(folder) if "r000" in e["filename"]]


def test_a_missing_folder_is_empty_not_an_exception(tmp_path):
    """read_folder RAISES FileNotFoundError; the seed runs on the request
    thread and must not turn a not-yet-created folder into a 500."""
    assert list_folder_index(tmp_path / "nope") == []


# ── it must not open files ──────────────────────────────────────────────────
def test_not_one_file_is_opened(folder, monkeypatch):
    """The point of the change. Asserted by banning open() outright rather
    than by timing, which is flaky on a loaded machine."""
    import builtins
    real_open = builtins.open
    opened = []

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    list_folder_index(folder)
    dats = [p for p in opened if str(p).endswith(".dat")]
    assert not dats, f"list_folder_index opened {len(dats)} .dat file(s)"


def test_it_is_much_faster_than_parsing(folder):
    """A loose bound — the measured gap is ~200x, so 5x fails only if the
    cheap path has started reading files again."""
    clear_read_cache()
    t0 = time.perf_counter(); read_folder(folder); t_read = time.perf_counter() - t0
    t0 = time.perf_counter(); list_folder_index(folder); t_list = time.perf_counter() - t0
    assert t_list * 5 < t_read, (
        f"list_folder_index ({t_list:.3f}s) is not meaningfully cheaper than "
        f"read_folder ({t_read:.3f}s)")


# ── the start path stays cheap ──────────────────────────────────────────────
def test_the_seed_does_not_parse_the_reduction_folder():
    """read_folder on the Start path is the regression. It belongs in the
    monitor loop, on a background thread, with skip_names — not in the request
    handler."""
    src = (_ROOT / "average" / "app.py").read_text()
    seed = src.split("def _seed_batch_state_from_disk")[1].split("\ndef ")[0]
    code = "\n".join(l for l in seed.splitlines()
                     if not l.strip().startswith("#"))
    assert "read_folder(" not in code, \
        "the seed parses the Reduction folder again — Start will hang on it"
    assert "list_folder_index(" in code


def test_the_subtraction_seed_still_only_stats():
    """The standard average is being held to. background's seed reads nothing;
    if that changes, its Start button gets the same disease."""
    src = (_ROOT / "background" / "app.py").read_text()
    seed = src.split("def _seed_sub_done_from_disk")[1].split("\ndef ")[0]
    code = "\n".join(l for l in seed.splitlines()
                     if not l.strip().startswith("#"))
    for parser in ("read_folder(", "read_dat_data_metadata(", "np.loadtxt("):
        assert parser not in code, f"background's seed now calls {parser}"
