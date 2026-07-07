"""
src/ai/plots.py — AI-Triggered Plot Generation
================================================
Generates matplotlib figures as base64-encoded PNG strings for inline
display in the AI assistant chat panel.

Every function returns a plain ``str`` (base64 PNG) or raises on failure.
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
import io
import logging
from typing import Any

import numpy as np

logger = logging.getLogger("swaxs_platform")

# Use non-interactive Agg backend (no display required)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── Style constants ────────────────────────────────────────────────────────────
_FIG_W    = 6.0    # inches
_FIG_H    = 4.0    # inches
_DPI      = 130    # render sharper / larger for easier on-screen reading
_DATA_C   = "#1565C0"
_FIT_C    = "#E53935"
_RANGE_C  = "#FFF9C4"
_GRID_KW  = {"alpha": 0.3, "linewidth": 0.6}
_ERR_KW   = {"alpha": 0.25, "linewidth": 0}


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

def plot_curve(
    q:       "np.ndarray",
    I:       "np.ndarray",
    sigma:   "np.ndarray | None" = None,
    label:   str = "I(q)",
    title:   str = "Scattering Curve",
    loglog:  bool = True,
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
    ax.plot(q[mask], I[mask], color=_DATA_C, linewidth=1.2, label=label)

    if loglog:
        ax.set_xscale("log")
        ax.set_yscale("log")

    ax.set_xlabel("q  (nm⁻¹)")
    ax.set_ylabel("I(q)  (a.u.)")
    ax.set_title(title)
    ax.grid(True, which="both", **_GRID_KW)
    ax.legend(fontsize=9)
    plt.tight_layout()
    return _fig_to_b64(fig)


def plot_guinier(
    q:       "np.ndarray",
    I:       "np.ndarray",
    sigma:   "np.ndarray | None" = None,
    q_min:   float | None = None,
    q_max:   float | None = None,
    Rg:      float | None = None,
    I0:      float | None = None,
    title:   str = "Guinier Analysis",
) -> str:
    """
    Guinier plot: ln I vs q².  Fit range highlighted; best-fit line overlaid.
    """
    q, I = np.asarray(q, dtype=float), np.asarray(I, dtype=float)
    mask = (q > 0) & (I > 0)
    q2   = q[mask] ** 2
    lnI  = np.log(I[mask])

    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H), dpi=_DPI)

    if sigma is not None:
        sig     = np.asarray(sigma, dtype=float)
        lnI_err = sig[mask] / I[mask]
        ax.fill_between(q2, lnI - lnI_err, lnI + lnI_err,
                        color=_DATA_C, **_ERR_KW)

    ax.plot(q2, lnI, ".", color=_DATA_C, markersize=3, label="ln I(q)")

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
        ax.axvline(qRg_lo ** 2, color="#4CAF50", linestyle="--",
                   linewidth=0.9, label=f"qRg=0.3  (q={qRg_lo:.4f})")
        ax.axvline(qRg_hi ** 2, color="#F44336", linestyle="--",
                   linewidth=0.9, label=f"qRg=1.3  (q={qRg_hi:.4f})")

    ax.set_xlabel("q²  (nm⁻²)")
    ax.set_ylabel("ln I(q)")
    ax.set_title(title)
    ax.grid(True, **_GRID_KW)
    ax.legend(fontsize=8)
    plt.tight_layout()
    return _fig_to_b64(fig)


def plot_kratky(
    q:       "np.ndarray",
    I:       "np.ndarray",
    sigma:   "np.ndarray | None" = None,
    title:   str = "Kratky Plot",
    Rg:      float | None = None,
    I0:      float | None = None,
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
    ax.plot(q[mask], y, color=_DATA_C, linewidth=1.2)
    ax.set_xlabel("q  (nm⁻¹)")
    ax.set_ylabel("q²·I(q)")
    ax.set_title("Kratky Plot")
    ax.grid(True, **_GRID_KW)

    # Dimensionless Kratky (if Rg and I0 available)
    if Rg and I0:
        ax2   = axes[1]
        qRg   = q[mask] * Rg
        ydk   = (qRg) ** 2 * I[mask] / I0
        ax2.plot(qRg, ydk, color=_DATA_C, linewidth=1.2,
                 label="Dimensionless Kratky")
        # Ideal globule marker at (√3, 3/e) ≈ (1.732, 1.103)
        ax2.plot(np.sqrt(3), 3 / np.e, "r*", markersize=12,
                 label=f"Ideal globule (√3, 3/e)")
        ax2.set_xlabel("qRg")
        ax2.set_ylabel("(qRg)²·I/I₀")
        ax2.set_title("Dimensionless Kratky")
        ax2.legend(fontsize=8)
        ax2.grid(True, **_GRID_KW)

    fig.suptitle(title)
    plt.tight_layout()
    return _fig_to_b64(fig)


def plot_porod(
    q:     "np.ndarray",
    I:     "np.ndarray",
    title: str = "Porod Analysis",
) -> str:
    """
    Porod plot: q⁴I vs q⁴.
    A flat plateau at high q confirms smooth surface scattering (Porod law).
    """
    q, I = np.asarray(q, dtype=float), np.asarray(I, dtype=float)
    mask = (q > 0) & (I > 0)
    q4   = q[mask] ** 4
    q4I  = q4 * I[mask]

    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H), dpi=_DPI)
    ax.plot(q4, q4I, color=_DATA_C, linewidth=1.2)
    ax.set_xlabel("q⁴  (nm⁻⁴)")
    ax.set_ylabel("q⁴·I(q)")
    ax.set_title(title)
    ax.grid(True, **_GRID_KW)
    plt.tight_layout()
    return _fig_to_b64(fig)


def plot_pair_distance(
    r:     "np.ndarray",
    pr:    "np.ndarray",
    Dmax:  float | None = None,
    title: str = "Pair Distance Distribution  p(r)",
) -> str:
    """
    p(r) pair distance distribution.  Dmax is annotated if provided.
    """
    r, pr = np.asarray(r, dtype=float), np.asarray(pr, dtype=float)

    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H), dpi=_DPI)
    ax.fill_between(r, 0, pr, color=_DATA_C, alpha=0.3)
    ax.plot(r, pr, color=_DATA_C, linewidth=1.5)

    if Dmax is not None:
        ax.axvline(Dmax, color=_FIT_C, linestyle="--", linewidth=1.2,
                   label=f"Dmax = {Dmax:.1f} nm")
        ax.legend(fontsize=9)

    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_xlabel("r  (nm)")
    ax.set_ylabel("p(r)")
    ax.set_title(title)
    ax.grid(True, **_GRID_KW)
    plt.tight_layout()
    return _fig_to_b64(fig)


def plot_multi(
    datasets: list[dict],
    title:    str = "Scattering Curves",
    loglog:   bool = True,
) -> str:
    """
    Overlay multiple 1D curves on one plot.

    ``datasets`` is a list of dicts, each with:
        q     : array-like
        I     : array-like
        label : str  (optional)
        sigma : array-like  (optional)
    """
    cmap   = plt.cm.get_cmap("tab10")
    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H + 0.5), dpi=_DPI)

    for i, ds in enumerate(datasets):
        q_   = np.asarray(ds["q"],  dtype=float)
        I_   = np.asarray(ds["I"],  dtype=float)
        mask = (q_ > 0) & (I_ > 0)
        lbl  = ds.get("label", f"Curve {i+1}")
        col  = cmap(i % 10)

        if "sigma" in ds:
            sig = np.asarray(ds["sigma"], dtype=float)
            ax.fill_between(q_[mask], (I_ - sig)[mask], (I_ + sig)[mask],
                            color=col, **_ERR_KW)
        ax.plot(q_[mask], I_[mask], color=col, linewidth=1.2, label=lbl)

    if loglog:
        ax.set_xscale("log")
        ax.set_yscale("log")

    ax.set_xlabel("q  (nm⁻¹)")
    ax.set_ylabel("I(q)  (a.u.)")
    ax.set_title(title)
    ax.legend(fontsize=8, ncol=min(2, len(datasets)))
    ax.grid(True, which="both", **_GRID_KW)
    plt.tight_layout()
    return _fig_to_b64(fig)


# ── Internal ───────────────────────────────────────────────────────────────────

def plot_fit_residuals(
    q_data: "np.ndarray",
    I_data: "np.ndarray",
    q_fit:  "np.ndarray",
    I_fit:  "np.ndarray",
    sigma:  "np.ndarray | None" = None,
    model:  str = "",
    chi2:   float | None = None,
    axis:   str = "loglog",
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
    ax.legend(fontsize=9)

    axr.axhline(0, color="#888", lw=0.8)
    axr.plot(q_data[good], resid[good], "o", ms=3, color=_DATA_C)
    if logx: axr.set_xscale("log")
    axr.set_xlabel("q  (nm⁻¹)")
    axr.set_ylabel(rlabel, fontsize=9)
    axr.grid(True, which="both", **_GRID_KW)
    plt.tight_layout()
    return _fig_to_b64(fig)


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

    data = []
    layout = {
        "title": {"text": title, "x": 0.5, "xanchor": "center"},
        "template": "plotly_white", "height": 430,
        "margin": {"t": 56, "l": 62, "r": 16, "b": 52},
        "hovermode": "closest", "legend": {"font": {"size": 10}},
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
            "yref": "paper", "showarrow": False, "font": {"size": 12, "color": "#8C1515"}})
        for ds in groups.get(det, []):
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
                "type": "scattergl", "mode": "lines", "name": ds.get("label", ""),
                "x": xs, "y": ys, "xaxis": xref, "yaxis": yref,
                "legendgroup": det,
                "hovertemplate": "q=%{x:.4g}<br>I=%{y:.4g}<extra>"
                                 + str(ds.get("label", "")) + "</extra>",
            })
    return {"data": data, "layout": layout}


def plot_overlay(
    groups: dict,
    axis:   str = "loglog",
    title:  str = "Overlay",
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
                             figsize=(6.2 * len(dets), 4.3), dpi=_DPI,
                             squeeze=False)
    cmap = plt.cm.get_cmap("tab10")
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
            col_c = cmap(i % 10)
            if ds.get("sigma") is not None:
                sig = np.asarray(ds["sigma"], dtype=float)
                ax.fill_between(q_[mask], (I_ - sig)[mask], (I_ + sig)[mask],
                                color=col_c, **_ERR_KW)
            ax.plot(q_[mask], I_[mask], color=col_c, lw=1.3,
                    label=ds.get("label", f"Curve {i+1}"))
        if logx:
            ax.set_xscale("log")
        if logy:
            ax.set_yscale("log")
        ax.set_xlabel("q  (nm⁻¹)")
        if col == 0:
            ax.set_ylabel("I(q)  (a.u.)")
        ax.set_title(det.upper(), fontsize=11, fontweight="bold")
        ax.grid(True, which="both", **_GRID_KW)
        if ds_list:
            ax.legend(fontsize=7, ncol=1)
    fig.suptitle(title, fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return _fig_to_b64(fig)


_METRIC_LABELS = {
    "i0":                   "I₀ (incident)",
    "bstop":                "bstop (transmitted)",
    "transmission":         "Transmission",
    "thickness_m":          "Thickness (m)",
    "normalization_factor": "Norm. factor",
    "ctemp":                "CTEMP (°C)",
    "temp":                 "TEMP (°C)",
}


def plot_metric_timeseries(
    series:  list[dict],
    params:  list[str],
    title:   str = "Metadata over time",
    xlabel:  str = "Timer — elapsed (s)",
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
                             figsize=(7.8 * ncols, 3.7 * nrows),
                             squeeze=False)
    cmap = plt.get_cmap("tab10")
    for col, det in enumerate(dets):
        det_series = [s for s in series if s["detector"] == det]
        for row, param in enumerate(params):
            ax = axes[row][col]
            for i, s in enumerate(sorted(det_series, key=lambda z: z["label"])):
                ys = s["values"].get(param)
                if not ys:
                    continue
                ax.plot(s["t"], ys, marker="o", ms=4, lw=1.6,
                        color=cmap(i % 10), label=s["label"])
            ax.grid(True, **_GRID_KW)
            ax.tick_params(labelsize=11)
            if row == 0:
                ax.set_title(det.upper(), fontsize=13, fontweight="bold")
            if col == 0:
                ax.set_ylabel(_METRIC_LABELS.get(param, param), fontsize=12)
            if row == nrows - 1:
                ax.set_xlabel(xlabel, fontsize=12)
    # one shared legend on the right of the top-right panel
    axes[0][ncols - 1].legend(fontsize=9, loc="center left",
                              bbox_to_anchor=(1.01, 0.5), title="Sample")
    fig.suptitle(title, fontsize=14, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return _fig_to_b64(fig)


def _fig_to_b64(fig: "plt.Figure") -> str:
    """Render a matplotlib figure to a base64-encoded PNG string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=_DPI)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")
