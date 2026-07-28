# Simulated 2D SAXS Data — Full-Pipeline Testing Without Beam

Generates **real `.raw` detector frames** whenever a collection is triggered in
mock mode, so the entire chain — reduction → averaging → subtraction → quality →
analyzer → optimizer — can be exercised off-rig against a **known ground truth**.

> **Why not an LLM?** An LLM cannot produce a physically consistent 2D intensity
> field, would be slow per frame, and would destroy the one property that makes a
> mock valuable: knowing the right answer. This uses the actual scattering
> physics instead — sphere form factor through your real `.poni` geometry — so
> the reduction and analyzer can be checked against the radius that was injected.

---

## Quick start

In `reactor/config.yml`, under `spec:`:

```yaml
spec:
  backend: "mock"
  simulator:
    enabled: true          # ← the only switch you need
    speed_factor: 1.0      # 1.0 = real time; 10 = 10× faster; 0 = instant
```

Then run the reactor normally. Every triggered acquisition writes:

```
<data_dir>/SAXS/{recipe_id}_sample_scan1_0000.raw   … int32, detector shape
<data_dir>/SAXS/{recipe_id}_bkg_scan1_0000.raw      … particle-free background
<data_dir>/{recipe_id}_sample.csv                   … i0 / bstop / temp per frame
```

Point the reduction app at `<data_dir>` and it processes them like real data.

**Turn `enabled` off before beamtime.** It is ignored when `backend: "real"`, but
don't rely on that alone.

---

## What gets simulated

| Aspect | Behaviour |
|---|---|
| Scattering | Schulz-polydisperse **sphere form factor**, `I(q) = scale·P(q) + background` |
| Geometry | Your real `.poni` (per-pixel q map). Falls back to synthetic geometry if no `.poni` exists yet |
| Mask | Your real `mask_files.saxs` `.edf`, zeroed |
| Beamstop | Disc defined in q (`q_beamstop`, nm⁻¹), zeroed |
| Noise | Poisson, per pixel |
| Counters | `i0`, `bstop = i0 × transmission`, `temp`, with mild beam decay across frames |
| Background | Solvent + capillary q⁻² upturn — **present in sample frames too**, so `sample − background` isolates the particle signal |

Units are **nm⁻¹ / nm**, matching the platform default `unit: q_nm^-1`, so a fitted
radius is directly comparable to the injected one.

---

## The hidden ground truth

The recipe → particle mapping lives in `src/simulator/ground_truth.py`. It is
never shown to the optimizer — the campaign only sees what the analyzer reports,
exactly as with real beam.

```
R(T, x_TOP)   = R_opt + dR_dT·(T − T_opt) + dR_dx·(x_TOP − x_opt) + residence(F_tot)
PDI(T, x_TOP) = pdi_min + cT·((T − T_opt)/σ_T)² + cx·((x_TOP − x_opt)/σ_x)²
```

`R` is locally linear, so it equals `R_opt` **exactly** at the optimum; `PDI` has a
strict interior minimum at the same point. Defaults:

| Knob | Value | Interior to bound |
|---|---|---|
| `T_opt` | 240 °C | `T_reac: [180, 300]` ✅ |
| `x_TOP_opt` | 0.15 | `x_each: [0.0, 0.3]` ✅ |
| `F_ref` | 80 µL/min | `F_tot: [40, 120]` ✅ |
| `R_opt` | 4.0 nm | radius produced at the optimum |
| `pdi_min` | 0.02 | best achievable polydispersity |

Clamped to **R ∈ [1, 10] nm** and **PDI ∈ [0.001, 0.5]**.

### Verifying the optimizer actually converges

Set the campaign's `target_size` to `R_opt` (4.0 nm). The optimizer's loss

```
loss = ((size − target)/tolerance)² + w·(PDI/pdi_cap)
```

then has its minimum exactly at **(240 °C, x_TOP = 0.15)**. Run a campaign and
check it walks there. If it doesn't, the problem is the optimizer, not the data.

Reruns are reproducible: scatter is derived from the `recipe_id`, so the same
recipe always yields the same particles, while different recipes differ.

---

## Configuration reference

```yaml
simulator:
  enabled:      false     # master switch
  detector:     "SAXS"    # SAXS only
  shape:        [1043, 981]
  poni:         ""        # blank → project config poni_directory + poni_files.saxs
  mask:         ""        # blank → project config mask_files.saxs
  metadata_format: ""     # blank → project config ("csv" or "pdi")
  speed_factor: 1.0       # 1.0 = real time, 10 = 10×, 0 = instant
  flux:         1.0e6     # counts/s at I(0)
  scale:        800.0     # particle signal strength
  solvent_bkg:  2.0       # flat solvent level
  capillary:    5.0       # q⁻² upturn intensity quoted at q = 0.1 nm⁻¹
  q_beamstop:   0.02      # nm⁻¹ — raise to stress low-q masking
  transmission: 0.62      # sets bstop = i0 × T in the metadata
  truth: { T_opt: 240.0, x_TOP_opt: 0.30, R_opt: 4.0, ... }
```

`speed_factor: 1.0` honours `exposure × frames` — a 10 × 10 s acquisition really
takes 100 s, so file watchers and the run-end logic are tested faithfully. Use
`0` when iterating on the pipeline itself.

---

## Verified behaviour

`tests/test_simulator_closed_loop.py` collects through the real `MockBeamline`,
reads back with the real reduction reader, subtracts, and fits with the real
analyzer. Measured recovery:

| Recipe | Injected R | Fitted R | Error | Injected PDI | Fitted PDI |
|---|---|---|---|---|---|
| 240 °C, x=0.30 | 4.09 nm | 4.09 nm | +0.0 % | 0.020 | 0.019 |
| 250 °C, x=0.28 | 4.42 nm | 4.43 nm | +0.2 % | 0.086 | 0.088 |
| 260 °C, x=0.30 | 4.45 nm | 4.45 nm | −0.0 % | 0.237 | 0.254 |
| 230 °C, x=0.33 | 3.59 nm | 3.60 nm | +0.2 % | 0.099 | 0.100 |
| 220 °C, x=0.35 | 3.24 nm | 3.23 nm | −0.3 % | 0.288 | 0.298 |

### Known caveats

- **A naive Guinier fit overestimates R at high PDI** (up to +100 % at PDI 0.5).
  That is correct physics — Guinier returns a z-averaged Rg biased toward large
  particles — not a simulator fault. The full analyzer fit handles it.
- Without a beamstop the q→0 centre pixel saturates the count clip. The collector
  always applies one, so this only shows up in hand-written harnesses.
- Keep `truth.T_opt` / `x_TOP_opt` **interior to the `bounds:` block**. If the
  optimum sits on an edge, a converging optimizer proves nothing.
- Keep the PDI curvature gentle enough that PDI stays **below `pdi_ceil` at the
  corners of the search box**. If it saturates, the landscape goes flat and the
  optimizer has no gradient — the defaults reach ≈0.42 at the corners.

---

## Metadata: is it enough for the whole pipeline?

Yes — verified by `tests/test_simulator_reduction_metadata.py`, which runs the
**real `run_pipeline`** over simulated frames.

| Stage | Needs | Provided by the simulator | Verified |
|---|---|---|---|
| **Reduction** | CSV one level above `SAXS/`, stem prefix-matching the raw stem, row per `_NNNN` | `<2D>/{prefix}.csv`, one row per frame | ✅ 4/4 frames reduced, no metadata errors |
| | `i0`, `bstop` counters | written per frame, with mild beam decay | ✅ parsed into the `.dat` |
| | temperature (`temp`/`CTEMP`) | `temp` column | ✅ `CTEMP=240.0 °C` in the log |
| | transmission `bstop/i0` < 1 | `bstop = i0 × transmission` (0.62) | ✅ `T=0.6200`, no `T_sample > 1` warning |
| **Averaging** | consistent naming across frames | `{prefix}_scan1_NNNN` | ✅ frames group correctly |
| **Subtraction** | matched sample/background pair | flush writes `{recipe_id}_bkg`, sample writes `{recipe_id}_sample`; both share the background curve | ✅ subtraction positive across the Guinier band |
| **Analyzer** | q in nm⁻¹, positive I | `unit: q_nm^-1` honoured | ✅ R recovered to **+0.0 %** |
| **Optimizer** | map result → recipe | `recipe_id` embedded in the filename | ✅ `match_recipe_id()` resolves it |
| **Back to reactor** | new recipe from the campaign | `CampaignController.ask()` | ✅ converges near the true optimum |

Full-chain measurement through the real pipeline (`.poni` geometry, pyFAI
integration, CSV metadata, background subtraction, analyzer fit):

```
STEP 1 reduction   -> 8 .dat files, T=0.6200, thickness=1.000 mm, CTEMP=240.0°C
STEP 2 averaging   -> 4 sample + 4 background frames
STEP 3 subtraction -> positive across the Guinier band
STEP 4 analyzer    -> R=3.993 nm  (injected 3.992 nm, +0.0 %)  PDI=0.026  conf=0.906
STEP 5 optimizer   -> best T=253.9 x_TOP=0.185 (true optimum 240 / 0.15)
```

**Analyzer confidence is 0.906** through the real reduction path — comfortably
above the campaign's `confidence_min: 0.5` gate, so simulated results are not
rejected. (A crude hand-rolled radial average gives a much lower confidence;
that is an artefact of the harness, not of the data.)

### What is *not* simulated

Only `i0`, `bstop` and `temp` are written, because that is all reduction
consumes. If your real CSVs carry extra columns that a downstream app reads,
add them to `writer.counters()`.

---

## Architecture

```
src/simulator/
├── ground_truth.py   recipe → (R, PDI), hidden landscape with interior optimum
├── pattern.py        form factor, q map from .poni, mask/beamstop, Poisson
├── writer.py         .raw + CSV/PDI metadata, beamline filename convention
└── collector.py      orchestration; the object MockBeamline holds
```

Hook: `MockBeamline._do_collect()` calls `SimulatedCollector.collect()`. The
controller pushes the live recipe via `beamline.set_recipe()` at `_begin_next()`
— a no-op on real SPEC hardware.
