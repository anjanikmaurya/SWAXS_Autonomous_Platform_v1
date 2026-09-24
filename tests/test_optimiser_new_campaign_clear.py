"""Producer-owned staleness: the optimiser sets aside leftover conditions when a
NEW campaign starts.

Added 2026-09-24 with the order-free startup policy. The reactor no longer clears
its queue on boot (that is what makes startup order-free), so a new campaign must
not dose stale conditions from a previous/aborted run against the new target.
_clear_conditions_for_new_campaign() moves them to Conditions/done/ with a note;
nothing is deleted. Resume/continue must NOT clear (tested by its absence there).
"""
import importlib

import pytest

a = importlib.import_module("analyzer.app")


@pytest.fixture
def cond_dir(tmp_path, monkeypatch):
    d = tmp_path / "1D" / "SAXS" / "Conditions"
    d.mkdir(parents=True)
    monkeypatch.setattr(a, "_project_root", str(tmp_path))
    monkeypatch.setattr(a, "_cond_folder", "1D/SAXS/Conditions")
    return d


def _drop(d, rid, suffix=".txt"):
    (d / f"{rid}{suffix}").write_text(
        "recipe_id = %s\nT_reac = 240.0\nF_tot = 80.0\n" % rid, encoding="utf-8")


def test_new_campaign_sets_aside_leftovers(cond_dir):
    _drop(cond_dir, "Run7_r001")
    _drop(cond_dir, "Run7_r002")
    n = a._clear_conditions_for_new_campaign()
    assert n == 2
    # originals gone from the watched folder, preserved in done/
    assert sorted(p.name for p in cond_dir.glob("*.txt")) == []
    moved = sorted(p.name for p in (cond_dir / "done").glob("*.txt"))
    assert moved == ["Run7_r001.txt", "Run7_r002.txt"]


def test_moved_file_is_recoverable_and_annotated(cond_dir):
    _drop(cond_dir, "Run7_r001")
    a._clear_conditions_for_new_campaign()
    body = (cond_dir / "done" / "Run7_r001.txt").read_text()
    assert "T_reac = 240" in body, "the recipe itself was not preserved"
    assert "NOT RUN" in body
    assert "new campaign" in body
    assert "Move this file back" in body


def test_covers_dat_txt_json(cond_dir):
    _drop(cond_dir, "Run7_r001", ".txt")
    _drop(cond_dir, "Run7_r002", ".dat")
    (cond_dir / "Run7_r003.json").write_text('{"recipe_id": "Run7_r003"}', encoding="utf-8")
    assert a._clear_conditions_for_new_campaign() == 3


def test_empty_folder_is_a_noop(cond_dir):
    assert a._clear_conditions_for_new_campaign() == 0
    assert not (cond_dir / "done").exists() or not any((cond_dir / "done").iterdir())


def test_existing_done_subfolder_is_not_swept(cond_dir):
    done = cond_dir / "done"
    done.mkdir()
    _drop(done, "Run6_r009")            # already retired — must be left alone
    _drop(cond_dir, "Run7_r001")        # the only live leftover
    assert a._clear_conditions_for_new_campaign() == 1
    # the previously-done file is untouched
    assert (done / "Run6_r009.txt").exists()
