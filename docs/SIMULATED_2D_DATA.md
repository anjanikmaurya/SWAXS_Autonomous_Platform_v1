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
    enabled: true          # already the committed default (config.yml:195)
    speed_factor: 1.0      # 1.0 = real time; 10 = 10× faster; 0 = instant
```

`enabled: true` ships as the default, so switching the backend to `mock` is
enough to start generating frames.

**One thing you must check:** `simulator.poni` is **not** blank in the committed
config — it is a machine-specific absolute path
(`/Users/akmaurya/Desktop/Data_local/Auto_Run/poni`, `reactor/config.yml:204`).
On any other machine that path does not exist, so the documented "blank → fall
back to the project `config.yml`" behaviour never fires. Set it to `""` or to
your own path. This matters more than it looks: the simulated q-scale comes from
this geometry and the reduction's comes from the project `.poni`. If they differ,
the recovered particle size is **silently wrong** — no error, just a wrong
number (`reactor/config.yml:200-202`).

Then run the reactor normally. Every triggered acquisition writes:

```
<2D>/SAXS/{recipe_id}_sample_scan1_0000.raw   … int32, detector shape
<2D>/SAXS/{recipe_id}_bkg_scan1_0000.raw      … particle-free background
<2D>/{recipe_id}_sample.csv                   … i0 / bstop / temp per frame
```

Point the reduction app's `data_directory` at `<2D>` and it processes them
like real data.

### Save folder in mock mode

On the rig the hub's Windows folder is translated to the beamline Linux path via
`spec.hub_path_map`. **In mock mode that translation is skipped** — no SPEC is
involved and the simulator writes with ordinary local file I/O, so a
`/msd_data/...` path would be unwritable on a laptop. The hub project folder is
used verbatim. Flipping the Mock/Real toggle re-resolves it automatically.

Pin mock output elsewhere with `spec.mock_data_dir` (blank = follow the hub;
ignored when the backend is `real`, so a leftover override can never redirect
real beamtime data).

### The 2D tree is created for you

**Missing folders are created at the start of each collection**, including
intermediate ones. Where the tree lands is resolved by `two_d_subdir: "auto"`:

| Save folder | Result |
|---|---|
| bare project root | creates `<folder>/2D/SAXS/` |
| already contains `2D/` | reuses `<folder>/2D/SAXS/` |
| already contains `SAXS/` (rig style) | uses `<folder>/SAXS/` as-is |
| literally named `2D` | uses `<folder>/SAXS/` — no nested `2D/2D` |

Set `two_d_subdir: ""` to always treat the save folder itself as the 2D base, or
to a literal name to force one.

**Turn `enabled` off before beamtime.** It is ignored when `backend: "real"`, but
don't rely on that alone. Note that the committed default is `enabled: true`
(`reactor/config.yml:195`), so this is a thing you have to *do*, not a thing you
can assume.

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

**`reactor/config.yml:195-262` is the reference.** Every knob is there with an
inline comment, including the ones a duplicated block here kept losing —
`porod`, `two_d_subdir`, `mock_data_dir`, and under `truth:` `noise_R_frac`,
`sigma_T` / `sigma_x`, `pdi_curv_T` / `pdi_curv_x`, `R_min` / `R_max`,
`pdi_floor` / `pdi_ceil`. Read it there; a copy in this file can only drift.

A few `truth` knobs exist only as code defaults and are not in the YAML at all —
`residence_gain` (0.15 nm per unit `ln(F_ref/F_tot)`), `noise_pdi` (0.01), and
`seed` (`None` → derived from `recipe_id`). They come from
`ground_truth.DEFAULTS` (`src/simulator/ground_truth.py:36-60`); add them to the
YAML if you need to change them.

Two knobs worth calling out because their behaviour is not obvious from the name:

- `speed_factor: 1.0` honours `exposure × frames` — a 10 × 10 s acquisition
  really takes 100 s, so file watchers and the run-end logic are tested
  faithfully. Use `0` when iterating on the pipeline itself.
- **Brightness.** `scale: 20000.0`, `solvent_bkg: 50.0`, `capillary: 120.0` are
  counts *per second of exposure*, and they were raised deliberately. The
  earlier values (`scale: 800`, `solvent_bkg: 2`, `capillary: 5`) gave a peak of
  ~900 counts in a 1 s frame, which renders as a black image on any linear
  display — the "the file is empty" symptom. The current values put a 1 s frame
  in the 10⁴ range, like real detector data. If you see black frames, check
  these first.

---

## Verified behaviour

`tests/test_simulator_closed_loop.py` collects through the real `MockBeamline`,
reads back with the real reduction reader, subtracts, and fits with the real
analyzer. It asserts, rather than reports:

| Test | Assertion |
|---|---|
| `test_injected_radius_is_recovered_by_the_real_analyzer` | recovered R within **10 %** of the injected R, parametrised over (240 °C, x=0.15), (250 °C, x=0.12), (230 °C, x=0.18) — three recipes straddling the optimum |
| `test_recovered_pdi_tracks_the_injected_pdi` | at a deliberately off-optimum recipe (285 °C, x=0.05, high PDI), fitted PDI within **0.08** of injected |
| `test_optimum_recipe_yields_the_target_size_and_lowest_pdi` | at (240 °C, x=0.15) R = 4.0 ± 0.3 nm — the campaign's `target_size` — and PDI lower than at (295 °C, x=0.15) or (240 °C, x=0.02) |

Run the suite for the actual numbers; they depend on `truth` and on the `.poni`
in use, so a table here goes stale the moment either changes. (An earlier
revision of this document carried a table of five recipes at x = 0.30–0.35. Those
figures came from when `x_TOP_opt` was 0.30; with the current 0.15 they are not
reproducible.)

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
| **Analyzer** | q in nm⁻¹, positive I | `unit: q_nm^-1` honoured | ✅ injected R recovered |
| **Optimizer** | map result → recipe | `recipe_id` embedded in the filename | ✅ `match_recipe_id()` resolves it |
| **Back to reactor** | new recipe from the campaign | `CampaignController.ask()` | ✅ converges near the true optimum |

The test walks the full chain through the real pipeline — `.poni` geometry, pyFAI
integration, CSV metadata, background subtraction, analyzer fit, optimizer ask —
and prints per-stage output. Run it to see the current numbers:

```bash
python -m pytest tests/test_simulator_reduction_metadata.py -s
```

Analyzer confidence through the real reduction path sits well above the
campaign's `confidence_min: 0.5`, so simulated results are not rejected. A crude
hand-rolled radial average gives a much lower confidence; that is an artefact of
the harness, not of the data.

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
