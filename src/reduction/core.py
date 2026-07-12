#!/usr/bin/env python3
"""
src/reduction/core.py — SWAXS Data Correction and 1D Integration
=================================================================
Designed for continuous, uninterrupted multi-day operation at the beamline.

Memory strategy
---------------
* Process ONE file at a time.
* After integrate1d() writes the .dat, the raw detector array and all
  intermediate numpy arrays are explicitly deleted and gc.collect() is called.
* Results returned from process_saxs_file / process_waxs_file contain ONLY
  small scalar values (corrections summary, output filename) — never the full
  detector array or the q/I/err vectors.  Those are written to disk and freed.
* run_pipeline() never accumulates a list of results.  It returns only counts.

Experiment caching
------------------
The Experiment object keeps PyFAI AzimuthalIntegrator objects loaded in RAM
between files.  Loading a PONI file is slow; keeping the integrator alive
avoids that cost for every subsequent file.  Callers (app.py) should keep ONE
Experiment instance alive for the lifetime of a monitoring session and only
recreate it if the config changes.
"""

import gc
import logging
import os
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import fabio
import numpy as np
import pyFAI
import xraydb

from . import process_metadata
from . import read_raw_file

__all__ = [
    "Experiment",           # Main class: holds PyFAI integrators, runs corrections
    "run_pipeline",         # Process all new .raw files in a folder
    "find_new_raw_files",   # Scan for unprocessed .raw files
    "resolve_normalization",# Collapse overlapping normalization terms to one mode
    "_fmt_result_line",     # Format a result dict into a log string (used by app.py)
]

logger = logging.getLogger("swaxs_pipeline")
logging.getLogger("pyFAI").setLevel(logging.ERROR)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_VALID_NORM_TERMS = ("bstop", "i0", "absolute")


def resolve_normalization(raw_norm) -> Tuple[List[str], List[str]]:
    """
    Collapse a list of normalization terms to ONE physically-meaningful mode.

    The terms overlap, so multiplying them is non-physical:
      * "absolute" already includes bstop (NF = bstop·d/K).
      * dividing by both "i0" and "bstop" double-counts the flux (counts/(I0²·T)).

    Resolution rules:
      * unknown terms are dropped (with a warning),
      * "absolute" + anything  → ["absolute"],
      * "i0" + "bstop"         → ["bstop"]  (keep transmission correction),
      * otherwise the de-duplicated single term is kept.

    Parameters
    ----------
    raw_norm : str | list[str] | None
        The ``normalization`` config value.

    Returns
    -------
    (resolved, warnings) : tuple[list[str], list[str]]
        ``resolved`` is a 0- or 1-element list; ``warnings`` are human-readable
        messages the caller should log.
    """
    if raw_norm is None:
        raw_norm = ["bstop"]
    if isinstance(raw_norm, str):
        raw_norm = [raw_norm]

    warnings: List[str] = []
    cleaned: List[str] = []
    for term in raw_norm:
        t = str(term).lower().strip()
        if not t:
            continue
        if t not in _VALID_NORM_TERMS:
            warnings.append(
                f"Unknown normalization term '{t}' ignored. "
                f"Valid terms: {set(_VALID_NORM_TERMS)}."
            )
            continue
        cleaned.append(t)

    seq = list(dict.fromkeys(cleaned))   # de-dupe, preserve order

    if "absolute" in seq and len(seq) > 1:
        dropped = [t for t in seq if t != "absolute"]
        warnings.append(
            f"normalization combines 'absolute' with {dropped}; 'absolute' "
            f"already includes the bstop·thickness term. Using 'absolute' alone."
        )
        seq = ["absolute"]
    elif "i0" in seq and "bstop" in seq:
        warnings.append(
            "normalization combines 'i0' and 'bstop', which double-normalizes "
            "the flux (counts/(I0²·T)). Using 'bstop' (flux × transmission)."
        )
        seq = ["bstop"]

    return seq, warnings


def _extract_ctemp(metadata: dict) -> Optional[float]:
    """
    Try common key names for sample temperature in beamline metadata.
    Returns the temperature as a float (°C or K depending on beamline),
    or None if no temperature field is present.
    """
    for k in ("ctemp", "CTEMP", "sample_temp", "SampleTemp",
              "temperature", "Temperature", "temp", "Temp", "T_sample"):
        v = metadata.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Experiment class
# ─────────────────────────────────────────────────────────────────────────────

class Experiment:
    """
    Holds PyFAI integrators and config for one processing session.

    Create ONE instance per monitoring session and keep it alive.
    process_saxs_file / process_waxs_file are safe to call repeatedly
    from a single background thread — each call frees all large arrays
    before returning.
    """

    def __init__(self, config: dict, log_callback: Optional[Callable] = None):
        self._log = log_callback or (lambda msg, tag="info": None)

        self.data_directory   = Path(config["data_directory"])
        self.poni_directory   = Path(config["poni_directory"])
        self.poni_files       = config["poni_files"]
        self.mask_files       = config["mask_files"]
        self.detector_shapes  = config["detector_shapes"]
        self.compound         = config["compound"]
        self.energy_keV       = float(config["energy_keV"])
        self.density_g_cm3    = float(config["density_g_cm3"])
        self.thickness        = config.get("thickness")
        self.mode             = config["mode"].upper()
        self.metadata_format  = config["metadata_format"]
        self.npt_radial       = int(config["npt_radial"])
        self.error_model      = config["error_model"]
        self.i0_offset        = float(config.get("i0_offset",    0.0))
        self.bstop_offset     = float(config.get("bstop_offset", 0.0))
        # Air (empty-beam) measurements — used to normalise out the air-path
        # attenuation and compute the true sample transmission:
        #   T_sample = (bstop_corr/i0_corr) / (bstop_air_corr/i0_air_corr)
        # Both must be > 0 for air normalisation to be applied.
        self.i0_air           = float(config.get("i0_air",    0.0))
        self.bstop_air        = float(config.get("bstop_air", 0.0))
        self.saxs_prefix      = config.get("saxs_filename_prefix", "")
        self.waxs_prefix      = config.get("waxs_filename_prefix", "")
        beamline              = config.get("beamline", {"data_format": "raw"})
        self.data_format      = "." + beamline["data_format"]

        # Normalization options — any subset of {"bstop", "i0", "absolute"}.
        # The PyFAI normalization_factor is the PRODUCT of all selected terms.
        # An empty list means raw integrated counts (no normalization).
        #   "bstop"    → divide by transmitted-beam monitor (∝ Φ₀·T)
        #   "i0"       → divide by incident-beam monitor (∝ Φ₀)
        #   "absolute" → divide by bstop × thickness_cm  (gives cm⁻¹ units)
        self.normalization, _norm_warnings = resolve_normalization(
            config.get("normalization", ["bstop"])
        )
        for _w in _norm_warnings:
            logger.warning(_w)
            self._log(f"  ⚠ {_w}", "warn")

        # "i0" alone normalizes by incident flux only — it does NOT correct for
        # sample absorption/transmission.  Make that explicit.  (Audit fix 3.4)
        if self.normalization == ["i0"]:
            self._log(
                "  ⚠ 'i0' normalization corrects incident flux only — NOT sample "
                "absorption/transmission.", "warn"
            )

        if not self.data_directory.exists():
            raise RuntimeError(f"Data directory not found: {self.data_directory}")
        if not self.poni_directory.exists():
            raise RuntimeError(f"PONI directory not found: {self.poni_directory}")
        if self.mode not in ("SAXS", "WAXS", "SWAXS"):
            raise ValueError(f"mode must be SAXS, WAXS, or SWAXS — got '{self.mode}'")

        # Allow caller to specify an explicit output directory; default to
        # sibling "1D" folder next to the data_directory.
        _out = (config.get("output_directory") or "").strip()
        self.output_dir_1d = Path(_out) if _out else self.data_directory.parent / "1D"
        self.output_dir_1d.mkdir(parents=True, exist_ok=True)
        self._log(f"  Output directory: {self.output_dir_1d}", "info")

        _norm_display = {
            "bstop": "Bstop", "i0": "I₀", "absolute": "Absolute (Bstop×d)"
        }
        norm_label = (
            " + ".join(_norm_display.get(n, n) for n in self.normalization)
            if self.normalization else "None (raw counts)"
        )
        self._log(f"  Normalization:    {norm_label}", "info")

        # Absolute calibration constant K (from water or glassy-carbon standard).
        # Only meaningful when "absolute" is in self.normalization.
        # K > 0 converts semi-absolute → true absolute:
        #   norm_factor = (bstop × d_cm) / K
        #   → I(q) = K × counts / (bstop × d_cm)  [cm⁻¹]
        # K = 1.0 means uncalibrated (semi-absolute, same as before).
        self.calibration_factor = float(config.get("absolute_calibration_factor", 1.0))
        if self.calibration_factor <= 0:
            logger.warning("absolute_calibration_factor must be > 0; defaulting to 1.0")
            self.calibration_factor = 1.0
        if "absolute" in self.normalization:
            if self.calibration_factor == 1.0:
                self._log("  Calibration K:    1.0 (semi-absolute; set K from water/GC standard for true cm⁻¹)", "warn")
            else:
                self._log(f"  Calibration K:    {self.calibration_factor:.6g}", "info")

        # ── PyFAI integration parameters ────────────────────────────────────
        # unit — q-axis output unit passed to integrate1d
        #   "q_nm^-1" → nm⁻¹ (DEFAULT — matches the rest of the platform:
        #               averaging writer, analysis, Guinier/Dmax interpretation)
        #   "q_A^-1"  → Å⁻¹
        #   "2th_deg" → 2θ in degrees
        #   "2th_rad" → 2θ in radians
        # NOTE: the platform's downstream modules assume nm⁻¹.  If you override
        # this, Rg/Dmax magnitudes will be in the chosen unit (dimensionless
        # qRg checks are unaffected).  (Audit fix 3.3)
        self.unit = config.get("unit", "q_nm^-1")

        # Solid angle correction (cos³θ factor, almost always True)
        self.correct_solid_angle = bool(config.get("correct_solid_angle", True))

        # Polarization correction factor p ∈ [-1, 1]
        #   +1 → horizontal,  -1 → vertical,  0 → unpolarised
        #   ~0.99 is typical for a synchrotron undulator or wiggler
        #   None → PyFAI skips the correction
        pol = config.get("polarization_factor")
        self.polarization_factor = float(pol) if pol is not None else None

        # Radial (q or 2θ) integration range — (min, max) in the chosen unit
        r_min = config.get("radial_range_min")
        r_max = config.get("radial_range_max")
        self.radial_range = (
            (float(r_min), float(r_max))
            if (r_min is not None and r_max is not None) else None
        )

        # Azimuthal (χ) range in degrees — (min, max); None = full circle
        a_min = config.get("azimuth_range_min")
        a_max = config.get("azimuth_range_max")
        self.azimuth_range = (
            (float(a_min), float(a_max))
            if (a_min is not None and a_max is not None) else None
        )

        # Dummy / delta-dummy — pixel values to treat as masked
        dum = config.get("dummy")
        self.dummy       = float(dum) if dum is not None else None
        ddm = config.get("delta_dummy")
        self.delta_dummy = float(ddm) if ddm is not None else None

        # Dark-current frame files (2D, one per detector)
        # Subtracted pixel-by-pixel from detector image before integration.
        # Different from the scalar i0_offset / bstop_offset corrections.
        self.dark_file_cfg = config.get("dark_files") or {}
        self.flat_file_cfg = config.get("flat_files") or {}

        # Log integration settings
        self._log(f"  Output unit:      {self.unit}", "info")
        self._log(f"  Solid angle corr: {self.correct_solid_angle}", "info")
        pol_str = f"{self.polarization_factor:.3f}" if self.polarization_factor is not None else "none"
        self._log(f"  Polarization p:   {pol_str}", "info")
        if self.polarization_factor is None:
            # Synchrotron beams are strongly horizontally polarized; a factor of
            # ~0.95–0.99 is recommended for quantitative work.  (Audit fix 3.5)
            self._log(
                "  ⚠ No polarization correction set — for synchrotron data set "
                "polarization_factor ≈ 0.95–0.99 in config.yml.", "warn"
            )
        if self.radial_range:
            self._log(f"  q range:          {self.radial_range[0]} – {self.radial_range[1]} ({self.unit})", "info")
        if self.azimuth_range:
            self._log(f"  χ range:          {self.azimuth_range[0]}° – {self.azimuth_range[1]}°", "info")

        self._load_integrators()

    def _load_integrators(self):
        """Load PyFAI integrators and optional 2D correction frames — kept alive for the session."""
        if self.mode in ("SAXS", "SWAXS"):
            poni_path = self.poni_directory / self.poni_files["saxs"]
            if not poni_path.exists():
                raise FileNotFoundError(f"SAXS PONI not found: {poni_path}")
            self.ai_saxs = pyFAI.load(str(poni_path))
            self._log(f"  Loaded SAXS integrator: {poni_path.name}", "info")

            saxs_mask_name = (self.mask_files or {}).get("saxs")
            if saxs_mask_name:
                mask_path = self.poni_directory / saxs_mask_name
                self.saxs_mask = fabio.open(str(mask_path)).data
                self._log(f"  Loaded SAXS mask:       {mask_path.name}", "info")
            else:
                self.saxs_mask = None

            # 2D dark current frame (pixel-level subtraction before integration)
            saxs_dark_name = self.dark_file_cfg.get("saxs")
            if saxs_dark_name:
                dark_path = self.poni_directory / saxs_dark_name
                self.saxs_dark = fabio.open(str(dark_path)).data.astype(np.float32)
                self._log(f"  Loaded SAXS dark frame: {dark_path.name}", "info")
            else:
                self.saxs_dark = None

            # 2D flat field frame (pixel-level sensitivity correction)
            saxs_flat_name = self.flat_file_cfg.get("saxs")
            if saxs_flat_name:
                flat_path = self.poni_directory / saxs_flat_name
                self.saxs_flat = fabio.open(str(flat_path)).data.astype(np.float32)
                self._log(f"  Loaded SAXS flat field: {flat_path.name}", "info")
            else:
                self.saxs_flat = None

        if self.mode in ("WAXS", "SWAXS"):
            poni_path = self.poni_directory / self.poni_files["waxs"]
            if not poni_path.exists():
                raise FileNotFoundError(f"WAXS PONI not found: {poni_path}")
            self.ai_waxs = pyFAI.load(str(poni_path))
            self._log(f"  Loaded WAXS integrator: {poni_path.name}", "info")

            waxs_mask_name = (self.mask_files or {}).get("waxs")
            if waxs_mask_name:
                mask_path = self.poni_directory / waxs_mask_name
                self.waxs_mask = fabio.open(str(mask_path)).data
                self._log(f"  Loaded WAXS mask:       {mask_path.name}", "info")
            else:
                self.waxs_mask = None

            waxs_dark_name = self.dark_file_cfg.get("waxs")
            if waxs_dark_name:
                dark_path = self.poni_directory / waxs_dark_name
                self.waxs_dark = fabio.open(str(dark_path)).data.astype(np.float32)
                self._log(f"  Loaded WAXS dark frame: {dark_path.name}", "info")
            else:
                self.waxs_dark = None

            waxs_flat_name = self.flat_file_cfg.get("waxs")
            if waxs_flat_name:
                flat_path = self.poni_directory / waxs_flat_name
                self.waxs_flat = fabio.open(str(flat_path)).data.astype(np.float32)
                self._log(f"  Loaded WAXS flat field: {flat_path.name}", "info")
            else:
                self.waxs_flat = None

    def _read_metadata(self, raw_file_path: Path, detector_type: str) -> dict:
        if self.metadata_format == "csv":
            return process_metadata.process_csv_metadata(raw_file_path)
        elif self.metadata_format == "pdi":
            return process_metadata.process_pdi_full(raw_file_path, detector_type)
        else:
            raise RuntimeError(f"Unknown metadata_format '{self.metadata_format}'.")

    def _compute_corrections(self, metadata: dict, raw_file_path: Path) -> dict:
        fname = raw_file_path.name

        # ── Bug fix 1: detect missing metadata keys explicitly ───────────────
        # Silently falling back to 1.0 when i0/bstop are absent would produce
        # completely wrong normalization with no warning.  We now check for the
        # key first and raise a clear error so the user knows the metadata
        # format doesn't match what the pipeline expects.
        I0_KEY    = next((k for k in ("i0", "I0", "Ion0", "ion0", "I0_diode") if k in metadata), None)
        BSTOP_KEY = next((k for k in ("bstop", "Bstop", "bstop_diode", "Bstop_diode", "bs") if k in metadata), None)

        if I0_KEY is None:
            raise KeyError(
                f"{fname}: 'i0' not found in metadata. "
                f"Available keys: {list(metadata.keys())}. "
                "Check that metadata_format matches your files (csv vs pdi) "
                "and that the CSV/PDI contains an 'i0' or 'I0' column."
            )
        if BSTOP_KEY is None:
            raise KeyError(
                f"{fname}: 'bstop' not found in metadata. "
                f"Available keys: {list(metadata.keys())}. "
                "Check that metadata_format matches your files (csv vs pdi) "
                "and that the CSV/PDI contains a 'bstop' or 'Bstop' column."
            )

        i0    = float(metadata[I0_KEY])
        bstop = float(metadata[BSTOP_KEY])

        i0_corr    = i0    - self.i0_offset
        bstop_corr = bstop - self.bstop_offset

        # ── Bug fix 2: bad-fallback for negative corrected values ────────────
        # Original code fell back to the raw (un-corrected) i0/bstop value,
        # which is scientifically inconsistent — the offset subtraction was
        # meant to remove dark current; using the uncorrected value defeats that.
        # Correct action: use a tiny epsilon, log a prominent warning, and
        # flag the result so the user can investigate the offset settings.
        # Non-positive corrected diode readings make transmission, thickness and
        # normalization all meaningless. Rather than emit a silently-wrong .dat
        # (old behaviour: substitute ε=1e-10), skip the file with a clear error.
        # run_pipeline() catches this per-file, logs ✗, and continues. (Audit fix 3.7)
        if i0_corr <= 0:
            self._log(
                f"  ⛔ {fname}: I0_corrected ≤ 0 (i0={i0:.4f}, offset={self.i0_offset:.4f}) "
                f"— skipping file. Check i0_offset.", "error"
            )
            raise ValueError(
                f"{fname}: I0_corrected = {i0_corr:.4f} ≤ 0 (i0={i0:.4f} − offset "
                f"{self.i0_offset:.4f}). File skipped to avoid corrupt normalization; "
                f"i0_offset should be ≤ the dark-current reading (shutter closed)."
            )

        if bstop_corr <= 0:
            self._log(
                f"  ⛔ {fname}: Bstop_corrected ≤ 0 (bstop={bstop:.4f}, offset={self.bstop_offset:.4f}) "
                f"— skipping file. Check bstop_offset.", "error"
            )
            raise ValueError(
                f"{fname}: Bstop_corrected = {bstop_corr:.4f} ≤ 0 (bstop={bstop:.4f} − "
                f"offset {self.bstop_offset:.4f}). File skipped to avoid corrupt "
                f"normalization; check bstop_offset in config."
            )

        # ── Air-path normalisation (optional) ────────────────────────────────
        # If i0_air / bstop_air are configured, compute the TRUE sample
        # transmission by dividing out the empty-beam ratio:
        #   T_sample = (bstop_corr / i0_corr) / (bstop_air_corr / i0_air_corr)
        #            = (bstop_corr × i0_air_corr) / (i0_corr × bstop_air_corr)
        # Without air values (both default to 0.0) behaviour is unchanged.
        i0_air_corr    = self.i0_air    - self.i0_offset
        bstop_air_corr = self.bstop_air - self.bstop_offset

        if i0_air_corr > 0.0 and bstop_air_corr > 0.0:
            T_raw_air  = bstop_air_corr / i0_air_corr
            T_sample   = (bstop_corr / i0_corr) / T_raw_air
            # Air-corrected bstop for normfactor: T_sample × i0_corr
            bstop_norm = bstop_corr * (i0_air_corr / bstop_air_corr)
        else:
            T_sample   = bstop_corr / i0_corr
            bstop_norm = bstop_corr  # no air measurement: unchanged behaviour

        if T_sample > 1.0:
            self._log(
                f"  ⚠ {fname}: T_sample = {T_sample:.4f} > 1.0 "
                "(physically unreasonable — check air measurement values). Clipping to 1.0.",
                "warn"
            )

        transmission = np.clip(T_sample, 1e-6, 1.0)

        # Thickness — use configured value or derive via Beer-Lambert
        # xraydb.material_mu() returns μ in cm⁻¹; × 100 converts to m⁻¹.
        # Beer-Lambert: T = exp(−μ_m · d_m)  →  d_m = −ln(T) / μ_m
        # NOTE (audit 3.6): deriving thickness from transmission assumes the
        # configured `compound`/`density` are the BULK material in the beam
        # (e.g. the solvent/buffer). For dilute samples this is an approximation;
        # prefer setting an explicit `thickness` when the cell path length is known.
        if self.thickness is not None:
            thickness_m = float(self.thickness)
        else:
            mu_cm = xraydb.material_mu(
                self.compound,
                energy=self.energy_keV * 1000,   # xraydb expects energy in eV
                density=self.density_g_cm3,       # → returns cm⁻¹
            )
            mu_m = mu_cm * 100.0                 # cm⁻¹ × 100 = m⁻¹
            thickness_m = (-np.log(float(transmission)) / mu_m) if mu_m > 0 else 0.0

        # ── Normalization factor ─────────────────────────────────────────────
        # PyFAI's integrate1d DIVIDES every pixel count by normalization_factor
        # before azimuthal averaging.  For a scalar NF this is equivalent to
        # dividing the final 1-D profile — but passing it inside integrate1d is
        # more correct for Poisson error propagation.
        #
        # Supported terms:
        #   "bstop"    → NF = bstop_corr              (∝ Φ₀·T — standard SSRL)
        #   "i0"       → NF = i0_corr                 (∝ Φ₀ only)
        #   "absolute" → NF = (bstop_corr × d_cm) / K (→ I(q) ∝ dΣ/dΩ [cm⁻¹])
        #
        # An empty list → NF = 1.0 (raw integrated counts).
        t_cm = thickness_m * 100.0          # m → cm  (used in "absolute" term)
        norm_factor = 1.0
        _known_terms = {"bstop", "i0", "absolute"}
        for term in self.normalization:
            if term not in _known_terms:
                # Minor fix: warn on unrecognised terms so they don't silently vanish
                logger.warning(
                    f"Unknown normalization term '{term}' ignored. "
                    f"Valid terms: {_known_terms}."
                )
                self._log(f"  ⚠ Unknown normalization term '{term}' ignored.", "warn")
                continue
            if term == "bstop":
                # Use air-normalised bstop when air values are available.
                # bstop_norm = bstop_corr × (i0_air_corr/bstop_air_corr) = T_sample × i0_corr
                # Falls back to bstop_corr when no air measurement is provided.
                norm_factor *= bstop_norm
            elif term == "i0":
                norm_factor *= i0_corr
            elif term == "absolute":
                # norm_factor = (bstop_norm × d_cm) / K
                # PyFAI then computes: I(q) = counts / NF = K·counts/(I0·T·d_cm)
                # Use bstop_norm (air-corrected transmitted flux = I0·T_sample) so
                # that, when air measurements are configured, absolute scale is
                # air-corrected consistently with the "bstop" mode. Falls back to
                # bstop_corr when no air measurement is given. (Audit fix 3.2)
                abs_norm = bstop_norm * max(t_cm, 1e-10)
                norm_factor *= abs_norm / max(self.calibration_factor, 1e-30)

        if norm_factor <= 0:
            logger.warning(f"normalization_factor ≤ 0 for {fname}; defaulting to 1")
            norm_factor = 1.0

        return {
            "i0":                        i0,
            "bstop":                     bstop,
            "i0_corrected":              i0_corr,
            "bstop_corrected":           bstop_corr,
            "transmission":              float(transmission),
            "normalization_factor":      norm_factor,
            "thickness_m":               thickness_m,
            "calibration_factor":        self.calibration_factor,
        }

    def _make_output_path(self, raw_file_path: Path,
                          detector_type: str, prefix: str) -> Path:
        output_dir = self.output_dir_1d / detector_type.upper() / "Reduction"
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = raw_file_path.name.replace(self.data_format, "")
        if prefix and stem.startswith(prefix):
            stem = stem[len(prefix):]
        return output_dir / f"{stem}_{detector_type.upper()}.dat"

    # ── Public file-processing methods ────────────────────────────────────────

    def process_saxs_file(self, raw_file_path: Path) -> dict:
        """
        Process one SAXS .raw file end-to-end.

        Memory contract: detector array and q/I/err vectors are allocated,
        used, then explicitly deleted inside a finally block before returning.
        The caller sees ONLY a small dict of scalars (no arrays).

        Returned dict keys:
          filename    – output .dat filename
          corrections – I0/bstop/transmission/thickness/normalization_factor
          ctemp       – sample temperature float or None
        """
        detector_data = q = intensity = error = None
        try:
            detector_data = read_raw_file.read_detector_image(
                raw_file_path, self.detector_shapes["saxs"]
            )
            metadata    = self._read_metadata(raw_file_path, "SAXS")
            corrections = self._compute_corrections(metadata, raw_file_path)
            ctemp       = _extract_ctemp(metadata)

            output_path = self._make_output_path(raw_file_path, "SAXS", self.saxs_prefix)

            q, intensity, error = self.ai_saxs.integrate1d(
                detector_data, self.npt_radial,
                unit=self.unit,
                correctSolidAngle=self.correct_solid_angle,
                polarization_factor=self.polarization_factor,
                radial_range=self.radial_range,
                azimuth_range=self.azimuth_range,
                dark=self.saxs_dark,
                flat=self.saxs_flat,
                dummy=self.dummy,
                delta_dummy=self.delta_dummy,
                error_model=self.error_model,
                mask=self.saxs_mask,
                normalization_factor=corrections["normalization_factor"],
                filename=str(output_path),
            )
            _append_metadata_to_dat(output_path, metadata)

            return {
                "filename":    output_path.name,
                "corrections": corrections,
                "ctemp":       ctemp,
            }
        finally:
            # Free every large array immediately — the caller gets nothing back
            del detector_data, q, intensity, error
            gc.collect()

    def process_waxs_file(self, raw_file_path: Path) -> dict:
        """Same as process_saxs_file but for the WAXS detector."""
        detector_data = q = intensity = error = None
        try:
            detector_data = read_raw_file.read_detector_image(
                raw_file_path, self.detector_shapes["waxs"]
            )
            metadata    = self._read_metadata(raw_file_path, "WAXS")
            corrections = self._compute_corrections(metadata, raw_file_path)
            ctemp       = _extract_ctemp(metadata)

            output_path = self._make_output_path(raw_file_path, "WAXS", self.waxs_prefix)

            q, intensity, error = self.ai_waxs.integrate1d(
                detector_data, self.npt_radial,
                unit=self.unit,
                correctSolidAngle=self.correct_solid_angle,
                polarization_factor=self.polarization_factor,
                radial_range=self.radial_range,
                azimuth_range=self.azimuth_range,
                dark=self.waxs_dark,
                flat=self.waxs_flat,
                dummy=self.dummy,
                delta_dummy=self.delta_dummy,
                error_model=self.error_model,
                mask=self.waxs_mask,
                normalization_factor=corrections["normalization_factor"],
                filename=str(output_path),
            )
            _append_metadata_to_dat(output_path, metadata)

            return {
                "filename":    output_path.name,
                "corrections": corrections,
                "ctemp":       ctemp,
            }
        finally:
            del detector_data, q, intensity, error
            gc.collect()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_result_line(result: dict) -> str:
    """Format the per-file success log line (I0, bstop, T, thickness, K, CTEMP)."""
    c     = result["corrections"]
    i0    = c["i0_corrected"]
    bstop = c["bstop_corrected"]
    t     = c["transmission"]
    thick = c["thickness_m"] * 1000          # → mm
    ctemp = result.get("ctemp")
    calib = c.get("calibration_factor", 1.0)

    ctemp_str = f"  CTEMP={ctemp:.1f}°C" if ctemp is not None else ""
    calib_str = f"  K={calib:.6g}" if calib != 1.0 else ""
    return (
        f"    ✓ {result['filename']}\n"
        f"      I0={i0:.2f}  Bstop={bstop:.2f}  T={t:.4f}  "
        f"thickness={thick:.3f} mm{calib_str}{ctemp_str}"
    )


def _append_metadata_to_dat(dat_path: Path, metadata: dict):
    with open(dat_path, "a", encoding="utf-8") as f:
        f.write("\n# METADATA INFORMATION (YML FORMAT)\n")
        for key, value in metadata.items():
            if key != "#":
                f.write(f"# {key}: {value}\n")


def find_new_raw_files(
    config: dict, processed_files: set
) -> Tuple[List[Path], List[Path]]:
    """
    Scan the data directory for .raw files not yet in processed_files.
    Returns (saxs_files, waxs_files) sorted by filename.
    """
    data_dir    = Path(config["data_directory"])
    mode        = config["mode"].upper()
    data_format = "." + config.get("beamline", {}).get("data_format", "raw")

    saxs_files: List[Path] = []
    waxs_files: List[Path] = []

    if mode in ("SAXS", "SWAXS"):
        for f in sorted(data_dir.glob(f"**/SAXS/**/*{data_format}")):
            if (
                not str(f).endswith(".raw.pdi")
                and str(f) not in processed_files
                and not os.path.basename(f).startswith("._")
            ):
                saxs_files.append(f)

    if mode in ("WAXS", "SWAXS"):
        for f in sorted(data_dir.glob(f"**/WAXS/**/*{data_format}")):
            if (
                not str(f).endswith(".raw.pdi")
                and str(f) not in processed_files
                and not os.path.basename(f).startswith("._")
            ):
                waxs_files.append(f)

    return saxs_files, waxs_files


def run_pipeline(
    config: dict,
    log_callback: Optional[Callable] = None,
    processed_files: Optional[set] = None,
    experiment: Optional[Experiment] = None,
    stop_event=None,          # threading.Event — set it to request a graceful stop
    file_done_callback: Optional[Callable] = None,
    # Optional callable(result: dict, raw_path: Path, detector: str) → None.
    # Called after each file is successfully processed.  Used by app.py to
    # emit event-bus events and register files in the manifest.  Any exception
    # raised inside the callback is caught and silently ignored so that a
    # broken callback never stops the pipeline.
) -> dict:
    """
    Process all new .raw files, strictly one at a time.

    Pass a pre-created Experiment via the `experiment` argument to avoid
    reloading PyFAI integrators on every call (critical for monitoring mode).

    Pass a threading.Event as `stop_event` to allow the caller to request a
    graceful stop between files.  The pipeline checks the event before each
    file; the current file always completes before the stop takes effect.

    Pass a ``file_done_callback`` to receive a notification after each
    successfully reduced file (used by app.py for event-bus publishing and
    manifest registration).

    Returns {"saxs_count": int, "waxs_count": int, "stopped": bool}.
    """
    if processed_files is None:
        processed_files = set()

    log     = log_callback or (lambda msg, tag="info": None)
    stopped = False          # set True if stop_event fires mid-run

    def _is_stopped():
        return stop_event is not None and stop_event.is_set()

    if experiment is None:
        log("Loading PyFAI integrators…", "info")
        try:
            experiment = Experiment(config, log_callback=log)
        except Exception as e:
            log(f"Setup failed: {e}", "error")
            raise

    saxs_files, waxs_files = find_new_raw_files(config, processed_files)
    log(f"New files: {len(saxs_files)} SAXS + {len(waxs_files)} WAXS", "info")

    n_saxs = n_waxs = 0

    if saxs_files:
        log(f"\nProcessing {len(saxs_files)} SAXS file(s)…", "header")
        for i, f in enumerate(saxs_files, 1):
            if _is_stopped():
                log("⏹  Stop requested — halting after current batch.", "warn")
                stopped = True
                break
            log(f"  [{i}/{len(saxs_files)}]  {f.name}", "info")
            try:
                result = experiment.process_saxs_file(f)  # arrays freed inside
                processed_files.add(str(f))
                n_saxs += 1
                log(_fmt_result_line(result), "ok")
                if file_done_callback is not None:
                    try:
                        file_done_callback(result, f, "saxs")
                    except Exception:
                        pass
            except Exception as e:
                log(f"    ✗ {f.name}: {e}", "error")
                logger.exception(e)
            gc.collect()   # belt-and-suspenders between files

    if waxs_files and not stopped:
        log(f"\nProcessing {len(waxs_files)} WAXS file(s)…", "header")
        for i, f in enumerate(waxs_files, 1):
            if _is_stopped():
                log("⏹  Stop requested — halting after current batch.", "warn")
                stopped = True
                break
            log(f"  [{i}/{len(waxs_files)}]  {f.name}", "info")
            try:
                result = experiment.process_waxs_file(f)
                processed_files.add(str(f))
                n_waxs += 1
                log(_fmt_result_line(result), "ok")
                if file_done_callback is not None:
                    try:
                        file_done_callback(result, f, "waxs")
                    except Exception:
                        pass
            except Exception as e:
                log(f"    ✗ {f.name}: {e}", "error")
                logger.exception(e)
            gc.collect()

    log(f"\nOutput written to: {experiment.output_dir_1d}", "ok")
    return {"saxs_count": n_saxs, "waxs_count": n_waxs, "stopped": stopped}
