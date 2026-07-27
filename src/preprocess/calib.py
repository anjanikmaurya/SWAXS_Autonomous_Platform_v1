"""
src/preprocess/calib.py — pyFAI calibration helpers for the calibration app.

One path to a .poni from a calibrant image (AgBehenate / LaB6 …): launch
pyFAI-calib2, the standard interactive GUI, preloaded with the CBF + calibrant +
energy + pixel size. The user picks rings, refines, and saves the .poni himself.

The GUI's working directory is set to the project's poni/ folder so its
"Save as…" dialog already points there.

pyFAI is imported lazily so the module loads even where pyFAI isn't installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Common transmission-SAXS/WAXS calibrants known to pyFAI (get_calibrant names).
CALIBRANTS = ["AgBehenate", "LaB6", "CeO2", "Si", "Cr2O3", "Au", "Ni", "alpha_Al2O3"]


def energy_to_wavelength_m(energy_keV):
    """X-ray energy (keV) → wavelength (metres). λ[Å] = 12.39842 / E[keV]."""
    return 12.39842 / float(energy_keV) * 1e-10


def _calib2_launcher():
    """Resolve how to start pyFAI-calib2. Prefers the console script; falls back
    to ``python -m pyFAI.app.calib2`` using the CURRENT interpreter, which is
    what makes this work when the hub spawns the app without the env's bin/ on
    PATH. Returns (argv_prefix, how) or (None, reason)."""
    exe = shutil.which("pyFAI-calib2")
    if exe:
        return [exe], "console script"
    try:
        import pyFAI.app.calib2  # noqa: F401,PLC0415
        return [sys.executable, "-m", "pyFAI.app.calib2"], "python -m pyFAI.app.calib2"
    except Exception as exc:
        return None, f"pyFAI GUI entry point not importable: {exc}"


def build_calib2_command(cbf_path, calibrant, energy_keV, pixel_um=None,
                         poni_init=None, argv_prefix=None):
    """Build the pyFAI-calib2 command (list) to open a calibrant image preloaded.

    NOTE pyFAI's ``-p/--pixel`` expects MICRONS (not metres), and ``-i/--poni``
    takes an optional starting geometry.
    """
    cmd = list(argv_prefix or ["pyFAI-calib2"])
    cmd += ["--calibrant", str(calibrant), "--energy", str(energy_keV)]
    if pixel_um:
        cmd += ["--pixel", str(pixel_um)]          # microns
    if poni_init:
        cmd += ["--poni", str(poni_init)]
    cmd.append(str(cbf_path))
    return cmd


def launch_calib2(cbf_path, calibrant, energy_keV, pixel_um=None,
                  poni_init=None, workdir=None):
    """Spawn the pyFAI-calib2 GUI for interactive calibration.

    Returns (ok, message, command_string). Needs a display + Qt. If the process
    dies immediately (missing Qt, no display) its stderr is reported rather than
    falsely claiming success.
    """
    argv_prefix, how = _calib2_launcher()
    if argv_prefix is None:
        cmd_str = " ".join(build_calib2_command(cbf_path, calibrant, energy_keV, pixel_um, poni_init))
        return False, how, cmd_str

    cmd = build_calib2_command(cbf_path, calibrant, energy_keV, pixel_um, poni_init, argv_prefix)
    cmd_str = " ".join(cmd)

    if workdir:
        try:
            Path(workdir).mkdir(parents=True, exist_ok=True)
        except Exception:
            workdir = None

    env = dict(os.environ)
    env.pop("MPLBACKEND", None)          # the app forces Agg; the GUI must not inherit it
    env.pop("QT_QPA_PLATFORM", None)     # never inherit "offscreen"

    try:
        proc = subprocess.Popen(cmd, cwd=str(workdir) if workdir else None, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return False, ("pyFAI-calib2 not found. Run the command below in a terminal "
                       "with the platform env active."), cmd_str
    except Exception as exc:
        return False, f"could not launch pyFAI-calib2: {exc}", cmd_str

    time.sleep(1.5)                       # catch an immediate crash
    if proc.poll() is not None:
        err = ""
        try:
            err = (proc.stderr.read() or b"").decode(errors="replace").strip()
        except Exception:
            pass
        tail = " · ".join(err.splitlines()[-3:])[:400] if err else "no stderr"
        return False, (f"pyFAI-calib2 exited immediately (rc={proc.returncode}). "
                       f"Usually a missing Qt or no display. {tail}"), cmd_str

    where = f" Save the .poni into {workdir}." if workdir else ""
    return True, (f"Launched pyFAI-calib2 via {how} — pick rings, refine, then "
                  f"save the .poni.{where}"), cmd_str


def list_poni_files(poni_dir):
    """List existing .poni files in a directory (name, path)."""
    poni_dir = Path(poni_dir)
    if not poni_dir.is_dir():
        return []
    return [{"name": p.name, "path": str(p)} for p in sorted(poni_dir.glob("*.poni"))]
