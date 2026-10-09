"""Least-squares polydisperse-sphere fitter — the current, physics-based method.

A thin wrapper around src.analysis.nanoparticle.analyze_profile so the fit the
closed loop sees is byte-identical to before the methods refactor. All the maths
stays in nanoparticle.py; this only adapts it to the Fitter interface.
"""
from __future__ import annotations

from src.analysis.nanoparticle import analyze_profile
from . import register


class LeastSquaresSphere:
    id = "ls_sphere"
    label = "Least-squares sphere"
    ready = True

    def fit(self, q, I, sigma=None, **opts) -> dict:
        # dist: 'schulz' | 'lognormal' | 'auto' (fit both, keep the better)
        return analyze_profile(q, I, sigma, dist=opts.get("dist", "auto"))


register(LeastSquaresSphere())
