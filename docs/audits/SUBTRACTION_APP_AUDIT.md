# Subtraction App — Full Audit (functionality · correctness · UX)

**App:** `background/` (Background Subtraction, port 5003)
**Date:** 2026-06-18
**Scope:** `background/app.py` + `background/templates/index.html` after the Phase-1
and Phase-2 redesign.
**Method:** static code review + numeric validation of the core math (synthetic
data) + checks against SAXS subtraction literature (SSRL / EMBL / BioXTAS).

> **Update 2026-06-18 — items resolved:** C1, C2, C3, U1, U2, U4 have been fixed
> and validated (see §6). Remaining open: C4 (overlap warning), C5 (detector
> inference), U3 (busy state), U5 (metadata source switch), U6 (deprecate keyword
> endpoint) — all low/info.

**Verdict:** The app is **functionally complete and correct** for its supported
workflows. Core math (interpolation, scaled subtraction, error propagation,
auto high-q scaling, QC metrics, filename suggestion) is validated. A handful of
**medium/low** items remain — mostly edge cases (scan-index parsing, log-axis
data dropping) and small UX gaps (no q-window control for auto-scale, preview vs
save selection mismatch). None block normal use.

---

## 1. Functionality

### Backend endpoints
| Endpoint | Purpose | Status |
|---|---|---|
| `GET /api/health` | liveness | ✅ |
| `GET/POST /api/set_project`, `GET /api/project` | hub project root | ✅ |
| `GET /api/browse` | dir + `.dat` file browser | ✅ |
| `POST /api/scan` | list `.dat` (name, stem, scan_idx, keyword) | ✅ |
| `POST /api/preview` | sample/bkg/subtracted curves + scale + QC + window | ✅ |
| `POST /api/auto_scale` | high-q least-squares scale | ✅ |
| `POST /api/pair_qc` | scale + QC only (fast, for batch review) | ✅ |
| `POST /api/metadata` | footer metadata table | ✅ |
| `POST /api/suggest` | per-sample background suggestion + file list | ✅ |
| `POST /api/subtract/individual` | explicit file list − one background | ✅ |
| `POST /api/subtract/scan_matched` | pair folders by scan index | ✅ |
| `POST /api/subtract/keyword` | one bkg − folder by keyword | ⚠ present but **unused by UI** (dead-ish) |

### UI features
| Feature | Status |
|---|---|
| 4 top tabs: Setup · BG-Sub SAXS · BG-Sub WAXS · Average Metadata | ✅ |
| Left modes: Individual · Scan · Suggested | ✅ |
| Explicit SAXS/WAXS detector toggle (tags output, routes results) | ✅ |
| Hub auto-fill of folders per detector | ✅ |
| Individual: list + multi-select files, per-file preview | ✅ |
| Suggested: token/keyword matching + editable per-row background dropdown | ✅ |
| Suggested: Review grid (per-row scale + QC dot) | ✅ |
| Methods: Manual scale + Auto high-q | ✅ |
| Result tabs: raw+bkg / subtracted overlay, dynamic scale slider | ✅ |
| Result tabs: Overlay (log) ↔ Residual (linear, zero line) view | ✅ |
| Result tabs: Compare methods (manual vs auto) | ✅ |
| QC metrics + warnings inline | ✅ |
| Metadata table (union of footer fields) | ✅ |
| Folder/file browser modal (.dat aware) | ✅ |

---

## 2. Correctness

### Validated (with numeric evidence)
Synthetic test: `I_sample = 0.85·I_bkg + low-q signal`, σ = √I.

- **Scaled subtraction + error propagation** — `I_sub = I_s − c·I_b`,
  `σ_sub = √(σ_s² + c²σ_b²)`; background log-interpolated onto the sample
  q-grid. ✅ Matches theory.
- **Auto high-q scaling** — weighted least squares `c = Σw·I_s·I_b / Σw·I_b²`
  (w = 1/σ_s²) over the top-25% q window. **Recovered c = 0.850 vs true 0.85.** ✅
- **QC over/under-subtraction** — at c = 1.3 (too high): **46 % negative points →
  error + warning** raised; at the recovered c: **0 % negative, high-q residual
  0.0**. ✅ Mirrors the literature validity check (over-subtraction → negatives/
  upturns in log; high-q overlay when correct).
- **Filename suggestion** — `BSA_PBS_0003` → `buffer_PBS_0001` (shared “PBS”
  token + background keyword); with hint `water` → `water_0001`. ✅
- **Units** — output `.dat` and plots use q in **nm⁻¹**, consistent with the rest
  of the platform. ✅
- **Manifest writes** — all subtraction endpoints register via the locked
  `update_manifest` (provenance: scale, method, mode, detector). ✅

### Issues
| # | Severity | Finding | Recommendation |
|---|---|---|---|
| C1 | **Medium** | `_scan_idx` returns 0 when a filename has no trailing `_NNNN`. In `scan_matched`, files are keyed in a dict by scan_idx, so several index-less files collapse to key 0 and overwrite each other → only one pair is processed silently. | Detect when most indices are 0 and warn; or fall back to sorted-order pairing; surface "N pairs matched" prominently (already returned as `n_matched`). |
| C2 | Low | `_load_dat` keeps only `q>0 & I>0`, dropping non-positive points from sample **and** background before interpolation. For WAXS regions with near-zero/negative counts this can subtly bias the interpolated background. | Keep all finite points for interpolation; apply the positivity mask only for display/log plotting. |
| C3 | Low | Auto high-q uses the top 25 % of q. For **WAXS** with Bragg peaks in that window, the least-squares scale can be skewed by peaks. | Allow a user q-window (see U1); optionally use a robust/median estimator for WAXS. |
| C4 | Low | `np.interp` clamps outside the overlap (no extrapolation) — if sample and background q-ranges barely overlap, the edges use flat end values. | Warn when overlap fraction is small. |
| C5 | Info | Detector tag comes from the toggle, not the data — a mismatched toggle mislabels output `detector`. | Optionally infer detector from path (`/SAXS/` vs `/WAXS/`) and warn on conflict. |

---

## 3. User-friendliness

### Strengths
- Clear **viewer-style** layout: modes left, views on top, controls in Setup.
- **Auto-suggestion + editable review grid** with per-row scale/QC dots makes
  batch subtraction fast and verifiable.
- **Live validation**: QC metrics/warnings, dynamic scale slider, residual view,
  and method comparison directly support spotting over/under-subtraction.
- Hub-driven **auto-filled folders** per detector; step guide; dynamic Run label.
- `.dat`-aware browser; graceful, specific error messages.

### Gaps
| # | Severity | Finding | Recommendation |
|---|---|---|---|
| U1 | **Medium** | Auto high-q has no UI control for the q-window (backend supports `qmin`/`qmax`). | Add optional "high-q window" inputs shown when Auto is selected. |
| U2 | Medium | Individual mode: clicking a filename **previews** it, but **save** uses the **checked** rows — clicking ≠ selecting can confuse. | After clicking to preview, auto-check that row, or show "previewing X (not selected for save)". |
| U3 | Low | No busy/disabled state on Preview/Review during long batch loops (only status text). | Disable buttons + show progress count during batch review/run. |
| U4 | Low | Residual view has no σ band / ratio (I_sample/c·I_bkg) option. | Phase 3: add ratio view + error band. |
| U5 | Low | Metadata tab is sample-folder only; not linked to the subtracted outputs. | Add a source switch (sample / background / subtracted). |
| U6 | Info | `keyword` mode exists in backend but not surfaced — possible confusion for API users. | Remove or document as deprecated. |

---

## 4. Prioritized recommendations
1. **C1** — make scan-index pairing robust (warn / fallback) — prevents silent
   single-pair batches.
2. **U1** — expose the auto high-q q-window (1 small UI block; backend ready).
3. **U2** — align preview-click with save-selection in Individual mode.
4. **C2/C3** — interpolation positivity + WAXS-aware auto-scale.
5. Tidy: remove/deprecate unused `keyword` endpoint (U6).

None are blocking; the app is correct and usable today. Items above are
incremental hardening + polish.

---

## 5. Validation evidence (reproducible)
Numeric check of `_auto_scale`, `_subtract`, `_qc_metrics`, `_suggest_background`
on synthetic data:

```
auto-scale recovered 0.850 (true 0.85)  window q∈[2.37,3.16]
over-sub (c=1.3): pct_neg=46%  warns=[error, warning]
auto-sub:         pct_neg=0.0% highq_ratio=0.0
suggest(BSA_PBS) -> buffer_PBS_0001.dat
suggest(hint=water) -> water_0001.dat
```

`background/app.py` compiles; template structure balanced (4 tabs, divs matched).
Apps were **not launched** (per project rule) — checks are static + unit-level.

---

## 6. Resolution log (2026-06-18)

| Item | Fix | Validation |
|---|---|---|
| **C1** scan pairing | `scan_matched` falls back to **sorted-order pairing** + returns a `warning` (shown in UI) when filenames lack unique scan indices | code review; `n_matched` now real |
| **C2** interpolation | `_load_dat` keeps all finite q>0 points (no longer drops non-positive I); `_interpolate_onto` uses only finite/positive source points | numeric: negative bkg point → interp all finite ✔ |
| **C3** WAXS auto-scale | `_auto_scale` adds one **sigma-clip (MAD) pass** on residuals to reject Bragg peaks/outliers; reports `n_clipped` | numeric: 6× spike → scale 0.900 vs true 0.9, 1 pt clipped ✔ |
| **U1** q-window | Auto high-q now shows **q-min / q-max** inputs; threaded to preview, subtract (both), compare, and pair_qc | endpoints accept `qmin`/`qmax` ✔ |
| **U2** preview/select | Clicking a sample filename now **checks (selects)** it as well as previewing | code review ✔ |
| **U4** ratio view | New **Ratio** view: I_sample/(scale·I_bkg) with a y=1 reference (≈1 at high q when scaled right); backend returns the ratio array | code review ✔ |

Open (low/info): C4 small-overlap warning, C5 detector inference from path,
U3 busy/disabled state during batch loops, U5 metadata source switch,
U6 deprecate the unused `keyword` endpoint.

Sources: [SSRL SAXS data-analysis primer](https://www-ssrl.slac.stanford.edu/smb-saxs/content/data-analysis-primer) ·
[EMBL BioSAXS reduction](https://www.embl-hamburg.de/biosaxs/courses/embo2012/slides/data-reduction-processing-kikhney.pdf) ·
[BioXTAS RAW docs](https://bioxtas-raw.readthedocs.io/en/latest/).
