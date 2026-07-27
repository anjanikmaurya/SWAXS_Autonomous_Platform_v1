"""
src/preprocess/calib.py — pyFAI calibration helpers for the calibration app.

Two paths to a .poni from a calibrant image (AgBehenate / LaB6 …):
  • launch pyFAI-calib2 (the standard interactive GUI) preloaded with the CBF +
    calibrant + energy — the user picks rings and saves the .poni; and
  • auto_calibrate(): a best-effort HEADLESS refine (pyFAI SingleGeometry) when a
    reasonable distance + beam-centre guess is given.

pyFAI is imported lazily so the module loads even where pyFAI isn't installed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# Common transmission-SAXS/WAXS calibrants known to pyFAI (get_calibrant names).
CALIBRANTS = ["AgBehenate", "LaB6", "CeO2", "Si", "Cr2O3", "Au", "Ni", "alpha_Al2O3"]


def energy_to_wavelength_m(energy_keV):
    """X-ray energy (keV) → wavelength (metres). λ[Å] = 12.39842 / E[keV]."""
    return 12.39842 / float(energy_keV) * 1e-10


def build_calib2_command(cbf_path, calibrant, energy_keV, pixel_m=None, poni_init=None):
    """Build the pyFAI-calib2 command (list) to open a calibrant image preloaded.
    Flags vary slightly across pyFAI versions — the app shows this string so it
    can be edited if needed."""
    cmd = ["pyFAI-calib2", "--calibrant", str(calibrant), "--energy", str(energy_keV)]
    if pixel_m:
        cmd += ["--pixel", str(pixel_m)]      # detector pixel size in metres
    if poni_init:
        cmd += ["--poni", str(poni_init)]
    cmd.append(str(cbf_path))
    return cmd


def launch_calib2(cbf_path, calibrant, energy_keV, pixel_m=None, poni_init=None):
    """Spawn pyFAI-calib2 (GUI) for interactive calibration. Returns
    (ok, message, command_string). Best-effort — needs a display + pyqt."""
    cmd = build_calib2_command(cbf_path, calibrant, energy_keV, pixel_m, poni_init)
    cmd_str = " ".join(cmd)
    try:
        subprocess.Popen(cmd)
        return True, "Launched pyFAI-calib2 — pick rings and save the .poni.", cmd_str
    except FileNotFoundError:
        return False, ("pyFAI-calib2 not found on PATH. Run the command below in a "
                       "terminal in the same env."), cmd_str
    except Exception as exc:
        return False, f"could not launch pyFAI-calib2: {exc}", cmd_str


def auto_calibrate(data, calibrant, energy_keV, pixel_m, shape,
                   dist_m, beam_x_px, beam_y_px, poni_out, max_rings=8):
    """HEADLESS best-effort calibration → write a .poni. Needs a decent initial
    guess (sample-detector distance in m, beam centre in pixels). Returns
    (ok, message). Ring extraction can fail on noisy/partial patterns — in that
    case use launch_calib2() instead."""
    try:
        import numpy as np                                  # noqa: PLC0415
        from pyFAI.calibrant import get_calibrant           # noqa: PLC0415
        from pyFAI.detectors import Detector                # noqa: PLC0415
        from pyFAI.geometry import Geometry                 # noqa: PLC0415
        from pyFAI.goniometer import SingleGeometry         # noqa: PLC0415
    except Exception as exc:
        return False, f"pyFAI not available: {exc}"
    try:
        data = np.asarray(data)
        wl = energy_to_wavelength_m(energy_keV)
        det = Detector(pixel1=float(pixel_m), pixel2=float(pixel_m),
                       max_shape=(int(shape[0]), int(shape[1])))
        cal = get_calibrant(calibrant); cal.wavelength = wl
        # initial geometry: poni1 = y (rows) · pixel, poni2 = x (cols) · pixel
        geo = Geometry(dist=float(dist_m), poni1=float(beam_y_px) * float(pixel_m),
                       poni2=float(beam_x_px) * float(pixel_m), detector=det, wavelength=wl)
        sg = SingleGeometry("calib", data, calibrant=cal, detector=det, geometry=geo)
        sg.extract_cp(max_rings=int(max_rings))
        n = len(getattr(sg.geometry_refinement, "data", []) or [])
        if n < 5:
            return False, (f"only {n} control points found — initial guess likely off; "
                           f"use the interactive pyFAI-calib2 instead.")
        sg.geometry_refinement.refine2()
        Path(poni_out).parent.mkdir(parents=True, exist_ok=True)
        sg.geometry_refinement.save(str(poni_out))
        return True, f"refined on ~{n} control points → {poni_out}"
    except Exception as exc:
        return False, f"auto-calibration failed ({exc}); use interactive pyFAI-calib2."


def list_poni_files(poni_dir):
    """List existing .poni files in a directory (name, path)."""
    poni_dir = Path(poni_dir)
    if not poni_dir.is_dir():
        return []
    return [{"name": p.name, "path": str(p)} for p in sorted(poni_dir.glob("*.poni"))]
