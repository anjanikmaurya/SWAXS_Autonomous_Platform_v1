# Reduction & Correction Pipeline — Physics Correctness Audit

**Date:** 2026-06-15
**Files audited:** `src/reduction/core.py` (`Experiment._compute_corrections`,
`process_saxs_file`/`process_waxs_file`), `src/reduction/process_metadata.py`,
`src/reduction/read_raw_file.py`.
**Question:** Are the corrections applied correctly, and is the normalization by
each factor mathematically correct per SAXS literature?

**Bottom line:** The core correction math is **correct** — transmission, the
beamstop-as-transmitted-flux normalization, Beer–Lambert thickness, and the
absolute-scale factor all match the canonical formula. There are **no errors in
the single-factor math**. I found **3 medium** and **4 minor** issues, mostly
around *combining* factors, an air-path inconsistency in the absolute term, and a
unit-labeling mismatch downstream.

---

## 1. The canonical formula (reference)

From the SAXS literature, the intensity recorded on the detector is

```
I_meas(q) = I0 · T · t · ΔΩ · ε · (dΣ/dΩ)(q)          (+ background)
```

where `I0` = incident flux, `T` = sample transmission, `t` = sample thickness,
`ΔΩ` = pixel solid angle, `ε` = detector efficiency, and `dΣ/dΩ` is the absolute
differential cross-section per unit volume (cm⁻¹·sr⁻¹). Hence

```
dΣ/dΩ (q) = I_meas / (I0 · T · t · ΔΩ · ε)
```

A beamstop pin-diode measures the **transmitted** flux, so

```
bstop ∝ I0 · T      and      i0 ∝ I0      ⟹      T = bstop / i0
```

This is the key identity the pipeline uses, and it is correct. Sources:
[Pauw, "Everything SAXS" (J. Phys. Condens. Matter 2013)](https://iopscience.iop.org/article/10.1088/0953-8984/25/38/383201),
[BSRF absolute-intensity calibration (NIM A 2018)](https://www.sciencedirect.com/science/article/abs/pii/S0168900218306260),
[EMBL BioSAXS data reduction notes](https://www.embl-hamburg.de/biosaxs/courses/embo2012/slides/data-reduction-processing-kikhney.pdf),
[USP SAXS/SANS normalization notes](https://portal.if.usp.br/cristal/sites/portal.if.usp.br.cristal/files/Treatment_SAXS_crislpo.pdf).

---

## 2. What is correct ✅

**2.1 Transmission `T = bstop_corr / i0_corr`.** Matches `T = bstop/i0` with
dark-current offsets subtracted from both. ✅

**2.2 Dark-current offsets** are subtracted from i0, bstop **and** the air
readings (`i0_air_corr`, `bstop_air_corr`) using the same offsets — correct, the
diode dark current applies to every reading. ✅

**2.3 "bstop" normalization** `NF = bstop_corr` ⟹ `I = counts/bstop =
counts/(I0·T)`. This is flux- and transmission-corrected (semi-absolute) — the
standard SSRL scheme. ✅

**2.4 "absolute" normalization** `NF = (bstop_corr · d_cm)/K` ⟹
`I = K·counts/(bstop·d) = K·counts/(I0·T·d)`. This equals `dΣ/dΩ` up to the
calibration constant `K` (which absorbs ΔΩ, ε, area). Mathematically correct. ✅

**2.5 Beer–Lambert thickness.** `T = exp(−μ·d) ⟹ d = −ln(T)/μ`. Units check out:
`μ_cm = xraydb.material_mu(energy_eV, density)` [cm⁻¹], `μ_m = μ_cm·100`,
`d_m = −ln(T)/μ_m`, `d_cm = d_m·100 = −ln(T)/μ_cm`. Consistent and correct, and
`xraydb` is called correctly (energy in eV, returns linear μ in cm⁻¹). ✅

**2.6 Air-path transmission.** `T_sample = (bstop/i0)/(bstop_air/i0_air)` is the
correct empty-beam-referenced transmission. The derived
`bstop_norm = bstop_corr·(i0_air_corr/bstop_air_corr)` algebraically equals
`T_sample · i0_corr = I0·T_sample`, i.e. it normalizes by the flux transmitted by
the **sample alone** (air/cell path divided out). The algebra is exact and the
approach is defensible. ✅

**2.7 Error propagation.** The factor is passed into `integrate1d(...,
normalization_factor=NF, error_model="poisson")` rather than dividing afterward,
so Poisson variances scale correctly by 1/NF². ✅ Solid-angle correction is on by
default. ✅

---

## 3. Issues found ⚠

### 3.1 [MEDIUM] Normalization terms multiply — overlapping terms double-count
`self.normalization` is a list and `norm_factor` is the **product** of the chosen
terms. The terms are not independent:
- `absolute` already contains `bstop` (`NF = bstop·d/K`).
- Choosing `["bstop","absolute"]` ⟹ `NF = bstop·(bstop·d/K) = bstop²·d/K` →
  `I ∝ counts/(I0²T²·d)` — **physically meaningless** (divides by flux²).
- Choosing `["i0","bstop"]` ⟹ `NF = i0·bstop` → `I ∝ counts/(I0²·T)` — also wrong.

Only `["bstop"]`, `["i0"]`, or `["absolute"]` individually are meaningful (and
`["i0","absolute"]`-type combos are likewise double-counted). Nothing warns the
user.

**Fix:** treat normalization as a single mode (mutually exclusive), or build the
factor from *independent* physical components (flux, transmission, thickness)
selected by booleans rather than multiplying composite terms.

### 3.2 [MEDIUM] Absolute term ignores the air-path correction
When air values are supplied, the `bstop` mode correctly uses the air-corrected
`bstop_norm`, but the `absolute` term still uses the raw `bstop_corr`:

```python
abs_norm = bstop_corr * max(t_cm, 1e-10)      # uses raw bstop_corr
```

For consistency, absolute scale should use the same air-corrected transmitted
flux: `abs_norm = bstop_norm * d_cm`. As written, absolute results are **not**
air-corrected even when the user configured air measurements, contradicting the
`bstop` path.

**Fix:** use `bstop_norm` (not `bstop_corr`) in `abs_norm`.

### 3.3 [MEDIUM] Output q-unit default conflicts with downstream nm⁻¹ assumptions
`core.py` defaults `unit = "q_A^-1"` (Å⁻¹), but the averaging writer labels columns
`q_nm-1`, and the analysis knowledge/Rg-interpretation and p(r)/Dmax assume nm⁻¹.
Dimensionless checks (qRg ∈ [0.3,1.3]) are unaffected, but **absolute Rg and Dmax
would be mislabeled by 10×** if the default is used. This is a cross-module
consistency risk, not an error inside the correction math itself.

**Fix:** pick one convention (recommend nm⁻¹ throughout) and make the `.dat`
column label follow the actual configured `unit`, or set the default to
`q_nm^-1`.

### 3.4 [MINOR] "i0" normalization omits absorption correction
`NF = i0_corr` ⟹ `I = counts/I0`: normalized to incident flux but **not** to
transmission/absorption. Valid as an explicit choice, but physically incomplete
for absorbing samples. Should be clearly labeled as "incident-flux only (no
transmission correction)".

### 3.5 [MINOR] Polarization correction off by default
`polarization_factor` defaults to `None` (PyFAI skips it). Synchrotron beams are
highly horizontally polarized; a factor of ~0.95–0.99 is normally recommended for
quantitative work. Document/encourage setting it.

### 3.6 [MINOR] Thickness-from-transmission uses the configured compound as the absorber
`d = −ln(T)/μ(compound, density)` is exact only if `compound`/`density` represent
the **bulk** material in the beam (e.g. the solvent/buffer), not a dilute solute.
For dilute samples this is an approximation — worth a note in the config docs.

### 3.7 [MINOR / robustness] Epsilon fallback still writes a (wrong) file
When `i0_corr` or `bstop_corr` ≤ 0, the code substitutes `ε = 1e-10` and loudly
warns, but the `.dat` is **still produced** with garbage normalization. Consider
skipping the file (or tagging it `status: error` in the manifest) so a bad frame
can't silently enter averaging/analysis.

---

## 4. Severity summary

| # | Issue | Severity | Math wrong? | Fix effort |
|---|---|---|---|---|
| 3.1 | Normalization terms multiply (overlap) | Medium | Only if misconfigured | Small |
| 3.2 | Absolute term ignores air correction | Medium | Yes, when air used | 1 line |
| 3.3 | Default q-unit Å⁻¹ vs nm⁻¹ downstream | Medium | Labeling/10× | Small |
| 3.4 | "i0" mode no absorption correction | Minor | No (by design) | Doc |
| 3.5 | Polarization off by default | Minor | Incomplete | Config |
| 3.6 | Thickness assumes bulk = compound | Minor | Approximation | Doc |
| 3.7 | Epsilon fallback still writes file | Minor | Robustness | Small |

---

## 4b. Resolution status (applied 2026-06-15)

All seven items have been addressed in `src/reduction/core.py`:

- **3.1** — Normalization now resolves to a single mode; overlapping combos
  (`absolute`+others, `i0`+`bstop`) are collapsed with a clear warning.
- **3.2** — The `absolute` term now uses `bstop_norm` (air-corrected flux).
- **3.3** — Default output unit changed to `q_nm^-1` to match downstream modules.
- **3.4** — `i0`-only mode now logs that it does not correct absorption.
- **3.5** — A warning is logged when no polarization factor is set.
- **3.6** — Code comment documents the bulk-absorber assumption for thickness.
- **3.7** — Non-positive corrected diodes now raise (file skipped) instead of
  writing a silently-wrong `.dat`.

Single-factor math was already correct (Section 2) and is unchanged.

## 4c. Full-app verification (2026-06-15)

Reviewed the entire reduction app end-to-end, not just the math:

- **`reduction/app.py`** — Experiment is cached by config hash and reused across
  runs/monitor cycles (operator is popped before hashing, so it never thrashes the
  cache); files are processed strictly one at a time; per-file exceptions are
  caught so the monitor loop survives bad frames; SSE log + manifest writes are
  isolated from the pipeline. ✅
- **`read_raw_file.py`** — reads `int32`, validates `size == rows×cols`, reshapes to
  `[rows, cols]`. The 2D preview (`_render_image`) uses the **same** dtype/shape, so
  the previewed image matches what is integrated. ✅
- **`process_metadata.py`** — CSV row indexed by the 4-digit filename index; PDI
  parser with SAXS-fallback for empty WAXS PDIs; raises clear errors on malformed
  input. ✅ (I/O only — no physics.)
- **Correctness math** — covered by Sections 2–3 and now locked by
  `tests/test_reduction_corrections.py` (18 tests: transmission, offsets,
  bstop/i0/absolute factors, air-path, absolute-air-corrected, Beer–Lambert,
  overlap guard, bad-diode raise, provenance user).

## 4d. Operator / user capture (added 2026-06-15)

The pipeline now records **who ran a reduction and when**:

- `make_provenance(..., user=...)` adds a `user` field to every file's provenance.
- `reduction/app.py` resolves the operator as: UI field → `SWAXS_USER_ID` env →
  OS login → `"unknown"`, and writes `project_meta.users` (unique),
  `last_run_by`, `last_run_at`, `last_run_app`, `last_run_mode` to the manifest at
  the start of each run/monitor session.
- The reduction UI has an **Operator** field (toolbar, persisted in `localStorage`)
  sent with `/api/run` and `/api/monitor/start`; the operator is also echoed in the
  live log. Operator is metadata only — popped before the Experiment config is
  hashed, so it never triggers a PyFAI reload.

## 5. Recommendation

The single-factor corrections and normalizations are **mathematically correct and
literature-consistent** — safe to trust for the standard `["bstop"]` (and properly
configured `["absolute"]`) modes. Before relying on combined modes or absolute
scale with air measurements, address 3.1 and 3.2. I can implement the fixes
(guard against overlapping terms, use `bstop_norm` in the absolute term, unify q
units, and harden the epsilon path) on request — these are small, contained
changes, but they touch scientific output so I'd want your sign-off first.

Sources: [Pauw 2013 — Everything SAXS](https://iopscience.iop.org/article/10.1088/0953-8984/25/38/383201) ·
[BSRF absolute calibration](https://www.sciencedirect.com/science/article/abs/pii/S0168900218306260) ·
[EMBL BioSAXS reduction](https://www.embl-hamburg.de/biosaxs/courses/embo2012/slides/data-reduction-processing-kikhney.pdf) ·
[SAXS/SANS normalization notes](https://portal.if.usp.br/cristal/sites/portal.if.usp.br.cristal/files/Treatment_SAXS_crislpo.pdf)
