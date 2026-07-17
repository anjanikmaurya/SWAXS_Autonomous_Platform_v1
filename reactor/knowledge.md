# Flow Synthesis — knowledge

The Flow Synthesis app (port 5007) is the **execution layer** for the 5-pump
continuous-flow nanoparticle reactor (Fong et al., J. Chem. Phys. 154, 224201,
2021 — Dolomite Mitos P-pumps + LG16 flow sensors). It receives an
already-predicted recipe and drives the pumps. It does **not** run the Bayesian
optimization / SAXS analysis — those push recipes to it.

## Pumps & setpoints

Five pumps: `pd_top_precursor`, `oleylamine`, `top`, `ode_dilution`, `ode_flush`.
A recipe (`T_reac`, `F_tot`, `x_ODE`, `x_TOP`, `x_oley`) converts to flows (µL/min):

- `ode_dilution = x_ODE · F_tot`
- `top = x_TOP · F_tot`
- `oleylamine = x_oley · F_tot`
- `pd_top_precursor = (1 − x_ODE − x_TOP − x_oley) · F_tot`
- `ode_flush = 0` during synthesis

Each pump's `[sensor_min, max_flow]` is a **hard limit**: a recipe whose computed
setpoint for any pump is nonzero-but-below its minimum, or above its maximum, is
**rejected** (nothing is clamped or sent to hardware); a true 0 is always allowed.
Recipes are also validated against config bounds and **hard safety caps**; a
runtime breach trips the emergency stop.

## Intake

Three ways in: a watched `recipes/` folder (drop `*.json`), `POST /api/recipe`
(JSON, for the BO/SAXS side), and the manual form. An **Auto-run** toggle decides
whether an arriving recipe starts automatically or waits for operator Start.
Recipes that arrive while busy are **queued (FIFO)**.

## Run lifecycle

`idle → arming → running → flushing → ready`. Temperature is **commanded through
SPEC** (`csettemp <T>`) and read back from the `CTEMP` counter — the app is not
gate-only. Arming supports three modes: **temperature** (wait for `CTEMP` to
reach/hold `T_reac`), **timed** (a fixed wait), and **ramp** (wait scaled by the
ramp rate). A run **ends** on a fixed-duration fallback or a manual Stop. Near the
run end (and again near the flush end) the app fires a `recipe_id`-tagged SPEC 2D
collection — the sample scatter is captured near run end and the background near
flush end. **Abort** goes straight to flush; **E-stop** idles everything
immediately.

## Flush & feedback

After every run the 4 reagent pumps zero and `ode_flush` runs at the configured
rate/duration (new recipes blocked until done); a manual **Flush now** is also
available. After flushing it **auto-advances** to the next
queued recipe. On completion it records the run in `manifest.json`
(`reactor` key), writes `reactor/feedback/<recipe_id>.done.json`, and emits a
`reactor.run_complete` bus event so the optimizer can predict the next recipe.

## Hardware swap

`backend=mock` (default) uses in-memory pumps so everything runs with no
hardware. Set `SWAXS_REACTOR_BACKEND=real` to use the vendored `Py_P_Pump` SDK
(`src/reactor/drivers/`). The real call points are marked `⟵ REAL DRIVER` in
`src/reactor/hardware.py`. Pumps are assumed **pre-tared** (the SDK tare is
interactive and is done from a console). All tunables live in `reactor/config.yml`.
