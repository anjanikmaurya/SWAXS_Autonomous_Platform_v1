"""src/analysis/model_fit.py: robust fit of one catalogue model to a SAXS curve.

Why not `core.sasmodels_fit`: it fits the intensity scale and the sizes
together in raw units with a scalar minimiser, and stalls near its start
(a 50 Å sphere came back as 76 Å with reduced χ² 282). Here:

  * scale and background are solved EXACTLY at every step: for a given shape,
    I(q) = a·F(q) + b is linear in (a, b), so they are a weighted linear
    least-squares problem (a ≥ 0, b ≥ 0), not search directions;
  * positive parameters (lengths, polydispersity) are searched on a log scale,
    so 5 Å and 500 Å are equally easy to reach;
  * scipy's bounded least squares on the residual vector (not a scalar sum),
    with several starting points seeded from the curve's features
    (Guinier Rg, the first oscillation minimum) plus spread-out defaults;
  * 1σ errors from the Jacobian at the best fit; χ², AIC and BIC with the
    two linear parameters counted.

Lengths in and out are nm (the platform's unit); sasmodels works in Å, so they
are converted here. Model definitions come from ai_knowledge/rules/saxs_models.yml.
"""
from __future__ import annotations

import math
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml

CATALOGUE = Path(__file__).resolve().parents[2] / "ai_knowledge" / "rules" / "saxs_models.yml"
PD_N = 25                      # quadrature points for size distributions (final fit)
PD_N_SEARCH = 9                # coarser during the multi-start search
SEARCH_POINTS = 120            # data points used during the search
TIME_BUDGET_S = 6.0            # per model: shapes that are slow to evaluate (long
                               # rods, orientation averaging) get fewer evaluations
PD_TYPE = "schulz"


def load_catalogue(path: Path = CATALOGUE) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@lru_cache(maxsize=32)
def _kernel(name: str):
    from sasmodels.core import load_model
    return load_model(name)


def _param_info(name: str) -> dict:
    return {p.name: p for p in _kernel(name).info.parameters.kernel_parameters}


def _is_length(units: str) -> bool:
    return (units or "").strip() in ("Ang", "Å")


class _OutOfTime(Exception):
    """The model's time budget ran out; the best point seen so far is kept."""


class _Problem:
    """One model + optional structure factor, set up for least squares."""

    def __init__(self, q_nm, I, sigma, model, entry, sq=None, sq_entry=None,
                 pd_n: int = PD_N):
        from sasmodels import data as smd, direct_model as dm
        self.name = f"{model}@{sq}" if sq else model
        self.info = _param_info(self.name)
        self.q, self.I, self.s = q_nm, I, sigma
        self.calc = dm.DirectModel(smd.Data1D(x=q_nm * 0.1, y=I, dy=sigma), _kernel(self.name))
        self.fixed = {"scale": 1.0, "background": 0.0, **(entry.get("fixed") or {})}
        if sq_entry:
            self.fixed.update(sq_entry.get("fixed") or {})
        for pdn in entry.get("pd", []):
            self.fixed[f"{pdn}_pd_n"] = pd_n
            self.fixed[f"{pdn}_pd_type"] = PD_TYPE
        names, lo, hi, logs = [], [], [], []
        b = dict(entry.get("bounds") or {})
        if sq_entry:
            b.update(sq_entry.get("bounds") or {})
        fit = list(entry.get("fit", [])) + list((sq_entry or {}).get("fit", []))
        for n in fit:
            l, h = b.get(n, (None, None))
            l, h = float(l), float(h)
            logs.append(l > 0)                  # positive → search on log scale
            names.append(n); lo.append(l); hi.append(h)
        for pdn in entry.get("pd", []):
            names.append(f"{pdn}_pd"); lo.append(0.005); hi.append(0.6); logs.append(True)
        self.names, self.lo, self.hi, self.logs = names, np.array(lo), np.array(hi), np.array(logs)
        self.deadline = None                    # perf_counter time; None = no limit
        self.best_cost, self.best_x = np.inf, None

    # conversions: platform values (nm) ↔ sasmodels (Å) ↔ search space (log)
    def _to_model(self, n, v):
        base = n[:-3] if n.endswith("_pd") else n
        if not n.endswith("_pd") and base in self.info and _is_length(self.info[base].units):
            return v * 10.0
        return v

    def to_x(self, vals):
        v = np.clip(np.asarray(vals, float), self.lo, self.hi)
        return np.where(self.logs, np.log(np.maximum(v, 1e-12)), v)

    def from_x(self, x):
        return np.where(self.logs, np.exp(x), x)

    def shape(self, vals):
        p = dict(self.fixed)
        p.update({n: self._to_model(n, v) for n, v in zip(self.names, vals)})
        return np.asarray(self.calc(**p), float)

    def linear(self, F):
        """Weighted LS for I ≈ a·F + b with a, b ≥ 0. Returns a, b."""
        w = 1.0 / self.s
        A = np.vstack([F * w, w]).T
        sol, *_ = np.linalg.lstsq(A, self.I * w, rcond=None)
        a, b = float(sol[0]), float(sol[1])
        if b < 0:
            b = 0.0
            a = float(np.sum(F * self.I * w * w) / max(np.sum((F * w) ** 2), 1e-300))
        return max(a, 1e-300), b

    def resid(self, x):
        if self.deadline is not None and time.perf_counter() > self.deadline:
            raise _OutOfTime()
        try:
            F = self.shape(self.from_x(x))
            if not np.all(np.isfinite(F)) or np.all(F <= 0):
                return np.full_like(self.I, 1e6)
            a, b = self.linear(F)
            r = (self.I - (a * F + b)) / self.s
        except Exception:                                    # noqa: BLE001
            return np.full_like(self.I, 1e6)
        c = float(np.dot(r, r))
        if c < self.best_cost:
            self.best_cost, self.best_x = c, np.array(x, float)
        return r


def _starts(prob: _Problem, seeds: dict, n_extra: int = 3, rng=None) -> list[np.ndarray]:
    """Starting points: seeded sizes first, then spread-out defaults."""
    rng = rng or np.random.default_rng(0)
    R = seeds.get("R")
    out = []
    base = []
    for n, l, h in zip(prob.names, prob.lo, prob.hi):
        if n.endswith("_pd"):
            base.append(0.1)
        elif R and n in ("radius", "radius_equatorial"):
            # the first minimum measures the OUTER radius; with a shell, the
            # core is smaller
            base.append(0.8 * R if "thickness" in prob.names else R)
        elif R and n == "radius_polar":
            base.append(R)
        elif R and n == "length":
            base.append(4 * R)
        elif R and n == "thickness":
            base.append(max(R * 0.2, l))
        elif l > 0:
            base.append(math.sqrt(l * h))
        else:
            base.append(0.5 * (l + h))
    out.append(np.array(base))
    if R and "radius_polar" in prob.names:              # prolate and oblate guesses
        for f in (2.5, 0.4):
            v = np.array(base); v[prob.names.index("radius_polar")] = R * f
            out.append(v)
    if R and "length" in prob.names:
        v = np.array(base); v[prob.names.index("length")] = 12 * R
        out.append(v)
    # Parameters that can sit either side of a reference (a shell contrast
    # above or below the solvent) get starts spread over their range.
    for i, (n, l, h, lg) in enumerate(zip(prob.names, prob.lo, prob.hi, prob.logs)):
        if not lg:
            for f in (0.15, 0.5, 0.85):
                v = np.array(base); v[i] = l + f * (h - l)
                out.append(v)
    for _ in range(n_extra):
        u = rng.random(len(prob.names))
        v = np.where(prob.logs,
                     np.exp(np.log(np.maximum(prob.lo, 1e-12))
                            + u * (np.log(prob.hi) - np.log(np.maximum(prob.lo, 1e-12)))),
                     prob.lo + u * (prob.hi - prob.lo))
        out.append(v)
    return out


def seeds_from_features(features: dict) -> dict:
    """A size guess (nm) from the first minimum, else from Guinier Rg."""
    R = (features.get("minima") or {}).get("sphere_R_from_first_min")
    g = features.get("guinier") or {}
    if not R and g.get("valid") and g.get("Rg"):
        R = math.sqrt(5.0 / 3.0) * g["Rg"]
    return {"R": R} if R else {}


def _rg_of(prob: _Problem, vals, a) -> float | None:
    """Rg of the fitted model (nm), from a Guinier fit of the model itself on a
    low-q grid, so it is defined for every shape."""
    try:
        from sasmodels import data as smd, direct_model as dm
        Rmax = max(float(v) for n, v in zip(prob.names, vals)
                   if not n.endswith("_pd") and n in prob.info and _is_length(prob.info[n].units))
        qg = np.geomspace(0.02 / Rmax, 0.6 / Rmax, 40)       # nm⁻¹, q·Rg ≲ 0.5
        calc = dm.DirectModel(smd.empty_data1D(qg * 0.1), _kernel(prob.name.split("@")[0]))
        p = {k: v for k, v in prob.fixed.items() if k in _param_info(prob.name.split("@")[0])
             or k.endswith(("_pd_n", "_pd_type")) or k in ("scale", "background")}
        p.update({n: prob._to_model(n, v) for n, v in zip(prob.names, vals)
                  if (n[:-3] if n.endswith("_pd") else n) in _param_info(prob.name.split("@")[0])})
        Iq = np.asarray(calc(**p), float)
        slope = np.polyfit(qg ** 2, np.log(Iq), 1)[0]
        return float(math.sqrt(-3 * slope)) if slope < 0 else None
    except Exception:                                        # noqa: BLE001
        return None


def fit_model(q, I, sigma, model: str, catalogue: dict | None = None,
              structure_factor: str | None = None, seeds: dict | None = None,
              starts: list[dict] | None = None, n_extra: int = 3) -> dict:
    """Fit one catalogue model (optionally ×structure factor). Never raises.

    Returns {model, params (nm), errors, scale, background, chi2, chi2_total,
    n_points, n_free, aic, bic, converged, at_bounds, rg, equivalent_radius,
    pdi, I_fit} or {model, error}.
    """
    from scipy.optimize import least_squares
    cat = catalogue or load_catalogue()
    name = f"{model}@{structure_factor}" if structure_factor else model
    try:
        entry = cat["models"][model]
        sq_entry = cat["structure_factors"][structure_factor] if structure_factor else None
        q = np.asarray(q, float); I = np.asarray(I, float)
        s = np.asarray(sigma, float) if sigma is not None else np.abs(I) * 0.05
        m = np.isfinite(q) & np.isfinite(I) & np.isfinite(s) & (q > 0) & (s > 0)
        q, I, s = q[m], I[m], s[m]
        prob = _Problem(q, I, s, model, entry, structure_factor, sq_entry)
        # Coarse search: every start gets a short fit on a thinned grid with a
        # coarse size distribution (orientation-averaged shapes cost ~10 ms per
        # evaluation); only the best start is refined on the full data.
        step = max(1, len(q) // SEARCH_POINTS)
        coarse = _Problem(q[::step], I[::step], s[::step], model, entry,
                          structure_factor, sq_entry, pd_n=PD_N_SEARCH)
        cands = _starts(prob, seeds or {}, n_extra=n_extra)
        for st in starts or []:
            v = np.array([float(st.get(n, c)) for n, c in zip(prob.names, cands[0])])
            cands.insert(0, v)
        lo_x, hi_x = prob.to_x(prob.lo), prob.to_x(prob.hi)
        x0s = [np.clip(prob.to_x(v0), lo_x + 1e-9, hi_x - 1e-9) for v0 in cands]
        t0 = time.perf_counter()
        costs = [float(np.sum(coarse.resid(x) ** 2)) for x in x0s]   # one eval each
        t_eval = max((time.perf_counter() - t0) / len(x0s), 1e-5)
        # max_nfev counts function evaluations only; each step also costs about
        # n_par more for the numerical Jacobian, so the budget divides by that.
        n_par = len(prob.names) + 1
        nfev_c = int(np.clip(0.5 * TIME_BUDGET_S / (t_eval * len(x0s) * n_par), 2, 40))
        order = np.argsort(costs)                       # most promising starts first
        coarse.deadline = t0 + 0.6 * TIME_BUDGET_S      # hard stops, so no model can
        prob.deadline = t0 + 1.5 * TIME_BUDGET_S        # run away (long rods did)
        x_start = x0s[int(order[0])]
        for i in order:
            try:
                least_squares(coarse.resid, x0s[i], bounds=(lo_x, hi_x), method="trf",
                              x_scale="jac", max_nfev=nfev_c)
            except _OutOfTime:
                break
        if coarse.best_x is not None:
            x_start = coarse.best_x
        t_full = t_eval * len(q) / max(len(q[::step]), 1) * (PD_N / PD_N_SEARCH)
        nfev_f = int(np.clip(0.4 * TIME_BUDGET_S / (t_full * n_par), 3, 60))
        timed_out = False
        try:
            best = least_squares(prob.resid, x_start, bounds=(lo_x, hi_x), method="trf",
                                 x_scale="jac", max_nfev=nfev_f)
            x_best = best.x
        except _OutOfTime:
            timed_out, best = True, None
            x_best = prob.best_x if prob.best_x is not None else x_start
        prob.deadline = None
        vals = prob.from_x(x_best)
        F = prob.shape(vals)
        a, b = prob.linear(F)
        I_fit = a * F + b
        chi2_total = float(np.sum(((I - I_fit) / s) ** 2))
        n, k = len(I), len(prob.names) + 2
        chi2 = chi2_total / max(n - k, 1)
        errors = {}
        try:                                   # Jacobian in x-space → value space
            J = best.jac if best is not None else None
            cov = np.linalg.inv(J.T @ J) * max(chi2, 1.0)
            dx = np.sqrt(np.clip(np.diag(cov), 0, None))
            for nm, v, d, lg in zip(prob.names, vals, dx, prob.logs):
                errors[nm] = float(v * d) if lg else float(d)
        except Exception:                                    # noqa: BLE001
            pass
        at_bounds = [nm for nm, v, l, h in zip(prob.names, vals, prob.lo, prob.hi)
                     if v <= l * (1 + 1e-3) + 1e-12 or v >= h * (1 - 1e-3)]
        params = {nm: float(v) for nm, v in zip(prob.names, vals)}
        rg = _rg_of(prob, vals, a)
        eq = entry.get("equivalent_radius", "rg")
        R_eq = params.get(eq) if eq in params else (math.sqrt(5 / 3) * rg if rg else None)
        pd_keys = [f"{p}_pd" for p in entry.get("pd", [])]
        return {
            "model": name, "params": params, "errors": errors,
            "scale": a, "background": b,
            "chi2": round(chi2, 5), "chi2_total": round(chi2_total, 4),
            "n_points": n, "n_free": k,
            "aic": round(chi2_total + 2 * k, 4),
            "bic": round(chi2_total + k * math.log(n), 4),
            "converged": bool(best.success) if best is not None else False,
            "timed_out": timed_out, "at_bounds": at_bounds,
            "rg": rg, "equivalent_radius": R_eq,
            "pdi": params.get(pd_keys[0]) if pd_keys else None,
            "q": q.tolist(), "I_fit": I_fit.tolist(),
        }
    except Exception as exc:                                 # noqa: BLE001
        return {"model": name, "error": f"{type(exc).__name__}: {exc}"}
