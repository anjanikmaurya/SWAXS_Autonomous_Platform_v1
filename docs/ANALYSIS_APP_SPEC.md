# Analysis App — Reference

What the analysis app does and where it puts things. Originally a redesign
specification from a 20-question requirements interview (2026-06-20); the
redesign is fully built, so this now reads as reference. The app is organised
into four analysis **categories**, supports **individual and batch** runs, saves
results under **`Analysed/`**, registers them in the manifest, and annotates the
source `.dat`.

---

## 1. Categories (left nav)

| Category | Contents |
|---|---|
| **Classical** | Guinier (Rg, I0), Kratky + dimensionless Kratky, Porod (exponent, volume, surface area), p(r)/Dmax (IFT) + Invariant Q + MW |
| **SASView** | Full sasmodels catalog; per-parameter bounds + free/fix, polydispersity, structure factor S(q), model comparison |
| **ATSAS** | AUTORG + DATMW, DATGNOM/GNOM p(r), DATPOROD/DATVC, DAMMIF/DAMMIN — command-line binaries, used **if ATSAS is on `PATH`** (`src/analysis/atsas.py:120` probes `shutil.which`; each wrapper returns a clean error when its binary is absent) |
| **WAXS peaks** | Auto-detect peaks, fit with Gaussian / Lorentzian / Voigt (user-selectable) on a linear background → position, FWHM, area |

Fit ranges (Guinier q-range, etc.): **auto-detect with manual override**.

The Guinier fit itself returns Rg, I0, χ² and a quality verdict — **not** MW.
Molecular weight comes from `classical_invariants` (`src/analysis/core.py:358`),
which uses the fitted Rg and I0 to compute the Porod invariant Q, Vp, Vc, Qr and
two approximate protein MW estimates (`mw_porod_kda`, `mw_vc_kda`), or from
ATSAS `datmw` / `datvc`. All MW numbers assume a globular two-phase particle and
are approximate.

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

An **`Analysed/`** folder, sibling of `Averaged/` and `Subtracted/`, organised
**by detector then analysis type** (`src/analysis/io.py:54-65`):

```
1D/SAXS/Analysed/Guinier/<sample>_guinier.json + .dat + .png
1D/SAXS/Analysed/Model/…
1D/SAXS/Analysed/ATSAS/…
1D/WAXS/Analysed/Peaks/…
```

The detector directory comes first. `analysed_dir_for_source` takes the source
curve's parent (`Subtracted/`, `Averaged/`, `Reduction/`), steps up one level to
the detector directory, and creates `Analysed/<Type>/` there. If the source sits
somewhere else entirely, `Analysed/<Type>/` is created next to the source file.
`<Type>` is one of `Guinier`, `Kratky`, `Porod`, `PairDistance`, `Invariant`,
`Model`, `Peaks`, `ATSAS` (mapped in `_TYPE_DIR`, `src/analysis/io.py:31-44`).

Saved per analysis:
- **params + uncertainties + reduced χ²** (JSON; CSV for tables)
- **fit curve** sampled over q (`.dat`) for replotting
- **plot image** (PNG: data + fit + residuals)
- **provenance** (source file, method, params, timestamp, user)
- **annotation back into the source subtracted `.dat`** footer (the fit
  parameters are appended to the file that was fitted)

Batch: per-file results **plus** one combined **summary table** (CSV/XLSX).

All analyses are **registered in `manifest.json`** (so the average app and the AI
assistant can see them).

---

## 4. UI

- **Left nav:** Classical · SASView · ATSAS · WAXS peaks.
- **Top tabs:** Setup · SAXS results · WAXS results · Batch — mirroring the
  average and subtraction apps (consistent design system).
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

## 6. Endpoints

Every panel is backed by an individual endpoint and, where batch applies, a
`/batch` sibling. All are in `analysis/app.py`.

| Panel | Individual | Batch | Other |
|---|---|---|---|
| Classical | `/api/classical` | `/api/classical/batch` | `/api/guinier`, `/api/kratky`, `/api/porod` (legacy single-fit routes) |
| SASView | `/api/sasview` | `/api/sasview/batch` | `/api/sasmodels/list`, `/api/sasmodels/params`, `/api/sasview/compare`, `/api/model` |
| ATSAS | `/api/atsas` | `/api/atsas/batch` | `/api/atsas/available` (drives the availability banner) |
| WAXS peaks | `/api/waxs_peaks` | `/api/waxs_peaks/batch` | `/api/peak` |
| Shared | — | — | `/api/list_subtracted` (takes a `stage`, so WAXS peaks can read `Averaged` or `Subtracted`), `/api/load_dat`, `/api/browse`, `/api/save`, `/api/project`, `/api/set_project`, `/api/health` |

---

## 7. Build history

The redesign is complete. Order it was built in, and what each step added:

1. **Classical** — `src/analysis/io.py` (the `Analysed/` writer: JSON + fit
   `.dat` + PNG + provenance, idempotent source-`.dat` annotation, manifest
   registration, batch CSV/XLSX summary) and the `src/analysis/core.py`
   extensions: `dimensionless_kratky` (`:337`), `classical_invariants` (`:358`,
   Porod invariant/volume sphere-validated to 0.2%, Vc/Qr/MW, surface area),
   `guinier_quality` (`:428`). Plotly vendored offline.
2. **SASView** — `sasmodels_fit` (`:617`) gained per-parameter bounds (L-BFGS-B)
   plus convergence and at-bounds flags; `sasmodels_params` (`:795`) lists a
   model's parameters/defaults/limits. Structure factor via `form@structure`,
   polydispersity via the model's `*_pd*` params.
3. **Nav reorganised** by detector regime: a **SAXS** group (Classical, SASView,
   ATSAS) and a **WAXS** group (Peak fitting).
4. **WAXS peaks** — `peak_fit` (`:504`) rewritten with numpy peak
   auto-detection and Gaussian / Lorentzian / pseudo-Voigt shapes on a linear
   background; returns per peak q₀, FWHM, area, height, d-spacing (nm and Å),
   η for Voigt, per-peak components and reduced χ².
5. **ATSAS** — `src/analysis/atsas.py` wraps **autorg** (Rg/I0/quality),
   **datgnom** (p(r), Dmax, real-space Rg/I0), **datporod** (Porod volume,
   chained from datgnom), **datvc** (Vc/MW), **datmw** (MW), and **dammif**
   (ab-initio bead model; runs datgnom first, slow, models → `Analysed/ATSAS/`).
   Tolerant parsers, raw output preserved, clean error when a binary is absent.

---

## 8. Architecture (per CLAUDE.md: logic in `src/`)

- `src/analysis/core.py` — fit math: `guinier_fit`, `porod_fit`, `kratky_plot`,
  `pair_distance_ift`, `dimensionless_kratky`, `classical_invariants`,
  `guinier_quality`, `peak_fit`, `sasmodels_fit`, `sasmodels_params`.
- `src/analysis/io.py` — `Analysed/` paths, save bundle (JSON/.dat/PNG),
  `.dat` footer annotation, manifest registration, batch summary.
- `src/analysis/atsas.py` — detect + wrap ATSAS binaries.
- `analysis/app.py` — thin Flask routes (individual + batch + save).
- `analysis/templates/index.html` — left-nav + top-tab UI, Plotly panels.
