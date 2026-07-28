"""
src/simulator/ground_truth.py — the hidden recipe → nanoparticle mapping.

This is the "physics" the autonomous loop is supposed to discover. It is
deliberately NOT visible to the optimizer: the campaign only ever sees the size
and PDI that come back from the analyzer, exactly as it would with real beam.

Landscape (interior optimum, per design interview)
--------------------------------------------------
    R(T, x_TOP)   = R_opt + dR_dT·(T − T_opt) + dR_dx·(x_TOP − x_opt)
                          + residence term in F_tot
    PDI(T, x_TOP) = pdi_min + cT·((T − T_opt)/σ_T)² + cx·((x_TOP − x_opt)/σ_x)²

R is locally linear, so it equals ``R_opt`` exactly at (T_opt, x_opt); PDI has a
strict interior minimum at the same point. The optimizer's loss

    loss = ((size − target)/tolerance)² + w·(PDI/pdi_cap)

therefore has a well-defined interior optimum — set the campaign's
``target_size`` to ``R_opt`` (default 4.0 nm) and the true optimum sits exactly
at (T_opt, x_opt), which makes convergence trivially checkable.

Everything is configurable under ``simulator.truth`` in reactor/config.yml, so
the landscape can be reshaped without touching code.
"""
from __future__ import annotations

import hashlib
import math

#: Defaults — a plausible hot-injection nanocrystal landscape.
#: Chosen to sit INTERIOR to the platform's default recipe bounds in
#: reactor/config.yml (T_reac 180–300 °C, F_tot 40–120 µL/min, x_each 0–0.3),
#: so the optimizer has to find a true interior optimum rather than rail to an
#: edge. If you widen those bounds, move these with them.
DEFAULTS: dict = {
    "T_opt":        240.0,   # °C     — optimum reaction temperature (in 180–300)
    "x_TOP_opt":    0.15,    # –      — optimum TOP mole fraction (in 0–0.3)
    "R_opt":        4.0,     # nm     — radius produced AT the optimum
    "dR_dT":        0.020,   # nm/°C  — size grows with temperature
    "dR_dx_TOP":   -6.0,     # nm     — more ligand → smaller particles
    # σ and curvature are sized so PDI stays BELOW pdi_ceil across the whole
    # search box (worst corner ≈ 0.42). If PDI saturates at the ceiling the
    # landscape goes flat and the optimizer has no gradient to follow — that
    # silently turns a convergence test into a no-op.
    "sigma_T":      60.0,    # °C     — half-range of T_reac bounds
    "sigma_x":      0.15,    # –      — half-range of x_each bounds
    "pdi_min":      0.02,    # –      — best achievable polydispersity
    "pdi_curv_T":   0.20,    # –      — PDI penalty per (ΔT/σ_T)²
    "pdi_curv_x":   0.20,    # –      — PDI penalty per (Δx/σ_x)²
    "F_ref":        80.0,    # µL/min — reference total flow (mid of 40–120)
    "residence_gain": 0.15,  # nm     — size gain per unit ln(F_ref/F_tot)
    "R_min":        1.0,     # nm     — clamp (interview: 1–10 nm)
    "R_max":        10.0,
    "pdi_floor":    0.001,   # clamp (interview: 0.001–0.5)
    "pdi_ceil":     0.5,
    "noise_R_frac": 0.03,    # 3 % run-to-run scatter on R
    "noise_pdi":    0.01,    # absolute scatter on PDI
    "seed":         None,    # None → derive deterministically from recipe_id
}


def _cfg(cfg: dict | None) -> dict:
    out = dict(DEFAULTS)
    if cfg:
        out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    return out


def _jitter(seed_key: str, salt: str) -> float:
    """Deterministic pseudo-random in [-1, 1] from a string key.

    Derived from the recipe_id so the SAME recipe always yields the SAME
    particles — reruns are reproducible, which matters when debugging a loop.
    """
    h = hashlib.sha256(f"{seed_key}|{salt}".encode()).digest()
    return (int.from_bytes(h[:8], "big") / 2 ** 63) - 1.0


def truth_from_recipe(recipe, cfg: dict | None = None, seed_key: str = "") -> dict:
    """Recipe → the nanoparticles it 'really' produced.

    ``recipe`` may be a Recipe dataclass or a plain dict; only T_reac, x_TOP and
    F_tot are used. Returns R_nm, pdi and the diagnostics needed to verify a
    closed-loop run against the known optimum.
    """
    c = _cfg(cfg)
    g = (lambda k, d: float(getattr(recipe, k, None) if not isinstance(recipe, dict)
                            else recipe.get(k, d)) if _has(recipe, k) else d)

    T     = g("T_reac", c["T_opt"])
    x_TOP = g("x_TOP", c["x_TOP_opt"])
    F_tot = g("F_tot", c["F_ref"])

    dT = T - c["T_opt"]
    dx = x_TOP - c["x_TOP_opt"]

    # ── size: locally linear so R == R_opt exactly at the optimum ────────────
    R = c["R_opt"] + c["dR_dT"] * dT + c["dR_dx_TOP"] * dx
    # residence time: slower flow → longer growth → bigger particles
    if F_tot > 0 and c["F_ref"] > 0:
        R += c["residence_gain"] * math.log(c["F_ref"] / F_tot)

    # ── polydispersity: strict interior minimum at the optimum ───────────────
    pdi = (c["pdi_min"]
           + c["pdi_curv_T"] * (dT / c["sigma_T"]) ** 2
           + c["pdi_curv_x"] * (dx / c["sigma_x"]) ** 2)

    # ── reproducible run-to-run scatter ──────────────────────────────────────
    key = seed_key or _recipe_id(recipe)
    if c["seed"] is not None:
        key = f"{c['seed']}|{key}"
    R   *= 1.0 + c["noise_R_frac"] * _jitter(key, "R")
    pdi += c["noise_pdi"] * _jitter(key, "pdi")

    R_clamped   = min(max(R, c["R_min"]), c["R_max"])
    pdi_clamped = min(max(pdi, c["pdi_floor"]), c["pdi_ceil"])

    return {
        "R_nm": R_clamped,
        "pdi": pdi_clamped,
        "R_unclamped": R,
        "pdi_unclamped": pdi,
        "clamped": (R_clamped != R) or (pdi_clamped != pdi),
        "T_reac": T, "x_TOP": x_TOP, "F_tot": F_tot,
        "distance_from_optimum": math.hypot(dT / c["sigma_T"], dx / c["sigma_x"]),
        "optimum": {"T_reac": c["T_opt"], "x_TOP": c["x_TOP_opt"],
                    "R_nm": c["R_opt"], "pdi": c["pdi_min"]},
    }


def _has(recipe, key) -> bool:
    if isinstance(recipe, dict):
        return recipe.get(key) is not None
    return getattr(recipe, key, None) is not None


def _recipe_id(recipe) -> str:
    if isinstance(recipe, dict):
        return str(recipe.get("recipe_id", "") or "no-id")
    return str(getattr(recipe, "recipe_id", "") or "no-id")


def describe(cfg: dict | None = None) -> str:
    """One-line summary of where the true optimum sits (for logs/docs)."""
    c = _cfg(cfg)
    return (f"true optimum: T_reac={c['T_opt']:g}°C, x_TOP={c['x_TOP_opt']:g} "
            f"→ R={c['R_opt']:g} nm, PDI={c['pdi_min']:g} "
            f"(set the campaign target_size to {c['R_opt']:g} nm to make the "
            f"loss minimum coincide exactly)")
