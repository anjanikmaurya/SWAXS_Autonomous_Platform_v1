"""
tests/test_demo_pipeline_regression.py — Full-pipeline golden-master test
============================================================================
Runs the ENTIRE SWAXS pipeline — raw detector frames -> reduction ->
averaging -> background subtraction -> quality grading -> Guinier/Porod
analysis — on a small, real subset of beamline data checked in under
``tests/fixtures/demo_pipeline/`` (copied from Demo_Data/), and checks that
the numeric results never silently change.

Every stage calls the exact same ``src/`` functions the live Flask apps use
(``src.reduction.core.Experiment``/``run_pipeline``, ``src.plot_reduction
.average_and_save``, ``background.app._auto_scale``/``_subtract``,
``src.quality.core.grade_profile``, ``src.analysis.core.guinier_fit``/
``porod_fit``). Nothing here is mocked — this is a real PyFAI integration
over real detector images, not a unit test of pure math.

Fixture data
------------
Sample   : Run1_12CE_TMC_013_c_PES_support_x-113.06_y61.78_dy-1_ctr0_scan1
           (3 SAXS + 3 WAXS frames — a compound-loaded PES support scan)
Dark     : ..._dark_x-113.06_y61.78_dy-1_ctr0_scan1
           (3+3 frames — a shutter-closed reference at the same position;
           included only to exercise reduction/averaging on a second,
           very differently-scaled dataset — NOT used as a background)
Background: Run1_PES_support_x-165.15_y89.20_dy1_ctr0_scan1
           (3+3 frames — the bare support substrate, beam-on, comparable
           I0/Bstop magnitude to the sample — the scientifically sensible
           background partner for subtraction)

This is real experimental data, not synthetic — the quality/Guinier/Porod
numbers reflect an actual (imperfect) membrane sample, not a clean standard.
That's fine: this test checks REPRODUCIBILITY, not scientific validity.

Two checks
----------
1. ``test_pipeline_is_deterministic_across_runs`` — runs the full pipeline
   twice, in two independent temp directories, and asserts the two runs
   produce IDENTICAL numeric output. Needs no stored reference; it fails
   only if something in the pipeline is non-deterministic (unordered sets/
   dicts, uncontrolled randomness, etc).

2. ``test_pipeline_matches_golden_reference`` — runs the pipeline once and
   compares it against a stored "golden" reference
   (tests/fixtures/demo_pipeline/golden/). This is what catches an
   unintended change in scientific output the next time you edit reduction/
   averaging/subtraction/quality/analysis code.

   The golden reference is generated automatically the first time this test
   runs (if the golden files don't exist yet). To deliberately accept a new
   result after an intentional algorithm change, regenerate it:

       SWAXS_UPDATE_GOLDEN=1 uv run pytest tests/test_demo_pipeline_regression.py -s

   Then inspect the diff of tests/fixtures/demo_pipeline/golden/ in git
   before committing it.

Run:
    uv run pytest tests/test_demo_pipeline_regression.py -v
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import warnings
from pathlib import Path

import numpy as np
import pytest

# ── Make `import src.*` / `import background.app` work ──────────────────────
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "demo_pipeline"
GOLDEN_DIR = FIXTURE_DIR / "golden"
GOLDEN_ARRAYS_PATH = GOLDEN_DIR / "golden_arrays.npz"
GOLDEN_SCALARS_PATH = GOLDEN_DIR / "golden_scalars.json"

SAMPLE_KW = "Run1_12CE_TMC_013_c_PES_support_x-113.06_y61.78_dy-1_ctr0_scan1"
DARK_KW = "Run1_12CE_TMC_013_c_PES_support_dark_x-113.06_y61.78_dy-1_ctr0_scan1"
BKG_KW = "Run1_PES_support_x-165.15_y89.20_dy1_ctr0_scan1"
ALL_KEYWORDS = [SAMPLE_KW, DARK_KW, BKG_KW]

# Reduced from the production default (1000) purely for test speed — same
# physics, fewer q-bins. Everything downstream is compared at this resolution.
NPT_RADIAL = 200

# Fixed, sensible q-windows for this specific fixture (chosen once by hand —
# see docs/audits/*, standard practice is a user-selected Guinier/Porod
# window, not the full detector range). Kept fixed here for reproducibility.
GUINIER_QMAX = 0.5
POROD_QRANGE = (0.3, 1.9)

RTOL = 1e-6
ATOL = 1e-9

import importlib

# The full pipeline needs the REAL scientific libraries (pyFAI, which imports
# scipy.ndimage; fabio; pandas; xraydb). Several sibling unit tests install
# lightweight stand-in modules for these into ``sys.modules`` at import time,
# and any ``src.*`` module first imported against those stubs caches the stub
# references. In a full ``pytest`` run that would otherwise make this test
# either *skip* (a stub scipy breaks pyFAI's import) or *fail* (a stub pandas/
# fabio in the reduction path).
#
# This autouse fixture snapshots and removes every affected module, forces the
# real libraries (and a fresh import of the ``src.*`` / ``background.*``
# packages) for the duration of each test, then restores the snapshot so the
# stub-based sibling tests are left exactly as they were.
_REAL_DEPS = ("scipy", "pandas", "fabio", "pyFAI", "xraydb")


def _is_affected(name: str) -> bool:
    roots = _REAL_DEPS + ("src", "background")
    return name in roots or any(name.startswith(r + ".") for r in roots)


@pytest.fixture(autouse=True, scope="module")
def _real_scientific_libs():
    # Module-scoped: swap the real libraries in once for the whole module. Some
    # of them (e.g. silx, imported by pyFAI) register process-global state on
    # import and raise if imported twice, so we must NOT re-import per test.
    saved = {n: sys.modules[n] for n in list(sys.modules) if _is_affected(n)}

    def _restore():
        for n in [m for m in list(sys.modules) if _is_affected(m)]:
            del sys.modules[n]
        sys.modules.update(saved)

    for n in saved:
        del sys.modules[n]
    try:
        import scipy.ndimage  # noqa: F401
        import scipy.optimize  # noqa: F401
        import scipy.stats  # noqa: F401
        for _dep in ("pandas", "fabio", "pyFAI", "xraydb", "flask"):
            importlib.import_module(_dep)
    except ImportError as exc:  # the real libraries are genuinely not installed
        _restore()
        pytest.skip(f"real scientific libraries unavailable: {exc}")

    try:
        yield
    finally:
        _restore()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_config(project: Path) -> dict:
    """Same shape as a real config.yml, pointed at the temp project copy."""
    return {
        "data_directory": str(project / "2D"),
        "poni_directory": str(project / "poni"),
        "output_directory": str(project / "1D"),
        "poni_files": {"saxs": "SAXS.poni", "waxs": "WAXS.poni"},
        "mask_files": {"saxs": "RT_SAXS_mask_03.edf", "waxs": "RT_WAXS_mask.edf"},
        "dark_files": {"saxs": None, "waxs": None},
        "flat_files": {"saxs": None, "waxs": None},
        "detector_shapes": {"saxs": [1043, 981], "waxs": [195, 487]},
        "compound": "C2H4",
        "energy_keV": 12,
        "density_g_cm3": 0.92,
        "thickness": None,
        "mode": "SWAXS",
        "metadata_format": "csv",
        "npt_radial": NPT_RADIAL,
        "error_model": "poisson",
        "unit": "q_nm^-1",
        "correct_solid_angle": True,
        "polarization_factor": 0.95,
        "radial_range_min": None,
        "radial_range_max": None,
        "azimuth_range_min": None,
        "azimuth_range_max": None,
        "dummy": None,
        "delta_dummy": None,
        "i0_offset": 0,
        "bstop_offset": 0,
        "i0_air": 0,
        "bstop_air": 0,
        "saxs_filename_prefix": "sone_",
        "waxs_filename_prefix": "b_tassone_",
        "normalization": ["bstop"],
        "absolute_calibration_factor": 1,
        "beamline": {"type": "1-5", "data_format": "raw"},
    }


def _build_project(tmp_path: Path) -> Path:
    """Copy the read-only fixture into a fresh, writable project directory."""
    project = tmp_path / "project"
    shutil.copytree(FIXTURE_DIR / "2D", project / "2D")
    shutil.copytree(FIXTURE_DIR / "poni", project / "poni")
    return project


def _jsonable(obj):
    """Recursively convert numpy scalars/bools to plain Python types."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


def _run_full_pipeline(project: Path) -> dict:
    """
    Raw -> reduction -> averaging -> background subtraction -> quality ->
    Guinier/Porod, entirely via the same src/ functions the live apps use.

    Returns {"arrays": {name: np.ndarray}, "scalars": {...}} — fully
    JSON/NPZ-serialisable, with every non-deterministic field (timestamps,
    absolute tmp paths, run_id) already stripped out.
    """
    from src.reduction.core import Experiment, run_pipeline
    from src.plot_reduction import average_and_save
    from src.utils.read_dat_metadata import read_dat_data_metadata
    from src.quality.core import grade_profile
    from src.analysis.core import guinier_fit, porod_fit
    import background.app as bg
    bg._TRUNC["enabled"] = False   # golden tests the raw physics; ML truncate/rebin is a separate transform

    config = _build_config(project)
    corrections_by_file: dict = {}

    def _capture(result, raw_path, detector):
        corrections_by_file[raw_path.name] = result["corrections"]

    experiment = Experiment(config)
    counts = run_pipeline(config, experiment=experiment, file_done_callback=_capture)
    assert counts["stopped"] is False

    arrays: dict[str, np.ndarray] = {}
    scalars: dict = {"pipeline_counts": counts, "corrections": corrections_by_file}

    saxs_reduced_dir = project / "1D" / "SAXS" / "Reduction"
    waxs_reduced_dir = project / "1D" / "WAXS" / "Reduction"

    reduced_filenames: dict[str, list[str]] = {"saxs": [], "waxs": []}
    for label, folder in (("saxs", saxs_reduced_dir), ("waxs", waxs_reduced_dir)):
        for dat_path in sorted(folder.glob("*.dat")):
            _, q, I, sigma, _meta = read_dat_data_metadata(dat_path)
            key = f"reduced__{label}__{dat_path.stem}"
            arrays[f"{key}__q"] = np.asarray(q, dtype=float)
            arrays[f"{key}__I"] = np.asarray(I, dtype=float)
            arrays[f"{key}__sigma"] = np.asarray(sigma, dtype=float)
            reduced_filenames[label].append(dat_path.name)
    scalars["reduced_filenames"] = reduced_filenames

    saved_saxs = average_and_save(saxs_reduced_dir, keywords=ALL_KEYWORDS,
                                   output_dir=project / "1D" / "SAXS" / "Averaged")
    saved_waxs = average_and_save(waxs_reduced_dir, keywords=ALL_KEYWORDS,
                                   output_dir=project / "1D" / "WAXS" / "Averaged")
    scalars["averaged_keywords_saxs"] = sorted(kw for kw, _ in saved_saxs)
    scalars["averaged_keywords_waxs"] = sorted(kw for kw, _ in saved_waxs)

    def _subtract_and_grade(label: str, saved_pairs, sub_dir: Path):
        by_kw = dict(saved_pairs)
        sample_path, bkg_path = by_kw[SAMPLE_KW], by_kw[BKG_KW]
        q_s, I_s, sig_s = bg._load_dat(sample_path)
        q_b, I_b, sig_b = bg._load_dat(bkg_path)
        scale_info = bg._auto_scale(q_s, I_s, sig_s, q_b, I_b, sig_b)
        q_sub, I_sub, sig_sub = bg._subtract(
            q_s, I_s, sig_s, q_b, I_b, sig_b, scale_info["scale"])

        out_path = sub_dir / f"{SAMPLE_KW}_sub.dat"
        bg._write_dat(out_path, q_sub, I_sub, sig_sub)

        arrays[f"subtracted__{label}__q"] = q_sub
        arrays[f"subtracted__{label}__I"] = I_sub
        arrays[f"subtracted__{label}__sigma"] = sig_sub
        scalars[f"scale__{label}"] = scale_info

        grade = grade_profile(out_path, detector=label)
        grade.pop("path", None)     # absolute tmp path — not reproducible
        grade.pop("metrics", None)  # duplicates info already in the arrays
        scalars[f"quality__{label}"] = grade
        return q_sub, I_sub, sig_sub

    q_saxs_sub, I_saxs_sub, sig_saxs_sub = _subtract_and_grade(
        "saxs", saved_saxs, project / "1D" / "SAXS" / "Subtracted")
    _subtract_and_grade("waxs", saved_waxs, project / "1D" / "WAXS" / "Subtracted")

    guinier = guinier_fit(q_saxs_sub, I_saxs_sub, sig_saxs_sub, q_max=GUINIER_QMAX)
    guinier.pop("plot", None)
    porod = porod_fit(q_saxs_sub, I_saxs_sub, sig_saxs_sub,
                       q_min=POROD_QRANGE[0], q_max=POROD_QRANGE[1])
    porod.pop("plot", None)
    scalars["guinier"] = guinier
    scalars["porod"] = porod

    return {"arrays": arrays, "scalars": _jsonable(scalars)}


# ── Comparison helpers ───────────────────────────────────────────────────────

def _assert_scalars_equal(actual, expected, path: str = "root") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual)}"
        assert set(actual) == set(expected), (
            f"{path}: key mismatch — only in actual: {set(actual) - set(expected)}, "
            f"only in expected: {set(expected) - set(actual)}"
        )
        for k in expected:
            _assert_scalars_equal(actual[k], expected[k], f"{path}.{k}")
    elif isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected), f"{path}: length mismatch"
        for i, (a, e) in enumerate(zip(actual, expected)):
            _assert_scalars_equal(a, e, f"{path}[{i}]")
    elif isinstance(expected, float):
        if math.isnan(expected):
            assert isinstance(actual, float) and math.isnan(actual), (
                f"{path}: expected NaN, got {actual!r}")
        else:
            assert math.isclose(actual, expected, rel_tol=RTOL, abs_tol=ATOL), (
                f"{path}: {actual!r} != {expected!r}")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def _assert_arrays_equal(actual: dict, expected: dict) -> None:
    assert set(actual) == set(expected), (
        f"array key mismatch — only in actual: {set(actual) - set(expected)}, "
        f"only in expected: {set(expected) - set(actual)}"
    )
    for key in expected:
        np.testing.assert_allclose(
            actual[key], expected[key], rtol=RTOL, atol=ATOL,
            equal_nan=True, err_msg=f"array '{key}' differs",
        )


# ── Tests ────────────────────────────────────────────────────────────────────

def test_pipeline_is_deterministic_across_runs(tmp_path_factory):
    """Run the whole pipeline twice, in two independent temp dirs, and check
    the two runs produce byte-for-byte-equivalent numeric results. This needs
    no stored reference — it only fails if something in the pipeline is
    non-deterministic."""
    project_a = _build_project(tmp_path_factory.mktemp("run_a"))
    project_b = _build_project(tmp_path_factory.mktemp("run_b"))

    result_a = _run_full_pipeline(project_a)
    result_b = _run_full_pipeline(project_b)

    _assert_arrays_equal(result_a["arrays"], result_b["arrays"])
    _assert_scalars_equal(result_a["scalars"], result_b["scalars"])


def test_pipeline_matches_golden_reference(tmp_path):
    """Run the pipeline once and compare it to the stored golden reference.
    This is the test that catches an unintended change in scientific output
    the next time reduction/averaging/subtraction/quality/analysis code is
    edited. See the module docstring for how to regenerate the golden file."""
    project = _build_project(tmp_path)
    result = _run_full_pipeline(project)

    update = os.environ.get("SWAXS_UPDATE_GOLDEN") == "1"
    if update or not GOLDEN_ARRAYS_PATH.exists() or not GOLDEN_SCALARS_PATH.exists():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(GOLDEN_ARRAYS_PATH, **result["arrays"])
        GOLDEN_SCALARS_PATH.write_text(
            json.dumps(result["scalars"], indent=2, sort_keys=True) + "\n"
        )
        warnings.warn(
            "Golden reference was (re)generated from this run because it was "
            "missing or SWAXS_UPDATE_GOLDEN=1 was set. Inspect the diff of "
            "tests/fixtures/demo_pipeline/golden/ and commit it if correct."
        )
        return

    golden_arrays = dict(np.load(GOLDEN_ARRAYS_PATH))
    golden_scalars = json.loads(GOLDEN_SCALARS_PATH.read_text())

    _assert_arrays_equal(result["arrays"], golden_arrays)
    _assert_scalars_equal(result["scalars"], golden_scalars)
