"""
src/optimizer/plots.py — figures of the search space and of convergence.

Server-side matplotlib (Agg), same as the reduction/quality plots, so the
analyzer panel and the report figures come from ONE renderer. If the panel and
the paper figure disagree, one of them is a lie.

Three views:
    slice        — posterior mean loss / posterior sd / EI / predicted size,
                   on a 2-D cut, with the observations and the pending proposal
    convergence  — best-so-far loss, size vs the acceptance band, PDI vs cap,
                   and the retrospective max-posterior-sd decay
    trajectory   — parallel coordinates of all five knobs plus the (T, x_TOP)
                   projection, coloured by run index

Colour rules (deliberate, not decorative):
    * loss/EI use a perceptually ordered map (viridis / magma)
    * uncertainty uses its own map (cividis) so it is never confused with loss
    * the target iso-contour and the acceptance band are the one accent colour
    * failed measurements are drawn as hollow red rings, never as a loss value —
      a 1e3 sentinel would otherwise flatten every colour scale in the figure
"""

from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")                       # noqa: E402  (must precede pyplot)
import matplotlib.pyplot as plt             # noqa: E402
import numpy as np                          # noqa: E402

from . import diagnostics as dg             # noqa: E402
from .campaign import _FAIL_LOSS            # noqa: E402
from src.plot_lock import MPL_LOCK          # noqa: E402  (process-wide pyplot lock)

ACCENT = "#B1040E"          # SLAC cardinal — matches the apps
_FIGKW = dict(dpi=130, facecolor="white")
UNITS = {"T_reac": "°C", "F_tot": "µL/min", "x_ODE": "", "x_TOP": "", "x_oley": ""}


def _label(name: str) -> str:
    u = UNITS.get(name, "")
    return f"{name} ({u})" if u else name


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _empty(msg: str) -> bytes:
    fig, ax = plt.subplots(figsize=(7.2, 2.2), **_FIGKW)
    ax.axis("off")
    ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=11, color="#5c6470")
    return _png(fig)


# ── view 1: the slice ────────────────────────────────────────────────────────
def slice_figure(campaign, xname=None, yname=None, *, n=60,
                 anchor_mode="best", truth=None) -> bytes:
    """Four panels on one 2-D cut through the 5-D recipe space.

    ``truth`` (optional dict with T_opt / x_TOP_opt) marks the simulator's hidden
    optimum. It is available ONLY in mock — with real beam nobody knows it, and
    passing it in would be self-deception.
    """
    xname = xname or dg.DEFAULT_SLICE[0]
    yname = yname or dg.DEFAULT_SLICE[1]
    s = dg.slice_surfaces(campaign, xname, yname, n=n, anchor_mode=anchor_mode)
    if s is None:
        return _empty("No results yet — the surrogate needs at least one measured recipe.")

    xs, ys = np.array(s["x"]), np.array(s["y"])
    ext = [xs[0], xs[-1], ys[0], ys[-1]]
    fig, axes = plt.subplots(2, 2, figsize=(11.2, 8.2), **_FIGKW)

    obs = s["samples"]
    ok = [p for p in obs if not p["failed"]]
    bad = [p for p in obs if p["failed"]]

    def _overlay(ax, *, points=True):
        if points and ok:
            ax.scatter([p["x"] for p in ok], [p["y"] for p in ok],
                       s=[16 + 42 * p["confidence"] for p in ok],
                       facecolors="none", edgecolors="white", linewidths=1.3, zorder=4)
            # run index as a faint path: shows the order the loop moved in
            ax.plot([p["x"] for p in ok], [p["y"] for p in ok], color="white",
                    lw=0.6, alpha=0.35, zorder=3)
        if points and bad:
            ax.scatter([p["x"] for p in bad], [p["y"] for p in bad], s=44,
                       facecolors="none", edgecolors="#ff6b6b", linewidths=1.6,
                       marker="o", zorder=5, label="unsized")
        if s["proposal"]:
            ax.scatter([s["proposal"]["x"]], [s["proposal"]["y"]], s=190, marker="*",
                       color=ACCENT, edgecolors="white", linewidths=1.0, zorder=6,
                       label="next measurement")
        elif campaign.best is not None:
            bp = campaign.best["params"]
            ax.scatter([bp[xname]], [bp[yname]], s=190, marker="*", color=ACCENT,
                       edgecolors="white", linewidths=1.0, zorder=6,
                       label="accepted recipe")
        if truth:
            ax.scatter([truth.get("T_opt")] if xname == "T_reac" else [truth.get(xname + "_opt")],
                       [truth.get("x_TOP_opt")] if yname == "x_TOP" else [truth.get(yname + "_opt")],
                       s=120, marker="P", color="#4ade80", edgecolors="black",
                       linewidths=0.6, zorder=6, label="true optimum")
        ax.set_xlabel(_label(xname)); ax.set_ylabel(_label(yname))

    # (a) posterior mean loss — clipped at the failure sentinel so one unsized
    #     profile cannot wash out the whole map
    ax = axes[0, 0]
    Z = np.array(s["loss_mean"], float)
    finite = Z[np.isfinite(Z)]
    vmax = float(np.percentile(finite, 97)) if finite.size else 1.0
    im = ax.imshow(np.clip(Z, 0.0, vmax), origin="lower", extent=ext, aspect="auto",
                   cmap="viridis")
    # clipped at 0 because a zero-mean GP fitted to centred loss can predict a
    # negative loss, which is physically impossible — the clip is display-only
    _sc = s.get("loss_scale", "none")
    _ltxt = "log1p(loss)" if _sc == "log1p" else "loss"
    fig.colorbar(im, ax=ax, label=f"posterior mean {_ltxt} (clipped ≥0)")
    _overlay(ax)
    ax.set_title("(a) where the loop thinks the good recipes are", fontsize=10.5)

    # (b) posterior sd — the justification for the next measurement
    ax = axes[0, 1]
    im = ax.imshow(np.array(s["loss_sd"], float), origin="lower", extent=ext,
                   aspect="auto", cmap="cividis")
    fig.colorbar(im, ax=ax, label=f"posterior sd of {_ltxt}")
    _overlay(ax)
    ax.set_title("(b) where it is still ignorant", fontsize=10.5)

    # (c) acquisition
    ax = axes[1, 0]
    im = ax.imshow(np.array(s["ei"], float), origin="lower", extent=ext,
                   aspect="auto", cmap="magma")
    fig.colorbar(im, ax=ax, label="expected improvement")
    _overlay(ax)
    ax.set_title("(c) acquisition — the trade-off it resolves", fontsize=10.5)
    ax.legend(loc="upper right", fontsize=7.5, framealpha=0.85)

    # (d) predicted size with the target iso-contour
    ax = axes[1, 1]
    if s["size_mean"] is None:
        ax.axis("off")
        ax.text(0.5, 0.5, "predicted size needs ≥3 sized profiles",
                ha="center", va="center", fontsize=10, color="#5c6470")
    else:
        SZ = np.array(s["size_mean"], float)
        im = ax.imshow(SZ, origin="lower", extent=ext, aspect="auto", cmap="RdYlBu_r")
        fig.colorbar(im, ax=ax, label="predicted radius (nm)")
        tgt, tol = s["target_size"], s["tolerance"]
        with np.errstate(invalid="ignore"):
            cs = ax.contour(xs, ys, SZ, levels=[tgt - tol, tgt, tgt + tol],
                            colors=[ACCENT, ACCENT, ACCENT],
                            linewidths=[0.9, 2.2, 0.9], linestyles=[":", "-", ":"])
        ax.clabel(cs, fmt=lambda v: f"{v:.2f} nm", fontsize=7.5)
        _overlay(ax)
    ax.set_title(f"(d) predicted radius — solid line = target {s['target_size']:.2f} nm",
                 fontsize=10.5)

    fixed = ", ".join(f"{k}={v:.3g}" for k, v in s["fixed"].items())
    fig.suptitle(f"Recipe-space surrogate after {s['n_observations']} measured runs "
                 f"— slice at {fixed} ({s['anchor_mode']})", fontsize=12, y=0.985)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return _png(fig)


# ── view 2: convergence ──────────────────────────────────────────────────────
def convergence_figure(campaign, *, truth=None) -> bytes:
    c = dg.convergence(campaign)
    if not c["runs"]:
        return _empty("No results yet — nothing to converge.")
    r = np.array(c["runs"], float)
    fig, axes = plt.subplots(2, 2, figsize=(11.2, 7.0), **_FIGKW)

    def _mark_handoff(ax):
        """The Sobol → BO handoff. Everything before it is non-adaptive; judging
        the optimizer on those runs is judging a random design."""
        if c["n_init"] and c["n_init"] < len(r):
            ax.axvline(c["n_init"] + 0.5, color="#888780", ls="--", lw=1.0)
            ax.text(c["n_init"] + 0.7, ax.get_ylim()[1], " BO takes over",
                    fontsize=7.5, va="top", color="#5f5e5a")

    # (a) loss and best-so-far
    ax = axes[0, 0]
    loss = np.array([v if v < _FAIL_LOSS else np.nan for v in c["loss"]], float)
    ax.semilogy(r, loss, "o", ms=4, color="#2a78d6", alpha=0.65, label="run loss")
    ax.semilogy(r, c["best_loss"], "-", lw=2.0, color=ACCENT, label="best so far")
    fail = r[np.array(c["failed"], bool)]
    if fail.size:
        ax.semilogy(fail, np.full(fail.shape, np.nanmax(loss) if np.isfinite(loss).any() else 1),
                    "x", ms=7, color="#e34948", label="unsized (failed)")
    ax.set_ylabel("loss (log)"); ax.set_xlabel("run")
    ax.legend(fontsize=8); ax.grid(alpha=0.25); _mark_handoff(ax)
    ax.set_title("(a) objective — did it get better?", fontsize=10.5)

    # (b) size against the acceptance band
    ax = axes[0, 1]
    tgt, tol = c["target_size"], c["tolerance"]
    ax.axhspan(tgt - tol, tgt + tol, color=ACCENT, alpha=0.13,
               label=f"accept {tgt:g} ± {tol:g} nm")
    ax.axhline(tgt, color=ACCENT, lw=1.4)
    sz = np.array([np.nan if v is None else v for v in c["size"]], float)
    conf = np.array(c["confidence"], float)
    sc = ax.scatter(r, sz, c=conf, cmap="cividis", vmin=0, vmax=1, s=34,
                    edgecolors="black", linewidths=0.3, zorder=3)
    fig.colorbar(sc, ax=ax, label="fit confidence")
    ax.set_ylabel("measured radius (nm)"); ax.set_xlabel("run")
    ax.legend(fontsize=8); ax.grid(alpha=0.25)
    ax.set_title("(b) the physical quantity the stop rule tests", fontsize=10.5)

    # (c) PDI against the cap
    ax = axes[1, 0]
    pdi = np.array([np.nan if v is None else v for v in c["pdi"]], float)
    ax.plot(r, pdi, "o-", ms=4, lw=1.0, color="#199e70")
    ax.axhline(c["pdi_cap"], color=ACCENT, ls="--", lw=1.4,
               label=f"cap {c['pdi_cap']:g}")
    ax.set_ylabel("PDI"); ax.set_xlabel("run")
    ax.legend(fontsize=8); ax.grid(alpha=0.25)
    ax.set_title("(c) polydispersity — the other half of the stop rule", fontsize=10.5)

    # (d) model uncertainty decay + step size
    ax = axes[1, 1]
    # normalised, because the raw sd is measured against a prior that grows as
    # the campaign sees a wider range of losses — see diagnostics.convergence
    sd = np.array(c.get("mean_sd_rel") or [], float)
    sdm = np.array(c.get("max_sd_rel") or [], float)
    if sd.size and np.isfinite(sd).any():
        ax.plot(r, sd, "-", lw=1.9, color="#4a3aa7", label="mean sd (coverage)")
    if sdm.size and np.isfinite(sdm).any():
        ax.plot(r, sdm, ":", lw=1.4, color="#4a3aa7", alpha=0.75,
                label="max sd (worst corner)")
    ax.set_ylim(0, 1.05)
    ax.legend(fontsize=7.5, loc="lower left")
    ax.set_ylabel("posterior sd / prior sd", color="#4a3aa7")
    ax.tick_params(axis="y", labelcolor="#4a3aa7")
    ax.set_xlabel("run"); ax.grid(alpha=0.25)
    if c["step"]:
        ax2 = ax.twinx()
        ax2.plot(r[1:], c["step"], "-", lw=1.2, color="#eb6834", alpha=0.8)
        ax2.set_ylabel("step in unit space", color="#eb6834")
        ax2.tick_params(axis="y", labelcolor="#eb6834")
    ax.set_title("(d) is the loop still learning? (sd) and still roaming? (step)",
                 fontsize=10.5)

    head = (f"Campaign convergence — {len(r):.0f}/{c['budget']} runs, status "
            f"{c['status']}")
    if truth:
        head += (f"   ·   hidden optimum: T={truth.get('T_opt'):g} °C, "
                 f"x_TOP={truth.get('x_TOP_opt'):g} → R={truth.get('R_opt'):g} nm")
    fig.suptitle(head, fontsize=12, y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.955))
    return _png(fig)


# ── view 3: trajectory ───────────────────────────────────────────────────────
def trajectory_figure(campaign, *, truth=None) -> bytes:
    t = dg.trajectory(campaign)
    if not t["rows"]:
        return _empty("No results yet — no trajectory to draw.")
    names = t["names"]
    fig, axes = plt.subplots(1, 3, figsize=(15.2, 4.6),
                             gridspec_kw={"width_ratios": [1.7, 1, 0.9]}, **_FIGKW)
    cmap = plt.get_cmap("viridis")
    n = len(t["rows"])

    # (a) parallel coordinates in the unit cube — all five knobs at once
    ax = axes[0]
    for row in t["rows"]:
        frac = (row["run"] - 1) / max(1, n - 1)
        ax.plot(range(len(names)), row["unit"], "-", lw=1.1,
                color=cmap(frac), alpha=0.35 + 0.5 * frac)
    if t["best"]:
        ub = campaign.space.to_unit(t["best"]["params"])
        ax.plot(range(len(names)), ub, "-o", lw=2.4, ms=5, color=ACCENT,
                label="best so far", zorder=5)
    if t["proposal"]:
        up = campaign.space.to_unit(t["proposal"])
        ax.plot(range(len(names)), up, "--*", lw=1.6, ms=12, color="#eb6834",
                label="next measurement", zorder=6)
    if truth:
        tv = []
        for k in names:
            lo, hi = t["bounds"][k]
            key = {"T_reac": "T_opt", "x_TOP": "x_TOP_opt"}.get(k)
            tv.append((truth[key] - lo) / (hi - lo) if key and key in truth else np.nan)
        ax.plot(range(len(names)), tv, ":P", lw=1.6, ms=9, color="#199e70",
                label="true optimum", zorder=6)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([f"{k}\n[{t['bounds'][k][0]:g}, {t['bounds'][k][1]:g}]"
                        for k in names], fontsize=8.5)
    ax.set_ylim(-0.04, 1.04); ax.set_ylabel("position within bounds (0–1)")
    ax.grid(axis="y", alpha=0.25); ax.legend(fontsize=8, loc="upper right")
    ax.set_title("(a) every recipe tried — bunching would mean it localised",
                 fontsize=10.5)

    # (b) the (T, x_TOP) projection, coloured by run order
    ax = axes[1]
    xv = [row["params"]["T_reac"] for row in t["rows"]]
    yv = [row["params"]["x_TOP"] for row in t["rows"]]
    ax.plot(xv, yv, "-", lw=0.7, color="#888780", alpha=0.6, zorder=2)
    sc = ax.scatter(xv, yv, c=[row["run"] for row in t["rows"]], cmap="viridis",
                    s=42, edgecolors="black", linewidths=0.3, zorder=3)
    fig.colorbar(sc, ax=ax, label="run")
    if t["best"]:
        ax.scatter([t["best"]["params"]["T_reac"]], [t["best"]["params"]["x_TOP"]],
                   s=180, marker="*", color=ACCENT, edgecolors="white", zorder=5)
    if truth:
        ax.scatter([truth["T_opt"]], [truth["x_TOP_opt"]], s=130, marker="P",
                   color="#199e70", edgecolors="black", linewidths=0.6, zorder=5)
    ax.set_xlabel(_label("T_reac")); ax.set_ylabel(_label("x_TOP"))
    ax.grid(alpha=0.25)
    ax.set_title("(b) the two knobs that set the size", fontsize=10.5)

    # (c) the numeric version of "did the lines bunch?" — per-knob spread in the
    #     first vs the last third of the campaign. Eyeballing parallel coordinates
    #     is unreliable; this is the number that settles it.
    ax = axes[2]
    U = np.array([row["unit"] for row in t["rows"]], float)
    k = max(2, len(U) // 3)
    early = U[:k].std(axis=0)
    late = U[-k:].std(axis=0)
    idx = np.arange(len(names))
    ax.barh(idx + 0.19, early, height=0.36, color="#888780", label=f"first {k} runs")
    ax.barh(idx - 0.19, late, height=0.36, color=ACCENT, label=f"last {k} runs")
    ax.set_yticks(idx); ax.set_yticklabels(names, fontsize=9)
    ax.set_xlabel("sd within bounds (0–1)")
    ax.axvline(1 / np.sqrt(12), color="#2a78d6", ls=":", lw=1.2)
    ax.text(1 / np.sqrt(12), len(names) - 0.4, " uniform", fontsize=7.5,
            color="#2a78d6", va="top")
    ax.legend(fontsize=7.5, loc="lower right"); ax.grid(axis="x", alpha=0.25)
    ax.set_title("(c) did the search narrow?", fontsize=10.5)

    fig.suptitle(f"Sampling trajectory over {n} runs "
                 f"(first {t['n_init']} are the Sobol cold start)", fontsize=12, y=1.0)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return _png(fig)


import math                                               # noqa: E402


def _ncdf(z):
    """Standard-normal CDF, vectorised, no scipy dependency."""
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(z, float) / math.sqrt(2.0)))


# ── view 4: level set — the region of recipes predicted in-spec ────────────────
def levelset_figure(campaign, xname=None, yname=None, *, n=60,
                    anchor_mode="best", truth=None) -> bytes:
    """Map P(in-spec) = P(loss ≤ τ) over a 2-D cut: the set of recipes meeting the
    specification (the level-set goal), with the band boundary drawn and the
    measured recipes overlaid."""
    xname = xname or dg.DEFAULT_SLICE[0]
    yname = yname or dg.DEFAULT_SLICE[1]
    s = dg.slice_surfaces(campaign, xname, yname, n=n, anchor_mode=anchor_mode)
    if s is None:
        return _empty("No results yet — the surrogate needs at least one measured recipe.")
    mean = np.array(s["loss_mean"], float)
    sd = np.maximum(np.array(s["loss_sd"], float), 1e-9)
    # τ on the SAME (possibly transformed) scale as loss_mean.
    tau_raw = getattr(campaign, "_tau", 1.0 + getattr(campaign, "weight_pdi", 1.0))
    try:
        tau = float(campaign._transform(np.array([tau_raw]))[0])
    except Exception:
        tau = float(tau_raw)
    P = _ncdf((tau - mean) / sd)                          # probability each cell is in-spec
    xs, ys = np.array(s["x"]), np.array(s["y"])
    ext = [xs[0], xs[-1], ys[0], ys[-1]]
    fig, ax = plt.subplots(figsize=(7.4, 5.6), **_FIGKW)
    im = ax.imshow(P, origin="lower", extent=ext, aspect="auto", cmap="YlGn",
                   vmin=0.0, vmax=1.0)
    fig.colorbar(im, ax=ax, label="P(recipe meets the spec)")
    try:
        cs = ax.contour(xs, ys, P, levels=[0.5], colors=ACCENT, linewidths=1.6)
        ax.clabel(cs, fmt="band edge", fontsize=8)
    except Exception:
        pass
    obs = s.get("samples", [])
    ok = [p for p in obs if not p.get("failed")]
    if ok:
        ax.scatter([p["x"] for p in ok], [p["y"] for p in ok],
                   s=[16 + 42 * p.get("confidence", 0.3) for p in ok],
                   facecolors="none", edgecolors="white", linewidths=1.3, zorder=4,
                   label="measured")
    if campaign.best is not None:
        bp = campaign.best["params"]
        ax.scatter([bp[xname]], [bp[yname]], s=190, marker="*", color=ACCENT,
                   edgecolors="white", linewidths=1.0, zorder=6, label="best so far")
    ax.set_xlabel(_label(xname)); ax.set_ylabel(_label(yname))
    ax.set_title("Level set — recipes predicted to meet the spec", fontsize=11)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.85)
    return _png(fig)


# ── view 5: size–PDI Pareto front ──────────────────────────────────────────────
def pareto_figure(campaign, *, truth=None) -> bytes:
    """PDI vs diameter for every sized recipe, with the non-dominated trade-off
    front (minimising size error AND PDI) highlighted — the Pareto goal."""
    hist = [h for h in campaign.history if h.get("size") is not None]
    if not hist:
        return _empty("No sized results yet — the Pareto front needs measured recipes.")
    tgt = float(getattr(campaign, "target_size", 0.0))
    dia = np.array([2.0 * float(h["size"]) for h in hist])          # diameter (nm)
    pdi = np.array([float(h["pdi"]) if h.get("pdi") is not None else np.nan for h in hist])
    conf = np.array([float(h.get("confidence") or 0.0) for h in hist])
    # Two minimised objectives: distance from target size, and PDI.
    serr = np.abs(np.array([float(h["size"]) for h in hist]) - tgt)
    good = np.isfinite(pdi)
    idx = np.where(good)[0]
    nd = []
    for i in idx:                                                   # non-dominated set
        dominated = any((serr[j] <= serr[i] and pdi[j] <= pdi[i]) and
                        (serr[j] < serr[i] or pdi[j] < pdi[i]) for j in idx if j != i)
        if not dominated:
            nd.append(i)
    nd.sort(key=lambda i: dia[i])
    fig, ax = plt.subplots(figsize=(7.4, 5.0), **_FIGKW)
    sc = ax.scatter(dia[good], pdi[good], c=conf[good], cmap="viridis", vmin=0, vmax=1,
                    s=40, edgecolors="#333", linewidths=0.4, zorder=3)
    fig.colorbar(sc, ax=ax, label="fit confidence")
    if nd:
        ax.plot(dia[nd], pdi[nd], color=ACCENT, lw=1.8, marker="o", ms=6,
                markerfacecolor=ACCENT, markeredgecolor="white", zorder=5,
                label="Pareto front")
    if campaign.pdi_cap:
        ax.axhline(float(campaign.pdi_cap), color="#888", ls="--", lw=1.0,
                   label=f"PDI cap {campaign.pdi_cap:g}")
    ax.axvline(2.0 * tgt, color="#4ade80", ls=":", lw=1.0, label=f"target {2.0*tgt:g} nm")
    ax.set_xlabel("diameter (nm)"); ax.set_ylabel("PDI")
    ax.set_title("Size–PDI trade-off (Pareto front)", fontsize=11)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.85)
    return _png(fig)


FIGURES = {"slice": slice_figure,
           "convergence": convergence_figure,
           "trajectory": trajectory_figure,
           "levelset": levelset_figure,
           "pareto": pareto_figure}


def figure(view: str, campaign, **kw) -> bytes:
    # One process-wide lock: the analyzer renders these in Flask request threads
    # while its watcher thread writes per-fit PNGs; unsynchronised pyplot use
    # corrupts figures or crashes. On any error, close whatever was half-built so
    # a broken plot cannot leak a Figure (memory + file descriptors) over a long
    # run.
    with MPL_LOCK:
        fn = FIGURES.get(str(view))
        if fn is None:
            return _empty(f"unknown view {view!r} — expected one of {sorted(FIGURES)}")
        try:
            return fn(campaign, **kw)
        except Exception as exc:                   # a broken plot must not kill a run
            try:
                plt.close("all")
            except Exception:
                pass
            return _empty(f"could not render {view}: {exc}")


def study_figure(results: dict) -> bytes:
    """Best-loss-so-far vs iteration, one line per method, from a run_study()
    result. Averages across repeats and shades the min/max band. In-silico
    method-comparison figure for the Report tab (docs/AUTOFIT_REDESIGN.md)."""
    with MPL_LOCK:
        try:
            if not results:
                return _empty("run a comparison to see method sample-efficiency")
            fig, ax = plt.subplots(figsize=(7.6, 4.4), **_FIGKW)
            cmap = plt.get_cmap("tab10")
            for i, (mid, r) in enumerate(results.items()):
                curves = [run["best_loss_curve"] for run in r.get("repeats", [])
                          if run.get("best_loss_curve")]
                if not curves:
                    continue
                n = min(len(c) for c in curves)
                if n == 0:
                    continue
                arr = np.array([c[:n] for c in curves], float)
                x = np.arange(1, n + 1)
                mean = arr.mean(axis=0)
                col = cmap(i % 10)
                ax.plot(x, mean, lw=2.0, color=col, label=r.get("label", mid))
                if arr.shape[0] > 1:
                    ax.fill_between(x, arr.min(axis=0), arr.max(axis=0),
                                    color=col, alpha=0.15, lw=0)
            ax.set_yscale("log")
            ax.set_xlabel("evaluation (recipe #)")
            ax.set_ylabel("best loss so far")
            ax.set_title("Method comparison — sample efficiency (in silico)")
            ax.grid(True, which="both", alpha=0.25)
            ax.legend(loc="upper right", fontsize=9, frameon=False)
            fig.tight_layout()
            return _png(fig)
        except Exception as exc:
            try:
                plt.close("all")
            except Exception:
                pass
            return _empty(f"could not render study: {exc}")
