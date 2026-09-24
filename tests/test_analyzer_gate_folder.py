"""Analyzer watched-folder resolution and the empty-Good/ starvation trap.

Added 2026-09-24: with the Quality Gate NOT running, a leftover empty
``Subtracted/Good/`` folder used to divert the analyzer to it the moment the
directory existed — so it watched an empty folder, fitted nothing, the
optimizer never got a measurement, and the autonomous loop silently stalled.
Auto mode now keys on Good/ HAVING data. `mode="good"` stays strict;
`mode="off"` always reads the flat folder.
"""
import importlib

import pytest

a = importlib.import_module("analyzer.app")


@pytest.fixture
def project(tmp_path, monkeypatch):
    sub = tmp_path / "1D" / "SAXS" / "Subtracted"
    sub.mkdir(parents=True)
    (sub / "Run5_r012_sample_sub.dat").write_text("q I sigma\n")
    monkeypatch.setattr(a, "_project_root", str(tmp_path))
    monkeypatch.setattr(a, "_sub_folder", "1D/SAXS/Subtracted")
    monkeypatch.setattr(a, "_gate_note_shown", False)
    return sub


def test_auto_no_good_reads_flat(project, monkeypatch):
    monkeypatch.setattr(a, "_gate_mode", "auto")
    assert a._resolve_sub() == project


def test_auto_empty_good_falls_back_to_flat(project, monkeypatch):
    """The bug: an empty Good/ must NOT capture the analyzer."""
    (project / "Good").mkdir()
    monkeypatch.setattr(a, "_gate_mode", "auto")
    monkeypatch.setattr(a, "_gate_note_shown", False)
    assert a._resolve_sub() == project          # flat, not Good/


def test_auto_populated_good_is_honoured(project, monkeypatch):
    good = project / "Good"
    good.mkdir()
    (good / "Run5_r012_sample_sub.dat").write_text("q I sigma\n")
    monkeypatch.setattr(a, "_gate_mode", "auto")
    monkeypatch.setattr(a, "_gate_note_shown", False)
    assert a._resolve_sub() == good


def test_mode_good_is_strict_even_when_empty(project, monkeypatch):
    good = project / "Good"
    good.mkdir()
    monkeypatch.setattr(a, "_gate_mode", "good")
    monkeypatch.setattr(a, "_gate_note_shown", False)
    assert a._resolve_sub() == good


def test_mode_off_always_flat(project, monkeypatch):
    good = project / "Good"
    good.mkdir()
    (good / "x_sample_sub.dat").write_text("q I sigma\n")
    monkeypatch.setattr(a, "_gate_mode", "off")
    monkeypatch.setattr(a, "_gate_note_shown", False)
    assert a._resolve_sub() == project
