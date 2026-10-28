# Simulated 2D SAXS data: run the whole pipeline without beam

**What this is for.** When the Autonomous Synthesis app (port 5108) is in
**Simulation** mode, every triggered collection writes real `.raw` detector frames
plus their metadata. The particles in those frames come from a hidden recipe →
size model with a known optimum, so you can run the complete loop (reduction →
averaging → subtraction → analyser → optimiser → next recipe) on a laptop and check
that it finds the right answer.

**When to use it.** Learning the platform, rehearsing an autonomous run before
beamtime, or checking that a change to the pipeline still converges. Nothing here
can produce data on real hardware: the simulator exists only inside the Simulation
backend.

---

## Step by step

### 1. Make a scratch project folder

Create an empty folder (for example `~/Desktop/sim_test`) and put in it:

- `config.yml`, the reduction settings, and
- `poni/` with the SAXS `.poni` and mask your `config.yml` names.

Copy both from a previous experiment, or from `Demo_Data/` if your copy has it
(that folder is not in the git repository). Do **not** put real `.raw` files in
it: the simulator refuses to write into a `2D/SAXS` folder that already holds
frames it did not write, so synthetic and real data never mix.

The simulated q-scale comes from the same `.poni` the reduction uses, so the
recovered particle size is directly comparable to the injected one. If no `.poni`
can be found, the simulator falls back to a synthetic geometry and logs a warning;
the frames still flow through the pipeline but the recovered size will not match.

### 2. Start the apps

1. Start the hub (`./start_platform.sh` or `start_platform.bat`), open
   http://localhost:5100 and choose the scratch folder as the project folder.
2. Start **Reduction & Correction**, **Visualisation & Average**, **Background
   Subtraction**, **Autonomous Analyser** and **Autonomous Synthesis**.
   **Autonomous Watch** is optional (it shows the loop and any stall).

### 3. Check one frame

1. In **Autonomous Synthesis**, confirm the top-bar toggle shows **Simulation**.
2. On the **Hardware test** tab, step 3, press **Collect now** (as *sample*).
3. A frame appears under `<project>/2D/SAXS/`, with its metadata `.csv` in
   `<project>/2D/` (or a `.raw.pdi` beside the frame if your `config.yml` says
   `metadata_format: "pdi"`). The folders are created if missing.

### 4. Start the data monitors

1. **Reduction & Correction** → **Run & monitor** tab → switch on **Watch for new
   files**.
2. **Visualisation & Average** → **Auto-Average** tab → set **Frames per average
   (N)** to the reactor's **Number of frames** (10 by default) → **Start
   auto-averaging**.
3. **Background Subtraction** → **Auto-Subtract** tab → sample keyword `sample`,
   background keyword `bkg` → **Start auto-subtraction**.

### 5. Start a campaign

In **Autonomous Analyser** → **Setup**, set **Target diameter (nm)** to **8** (the
hidden optimum radius is 4.0 nm, and the app asks for a diameter), a tolerance,
then press **Start campaign**. It writes the first condition into
`1D/SAXS/Conditions/`.

### 6. Run the reactor

In **Autonomous Synthesis** → **Autonomous setup**:

1. Step 2 Timing: shorten **Synthesis duration** and **Flush duration** for a quick
   test. Reactor timing is real seconds in Simulation too (no time compression), so
   the shipped 20 min flush really takes 20 min.
2. Step 4 Run: press **Run autonomously**.

Each cycle is flush → background shot → arm → synthesis → sample shot, both shots
tagged with the same `recipe_id`; the data apps then reduce, average and subtract
it, the analyser fits it and queues the next condition. The campaign should walk
toward **240 °C, x_TOP = 0.15** (see below). If it does not, the problem is in the
optimiser, not the data.

---

## Settings (`reactor/config.yml`, under `spec:`)

```yaml
spec:
  backend: "mock"          # startup default; the app's toggle switches it live
  simulator:
    enabled: true          # shipped default; false = collections are logged, nothing written
    poni: ""               # blank = use the project folder's config.yml / poni/
    speed_factor: 1.0      # 1.0 = a 10 × 10 s acquisition takes 100 s; 0 = write instantly
  mock_data_dir: ""        # blank = write into the hub's project folder
```

- **Save folder.** In Simulation the beamline path translation (`hub_path_map`) is
  skipped and the hub project folder is used as is. Set `spec.mock_data_dir` to
  write somewhere else; it is ignored on the Hardware backend.
- **Where the 2D tree goes** (`simulator.two_d_subdir: "auto"`): a bare project
  folder gets `<folder>/2D/SAXS/`; a folder that already has `2D/` or `SAXS/`, or is
  itself named `2D`, is reused without nesting.
- **Speed.** `speed_factor` only changes how fast frames are written. Use `0` when
  iterating on the pipeline itself.
- **Brightness.** `flux`, `scale`, `solvent_bkg` and `capillary` are counts per
  second of exposure. The shipped values give detector-like counts and a fit
  confidence above the campaign's 0.5 gate at 10 × 10 s. If frames look black,
  check these first. Very short acquisitions give (correctly) low confidence.
- Every other knob, including the hidden `truth:` block, is listed with a comment
  in `reactor/config.yml`. A few `truth` defaults exist only in code
  (`residence_gain`, `noise_pdi`, `seed`) in `src/simulator/ground_truth.py`.

**Before beamtime** set `spec.simulator.enabled: false` as well. It is ignored on
the Hardware backend, but do not rely on that alone.

---

## What gets simulated

| Aspect | Behaviour |
|---|---|
| Scattering | Schulz-polydisperse sphere form factor, `I(q) = scale·P(q) + background` |
| Geometry | Your real `.poni` (per-pixel q map); synthetic fallback if none is found |
| Mask | Your real SAXS mask `.edf` |
| Beamstop | Disc of radius `q_beamstop` (nm⁻¹), zeroed |
| Noise | Poisson, per pixel |
| Counters | `i0`, `bstop = i0 × transmission` (0.62), `temp`, a per-frame `Timer`, mild beam decay |
| Background | Solvent + capillary q⁻² upturn, present in sample frames too, so `sample − background` isolates the particles |

Units are nm⁻¹ / nm, matching the platform default `unit: q_nm^-1`. SAXS only.
Only the counters reduction needs are written; if a downstream tool of yours reads
extra CSV columns, add them in `src/simulator/writer.py`.

---

## The hidden ground truth

The recipe → particle mapping (`src/simulator/ground_truth.py`) is never shown to
the optimiser; the campaign only sees what the analyser fits, as with real beam.

```
R(T, x_TOP)   = R_opt + dR_dT·(T − T_opt) + dR_dx·(x_TOP − x_opt) + residence(F_tot)
PDI(T, x_TOP) = pdi_min + cT·((T − T_opt)/σ_T)² + cx·((x_TOP − x_opt)/σ_x)²
```

| Knob | Default | Note |
|---|---|---|
| `T_opt` | 240 °C | inside the `T_reac` bounds 180 to 300 |
| `x_TOP_opt` | 0.15 | inside the `x_each` bounds 0 to 0.3 |
| `F_ref` | 80 µL/min | inside the `F_tot` bounds 40 to 120 |
| `R_opt` | 4.0 nm (radius) | enter **8 nm** as the analyser's target diameter |
| `pdi_min` | 0.02 | best achievable PDI |

R is clamped to 1 to 10 nm and PDI to 0.001 to 0.5. Results are reproducible: the
scatter is derived from the `recipe_id`, so the same recipe always gives the same
particles.

If you change `truth`, keep `T_opt` and `x_TOP_opt` inside the `bounds:` block (an
optimum on the edge proves nothing) and keep the PDI curvature gentle enough that
PDI stays below `pdi_ceil` at the corners (otherwise the landscape goes flat).

### Known caveats

- A plain Guinier fit overestimates R at high PDI (up to +100 % at PDI 0.5). That
  is real physics (Guinier gives a z-averaged Rg), not a simulator fault; the
  analyser's full fit handles it.
- Hand-written test harnesses without a beamstop saturate the centre pixel. The
  app always applies one.

---

## Checking it automatically

For the Analyser alone, `python tools/analyzer_check.py --loop` prints PASS / FAIL
per function and runs one simulated campaign per design-loop method (see
`analyzer/knowledge.md`, Troubleshooting).

These tests run the real reduction, subtraction and analyser on simulated frames
and assert the result (recovered R within 10 % of the injected R, PDI tracked, the
optimum recipe gives 4.0 ± 0.3 nm and the lowest PDI):

```bash
python -m pytest tests/test_simulator_closed_loop.py tests/test_simulator_reduction_metadata.py -s
```

The `-s` flag prints the per-stage numbers.

---

## Where the code is

```
src/simulator/
├── ground_truth.py   recipe → (R, PDI), hidden landscape with an interior optimum
├── pattern.py        form factor, q map from .poni, mask/beamstop, Poisson noise
├── writer.py         .raw + CSV/PDI metadata, beamline file naming
└── collector.py      orchestration; held by the Simulation beamline
```
