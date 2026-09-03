# Error estimation and propagation: literature vs. this pipeline

What the SAXS/WAXS literature says the uncertainty on I(q) should be, and
exactly how — and how well — this platform implements it, stage by stage:
2D detector image → 1D reduction → frame averaging → background subtraction →
model fitting. Every code claim below cites a file and line; every literature
claim cites its source. Verify anything surprising by reading the cited line
before acting on it.

---

## 1. The two competing error models, and what the literature actually says

Two independent ways exist to estimate σ(q) for one azimuthal bin.

**Poisson / counting.** Each detected photon count is a Poisson variate, so a
pixel with `signal` counts has variance ≈ `signal` (before normalization). In
the clean, no-correction limit this collapses to

```
σ²(q) ≈ I(q) / N(q)          σ(q)/I(q) ≈ 1/√(total counts in the bin)
```

where `N(q)` is the number of pixels contributing to that q-bin. pyFAI's
actual per-pixel form floors the variance at 1: `σᵢ² = max(1, signalᵢ)`, so an
empty pixel cannot produce a zero-variance bin.

**Azimuthal / empirical.** The standard deviation of the pixel values *within*
one q-bin, converted to a standard error of the mean:

```
σ_azimuthal(q) = s(q) / √n(q)
```

This captures anything Poisson counting statistics cannot see — detector
irregularities, pixel-sensitivity variation, beam instability, parasitic
scattering — because it measures how much nominally-equivalent pixels
actually disagree, not how precise each one claims to be.

**The central empirical result** (Sedlak, Bruetzel & Lipfert, *J. Appl.
Cryst.* **50**, 621–630, 2017, DOI 10.1107/S1600576717003077): on a
photon-counting detector (PILATUS) with an isotropic sample, the two
estimators agree across the whole q range. Where they disagree, the cause is
identifiable hardware — module gaps shrink `N(q)` and correctly increase σ;
unmasked broken pixels show up as azimuthal-variance outliers at high q.

**The recommended practice** (Pauw, "Everything SAXS", *J. Phys.: Condens.
Matter* **25**, 383201, 2013, DOI 10.1088/0953-8984/25/38/383201, and its 2014
corrigendum DOI 10.1088/0953-8984/26/23/239501):

```
σ(q) = max( σ_Poisson(q), σ_azimuthal-SEM(q), 0.01·I(q) )
```

Take the larger of the two estimators — Poisson is the absolute floor on
uncertainty, and if the empirical spread exceeds it, the empirical spread is
the more honest number — floored at 1% of intensity, because nothing in SAXS
is realistically known better than that. An inter-laboratory comparison
(Pauw, Smith, Snow, Terrill & Thünemann, *J. Appl. Cryst.* **50**, 1280–1288,
2017) found **31.4% of submitted datasets had stated uncertainties below that
1% floor** — a documented, common failure mode, not a hypothetical one.
**Nobody in the literature recommends averaging the two estimators.**

**Azimuthal fails specifically on anisotropic samples.** If the sample has
texture — flow alignment, large crystallites, a spotty ring — azimuthal
spread measures the sample's anisotropy, not measurement noise, and
overstates the true uncertainty. pyFAI's own `calc_spottiness()` exists to
flag this (S < 0.05 smooth, 0.05–0.15 mild texture, > 0.15 spotty). This is
relevant here specifically because the reactor performs flow synthesis, and
flow-aligned nanoparticles or precipitates are a realistic source of
anisotropy this platform could encounter.

**What each `error_model` value actually computes**, per pyFAI's own
documentation and Kieffer et al. ("Application of signal separation to
diffraction image compression and serial crystallography", *J. Appl. Cryst.*
**58**, 138–153, 2025, DOI 10.1107/S1600576724011038 — pyFAI's own
recommended citation for its error models):

| `error_model` | What it computes |
|---|---|
| `None` | No variance; `sigma` is `None` |
| `"poisson"` | `σᵢ² = max(1, signalᵢ)` per pixel — the Poisson floor |
| `"azimuthal"` | Empirical spread of pixel values within the bin (SEM) |
| `"hybrid"` | **Not a general 1D model.** pyFAI's own source comment: used for peak-picking / sigma-clipping, azimuthal in early iterations then Poisson at the end. Do not use it as a default 1D error model. |

pyFAI also documents two version-dependent correctness issues that matter for
any pipeline built on it: (1) versions ≤ 0.16–0.19 propagated errors correctly
**only** when solid-angle, flat-field, polarization and pixel-splitting were
all disabled — normalization applied *after* integration "distorts the
distribution of error, even at a level of a few percent"; the fix, in
production since v0.20, is the new-generation integrator (`integrate1d_ng`,
what plain `integrate1d` now dispatches to), which keeps normalization
*inside* the integration; (2) the azimuthal error model combined with
per-pixel weighting (solid angle + polarization) does not yet pass pyFAI's
own χ² validation — its own tutorial states this directly.

---

## 2. Stage 1 — 2D → 1D reduction

**Code:** `src/reduction/core.py`, `Experiment.process_saxs_file` /
`process_waxs_file` (lines 620–633, 666–679), calling
`self.ai_saxs.integrate1d(...)` / `self.ai_waxs.integrate1d(...)`.

**What ships:**

- `error_model: "poisson"` is the shipped default in `config.yml`, passed
  straight through to pyFAI (`core.py:631,677`) — the correct choice for a
  photon-counting detector, which this is.
- `requirements-core.txt:37` pins `pyfai>=2024.9`, well past the v0.20
  new-generation-integrator fix, and the call site uses plain `integrate1d`
  (not the deprecated `integrate1d_legacy`) — so normalization is applied
  inside the integration, correctly, automatically.
- `dark`, `flat`, `mask`, `dummy`, `delta_dummy` are all threaded through to
  `integrate1d` (`core.py:627-630, 673-676`) — dark-current and flat-field
  correction happen at the pixel level, before integration, which is where
  the literature says they belong.
- The scalar `normalization_factor` (built from transmission/thickness/i0/
  bstop, see `CLAUDE.md` § config.yml Reference) is passed as pyFAI's own
  `normalization_factor` argument (`core.py:633,679`), not applied by hand
  afterward — so σ is divided by the same scalar as I automatically, inside
  pyFAI's own bookkeeping. This is the correct behavior and it comes for
  free from using the argument rather than post-hoc division.

**What is not verified, and should be before trusting the numbers:**

- **Whether the `.raw` values really are undecorated photon counts.** The
  Poisson model requires this. If the beamline detector firmware applies any
  flat-field, count-rate/dead-time linearization, or gain correction before
  writing `.raw`, the stored value is no longer Poisson-distributed and
  `error_model: "poisson"` silently produces a wrong σ with no error message
  — pyFAI's own tutorial calls vendor-applied flat-fielding "a major issue"
  for exactly this reason. Nothing in this codebase checks this; it is a
  fact about the beamline hardware that has to be confirmed once, outside
  the code.
- **WAXS has no mask.** `config.yml`: `mask_files: {saxs: "RT_SAXS_mask_03.edf",
  waxs: null}` (`CLAUDE.md` line 193; loaded at `core.py:353-359`). A `null`
  WAXS mask means dead pixels and module edges on that detector are neither
  excluded from the Poisson sum nor from any future azimuthal check. The
  WAXS detector (195×487) already has few pixels per q-bin at some q — an
  unmasked bad pixel there has an outsized effect on both I and σ.
- **`dummy` / `delta_dummy` default to `None`** (`core.py:281-284`) unless
  set explicitly in `config.yml`. If the beamline writes a sentinel value for
  untrusted pixels (a common firmware behavior), an unset `dummy` means that
  sentinel enters the integration as real signal.
- **No `error_model: "azimuthal"` cross-check exists anywhere in the
  pipeline.** The literature's central validation — comparing Poisson against
  empirical spread to catch detector artefacts — is not run. This is not
  wrong (Poisson-only is a legitimate choice for a counting detector) but it
  forgoes a diagnostic the literature treats as close to mandatory, and
  it means `calc_spottiness()` (the anisotropy check relevant to flow
  synthesis) is never computed.
- **No end-to-end χ² self-check.** ATSAS's `DATCMP` recipe — collect ≥6
  consecutive blank/buffer frames, confirm similarity with CorMap, then check
  that reduced χ² between independent pairs falls in roughly [0.9, 1.1] — is
  the standard acceptance test for "is my σ believable at all," and this
  pipeline already collects a blank before every synthesis run
  (`background_when: "before"`, see `docs/REACTOR_SETUP.md`). Nothing runs
  this test today.

---

## 3. Stage 2 — averaging repeated frames

**Code:** `src/plot_reduction.py`, `_average_group` (lines 91–204), the shared
core behind both `average_and_save` (whole-folder averaging, the manual
Visualisation & Average path) and `average_batch` (the reactor's rolling auto-average).

**The formula** (lines 178–183):

```python
I_stack   = np.vstack(I_rows)
sig_stack = np.vstack(sig_rows)
n         = I_stack.shape[0]
I_out     = I_stack.mean(axis=0)                      # (1/n) Σ Iᵢ
sig_out   = np.sqrt((sig_stack**2).sum(axis=0)) / n    # √(Σ σᵢ²) / n
```

This is standard error propagation for an unweighted mean of independent
measurements: `σ_out = √(Σσᵢ²)/n`, which for equal σ correctly collapses to
`σ/√n`. Two things are done right and are easy to get wrong:

- **`n` is the number of frames that actually contributed**, not the number
  passed in. Frames dropped by the I0-outlier filter (`_average_group:112-132`)
  or with fewer than 3 valid points (`:154-159`) are excluded from *both* the
  sum and the denominator — so a skipped frame cannot silently bias I low and
  σ artificially small, which is exactly the trap an inflated denominator
  would create.
- **A single-frame "group" passes σ straight through** (`:137-140`) with no
  spurious `/√1` division.

**Where this diverges from the literature's exact recommendation:**

- **The average is unweighted.** Inverse-variance weighting,
  `I = Σ(Iᵢ/σᵢ²) / Σ(1/σᵢ²)`, is the statistically optimal combination when
  frames have different σ (different exposure, different I0, partial
  radiation damage) — see the general error-propagation literature and
  Pauw's rebinning discussion, which applies the same principle. This
  pipeline's `I_stack.mean(axis=0)` weights every contributing frame equally
  regardless of its stated σ. The effect is small for near-identical frames
  and real for a mixed batch. Tracked as **D4** in
  `docs/audits/OPEN_DEFECTS.md`.
- **σ is interpolated in log-space before averaging**
  (`_average_group:161-170`: `np.exp(np.interp(log q, log q_frame, log σ_frame))`),
  onto the common grid built by `_common_q_grid`. This is *not* the
  literature's exact prescription for propagating σ through interpolation —
  the correct rule for linear interpolation is
  `σ_new² = (1−t)²σ₁² + t²σ₂²` (Gardner, *J. Res. NIST* **108**(1), 69–78,
  2003) — but geometric (log-space) interpolation of σ is closer to that
  correct form than either of the two common wrong shortcuts (linearly
  interpolating σ, or linearly interpolating σ²), both of which
  systematically over-estimate. This is a reasonable practical choice, not a
  bug, but it is an approximation worth naming rather than assuming exact.
- **Interpolation induces correlation between neighboring q-points that
  nothing downstream accounts for.** Gardner (2003) shows interpolated
  values are correlated with their neighbors, and that this correlation must
  be carried through any subsequent combination of the data — which
  includes every integral quantity this platform computes afterward: I(0),
  Rg, the Porod invariant, p(r). Every one of those treats each q-point as
  independent. The practical consequence is that fitted uncertainties on Rg,
  size, and invariants downstream are probably tighter than they should be —
  not because the σ column is wrong, but because the covariance between
  points is silently assumed to be zero.
- **No scale-uncertainty tracking.** The scalar normalization terms
  (transmission, thickness, the absolute calibration factor K) each carry
  their own uncertainty in principle, and the literature (Pauw §"absolute
  calibration") is explicit that this uncertainty is perfectly correlated
  across *all* q-points — it changes the curve's overall scale, not its
  shape — and therefore must **not** be folded into the per-point σ(q)
  column, or it will bias fits toward under-weighting the whole curve. This
  pipeline does not track `σ(transmission)`, `σ(thickness)`, or `σ(K)`
  anywhere (`src/manifest.py`, `src/reduction/core.py`) — not incorrectly
  folded in, simply absent. This is a documentation/completeness gap, not an
  active error.

---

## 4. Stage 3 — background subtraction

**Code:** `background/app.py`, `_subtract` (lines 122–133).

**The formula** (line 132):

```python
sig_sub = np.sqrt(sig_sam**2 + (scale * sig_bkg)**2)
```

This is exactly the textbook form for subtracting two independent
measurements with a scale factor on one of them —
`σ²(A − c·B) = σ²(A) + c²σ²(B)` — and matches Sedlak et al.'s equation (6)
for sample-minus-buffer subtraction precisely. **This is correct as written.**

Two caveats from the literature, both currently unaddressed:

- **The uncertainty of the fitted `scale` itself is not propagated.**
  `_auto_scale` (`background/app.py:271`) fits the background scale factor by
  weighted least squares; that fit has its own uncertainty, and a rigorous
  treatment would add a term for it (roughly
  `(∂I_sub/∂scale)² · σ²(scale) = I_bkg² · σ²(scale)`) to the subtraction
  formula above. This is a second-order effect relative to the frame σ's
  themselves in most cases, but it is a documented omission rather than a
  deliberate simplification.
- **Sample and background are interpolated onto a common grid before
  subtracting** (`_interpolate_onto`, `background/app.py:103-119`, same
  log-space scheme as the averaging stage) whenever their native q-grids
  differ. The same Gardner correlation caveat from Stage 2 applies here: the
  literature's stronger recommendation is to avoid interpolation entirely by
  integrating sample and background onto the *identical* q-grid at the
  reduction stage (same `npt_radial`, same `radial_range`, same `unit`) so
  that subtraction is point-by-point with no induced correlation. Where the
  grids already match — the common case in this pipeline, since one
  `config.yml` governs both — this caveat does not apply; it only matters if
  sample and background were reduced under different settings.

---

## 5. Stage 4 — where σ used to stop being used

**Fixed.** This was the largest gap in the pipeline, and downstream of
everything else checked in this document being correct. As originally
audited, verified directly against `src/analysis/core.py` and
`src/analysis/nanoparticle.py`:

| Function | Took `sigma` as an argument | Used it in the fit |
|---|---|---|
| `pair_distance_ift` | yes | yes — weights + χ² |
| `sasmodels_fit` | yes | yes |
| `peak_fit` | yes | referenced once |
| `guinier_fit` (`core.py:54`) | yes | **no — 0 references in the body** |
| `porod_fit` (`core.py:140`) | yes | **no — 0 references in the body** |
| nanoparticle form-factor fit (`nanoparticle.py:154-170`) | not accepted at all | minimized an **unweighted** log-residual |

`guinier_fit` and `porod_fit` both accepted a `sigma` parameter and never
touched it — `linregress(q², ln I)` in the Guinier case was an ordinary
unweighted regression. The nanoparticle fit that drives the autonomous-loop
optimizer (`_fit_one`, `nanoparticle.py:154`) did not take `sigma` as an
argument at all; its objective was `log10(model) − log10(I)` with every
point weighted equally regardless of measurement precision.

**Why this mattered more than any single upstream approximation:** the
literature (Trewhella et al., "2017 publication guidelines...", *Acta Cryst.*
D **73**, 710–728, 2017, DOI 10.1107/S2059798317011597) is explicit that
correctly propagated statistical errors are what make a fit's χ² and
confidence intervals meaningful at all. Every correction made carefully in
Stages 1–3 above — the Poisson floor, the `n_used` denominator, the
subtraction-variance formula — had no effect on the Guinier fit, the Porod
exponent, or the size/PDI the optimizer trained on, because those fits never
looked at σ. The `confidence` score feeding the Bayesian optimizer's noise
weighting was built from an unweighted residual, not from propagated
measurement uncertainty.

**What changed.** `guinier_fit` and `porod_fit` now fit
`ln I = a + b·x` by weighted least squares (`_weighted_linregress`,
`core.py`), weighted by `1/σ(ln I)²` with `σ(ln I) ≈ σ(I)/I` — inverse-variance
weighting, the standard treatment (Sedlak, Bruetzel & Lipfert 2017, §1
above). `guinier_estimate` and `_fit_one` in `nanoparticle.py` do the same in
log-space and log10-space respectively. In all cases, `sigma=None` or an
all-invalid `sigma` falls back to exactly the previous unweighted behaviour —
verified bit-identical against the old code path — so nothing that omits
`sigma` changes. Fixing this also surfaced and fixed a real alignment bug:
`analyze_profile` was passing `sigma` to `guinier_estimate` **unmasked and
unsorted** relative to the already-cleaned, already-sorted `q`/`I` arrays —
harmless only because `sigma` was previously ignored. Weighting without
fixing that alignment would have applied each point's error bar to the wrong
point; both are fixed together (`src/analysis/nanoparticle.py`).

Verified on synthetic data against a known ground truth (not just "it runs"):
with heteroscedastic noise (a realistic high-q dropoff in precision), the
weighted Guinier fit recovered a true Rg = 5.0 nm with 0.02 nm error versus
0.55 nm unweighted; the weighted nanoparticle fit recovered a true 8.0 nm
radius to within 0.0015 nm versus 0.018 nm unweighted. Full test suite (522
tests) and the demo-pipeline golden-master reproducibility test both pass;
the golden reference numbers for the Guinier fit changed and were inspected
before regenerating — the auto-range refinement now correctly narrows a
too-broad initial q-range once weighting is applied, which is the fit
behaving more correctly on that (real, noisy) fixture data, not a
regression. See `docs/audits/OPEN_DEFECTS.md` item 13.

Still open, not addressed by this fix: `peak_fit` reads `sigma` only once
rather than weighting the fit by it.

---

## 6. Summary: what is right, what is approximate, what is missing

**Correctly implemented, verified against the cited literature:**

- Poisson error model on a photon-counting detector (§2)
- Normalization applied inside `integrate1d`, not post-hoc (§2)
- Dark/flat correction at the pixel level before integration (§2)
- Averaging denominator counts only contributing frames (§3)
- Subtraction variance formula `σ² = σ_sam² + (scale·σ_bkg)²` (§4)
- Guinier, Porod and the nanoparticle form-factor fit are now weighted by
  1/σ² (§5, fixed) — verified against synthetic ground truth

**Reasonable approximations, not literature-exact but defensible:**

- Log-space σ interpolation onto a common grid (§3, §4) — closer to correct
  than the two common wrong shortcuts, not exactly the linear-interpolation
  formula the literature derives
- No inverse-variance weighting in frame averaging (§3, tracked as D4)

**Gaps — present in the pipeline today, none of them silently wrong so much
as silently absent:**

1. No confirmation that `.raw` values are undecorated Poisson counts (beamline
   fact to verify once, not a code fix)
2. WAXS has no pixel mask (`mask_files.waxs: null`)
3. `dummy`/`delta_dummy` unset by default — beamline sentinel values, if any,
   are not excluded
4. No azimuthal-vs-Poisson cross-check anywhere (forgoes both the standard
   validation and the anisotropy/`calc_spottiness()` diagnostic relevant to
   flow synthesis)
5. No end-to-end χ² self-check (the ATSAS `DATCMP` recipe, directly runnable
   given the blank frame already collected before every run)
6. No scale-uncertainty (`σ(transmission)`, `σ(thickness)`, `σ(K)`) tracked
   anywhere, separate from per-point σ(q)
7. The uncertainty of the fitted background `scale` factor is not propagated
   into the subtraction
8. Interpolation-induced correlation between neighboring q-points is never
   accounted for in any downstream integral quantity
9. `peak_fit` reads `sigma` only once rather than weighting the fit by it
   (the higher-leverage version of this gap — Guinier, Porod and the
   nanoparticle fit — is now **fixed**; see §5)

None of items 1–9 make the pipeline wrong in the sense of producing a
different I(q) than it should. They make the stated σ(q) — which is
computed with real care through reduction, averaging and subtraction —
less connected than it could be to what the quality gate and the optimizer's
confidence scoring do with it. The single highest-leverage disconnect — the
fits that feed the autonomous loop ignoring σ entirely — has been closed.

---

## Sources

- Sedlak, Bruetzel & Lipfert, "Quantitative evaluation of statistical errors
  in small-angle X-ray scattering measurements", *J. Appl. Cryst.* **50**,
  621–630 (2017). DOI 10.1107/S1600576717003077.
  [journals.iucr.org](https://journals.iucr.org/j/issues/2017/02/00/jo5030/index.html) ·
  [open access, PMC5377352](https://pmc.ncbi.nlm.nih.gov/articles/PMC5377352/)
- Pauw, "Everything SAXS: small-angle scattering pattern collection and
  correction", *J. Phys.: Condens. Matter* **25**, 383201 (2013).
  DOI 10.1088/0953-8984/25/38/383201.
  [iopscience](https://iopscience.iop.org/article/10.1088/0953-8984/25/38/383201) ·
  [arXiv:1306.0637](https://arxiv.org/abs/1306.0637)
- Pauw, Corrigendum, *J. Phys.: Condens. Matter* **26**, 239501 (2014).
  DOI 10.1088/0953-8984/26/23/239501.
- Pauw, Smith, Snow, Terrill & Thünemann, "Nanoparticle size distribution
  quantification: results of a SAXS inter-laboratory comparison",
  *J. Appl. Cryst.* **50**, 1280–1288 (2017). DOI 10.1107/S160057671701010X.
  [arXiv:1702.03902](https://arxiv.org/abs/1702.03902)
- Kieffer, Orlans, Coquelle, Debionne, Basu, Homs, Santoni & De Sanctis,
  "Application of signal separation to diffraction image compression and
  serial crystallography", *J. Appl. Cryst.* **58**, 138–153 (2025).
  DOI 10.1107/S1600576724011038 — pyFAI's own recommended citation for its
  error models. [arXiv:2411.09515](https://arxiv.org/html/2411.09515v1)
- Ashiotis, Deschildre, Nawaz, Wright, Karkoulis, Picca & Kieffer, "The fast
  azimuthal integration Python library: pyFAI", *J. Appl. Cryst.* **48**,
  510–519 (2015) — cite for plain azimuthal averaging.
- pyFAI documentation: ["Weighted average and uncertainty
  propagation"](https://pyfai.readthedocs.io/en/stable/statistics.html) ·
  ["Variance of SAXS data" tutorial](https://www.silx.org/doc/pyFAI/latest/usage/tutorial/Variance/Variance.html)
  (the χ² validation and the normalization/pixel-splitting history) ·
  [source, `containers.py`](https://raw.githubusercontent.com/silx-kit/pyFAI/main/src/pyFAI/containers.py)
  (`ErrorModel` enum, `calc_spottiness`, `renormalize`)
- Trewhella et al., "2017 publication guidelines for structural modelling of
  small-angle scattering data from biomolecules in solution: an update",
  *Acta Cryst.* D **73**, 710–728 (2017). DOI 10.1107/S2059798317011597.
- Gardner, "Uncertainties in Interpolated Spectral Data", *J. Res. Natl.
  Inst. Stand. Technol.* **108**(1), 69–78 (2003).
  [nvlpubs.nist.gov](https://nvlpubs.nist.gov/nistpubs/jres/108/1/j80gar.pdf)
- Franke, Jeffries & Svergun, "Correlation Map, a goodness-of-fit test for
  one-dimensional X-ray scattering spectra", *Nature Methods* **12**,
  419–422 (2015). DOI 10.1038/nmeth.3358.
- ATSAS `DATCMP` manual — the standardized-residual / χ² validation recipe
  referenced in §2 and §6.
  [biosaxs-com.github.io](https://biosaxs-com.github.io/atsas/latest/manuals/datcmp.html)
- NXcanSAS application definition (`Idev` uncertainty field).
  [manual.nexusformat.org](https://manual.nexusformat.org/classes/applications/NXcanSAS.html)

See also `docs/audits/OPEN_DEFECTS.md` (item D4 — unweighted averaging) for
how the open items above are tracked for remediation.
