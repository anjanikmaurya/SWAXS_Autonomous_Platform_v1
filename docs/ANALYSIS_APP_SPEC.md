# Analysis App — Redesign Specification

From a 20-question requirements interview (2026-06-20). The analysis app is
reorganised into four analysis **categories**, supports **individual and batch**
runs, saves results to a new **`Analysed/`** folder, registers them in the
manifest, and annotates the source `.dat`.

---

## 1. Categories (left nav)

| Category | Contents |
|---|---|
| **Classical** | Guinier (Rg, I0, MW), Kratky + dimensionless Kratky, Porod (exponent, volume, surface area), p(r)/Dmax (IFT) + Invariant Q + MW |
| **SASView** | Full sasmodels catalog; per-parameter bounds + free/fix, polydispersity, structure factor S(q), model comparison |
| **ATSAS** | AUTORG + DATMW, DATGNOM/GNOM p(r), DATPOROD/DATVC, DAMMIF/DAMMIN (command-line binaries; ATSAS is installed locally) |
| **WAXS peaks** | Auto-detect peaks, fit with Gaussian / Lorentzian / Voigt (user-selectable) on a linear background → position, FWHM, area |

Fit ranges (Guinier q-range, etc.): **auto-detect with manual override**.

---

## 2. Workflow

- **Input stage:** Subtracted curves (`1D/<DET>/Subtracted/`). (Browse to any
  `.dat` allowed as a convenience.)
- **Modes:** Individual (interactive q-range + live replot before saving) and
  Batch (select by **keyword/token match**; same starting params, each file
  fitted independently).
- **Plots:** Interactive Plotly (vendored offline), with data + fit + residuals.

---

## 3. Output & saving

New **`Analysed/`** folder, sibling of `Averaged/` and `Subtracted/`, organised
**by detector then analysis type**:

```
1D/SAXS/Analysed/Guinier/<sample>_guinier.json + .dat + .png
1D/SAXS/Analysed/Model/…
1D/WAXS/Analysed/Peaks/…
```

Saved per analysis:
- **params + uncertainties + reduced χ²** (JSON; CSV for tables)
- **fit curve** sampled over q (`.dat`) for replotting
- **plot image** (PNG: data + fit + residuals)
- **provenance** (source file, method, params, timestamp, user)
- **annotation back into the source subtracted `.dat`** footer (the fit
  parameters are appended to the file that was fitted)

Batch: per-file results **plus** one combined **summary table** (CSV/XLSX).

All analyses are **registered in `manifest.json`** (so the viewer and the AI
assistant can see them).

---

## 4. UI

- **Left nav:** Classical · SASView · ATSAS · WAXS peaks.
- **Top tabs:** Setup · SAXS results · WAXS results · Batch — mirroring the
  viewer and subtraction apps (consistent design system).
- Interactive Plotly result panels with draggable/adjustable fit range.

---

## 5. Robustness (all requested)

- **Graceful missing deps** — clear message + fallback if sasmodels or ATSAS
  binaries are absent (the app must still run).
- **Fit-quality QC flags** — warn on qRg out of range, non-flat residuals, poor χ².
- **Convergence + bounds checks** — detect non-convergence and parameters
  pinned at a bound; suggest fixes.
- **Input validation** — q-range, positivity, sufficient points before fitting.

---

## 6. Build order

1. **Classical** — ✅ COMPLETE.
   - Foundation `src/analysis/io.py`: `Analysed/<DET>/<Type>/` writer (JSON +
     fit `.dat` + PNG + provenance), idempotent source-`.dat` annotation,
     manifest registration, batch CSV/XLSX summary. (tests: 4/4)
   - Core extensions `src/analysis/core.py`: dimensionless Kratky, Porod
     invariant/volume (sphere-validated to 0.2%), Vc/Qr/MW, surface area,
     Guinier QC. (tests: 4/4)
   - Endpoints: `/api/classical`, `/api/classical/batch`, `/api/list_subtracted`.
   - UI: left nav (Classical active; SASView/ATSAS/WAXS-peaks "soon") + top tabs
     (Setup/Result/Batch), interactive Plotly per analysis, QC badges, batch
     table. Plotly vendored offline.
2. **SASView** — ✅ COMPLETE.
   - Core: `sasmodels_fit` extended with parameter **bounds** (L-BFGS-B) +
     convergence/at-bounds flags; `sasmodels_params()` lists a model's
     parameters/defaults/limits. Structure factor via `form@structure`;
     polydispersity via the model's `*_pd*` params.
   - Endpoints: `/api/sasmodels/params`, `/api/sasview`, `/api/sasview/batch`,
     `/api/sasview/compare`. Saves to `Analysed/Model/`, annotates `.dat`,
     registers in manifest.
   - UI: SASView panel under the SAXS nav group — full-catalog model dropdown
     (common + all), S(q) selector, a parameter table (fit/fix + value + bounds,
     polydispersity rows included), fit + residuals Plotly, QC (convergence,
     bounds-hit, χ²), and keyword batch.
3. **Nav reorganised** by detector regime: **SAXS** group (Classical, SASView,
   ATSAS) and **WAXS** group (Peak fitting).
4. **WAXS peaks** — ✅ COMPLETE.
   - Core `peak_fit` rewritten: numpy peak **auto-detection**, fit with
     **Gaussian / Lorentzian / pseudo-Voigt** on a linear background, returns per
     peak: position q₀, FWHM, area, height, **d-spacing (nm & Å)**, η (voigt),
     plus per-peak components and reduced χ². (tests: detection + area formulas)
   - Endpoints: `/api/waxs_peaks`, `/api/waxs_peaks/batch`; `list_subtracted`
     now takes a `stage` so WAXS peaks can read **Averaged** or **Subtracted**.
   - UI: WAXS-group "Peak fitting" panel — shape selector, auto/fixed-N, source
     stage, q-range; plot shows data + total fit + per-peak components; results
     table (q₀/FWHM/d), QC, and keyword batch.
5. **ATSAS** — ✅ COMPLETE.
   - `src/analysis/atsas.py`: detects binaries on PATH; wraps **autorg** (Rg/I0/
     quality), **datgnom** (GNOM p(r), Dmax, real-space Rg/I0), **datporod**
     (Porod volume, chained from datgnom), **datvc** (Vc/MW), **datmw** (MW),
     and **dammif** (ab-initio bead model; runs datgnom first, slow, models →
     Analysed/ATSAS/). Tolerant parsers + raw output preserved; missing binaries
     return a clean error. (tests: parsers + graceful absence)
   - Endpoints: `/api/atsas/available`, `/api/atsas`, `/api/atsas/batch`.
   - UI: ATSAS panel under the SAXS nav group — availability banner (disables
     uninstalled tools), tool selector, Rg hint / MW method, GNOM p(r) plot,
     results table + raw output, keyword batch.

**Redesign COMPLETE** — SAXS group (Classical · SASView · ATSAS) and WAXS group
(Peak fitting) all built, each with individual + keyword-batch, saving to
`Analysed/<DET>/<Type>/` with `.dat` annotation and manifest registration,
interactive Plotly, and QC. Test suites: analysis-io, classical, waxs-peaks,
atsas (+ existing) — all green.

---

## 7. Architecture (per CLAUDE.md: logic in `src/`)

- `src/analysis/core.py` — fit math (extend: dimensionless Kratky, Porod
  volume/SA, invariant Q, MW; Lorentzian/Voigt peaks; QC checks).
- `src/analysis/io.py` (new) — `Analysed/` paths, save bundle (JSON/.dat/PNG),
  `.dat` footer annotation, manifest registration, batch summary.
- `src/analysis/atsas.py` (new) — detect + wrap ATSAS binaries.
- `analysis/app.py` — thin Flask routes (individual + batch + save).
- `analysis/templates/index.html` — left-nav + top-tab UI, Plotly panels.
