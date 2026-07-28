"""
src/simulator/pattern.py — synthetic 2D SAXS detector frames from real geometry.

Pipeline per frame:
    .poni geometry ─→ per-pixel q map (cached)
    R, PDI         ─→ Schulz-polydisperse sphere I(q) on a 1-D grid
    np.interp      ─→ 2-D intensity image
    × exposure·flux, + solvent background
    beamstop disc + mask .edf   ─→ zeroed regions
    Poisson                     ─→ int32 counts (the .raw format)

Working units are nm⁻¹ for q and nm for radii, matching the platform's default
``unit: q_nm^-1`` so the analyzer's fitted radius is directly comparable to the
injected one.

pyFAI/fabio are imported lazily so this module loads without them.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

#: cache: (poni, shape) → q map, so geometry is built once not once-per-frame
_QCACHE: dict = {}
_MASKCACHE: dict = {}


# ── form factor ───────────────────────────────────────────────────────────────
def sphere_form_factor(q, R):
    """Normalised sphere form factor P(q) = F², F(0) = 1. q in nm⁻¹, R in nm."""
    q = np.asarray(q, float)
    x = q * float(R)
    out = np.ones_like(x)
    nz = x > 1e-8
    xn = x[nz]
    out[nz] = 3.0 * (np.sin(xn) - xn * np.cos(xn)) / xn ** 3
    return out ** 2


def schulz_weights(R_mean, pdi, n=41):
    """Schulz-Zimm radius distribution → (radii, volume-squared weights).

    PDI here is σ/R (the coefficient of variation), matching how the analyzer
    reports it. Very low PDI collapses to a single radius.
    """
    R_mean = float(R_mean)
    pdi = max(float(pdi), 1e-4)
    if pdi < 5e-3:                                  # effectively monodisperse
        return np.array([R_mean]), np.array([1.0])
    z = 1.0 / pdi ** 2 - 1.0
    lo = max(1e-3, R_mean * (1.0 - 4.0 * pdi))
    hi = R_mean * (1.0 + 4.0 * pdi)
    R = np.linspace(lo, hi, n)
    # Schulz: f(R) ∝ R^z exp(-(z+1) R / R_mean)   (log form for numerical safety)
    with np.errstate(over="ignore", invalid="ignore"):
        logf = z * np.log(R / R_mean) - (z + 1.0) * (R / R_mean)
    f = np.exp(logf - np.nanmax(logf))
    f = np.nan_to_num(f, nan=0.0)
    V = (4.0 / 3.0) * math.pi * R ** 3
    w = f * V ** 2                                  # scattering ∝ V² per particle
    s = w.sum()
    return R, (w / s if s > 0 else np.ones_like(w) / w.size)


#: reference q (nm⁻¹) at which the capillary upturn coefficient is quoted
Q_REF = 0.1


def background_curve(q, solvent_bkg=2.0, capillary=5.0):
    """Solvent + capillary scattering — the part a background frame measures.

    The SAMPLE frame contains this too, so subtracting a background frame leaves
    the pure particle signal. That is what makes the subtraction app testable.

    ``capillary`` is the upturn intensity AT q = Q_REF (0.1 nm⁻¹), scaling as
    q⁻²; quoting it at a reference q keeps it interpretable and stops it
    exploding as q → 0.
    """
    q = np.maximum(np.asarray(q, float), 1e-4)
    return float(solvent_bkg) + float(capillary) * (q / Q_REF) ** -2.0


def iq_curve(q, R_nm, pdi, scale=1.0, bkg=0.0, porod=0.0):
    """Polydisperse sphere I(q) on a 1-D q grid (nm⁻¹). Particle term only,
    normalised so the particle contribution is ``scale`` at q → 0; ``bkg`` is
    added verbatim (pass background_curve(...) for a realistic frame)."""
    q = np.asarray(q, float)
    radii, w = schulz_weights(R_nm, pdi)
    I = np.zeros_like(q)
    for R, wi in zip(radii, w):
        I += wi * sphere_form_factor(q, R)
    I = scale * I
    if porod:                                       # optional extra q^-4 tail
        I += porod * np.power(np.maximum(q, 1e-6), -4.0)
    return I + bkg


# ── geometry ──────────────────────────────────────────────────────────────────
def q_map(poni_path, shape):
    """Per-pixel q (nm⁻¹) from a real .poni, cached by (poni, shape, mtime)."""
    poni_path = str(poni_path)
    try:
        mtime = Path(poni_path).stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (poni_path, tuple(shape), mtime)
    if key in _QCACHE:
        return _QCACHE[key]
    import pyFAI                                                # noqa: PLC0415
    try:
        from pyFAI.integrator.azimuthal import AzimuthalIntegrator  # noqa: PLC0415
    except Exception:                                            # older pyFAI
        from pyFAI.azimuthalIntegrator import AzimuthalIntegrator   # noqa: PLC0415
    ai = AzimuthalIntegrator()
    ai.load(poni_path)
    q = np.asarray(ai.qArray(tuple(shape)), float)               # nm⁻¹
    _QCACHE[key] = q
    return q


def synthetic_q_map(shape, pixel_m=172e-6, dist_m=1.0, wavelength_m=1.033e-10,
                    center=None):
    """Fallback q map when no .poni exists yet (keeps the mock usable off-rig)."""
    rows, cols = int(shape[0]), int(shape[1])
    cy, cx = center or (rows / 2.0, cols / 2.0)
    yy, xx = np.mgrid[0:rows, 0:cols]
    r_m = np.hypot((yy - cy) * pixel_m, (xx - cx) * pixel_m)
    two_theta = np.arctan2(r_m, dist_m)
    return (4.0 * math.pi / (wavelength_m * 1e9)) * np.sin(two_theta / 2.0)


def load_mask(mask_path, shape):
    """Load a mask .edf → boolean array (True = masked/dead). Cached."""
    if not mask_path:
        return None
    key = (str(mask_path), tuple(shape))
    if key in _MASKCACHE:
        return _MASKCACHE[key]
    try:
        import fabio                                             # noqa: PLC0415
        m = np.asarray(fabio.open(str(mask_path)).data)
        m = m.astype(bool) if m.shape == tuple(shape) else None
    except Exception:
        m = None
    _MASKCACHE[key] = m
    return m


def beamstop_mask(shape, q, q_beamstop=0.02, center=None, radius_px=None):
    """True inside the beamstop shadow.

    Defined in q by default (physically meaningful and geometry-independent);
    ``radius_px`` overrides with a plain circle when a center is known.
    """
    if radius_px and center:
        rows, cols = shape
        yy, xx = np.mgrid[0:rows, 0:cols]
        return np.hypot(yy - center[0], xx - center[1]) <= float(radius_px)
    return q < float(q_beamstop)


# ── frame synthesis ───────────────────────────────────────────────────────────
def simulate_frame(q, R_nm, pdi, *, exposure_s=1.0, flux=1e6, scale=1.0,
                   solvent_bkg=2.0, capillary=5.0, porod=0.0, beamstop=None,
                   mask=None, rng=None, particles=True, max_counts=2_000_000):
    """Build one int32 detector frame.

    ``particles=False`` produces a particle-free background frame — the flush
    collections use it, giving the subtraction app a genuine matched pair.
    Both frame types share the SAME background curve, so
    ``sample − background`` recovers the pure particle form factor.
    """
    rng = rng or np.random.default_rng()
    qg = np.linspace(float(np.nanmin(q)), float(np.nanmax(q)), 1500)
    bkg = background_curve(qg, solvent_bkg=solvent_bkg, capillary=capillary)
    Ig = iq_curve(qg, R_nm, pdi, scale=scale, bkg=bkg, porod=porod) if particles else bkg

    img = np.interp(q, qg, Ig) * float(exposure_s) * float(flux) / 1e6
    img = np.clip(img, 0.0, max_counts)
    out = rng.poisson(img).astype(np.int64)

    if beamstop is not None:
        out[beamstop] = 0
    if mask is not None:
        out[mask] = 0
    return np.clip(out, 0, np.iinfo(np.int32).max).astype(np.int32)
