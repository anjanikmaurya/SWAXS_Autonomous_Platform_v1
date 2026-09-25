"""
src/ai/plots.py — AI-Triggered Plot Generation
================================================
Generates matplotlib figures as base64-encoded PNG strings for inline
display in the AI assistant chat panel.

Static plot functions return a 300-dpi base64 PNG and accept an optional
``export_path`` ending in .png, .svg, or .pdf. ``overlay_plotly`` returns an
interactive figure dictionary using the same publication palette and styling.
The caller embeds it as:  <img src="data:image/png;base64,{result}">

Available plot functions
------------------------
    plot_curve(q, I, sigma, ...)     — plain 1D scattering curve
    plot_guinier(q, I, sigma, ...)   — ln I vs q² with fit overlay
    plot_kratky(q, I, sigma, ...)    — q²I vs q (folding / flexibility)
    plot_porod(q, I, ...)            — q⁴I vs q⁴ (surface area)
    plot_pair_distance(r, pr, ...)   — p(r) pair distance distribution
    plot_multi(datasets, ...)        — overlay multiple 1D curves

Usage
-----
    from src.ai.plots import generate_plot

    b64 = generate_plot(
        "guinier",
        q=q_arr, I=I_arr, sigma=sig_arr,
        q_min=0.012, q_max=0.045,
        Rg=3.2, I0=0.0142,
    )
"""

from __future__ import annotations

import base64
import functools
import io
import logging
import textwrap
import threading
from html import escape
from typing import Any
from pathlib import Path

import numpy as np

logger = logging.getLogger("swaxs_platform")

# Use non-interactive Agg backend (no display required)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── Thread safety ────────────────────────────────────────────────────────────
# pyplot keeps GLOBAL state and is NOT thread-safe. The assistant serves chat
# turns on a threaded Flask server, so two turns plotting at once corrupted that
# state — the symptom was `matplotlib has no attribute 'get_data_path'` — and a
# figure orphaned by an error under concurrency leaked file descriptors until the
# process hit "Too many open files". Serialise every entry point that touches
# matplotlib, and close any half-built figure on error. RLock so the
# generate_plot dispatcher can call an already-wrapped plot_* without deadlock.
_MPL_LOCK = threading.RLock()


def _serialized(fn):
    @functools.wraps(fn)
    def _wrap(*args, **kwargs):
        with _MPL_LOCK:
            try:
                return fn(*args, **kwargs)
            except Exception:
                plt.close("all")     # never leak a figure created before the error
                raise
    return _wrap

# ── Style constants ────────────────────────────────────────────────────────────
_FIG_W    = 6.4    # inches; readable at common manuscript figure widths
_FIG_H    = 4.4
_DPI      = 300
_PALETTE  = ("#0072B2", "#D55E00", "#009E73", "#CC79A7",
             "#E69F00", "#56B4E9", "#333333", "#777777")
_LINESTYLES = ("-", "--", "-.", ":")
_DATA_C   = _PALETTE[0]
_FIT_C    = _PALETTE[1]
_RANGE_C  = "#F0E4B8"
_GRID_KW  = {"alpha": 0.65, "linewidth": 0.5, "color": "#D9DEE3"}
_ERR_KW   = {"alpha": 0.18, "linewidth": 0}


@_serialized
def generate_plot(plot_type: str, **kwargs: Any) -> str:
    """
    Dispatcher — call the appropriate plot function by name.

    Parameters
    ----------
    plot_type : "curve" | "guinier" | "kratky" | "porod" | "pair_distance"
                | "multi"
    **kwargs  : passed directly to the specific plot function

    Returns
    -------
    str — base64-encoded PNG
    """
    _DISPATCH = {
        "curve":         plot_curve,
        "guinier":       plot_guinier,
        "kratky":        plot_kratky,
        "porod":         plot_porod,
        "pair_distance": plot_pair_distance,
        "multi":         plot_multi,
    }
    fn = _DISPATCH.get(plot_type)
    if fn is None:
        raise ValueError(
            f"Unknown plot_type '{plot_type}'. "
            f"Choose from: {list(_DISPATCH)}"
        )
    return fn(**kwargs)


# ── Individual plot functions ─────────────────────────────────────────────────

@_serialized
def plot_curve(
    q:       "np.ndarray",
    I:       "np.ndarray",
    sigma:   "np.ndarray | None" = None,
    label:   str = "I(q)",
    title:   str = "Scattering Curve",
    loglog:  bool = True,
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    Standard 1D scattering curve: I(q) vs q (log-log by default).
    """
    q, I = np.asarray(q), np.asarray(I)
    mask = (q > 0) & (I > 0)

    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H), dpi=_DPI)
    if sigma is not None:
        sig = np.asarray(sigma)
        ax.fill_between(q[mask], (I - sig)[mask], (I + sig)[mask],
                        color=_DATA_C, **_ERR_KW)
    ax.plot(q[mask], I[mask], color=_DATA_C, linewidth=1.6, label=label)

    if loglog:
        ax.set_xscale("log")
        ax.set_yscale("log")

    ax.set_xlabel("q  (nm⁻¹)")
    ax.set_ylabel("I(q)  (a.u.)")
    ax.set_title(title)
    ax.grid(True, which="both", **_GRID_KW)
    return _fig_to_b64(fig, export_path=export_path)


@_serialized
def plot_guinier(
    q:       "np.ndarray",
    I:       "np.ndarray",
    sigma:   "np.ndarray | None" = None,
    q_min:   float | None = None,
    q_max:   float | None = None,
    Rg:      float | None = None,
    I0:      float | None = None,
    title:   str = "Guinier Analysis",
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    Guinier plot: ln I vs q².  Fit range highlighted; best-fit line overlaid.
    """
    q, I = np.asarray(q, dtype=float), np.asarray(I, dtype=float)
    mask = (q > 0) & (I > 0)
    qm   = q[mask]
    q2   = qm ** 2
    lnI  = np.log(I[mask])
    lnI_err = (np.asarray(sigma, dtype=float)[mask] / I[mask]
               if sigma is not None else None)

    # Show ONLY the Guinier region of interest, not the whole curve. Upper bound:
    # a little past the fit window (or the qRg≈1.3 validity limit); a small low-q
    # margin for context.
    q_hi = (float(q_max) * 1.3 if q_max is not None
            else (1.5 / float(Rg) if Rg else float(qm.max())))
    keep = qm <= q_hi
    q2, lnI = q2[keep], lnI[keep]
    if lnI_err is not None:
        lnI_err = lnI_err[keep]

    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H), dpi=_DPI)

    if lnI_err is not None:
        ax.fill_between(q2, lnI - lnI_err, lnI + lnI_err,
                        color=_DATA_C, **_ERR_KW)

    ax.plot(q2, lnI, ".", color=_DATA_C, markersize=3.5, label="ln I(q)")

    # Fit range shading
    if q_min is not None and q_max is not None:
        r_mask = (q[mask] >= q_min) & (q[mask] <= q_max)
        if r_mask.any():
            x_lo = q_min ** 2
            x_hi = q_max ** 2
            ax.axvspan(x_lo, x_hi, color=_RANGE_C, alpha=0.8,
                       label=f"Fit range [{q_min:.4f}–{q_max:.4f} nm⁻¹]")

            # Overlay fit line if Rg and I0 are known
            if Rg is not None and I0 is not None:
                q2_fit = np.linspace(x_lo, x_hi, 200)
                lnI_fit = np.log(I0) - (Rg ** 2 / 3.0) * q2_fit
                ax.plot(q2_fit, lnI_fit, color=_FIT_C, linewidth=2,
                        label=f"Fit: Rg={Rg:.2f} nm, I0={I0:.3g}")

    if Rg is not None:
        # qRg validity markers
        qRg_lo = 0.3 / Rg if Rg > 0 else 0
        qRg_hi = 1.3 / Rg if Rg > 0 else 0
        ax.axvline(qRg_lo ** 2, color=_PALETTE[2], linestyle="--",
                   linewidth=0.9, label=f"qRg=0.3  (q={qRg_lo:.4f})")
        ax.axvline(qRg_hi ** 2, color=_FIT_C, linestyle=":",
                   linewidth=0.9, label=f"qRg=1.3  (q={qRg_hi:.4f})")

    ax.set_xlim(0, float(q_hi) ** 2)          # zoom to the region of interest
    ax.set_xlabel("q²  (nm⁻²)")
    ax.set_ylabel("ln I(q)")
    ax.set_title(title)
    ax.grid(True, **_GRID_KW)
    return _fig_to_b64(fig, export_path=export_path)


@_serialized
def plot_kratky(
    q:       "np.ndarray",
    I:       "np.ndarray",
    sigma:   "np.ndarray | None" = None,
    title:   str = "Kratky Plot",
    Rg:      float | None = None,
    I0:      float | None = None,
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    Kratky plot: q²I vs q.
    A bell-shaped peak indicates a folded, globular protein.
    A plateau or monotonic rise indicates flexibility / unfolding.
    Optionally overlays the dimensionless Kratky normalization.
    """
    q, I = np.asarray(q, dtype=float), np.asarray(I, dtype=float)
    mask = (q > 0) & (I > 0)

    fig, axes = plt.subplots(
        1, 2 if (Rg and I0) else 1,
        figsize=(_FIG_W * (1.7 if (Rg and I0) else 1), _FIG_H),
        dpi=_DPI,
    )
    ax = axes[0] if (Rg and I0) else axes

    # Standard Kratky
    y = q[mask] ** 2 * I[mask]
    if sigma is not None:
        sig = np.asarray(sigma, dtype=float)
        yerr = q[mask] ** 2 * sig[mask]
        ax.fill_between(q[mask], y - yerr, y + yerr, color=_DATA_C, **_ERR_KW)
    ax.plot(q[mask], y, color=_DATA_C, linewidth=1.6)
    ax.set_xlabel("q  (nm⁻¹)")
    ax.set_ylabel("q²·I(q)")
    ax.set_title("Kratky Plot" if (Rg and I0) else title)
    ax.grid(True, **_GRID_KW)

    # Dimensionless Kratky (if Rg and I0 available)
    if Rg and I0:
        ax2   = axes[1]
        qRg   = q[mask] * Rg
        ydk   = (qRg) ** 2 * I[mask] / I0
        ax2.plot(qRg, ydk, color=_DATA_C, linewidth=1.6,
                 label="Dimensionless Kratky")
        # Ideal globule marker at (√3, 3/e) ≈ (1.732, 1.103)
        ax2.plot(np.sqrt(3), 3 / np.e, "*", color=_FIT_C, markersize=10,
                 label=f"Ideal globule (√3, 3/e)")
        ax2.set_xlabel("qRg")
        ax2.set_ylabel("(qRg)²·I/I₀")
        ax2.set_title("Dimensionless Kratky")
        ax2.grid(True, **_GRID_KW)

    if Rg and I0:
        fig.suptitle(title)
    return _fig_to_b64(fig, export_path=export_path)


@_serialized
def plot_porod(
    q:     "np.ndarray",
    I:     "np.ndarray",
    title: str = "Porod Analysis",
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    Porod plot: q⁴·I(q) vs q  (the standard, readable form).

    Multiplying out the expected q⁻⁴ high-q decay flattens the curve, so a smooth,
    sharp particle/solvent interface shows a horizontal PLATEAU at high q (the
    "Porod constant", ∝ surface area). A plateau that instead RISES means excess
    high-q signal (background not fully subtracted, or a steeper-than-4 tail);
    one that FALLS means a diffuse/rough interface (exponent < 4).
    """
    q, I = np.asarray(q, dtype=float), np.asarray(I, dtype=float)
    mask = (q > 0) & (I > 0)
    qm   = q[mask]
    q4I  = qm ** 4 * I[mask]

    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H), dpi=_DPI)
    ax.plot(qm, q4I, ".", color=_DATA_C, markersize=3.5, label="q⁴·I(q)")
    # Guide line at the high-q median level — the eye reads "flat vs sloped"
    # against it. Uses the top third of the q-range (the Porod region).
    if qm.size > 6:
        hi = qm >= np.percentile(qm, 66)
        if hi.any():
            lvl = float(np.median(q4I[hi]))
            ax.axhline(lvl, color=_FIT_C, linestyle="--", linewidth=1.0,
                       label=f"high-q plateau ≈ {lvl:.3g} (Porod const if flat)")
    ax.set_xlabel("q  (nm⁻¹)")
    ax.set_ylabel("q⁴·I(q)   (Porod)")
    ax.set_title(title)
    ax.grid(True, **_GRID_KW)
    return _fig_to_b64(fig, export_path=export_path)


@_serialized
def plot_pair_distance(
    r:     "np.ndarray",
    pr:    "np.ndarray",
    Dmax:  float | None = None,
    title: str = "Pair Distance Distribution  p(r)",
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    p(r) pair distance distribution.  Dmax is annotated if provided.
    """
    r, pr = np.asarray(r, dtype=float), np.asarray(pr, dtype=float)

    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H), dpi=_DPI)
    ax.fill_between(r, 0, pr, color=_DATA_C, alpha=0.3)
    ax.plot(r, pr, color=_DATA_C, linewidth=1.5)

    if Dmax is not None:
        ax.axvline(Dmax, color=_FIT_C, linestyle="--", linewidth=1.6,
                   label=f"Dmax = {Dmax:.1f} nm")

    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_xlabel("r  (nm)")
    ax.set_ylabel("p(r)")
    ax.set_title(title)
    ax.grid(True, **_GRID_KW)
    return _fig_to_b64(fig, export_path=export_path)


@_serialized
def plot_multi(
    datasets: list[dict],
    title:    str = "Scattering Curves",
    loglog:   bool = True,
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    Overlay multiple 1D curves on one plot.

    ``datasets`` is a list of dicts, each with:
        q     : array-like
        I     : array-like
        label : str  (optional)
        sigma : array-like  (optional)
    """
    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H + 0.5), dpi=_DPI)

    for i, ds in enumerate(datasets):
        q_   = np.asarray(ds["q"],  dtype=float)
        I_   = np.asarray(ds["I"],  dtype=float)
        mask = (q_ > 0) & (I_ > 0)
        lbl  = ds.get("label", f"Curve {i+1}")
        col  = _PALETTE[i % len(_PALETTE)]

        if ds.get("sigma") is not None:
            sig = np.asarray(ds["sigma"], dtype=float)
            ax.fill_between(q_[mask], (I_ - sig)[mask], (I_ + sig)[mask],
                            color=col, **_ERR_KW)
        ax.plot(q_[mask], I_[mask], color=col, linewidth=1.6, label=lbl,
                linestyle=_LINESTYLES[(i // len(_PALETTE)) % len(_LINESTYLES)])

    if loglog:
        ax.set_xscale("log")
        ax.set_yscale("log")

    ax.set_xlabel("q  (nm⁻¹)")
    ax.set_ylabel("I(q)  (a.u.)")
    ax.set_title(title)
    ax.grid(True, which="both", **_GRID_KW)
    return _fig_to_b64(fig, export_path=export_path)


# ── Internal ───────────────────────────────────────────────────────────────────

@_serialized
def plot_fit_residuals(
    q_data: "np.ndarray",
    I_data: "np.ndarray",
    q_fit:  "np.ndarray",
    I_fit:  "np.ndarray",
    sigma:  "np.ndarray | None" = None,
    model:  str = "",
    chi2:   float | None = None,
    axis:   str = "loglog",
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    Two-panel model-fit figure: data + fit curve (top) and normalized residuals
    (bottom). Residuals use the fit interpolated onto the data q in log space;
    normalized by sigma when available, else by I_data.
    """
    q_data = np.asarray(q_data, float); I_data = np.asarray(I_data, float)
    q_fit  = np.asarray(q_fit, float);  I_fit  = np.asarray(I_fit, float)
    logx = axis == "loglog"
    logy = axis in ("loglog", "semilog")

    # Interpolate fit onto data q (log–log interpolation for scattering data).
    good = (q_data > 0) & (I_data > 0)
    mfit = (q_fit > 0) & (I_fit > 0)
    I_model = np.full_like(I_data, np.nan)
    if mfit.sum() >= 2:
        I_model[good] = np.exp(np.interp(np.log(q_data[good]),
                                         np.log(q_fit[mfit]), np.log(I_fit[mfit])))
    if sigma is not None:
        sig = np.asarray(sigma, float)
        resid = (I_data - I_model) / np.where(sig > 0, sig, np.nan)
        rlabel = "(data − fit) / σ"
    else:
        resid = (I_data - I_model) / np.where(I_data != 0, I_data, np.nan)
        rlabel = "(data − fit) / I"

    fig, (ax, axr) = plt.subplots(
        2, 1, figsize=(_FIG_W, _FIG_H + 1.2), dpi=_DPI,
        sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax.plot(q_data[good], I_data[good], "o", ms=3, color=_DATA_C, label="data")
    ax.plot(q_fit[mfit], I_fit[mfit], "-", lw=1.6, color=_FIT_C, label="fit")
    if logx: ax.set_xscale("log")
    if logy: ax.set_yscale("log")
    title = f"Model fit: {model}" + (f"   (χ²ᵣ = {chi2:g})" if chi2 is not None else "")
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_ylabel("I(q)  (a.u.)")
    ax.grid(True, which="both", **_GRID_KW)

    axr.axhline(0, color="#888", lw=0.8)
    axr.plot(q_data[good], resid[good], "o", ms=3, color=_DATA_C)
    if logx: axr.set_xscale("log")
    axr.set_xlabel("q  (nm⁻¹)")
    axr.set_ylabel(rlabel, fontsize=9)
    axr.grid(True, which="both", **_GRID_KW)
    return _fig_to_b64(fig, export_path=export_path)


def overlay_plotly(groups: dict, axis: str = "loglog", title: str = "Overlay") -> dict:
    """
    Build an INTERACTIVE Plotly figure dict (data+layout) for a curve overlay —
    one subplot column per detector. Mirrors plot_overlay's data; the frontend
    renders it with Plotly.js, falling back to the static PNG if unavailable.
    """
    dets = [d for d in ("saxs", "waxs") if groups.get(d)] or list(groups.keys())
    if not dets:
        dets = ["saxs"]
    logx = axis == "loglog"
    logy = axis in ("loglog", "semilog")
    n = len(dets)
    pad = 0.07

    # Reserve a row per curve below the plot; wrap without losing filename text.
    legend_lines = sum(
        max(1, len(textwrap.wrap(str(ds.get("label", "")), width=48)))
        for det in dets for ds in groups.get(det, [])
    )
    legend_height = 24 + 20 * legend_lines
    data = []
    layout = {
        "title": {"text": escape(title), "x": 0.5, "xanchor": "center", "font": {"size": 18}},
        "template": "plotly_white", "height": 500 + legend_height,
        "font": {"family": "DejaVu Sans, Arial, sans-serif", "size": 14, "color": "#222222"},
        "paper_bgcolor": "white", "plot_bgcolor": "white",
        "colorway": list(_PALETTE),
        "margin": {"t": 72, "l": 80, "r": 28, "b": 90 + legend_height},
        "hovermode": "closest", "legend": {"font": {"size": 12}, "x": 0, "y": -0.24,
                                             "xanchor": "left", "yanchor": "top"},
        "annotations": [],
    }
    for ci, det in enumerate(dets):
        xsuf = "" if ci == 0 else str(ci + 1)
        xkey, ykey = f"xaxis{xsuf}", f"yaxis{xsuf}"
        xref, yref = f"x{xsuf}", f"y{xsuf}"
        x0 = ci / n + (pad if ci > 0 else 0.0)
        x1 = (ci + 1) / n - 0.03
        layout[xkey] = {"domain": [x0, x1], "title": {"text": "q (nm⁻¹)"},
                        "type": "log" if logx else "linear", "anchor": yref}
        layout[ykey] = {"title": {"text": "I(q) (a.u.)" if ci == 0 else ""},
                        "type": "log" if logy else "linear", "anchor": xref}
        layout["annotations"].append({
            "text": det.upper(), "x": (x0 + x1) / 2, "y": 1.04, "xref": "paper",
            "yref": "paper", "showarrow": False, "font": {"size": 14, "color": "#222222"}})
        for i, ds in enumerate(groups.get(det, [])):
            qs, Is = ds.get("q", []), ds.get("I", [])
            xs, ys = [], []
            for qi, Ii in zip(qs, Is):
                if qi is None or Ii is None:
                    continue
                if logx and not (qi > 0):
                    continue
                if logy and not (Ii > 0):
                    continue
                xs.append(qi); ys.append(Ii)
            data.append({
                "type": "scatter", "mode": "lines",
                "line": {"color": _PALETTE[i % len(_PALETTE)], "width": 2,
                         "dash": ("solid", "dash", "dashdot", "dot")[(i // len(_PALETTE)) % 4]}, "name": "<br>".join(escape(line) for line in
                    textwrap.wrap(str(ds.get("label", "")), width=48)),
                "x": xs, "y": ys, "xaxis": xref, "yaxis": yref,
                "legendgroup": det, "legendgrouptitle": {"text": det.upper()},
                "hovertemplate": "q=%{x:.4g}<br>I=%{y:.4g}<extra>"
                                 + escape(str(ds.get("label", ""))) + "</extra>",
            })
    for key, value in layout.items():
        if key.startswith(("xaxis", "yaxis")):
            value.update({"showline": True, "mirror": True, "linecolor": "#444444",
                          "linewidth": 1, "ticks": "outside", "ticklen": 5,
                          "tickfont": {"size": 12}, "showgrid": True,
                          "gridcolor": "#E5E8EB", "zeroline": False,
                          "automargin": True})
            value["title"]["font"] = {"size": 16}
            if value["type"] == "log":
                value["dtick"] = 1  # Label decades instead of dense minor digits.
    return {"data": data, "layout": layout}


@_serialized
def plot_overlay(
    groups: dict,
    axis:   str = "loglog",
    title:  str = "Overlay",
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    Overlay multiple 1D curves, one panel per detector (SAXS/WAXS differ in q).

    ``groups`` maps detector -> list of {q, I, sigma?, label}.
    ``axis`` is 'loglog' | 'semilog' (log y, linear x) | 'linear'.
    """
    dets = [d for d in ("saxs", "waxs") if groups.get(d)]
    if not dets:
        dets = list(groups.keys()) or ["saxs"]
    logx = axis == "loglog"
    logy = axis in ("loglog", "semilog")

    fig, axes = plt.subplots(1, len(dets),
                             figsize=(_FIG_W * len(dets), _FIG_H), dpi=_DPI,
                             squeeze=False)
    for col, det in enumerate(dets):
        ax = axes[0][col]
        ds_list = groups.get(det, [])
        for i, ds in enumerate(ds_list):
            q_ = np.asarray(ds["q"], dtype=float)
            I_ = np.asarray(ds["I"], dtype=float)
            mask = np.isfinite(q_) & np.isfinite(I_)
            if logx:
                mask &= q_ > 0
            if logy:
                mask &= I_ > 0
            col_c = _PALETTE[i % len(_PALETTE)]
            if ds.get("sigma") is not None:
                sig = np.asarray(ds["sigma"], dtype=float)
                ax.fill_between(q_[mask], (I_ - sig)[mask], (I_ + sig)[mask],
                                color=col_c, **_ERR_KW)
            ax.plot(q_[mask], I_[mask], color=col_c, lw=1.6,
                    linestyle=_LINESTYLES[(i // len(_PALETTE)) % len(_LINESTYLES)],
                    label="\n".join(textwrap.wrap(
                        str(ds.get("label", f"Curve {i+1}")), width=48)))
        if logx:
            ax.set_xscale("log")
        if logy:
            ax.set_yscale("log")
        ax.set_xlabel("q  (nm⁻¹)")
        if col == 0:
            ax.set_ylabel("I(q)  (a.u.)")
        ax.set_title(det.upper(), fontsize=11, fontweight="bold")
        ax.grid(True, which="both", **_GRID_KW)
    fig.suptitle(title, fontsize=12, fontweight="bold")
    return _fig_to_b64(fig, export_path=export_path)


_METRIC_LABELS = {
    "i0":                   "I₀ (incident)",
    "bstop":                "bstop (transmitted)",
    "transmission":         "Transmission",
    "thickness_m":          "Thickness (m)",
    "normalization_factor": "Norm. factor",
    "ctemp":                "CTEMP (°C)",
    "temp":                 "TEMP (°C)",
}


@_serialized
def plot_metric_timeseries(
    series:  list[dict],
    params:  list[str],
    title:   str = "Metadata over time",
    xlabel:  str = "Timer — elapsed (s)",
    *,
    export_path: str | Path | None = None,
    **_ignore,          # tolerate extra kwargs the model may pass (sigma, q_min…)
) -> str:
    """
    Plot per-sample metadata time series. `series` is a list of:
        {"label": str, "detector": "saxs"|"waxs",
         "t": [floats], "values": {param: [floats], ...}}
    One row per parameter, one column per detector present.
    """
    dets = sorted({s["detector"] for s in series}) or ["saxs"]
    nrows, ncols = max(1, len(params)), len(dets)
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(_FIG_W * ncols, _FIG_H * nrows), dpi=_DPI,
                             squeeze=False)
    for col, det in enumerate(dets):
        det_series = [s for s in series if s["detector"] == det]
        for row, param in enumerate(params):
            ax = axes[row][col]
            ax._publication_panel = det.upper()
            for i, s in enumerate(sorted(det_series, key=lambda z: z["label"])):
                ys = s["values"].get(param)
                if not ys:
                    continue
                # Markers ONLY — no connecting lines. Connecting per-frame points
                # across a sample drew misleading straight "bolts" between the
                # start/end clusters; a scatter shows the actual per-frame spread.
                ax.plot(s["t"], ys, marker="o", ms=4, lw=0, linestyle="None",
                        color=_PALETTE[i % len(_PALETTE)], label=s["label"])
            ax.grid(True, **_GRID_KW)
            ax.tick_params(labelsize=11)
            if row == 0:
                ax.set_title(det.upper(), fontsize=13, fontweight="bold")
            if col == 0:
                ax.set_ylabel(_METRIC_LABELS.get(param, param), fontsize=12)
            if row == nrows - 1:
                ax.set_xlabel(xlabel, fontsize=12)
    fig.suptitle(title, fontsize=14, fontweight="bold")
    return _fig_to_b64(fig, export_path=export_path)


def _publication_style(fig: "plt.Figure") -> None:
    """Apply the same typography and legend layout to every static figure."""
    from matplotlib.ticker import NullFormatter

    fig.set_facecolor("white")
    entries = {}
    for ax in fig.axes:
        ax.set_facecolor("white")
        ax.set_axisbelow(True)
        ax.grid(False, which="both")
        ax.grid(True, which="major", color="#D9DEE3", linewidth=0.5, alpha=0.65)
        ax.tick_params(which="both", direction="out", colors="#222222",
                       labelsize=9, width=0.8)
        ax.tick_params(which="major", length=4)
        ax.tick_params(which="minor", length=2)
        for axis in (ax.xaxis, ax.yaxis):
            if axis.get_scale() == "log":
                axis.set_minor_formatter(NullFormatter())
            axis.label.set_size(11)
            axis.label.set_fontfamily("DejaVu Sans")
            axis.label.set_color("#222222")
            for text in axis.get_ticklabels() + [axis.get_offset_text()]:
                text.set_fontfamily("DejaVu Sans")
                text.set_fontsize(9)
        ax.title.set_fontsize(12)
        ax.title.set_fontfamily("DejaVu Sans")
        ax.title.set_fontweight("normal")
        ax.title.set_color("#222222")
        for spine in ax.spines.values():
            spine.set_linewidth(0.8)
            spine.set_color("#444444")
        handles, labels = ax.get_legend_handles_labels()
        for handle, label in zip(handles, labels):
            # Distinguish samples across detector panels without repeating
            # the same sample in each metadata row.
            panel = getattr(ax, "_publication_panel", ax.get_title())
            if panel in ("SAXS", "WAXS"):
                label = f"{panel} · {label}"
            color = str(getattr(handle, "get_color", lambda: "")())
            entries.setdefault((label, color), (handle, label))
        if ax.get_legend() is not None:
            ax.get_legend().remove()
    if fig._suptitle is not None:
        fig._suptitle.set_fontsize(13)
        fig._suptitle.set_fontfamily("DejaVu Sans")
        fig._suptitle.set_fontweight("normal")
    fig.tight_layout(pad=1.2, h_pad=1.5, w_pad=1.8)
    if entries:
        handles, labels = zip(*entries.values())
        # Legend on the RIGHT (outside the axes), not below. Wrap labels shorter
        # since a side column is narrower; savefig(bbox_inches="tight") expands the
        # canvas to include it so nothing is clipped.
        labels = ["\n".join(textwrap.wrap(label.replace("\n", ""), width=34))
                  for label in labels]
        fig.legend(handles, labels, loc="center left", bbox_to_anchor=(1.005, 0.5),
                   prop={"family": "DejaVu Sans", "size": 9}, frameon=False,
                   handlelength=2.2, handletextpad=0.7, labelspacing=0.6,
                   borderaxespad=0)


def _fig_to_b64(fig: "plt.Figure", export_path=None) -> str:
    """Return a 300-dpi PNG; optionally save PNG, editable SVG, or vector PDF."""
    try:
        _publication_style(fig)
        buf = io.BytesIO()
        options = dict(bbox_inches="tight", pad_inches=0.12, dpi=_DPI,
                       facecolor="white", transparent=False)
        # Scope export font settings instead of changing the application's style.
        with matplotlib.rc_context({"pdf.fonttype": 42, "ps.fonttype": 42,
                                    "svg.fonttype": "none"}):
            fig.savefig(buf, format="png", **options)
            if export_path is not None:
                path = Path(export_path)
                fmt = path.suffix.lower().lstrip(".")
                if fmt not in {"png", "svg", "pdf"}:
                    raise ValueError("Publication export must use .png, .svg, or .pdf")
                fig.savefig(path, format=fmt, **options)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    finally:
        plt.close(fig)
