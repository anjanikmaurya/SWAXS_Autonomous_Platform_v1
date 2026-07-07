"""
src/ai/hints.py — Proactive Hint Checker
==========================================
Rule-based quality checks that run after event bus messages.
Each check returns a list of Hint namedtuples (never raises).
Fast — no API calls — designed to run synchronously in the event callback.

Checks implemented
------------------
  check_guinier_range   — qRg outside [0.3, 1.3] after a Guinier fit
  check_aggregation     — low-q upturn in averaged scattering curve
  check_radiation_damage— I₀ increasing over successive frames
  check_snr             — sigma/I too large at high q
  check_i0_stability    — I₀ outliers across scans in a keyword group
  check_background_scale— background scale factor suspiciously far from 1

Each check returns list[Hint] (may be empty).

Usage
-----
    from src.ai.hints import HintChecker

    checker = HintChecker()

    # After an analysis.complete event:
    hints = checker.on_analysis(event_data)
    for h in hints:
        print(h.severity, h.message)

    # After a file.averaged event (loads .dat automatically):
    hints = checker.on_file_averaged(event_data)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("swaxs_platform")


@dataclass
class Hint:
    """A single proactive hint from the rule-based checker."""
    severity:  str   # "info" | "warning" | "error"
    message:   str
    file_path: str | None = None
    check:     str = ""  # which check generated this


class HintChecker:
    """
    Stateless collection of rule-based quality checks.
    Instantiate once and call the appropriate ``on_*`` method after each
    event bus message.
    """

    # ── Event-triggered entry points ──────────────────────────────────────────

    def on_file_reduced(self, data: dict) -> list[Hint]:
        """Run checks relevant after a single frame is reduced."""
        hints: list[Hint] = []
        file_path = data.get("file_path")
        meta      = data.get("metadata", {})

        # I0 sanity
        i0 = _float(meta.get("i0"))
        if i0 is not None and i0 <= 0:
            hints.append(Hint(
                severity  = "warning",
                message   = f"I₀ ≤ 0 ({i0:.3g}) — check detector or beamline issue.",
                file_path = file_path,
                check     = "i0_positive",
            ))

        # Transmission sanity
        T = _float(meta.get("transmission") or meta.get("T"))
        if T is not None:
            if T > 1.05:
                hints.append(Hint(
                    severity  = "warning",
                    message   = f"Transmission = {T:.4f} > 1 — "
                                "check I₀ air/sample offsets in config.yml.",
                    file_path = file_path,
                    check     = "transmission_gt1",
                ))
            elif T < 0.02:
                hints.append(Hint(
                    severity  = "warning",
                    message   = f"Transmission = {T:.4f} is very low — "
                                "sample may be too thick or beam misaligned.",
                    file_path = file_path,
                    check     = "transmission_low",
                ))

        return hints

    def on_file_averaged(self, data: dict) -> list[Hint]:
        """Run checks on a freshly averaged .dat file."""
        hints: list[Hint] = []
        file_path = data.get("file_path")
        if not file_path or not Path(file_path).exists():
            return hints

        q, I, sigma = _load_dat(file_path)
        if q is None:
            return hints

        hints += self.check_aggregation(q, I, sigma, file_path=file_path)
        hints += self.check_snr(q, I, sigma, file_path=file_path)
        return hints

    def on_analysis(self, data: dict) -> list[Hint]:
        """Run checks on a completed analysis result."""
        hints:        list[Hint] = []
        atype         = data.get("analysis_type", "")
        file_path     = data.get("file_path")
        results       = data.get("results", {})

        if atype == "guinier":
            hints += self.check_guinier_range(
                Rg       = _float(results.get("Rg")),
                q_min    = _float(results.get("q_min")),
                q_max    = _float(results.get("q_max")),
                chi2     = _float(results.get("chi2")),
                file_path= file_path,
            )

        if atype in ("guinier", "pair_distance"):
            Rg  = _float(results.get("Rg"))
            Dmax= _float(results.get("Dmax"))
            if Rg and Dmax:
                hints += self.check_rg_dmax_ratio(Rg, Dmax, file_path=file_path)

        return hints

    def on_file_subtracted(self, data: dict) -> list[Hint]:
        """Run checks after background subtraction."""
        hints     = []
        file_path = data.get("file_path")
        scale     = _float(data.get("scale"))

        if scale is not None:
            hints += self.check_background_scale(scale, file_path=file_path)

        if file_path and Path(file_path).exists():
            q, I, sigma = _load_dat(file_path)
            if q is not None:
                hints += self.check_negative_intensities(q, I, file_path=file_path)

        return hints

    def on_keyword_frames(
        self,
        i0_values:  list[float],
        keyword:    str,
        file_paths: list[str] | None = None,
    ) -> list[Hint]:
        """Run I₀ stability check across all frames in a keyword group."""
        return self.check_i0_stability(
            i0_values,
            keyword    = keyword,
            file_paths = file_paths,
        )

    # ── Individual checks (also callable directly) ────────────────────────────

    def check_guinier_range(
        self,
        Rg:        float | None,
        q_min:     float | None,
        q_max:     float | None,
        chi2:      float | None = None,
        file_path: str | None = None,
    ) -> list[Hint]:
        hints = []
        if Rg is None or Rg <= 0 or q_min is None or q_max is None:
            return hints

        qRg_lo = q_min * Rg
        qRg_hi = q_max * Rg

        if qRg_lo < 0.25:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"Guinier range may include beam artefacts: "
                    f"q_min·Rg = {qRg_lo:.3f} < 0.25 (recommended ≥ 0.3). "
                    f"Consider raising q_min."
                ),
                file_path = file_path,
                check     = "guinier_qRg_lo",
            ))

        if qRg_hi > 1.3:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"Guinier range extends too far: "
                    f"q_max·Rg = {qRg_hi:.3f} > 1.3. "
                    f"The Guinier approximation breaks down above qRg ≈ 1.3 for "
                    f"globular particles (and ≈ 1.0 for extended/rod-like ones) — "
                    f"Rg may be underestimated. Try lowering q_max."
                ),
                file_path = file_path,
                check     = "guinier_qRg_hi",
            ))

        if chi2 is not None and chi2 > 2.0:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"Guinier fit quality is poor (χ² = {chi2:.2f} > 2.0). "
                    f"Possible causes: aggregation, radiation damage, "
                    f"incorrect fit range."
                ),
                file_path = file_path,
                check     = "guinier_chi2",
            ))

        if not hints:
            hints.append(Hint(
                severity  = "info",
                message   = (
                    f"Guinier range OK: qRg ∈ [{qRg_lo:.3f}, {qRg_hi:.3f}] "
                    f"(recommended [0.3, 1.3]).  Rg = {Rg:.2f} nm."
                ),
                file_path = file_path,
                check     = "guinier_range_ok",
            ))

        return hints

    def check_aggregation(
        self,
        q:         "np.ndarray",
        I:         "np.ndarray",
        sigma:     "np.ndarray | None" = None,
        file_path: str | None = None,
        *,
        upturn_threshold: float = 0.20,   # 20 % slope excess
    ) -> list[Hint]:
        """
        Detect a low-q upturn that may indicate aggregation or repulsion.
        Compares the slope of ln I vs ln q in the first 10 % of q range
        to the global slope.  A strongly negative slope at low q suggests
        large aggregates.
        """
        q, I = np.asarray(q, dtype=float), np.asarray(I, dtype=float)
        mask = (q > 0) & (I > 0)
        if mask.sum() < 20:
            return []

        lnq = np.log(q[mask])
        lnI = np.log(I[mask])

        n_lo = max(5, int(mask.sum() * 0.10))
        n_hi = max(5, int(mask.sum() * 0.50))

        # Slope in lowest 10 % of q
        slope_lo = np.polyfit(lnq[:n_lo], lnI[:n_lo], 1)[0]
        # Slope over bottom half
        slope_gl = np.polyfit(lnq[:n_hi], lnI[:n_hi], 1)[0]

        hints = []
        if slope_lo < slope_gl - upturn_threshold * abs(slope_gl) and slope_lo < -4:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"Possible aggregation or large-particle contribution: "
                    f"low-q slope ({slope_lo:.2f}) is steeper than mid-q slope "
                    f"({slope_gl:.2f}).  Check for sample aggregation; consider "
                    f"SEC-SAXS or re-centrifugation."
                ),
                file_path = file_path,
                check     = "aggregation",
            ))
        return hints

    def check_radiation_damage(
        self,
        i0_time_series: list[float],
        keyword:        str = "",
        threshold_pct:  float = 10.0,   # % increase triggers warning
    ) -> list[Hint]:
        """
        Detect radiation damage from a time-ordered list of I₀ or
        low-angle intensity values.  An increasing trend suggests damage.
        """
        if len(i0_time_series) < 3:
            return []

        arr  = np.asarray(i0_time_series, dtype=float)
        valid= arr[arr > 0]
        if len(valid) < 3:
            return []

        # Linear trend via polyfit
        x     = np.arange(len(valid), dtype=float)
        slope = np.polyfit(x, valid, 1)[0]
        pct   = slope / valid[0] * 100 if valid[0] > 0 else 0

        hints = []
        if pct > threshold_pct:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"Possible radiation damage in '{keyword}': "
                    f"low-angle intensity increases ~{pct:.1f}% over the "
                    f"{len(valid)} frames. Consider discarding later frames "
                    f"or using sigma-clipping averaging."
                ),
                file_path = None,
                check     = "radiation_damage",
            ))
        return hints

    def check_snr(
        self,
        q:         "np.ndarray",
        I:         "np.ndarray",
        sigma:     "np.ndarray | None",
        file_path: str | None = None,
        *,
        high_q_frac:    float = 0.15,  # use top 15 % of q range
        snr_threshold:  float = 0.5,   # sigma/I ratio threshold
    ) -> list[Hint]:
        """Flag poor signal-to-noise at high q."""
        if sigma is None:
            return []
        q, I, sig = (np.asarray(x, dtype=float) for x in (q, I, sigma))
        mask  = (q > 0) & (I > 0) & (sig > 0)
        if mask.sum() < 10:
            return []

        n_hi   = max(5, int(mask.sum() * high_q_frac))
        ratio  = (sig[mask] / I[mask])[-n_hi:].mean()

        hints = []
        if ratio > snr_threshold:
            hints.append(Hint(
                severity  = "info",
                message   = (
                    f"High-q signal-to-noise is marginal: "
                    f"mean σ/I = {ratio:.2f} in the top {int(high_q_frac*100)}% "
                    f"of q.  Consider truncating the curve at a lower q_max "
                    f"for analysis."
                ),
                file_path = file_path,
                check     = "snr",
            ))
        return hints

    def check_i0_stability(
        self,
        i0_values:  list[float],
        keyword:    str = "",
        threshold:  float = 20.0,   # % deviation from median
        file_paths: list[str] | None = None,
    ) -> list[Hint]:
        """Flag individual frames whose I₀ deviates significantly from the median."""
        if not i0_values:
            return []
        arr    = np.asarray(i0_values, dtype=float)
        valid  = arr[arr > 0]
        if len(valid) < 2:
            return []

        median = float(np.median(valid))
        if median <= 0:
            return []

        outliers = [
            (i, float(v)) for i, v in enumerate(arr)
            if v > 0 and abs(v - median) / median * 100 > threshold
        ]
        hints = []
        for idx, val in outliers:
            pct    = (val - median) / median * 100
            fp     = file_paths[idx] if file_paths and idx < len(file_paths) else None
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"I₀ outlier in '{keyword}' frame {idx}: "
                    f"{val:.3g} deviates {pct:+.1f}% from median "
                    f"({median:.3g}).  This frame may have had a beam glitch "
                    f"and should be excluded from averaging."
                ),
                file_path = fp,
                check     = "i0_stability",
            ))
        return hints

    def check_background_scale(
        self,
        scale:     float,
        file_path: str | None = None,
    ) -> list[Hint]:
        """Warn if the background scale factor is far from 1.0."""
        hints = []
        if scale < 0.5:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"Background scale factor = {scale:.4f} is unusually small. "
                    f"Check that sample and background were measured in the same "
                    f"conditions and concentrations."
                ),
                file_path = file_path,
                check     = "bkg_scale_low",
            ))
        elif scale > 1.5:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"Background scale factor = {scale:.4f} is unusually large. "
                    f"Verify that the correct background file is selected."
                ),
                file_path = file_path,
                check     = "bkg_scale_high",
            ))
        return hints

    def check_negative_intensities(
        self,
        q:         "np.ndarray",
        I:         "np.ndarray",
        file_path: str | None = None,
        *,
        threshold_frac: float = 0.05,  # warn if > 5% of points are negative
    ) -> list[Hint]:
        """Flag significant negative intensities after background subtraction."""
        q, I    = np.asarray(q, dtype=float), np.asarray(I, dtype=float)
        n_neg   = (I < 0).sum()
        frac    = n_neg / len(I)
        hints   = []
        if frac > threshold_frac:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"{n_neg} points ({frac*100:.1f}%) have negative intensity "
                    f"after background subtraction. "
                    f"Consider reducing the scale factor or revisiting the "
                    f"background assignment."
                ),
                file_path = file_path,
                check     = "negative_intensity",
            ))
        return hints

    def check_rg_dmax_ratio(
        self,
        Rg:        float,
        Dmax:      float,
        file_path: str | None = None,
    ) -> list[Hint]:
        """
        Check that the Rg / Dmax ratio is physically reasonable.
        For compact globular particles: Rg ≈ 0.77 * Dmax/2 → ratio ~0.385.
        """
        ratio  = Rg / Dmax if Dmax > 0 else 0
        hints  = []
        if ratio > 0.6:
            hints.append(Hint(
                severity  = "warning",
                message   = (
                    f"Rg/Dmax = {ratio:.3f} is high (expected ≤ 0.4 for "
                    f"globular particles).  Either Rg is overestimated or "
                    f"Dmax is underestimated — review both fit ranges."
                ),
                file_path = file_path,
                check     = "rg_dmax_ratio",
            ))
        return hints


# ── Helpers ───────────────────────────────────────────────────────────────────

def _float(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _load_dat(file_path: str) -> tuple:
    """Load q, I, sigma from a .dat file. Returns (None, None, None) on failure."""
    try:
        from src.utils.read_dat_metadata import read_dat_data_metadata
        _, q, I, sigma, _ = read_dat_data_metadata(file_path)
        return q, I, sigma
    except Exception as exc:
        logger.debug("[Hints] Could not load %s: %s", file_path, exc)
        return None, None, None
