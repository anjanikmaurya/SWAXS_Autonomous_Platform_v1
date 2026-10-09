"""
src/preprocess/calib.py — pyFAI calibration helpers for the calibration app.

One path to a .poni from a calibrant image (AgBh / LaB6 …): launch
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
import tempfile
import time
from pathlib import Path

# Common transmission SAXS/WAXS calibrants, by their pyFAI names (case sensitive:
# pyFAI-calib2 looks them up in pyFAI.calibrant.CALIBRANT_FACTORY). Silver
# behenate is "AgBh" in pyFAI; "AgBehenate" is NOT a pyFAI name, and passing it
# made the GUI open with no calibrant selected. Old names are mapped below.
CALIBRANTS = ["AgBh", "LaB6", "CeO2", "Si", "Cr2O3", "Au", "Ni", "alpha_Al2O3"]
CALIBRANT_LABELS = {"AgBh": "AgBh (silver behenate)", "LaB6": "LaB6",
                    "CeO2": "CeO2", "Si": "Si", "Cr2O3": "Cr2O3", "Au": "Au",
                    "Ni": "Ni", "alpha_Al2O3": "alpha Al2O3 (corundum)"}
_CALIBRANT_ALIASES = {"agbehenate": "AgBh", "agbeh": "AgBh", "silver behenate": "AgBh",
                      "agbh": "AgBh"}

# How long to watch a freshly started GUI for an immediate crash. Importing
# silx + Qt on a cold start takes a few seconds, so a missing Qt binding can
# take longer than 1.5 s to fail; 1.5 s used to report success for a GUI that
# then died.
CRASH_WATCH_S = 6.0


def resolve_calibrant(name):
    """Map a calibrant name to pyFAI's spelling (AgBehenate → AgBh). Unknown
    names pass through unchanged (pyFAI also accepts a d-spacing file path)."""
    n = str(name or "").strip()
    return _CALIBRANT_ALIASES.get(n.lower(), n) or "AgBh"


def energy_to_wavelength_m(energy_keV):
    """X-ray energy (keV) → wavelength (metres). λ[Å] = 12.39842 / E[keV]."""
    return 12.39842 / float(energy_keV) * 1e-10


def _calib2_launcher():
    """Resolve how to start pyFAI-calib2. Prefers ``python -m pyFAI.app.calib2``
    with the CURRENT interpreter: that is the environment that has the platform's
    pyFAI and PySide6. A ``pyFAI-calib2`` script found on PATH may belong to a
    different environment (e.g. a conda base without Qt), so it is only the
    fallback. Returns (argv_prefix, how) or (None, reason)."""
    try:
        import importlib.util  # noqa: PLC0415
        if importlib.util.find_spec("pyFAI.app.calib2") is not None:
            return [sys.executable, "-m", "pyFAI.app.calib2"], "python -m pyFAI.app.calib2"
    except Exception:
        pass
    exe = shutil.which("pyFAI-calib2")
    if exe:
        return [exe], "console script"
    return None, "pyFAI is not installed in the environment that runs the platform"


#: pyFAI detector models the calibration can name, by image shape. A bare
#: ``--pixel`` makes pyFAI-calib2 build a detector WITHOUT a shape, and the GUI
#: then crashes on start ("detector.max_shape[1] … 'NoneType' object is not
#: subscriptable"). Naming the model gives pyFAI the shape, the pixel size and
#: the module gaps. Pilatus 1M = the SAXS detector (1043 × 981), Pilatus 100K =
#: the WAXS detector (195 × 487); the others are listed so a different Pilatus
#: is still recognised.
DETECTOR_MODELS = ("pilatus100k", "pilatus200k", "pilatus300k", "pilatus300kw",
                   "pilatus900k", "pilatus1m", "pilatus2m", "pilatus6m")


def detector_for_image(image_path, pixel_um=None):
    """The pyFAI detector model whose shape matches the image, or None.

    Reads only the image header/shape (fabio). When ``pixel_um`` is given it must
    agree with the model's pixel size (within 1 µm)."""
    try:
        import fabio                                       # noqa: PLC0415
        from pyFAI.detectors import detector_factory      # noqa: PLC0415
        with fabio.open(str(image_path)) as img:
            shape = tuple(int(n) for n in img.shape)
        for name in DETECTOR_MODELS:
            d = detector_factory(name)
            if tuple(d.max_shape) == shape and (
                    not pixel_um or abs(d.pixel1 * 1e6 - float(pixel_um)) < 1.0):
                return name
    except Exception:
        return None
    return None


def list_detectors() -> list[dict]:
    """Every detector model pyFAI knows, one entry per model (its aliases merged),
    with what the GUI needs to show: id (the name passed to pyFAI), name,
    manufacturer, image shape and pixel size. Empty when pyFAI is missing."""
    try:
        from pyFAI.detectors import ALL_DETECTORS        # noqa: PLC0415
    except Exception:
        return []
    by_cls: dict = {}
    for key, cls in ALL_DETECTORS.items():
        by_cls.setdefault(cls, []).append(key)
    out = []
    for cls, keys in by_cls.items():
        if cls.__name__ == "Detector":                   # the generic, shapeless one
            continue
        shape = getattr(cls, "MAX_SHAPE", None)
        px = None
        try:
            d = cls()
            shape = tuple(d.max_shape) if d.max_shape is not None else shape
            px = round(float(d.pixel1) * 1e6, 3) if d.pixel1 else None
        except Exception:
            pass
        if not shape:
            continue                                     # cannot be checked against an image
        out.append({"id": sorted(keys, key=lambda k: (len(k), k))[0], "name": cls.__name__,
                    "manufacturer": getattr(cls, "MANUFACTURER", None) or "Other",
                    "shape": [int(shape[0]), int(shape[1])], "pixel_um": px})
    out.sort(key=lambda d: (str(d["manufacturer"]).lower(), d["name"].lower()))
    return out


def image_shape(image_path):
    """(rows, cols) of an image, read with fabio; None if it cannot be read."""
    try:
        import fabio                                     # noqa: PLC0415
        with fabio.open(str(image_path)) as img:
            return tuple(int(n) for n in img.shape)
    except Exception:
        return None


def check_detector(image_path, detector_id):
    """(ok, message) — does this pyFAI detector model fit this image? Refusing a
    mismatch here is safer than letting pyFAI-calib2 open with the wrong pixel
    size or crash. A binned image (an integer fraction of the model) is allowed,
    as pyFAI handles binning."""
    try:
        from pyFAI.detectors import detector_factory     # noqa: PLC0415
        d = detector_factory(str(detector_id))
    except Exception:
        return False, f"pyFAI does not know a detector called {detector_id!r}"
    if d.max_shape is None:
        return False, f"{d.__class__.__name__} has no fixed image size, so it cannot be checked"
    full = tuple(int(n) for n in d.max_shape)
    shp = image_shape(image_path)
    if shp is None:
        return False, f"could not read the image size of {Path(image_path).name}"
    if shp == full:
        return True, ""
    if all(f % s == 0 for f, s in zip(full, shp)) and full[0] // shp[0] == full[1] // shp[1]:
        return True, f"image is {full[0] // shp[0]}×{full[1] // shp[1]} binned"
    return False, (f"the image is {shp[0]} × {shp[1]} pixels but a {d.__class__.__name__} is "
                   f"{full[0]} × {full[1]}. Pick the detector this image came from, or Auto.")


def build_calib2_command(cbf_path, calibrant, energy_keV, pixel_um=None,
                         poni_init=None, argv_prefix=None, mask=None, detector=None):
    """Build the pyFAI-calib2 command (list) to open a calibrant image preloaded.

    ``detector`` (a pyFAI model name, e.g. "pilatus1m") is preferred: it carries
    the image shape. ``--pixel`` alone (MICRONS) is only passed when no model is
    known AND the caller asks for it; on pyFAI 2024 it crashes the GUI, so
    launch_calib2 never does. ``-i/--poni`` takes an optional starting geometry.
    """
    cmd = list(argv_prefix or ["pyFAI-calib2"])
    cmd += ["--calibrant", resolve_calibrant(calibrant), "--energy", str(energy_keV)]
    if detector:
        cmd += ["--detector", str(detector)]
    elif pixel_um:
        cmd += ["--pixel", str(pixel_um)]          # microns
    if poni_init:
        cmd += ["--poni", str(poni_init)]
    if mask:
        cmd += ["--mask", str(mask)]
    cmd.append(str(cbf_path))
    return cmd


# Runs in a short-lived helper process. It starts the GUI in its own session,
# watches it for CRASH_WATCH_S, reports, and exits. The GUI is then nobody's
# child: stopping or restarting the calibration app (the hub kills the app's
# whole process tree) can no longer close a calibration the user is working on.
_DETACH = r"""
import json, subprocess, sys, time, os
cmd, cwd, err, watch = json.loads(sys.argv[1])
kw = {}
if os.name == "nt":
    kw["creationflags"] = 0x00000200 | 0x00000008   # NEW_PROCESS_GROUP | DETACHED_PROCESS
else:
    kw["start_new_session"] = True
with open(err, "ab") as fh:
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=fh, close_fds=True, **kw)
t0 = time.time()
while time.time() - t0 < watch:
    if p.poll() is not None:
        print("DIED", p.returncode, flush=True); sys.exit(0)
    time.sleep(0.1)
print("ALIVE", p.pid, flush=True)
"""


def launch_calib2(cbf_path, calibrant, energy_keV, pixel_um=None, detector=None,
                  poni_init=None, workdir=None, mask=None, watch_s=None):
    """Open the pyFAI-calib2 GUI for interactive calibration, detached from the app.

    Returns (ok, message, command_string). Needs a display + Qt. If the GUI dies
    within ``watch_s`` seconds (missing Qt, no display, bad image) its stderr is
    reported rather than falsely claiming success. Nothing here writes a .poni or
    touches any other app: the user saves the .poni from the GUI.
    """
    try:
        e = float(energy_keV)
        if not (e > 0):
            raise ValueError
    except (TypeError, ValueError):
        return False, f"energy must be a positive number of keV (got {energy_keV!r})", ""
    if not Path(cbf_path).is_file():
        return False, f"image not found: {cbf_path}", ""

    # The detector model, from the image shape. Never a bare --pixel: on pyFAI
    # 2024 that builds a detector with no shape and the GUI dies on start. With
    # no matching model the GUI opens without one and asks for the detector.
    # The operator's choice wins, after a shape check; "auto"/empty = from the
    # image size (and the legacy pixel field, if a caller still sends one).
    if detector and str(detector).lower() != "auto":
        ok_det, why = check_detector(cbf_path, detector)
        if not ok_det:
            return False, why, ""
        det = str(detector)
    else:
        det = detector_for_image(cbf_path, pixel_um)
    argv_prefix, how = _calib2_launcher()
    if argv_prefix is None:
        cmd_str = " ".join(build_calib2_command(cbf_path, calibrant, e, None, poni_init,
                                                mask=mask, detector=det))
        return False, how, cmd_str

    cmd = build_calib2_command(cbf_path, calibrant, e, None, poni_init, argv_prefix,
                               mask=mask, detector=det)
    cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)

    if workdir:
        try:
            Path(workdir).mkdir(parents=True, exist_ok=True)
        except Exception:
            workdir = None

    env = dict(os.environ)
    env.pop("MPLBACKEND", None)          # the app forces Agg; the GUI must not inherit it
    env.pop("QT_QPA_PLATFORM", None)     # never inherit "offscreen"

    # stderr goes to a file, NOT an unread PIPE: pyFAI-calib2 is a chatty Qt GUI
    # that lives for the whole calibration session, and an undrained PIPE fills its
    # ~64 KB OS buffer and freezes it mid-session. A file never blocks.
    err_log = tempfile.NamedTemporaryFile(prefix="calib2_", suffix=".log", delete=False)
    err_log.close()
    watch = CRASH_WATCH_S if watch_s is None else float(watch_s)
    import json  # noqa: PLC0415
    payload = json.dumps([cmd, str(workdir) if workdir else None, err_log.name, watch])
    try:
        helper = subprocess.run([sys.executable, "-c", _DETACH, payload], env=env,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=watch + 30)
    except FileNotFoundError:
        return False, ("pyFAI-calib2 not found. Run the command below in a terminal "
                       "with the platform env active."), cmd_str
    except Exception as exc:
        return False, f"could not launch pyFAI-calib2: {exc}", cmd_str

    out = (helper.stdout or "").strip().split()
    if out[:1] == ["ALIVE"]:
        where = f" Save the .poni into {workdir}." if workdir else ""
        dmsg = (f"detector {det}" if det else
                "no known detector matches this image size, so choose the detector in the GUI")
        return True, (f"Opened pyFAI-calib2 with {Path(cbf_path).name}, "
                      f"{resolve_calibrant(calibrant)}, {e:g} keV, {dmsg}. Pick rings, "
                      f"refine, then save the .poni.{where}"), cmd_str

    err = ""
    try:
        err = Path(err_log.name).read_text(errors="replace").strip()
    except Exception:
        pass
    if not err:
        err = (helper.stderr or "").strip()
    tail = " · ".join(err.splitlines()[-3:])[:400] if err else "no error output"
    rc = out[1] if out[:1] == ["DIED"] and len(out) > 1 else helper.returncode
    # The most common cause: the calib2 GUI needs a Qt binding. Name the exact fix.
    if "No Qt wrapper" in err or "Qt wrapper found" in err:
        hint = ("the calibration GUI needs a Qt binding (PySide6). It ships in "
                "requirements-core.txt, so reinstall it into the SAME environment "
                "that runs the platform:  pip install -r requirements-core.txt  "
                "(or: pip install PySide6)")
    elif "display" in err.lower() or "xcb" in err.lower():
        hint = "no display is available to show the GUI"
    else:
        hint = "usually a missing Qt binding (pip install PySide6) or no display"
    return False, (f"pyFAI-calib2 closed right after starting (rc={rc}): "
                   f"{hint}. {tail}"), cmd_str


def list_poni_files(poni_dir):
    """List existing .poni files in a directory (name, path)."""
    poni_dir = Path(poni_dir)
    if not poni_dir.is_dir():
        return []
    return [{"name": p.name, "path": str(p)} for p in sorted(poni_dir.glob("*.poni"))]
