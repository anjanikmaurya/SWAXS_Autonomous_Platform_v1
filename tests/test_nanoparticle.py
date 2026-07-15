"""
tests/test_nanoparticle.py — the autopilot's SAXS analyzer (size, PDI, phase).

Validates the deterministic heart of the closed loop:
  • recovery of a KNOWN radius + PDI from synthetic polydisperse-sphere data,
  • honest confidence (high for a clean sphere fit, ~0 for a non-sphere),
  • ordered-phase (superlattice) indexing from Bragg-peak ratios,
  • graceful, non-raising degradation on junk / tiny input.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analysis.nanoparticle import (analyze_profile, model_intensity,     # noqa: E402
                                       index_phase, detect_bragg_peaks)


def _synthetic_spheres(R, pdi, dist="schulz", noise=0.03, seed=0):
    rng = np.random.default_rng(seed)
    q = np.linspace(0.02, 3.0, 400)
    I = model_intensity(q, R, pdi, scale=1e-6, bkg=0.0, dist=dist)
    I = I * (1 + noise * rng.standard_normal(q.size)) + I.max() * 1e-4
    return q, I


def test_recovers_known_size_and_pdi():
    q, I = _synthetic_spheres(8.0, 0.12)
    r = analyze_profile(q, I, dist="auto")
    assert r["size"]["source"] == "form_factor"
    assert abs(r["size"]["radius"] - 8.0) / 8.0 < 0.10      # radius within 10%
    assert abs(r["pdi"] - 0.12) < 0.04                       # PDI within 0.04
    assert r["confidence"] > 0.7                             # clean sphere → high confidence


def test_lognormal_data_is_fit():
    q, I = _synthetic_spheres(5.0, 0.18, dist="lognormal")
    r = analyze_profile(q, I, dist="auto")
    assert abs(r["size"]["radius"] - 5.0) / 5.0 < 0.15
    assert r["confidence"] > 0.5


def test_non_sphere_is_low_confidence():
    # a featureless power law is not a sphere → confidence must be low
    q = np.linspace(0.02, 3.0, 300)
    I = q ** -3.0
    r = analyze_profile(q, I, dist="auto")
    assert r["confidence"] < 0.3


def test_phase_indexing_from_ratios():
    assert index_phase([0.25, 0.25 * np.sqrt(3), 0.5])["phase"] == "hexagonal"
    assert index_phase([0.2, 0.4, 0.6])["phase"] == "lamellar"
    assert index_phase([0.25])["phase"] == "none"            # need ≥2 peaks


def test_phase_detected_in_profile():
    q = np.linspace(0.02, 3.0, 700)
    base = model_intensity(q, 6.0, 0.10, 1e-6, 0.0, "schulz")
    q1 = 0.25
    g = lambda c, a, w: a * np.exp(-0.5 * ((q - c) / w) ** 2)
    I = base + g(q1, base.max() * 0.6, 0.006) + g(q1 * np.sqrt(3), base.max() * 0.3, 0.007) \
        + g(2 * q1, base.max() * 0.2, 0.008)
    r = analyze_profile(q, I)
    assert r["phase"]["phase"] == "hexagonal"
    assert r["phase"]["n_peaks"] >= 3


def test_graceful_on_junk():
    r = analyze_profile(np.array([1.0, 2.0, 3.0]), np.array([1.0, 1.0, 1.0]))
    assert r["confidence"] == 0.0 and r["size"] is None       # too few points, no crash
