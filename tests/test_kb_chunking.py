"""
tests/test_kb_chunking.py — the KB chunker must not explode into near-duplicate
fragments.

Regression for the Sept 2026 audit finding: `_chunk_text` advanced the window by
a SINGLE character whenever a paragraph/sentence break landed within the overlap
of the window start, emitting ~100 near-identical 1-char-shifted chunks per
section and inflating the vector DB ~60%. These assert forward progress.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.ai.knowledge import _chunk_text, _chunk_markdown, _CHUNK_SIZE, _MIN_CHUNK


def test_chunk_text_does_not_explode_on_an_early_break():
    # early "\n\n" then a long tail — the exact shape that used to stall the loop
    text = "Short intro paragraph." + "\n\n" + ("data " * 500)
    chunks = _chunk_text(text)
    # a ~2500-char body should be a handful of ~900-char chunks, not ~100 shards
    assert len(chunks) <= max(4, len(text) // (_CHUNK_SIZE // 2)), \
        f"degenerate chunking: {len(chunks)} chunks for {len(text)} chars"
    # no run of many chunks whose lengths only differ by ~1 (the tell-tale shards)
    lens = sorted(len(c) for c in chunks)
    tiny_cluster = sum(1 for a, b in zip(lens, lens[1:]) if 0 < b - a <= 2)
    assert tiny_cluster < 5, f"looks like 1-char-shifted shards: {lens[:12]}"


def test_chunk_text_covers_the_whole_document():
    text = ("alpha " * 100) + "\n\n" + ("beta " * 100) + "\n\n" + ("gamma " * 100)
    joined = " ".join(_chunk_text(text))
    for token in ("alpha", "beta", "gamma"):
        assert token in joined, f"{token} dropped by the chunker"


def test_chunk_text_progress_is_bounded_for_many_sizes():
    for n in (300, 1000, 2000, 5000):
        text = "Intro. \n\n" + ("x " * n)
        chunks = _chunk_text(text)
        assert len(chunks) <= (len(text) // (_CHUNK_SIZE - 180)) + 3, \
            f"n={n}: {len(chunks)} chunks is too many for {len(text)} chars"
        assert all(len(c) >= _MIN_CHUNK for c in chunks)


def test_markdown_sections_stay_whole_when_small():
    md = "# Title\n" + ("para " * 20) + "\n\n## Section B\n" + ("more " * 20)
    chunks = _chunk_markdown(md)
    assert len(chunks) <= 3, f"small sections over-chunked: {len(chunks)}"
