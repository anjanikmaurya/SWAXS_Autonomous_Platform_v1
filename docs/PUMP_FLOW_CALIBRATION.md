# Pump flow calibration (per pump)

The Mitos LG16 sensors are water-calibrated and the reactor app bypasses the Dolomite
FCC, so the app corrects water→fluid flow itself. There are **two ways**, per pump:

| Method | What | Where | Model | Use when |
|---|---|---|---|---|
| **A — `cal ×` factor** | one number | **live in the app** (Pump-limits card) | linear: `true = raw × cf` | response is ~proportional; quick |
| **B — flowrate table** | `[setpoint, measured]` pairs | `reactor/config.yml` (edit + restart) | power law: `true = raw^a` | response is nonlinear across the range |

Precedence: **if a `flowrate_table` is present it wins and `cal ×` is ignored.** Both
default to identity (`cal × = 1.0`, no table) = no correction.

This doc covers the how-to for both, plus how to measure the numbers.

---

## Measuring the "true" flow (needed for either method)

Command a flow and measure what's actually delivered, one of:
- **Gravimetric (most reliable):** collect the output for a timed interval, weigh it,
  `true µL/min = mass_g / density_g_per_µL / minutes`.
- **FCC reference:** in the Dolomite Flow Control Center with **Hexadecane** selected
  (closest preset to ODE), read the flow it reports.

> Only one program controls a pump at a time. If you read with the FCC, the reactor
> app can't control that pump at the same moment — measure with one, configure the other.

---

## Method A — `cal ×` factor (live, in the app)

Best for a quick proportional correction; no restart.

1. In the reactor app, open the **Pump flow limits & calibration** card.
2. For the pump, at one fixed setpoint, get two numbers **at the same pressure/flow**:
   - `app_water` = the flow the app shows (water units, `cal ×` = 1.0), and
   - `true` = the measured true flow (gravimetric or FCC-Hexadecane).
3. Compute `cal × = true / app_water`.
4. Enter it in that pump's **`cal ×`** field and click **Apply limits**.
   - Applies immediately on the serial link and is saved to the project
     (`reactor_limits.json`), so it persists across restarts.
5. **Verify** (below).

Example: app shows 40 µL/min, gravimetric true = 34 µL/min → `cal × = 34/40 = 0.85`.

---

## Method B — flowrate table (config, power-law)

Best when the setpoint→true-flow curve bends across the range.

1. **Identity mode first.** In `reactor/config.yml` for that pump set
   `calibration_factor: 1.0` and make sure `flowrate_table` is absent/commented, then
   restart the app. Now the number you command = the raw instrument setpoint.
2. **Measure 3–5 pairs** spanning the pump's range (e.g. 10, 20, 30, 40 µL/min). For
   each: command it, let it stabilize (~30–60 s), measure the true flow, record
   `[commanded, measured]` (both µL/min).

   | commanded | measured true |
   |---|---|
   | 10 | 8.6 |
   | 20 | 18.1 |
   | 30 | 27.9 |
   | 40 | 38.8 |

3. **Enter the table** under that pump in `reactor/config.yml` (≥ 2 points):
   ```yaml
   pumps:
     ode_dilution:
       serial: "4902276A"
       sensor: "LG16-0480"
       sensor_min: 0.0
       max_flow: 50.0
       max_pressure: 10000.0
       calibration_factor: 1.0        # ignored while a table is present
       flowrate_table:
         - [10, 8.6]
         - [20, 18.1]
         - [30, 27.9]
         - [40, 38.8]
   ```
   Repeat per pump (each LG16 differs).
4. **Apply:** restart the reactor app (or flip the Mock↔Real backend toggle). The
   table is fitted to a power-law exponent at startup.
5. **Verify** (below).

After this, when you command a **true** flow `Q` the driver sends `Q^(1/a)` to the
pump and reports `raw^a` back — so the app's setpoints and live flow are true fluid
µL/min.

---

## Verify (both methods)

1. Command a flow that was **not** one of your calibration points (e.g. 25 µL/min).
2. Measure the true delivered flow.
3. It should match the commanded value within tolerance. If off:
   - Method A: recompute `cal ×` at a representative flow.
   - Method B: add that point to the table and restart, or add more points.

---

## Notes
- Units: all flows are µL/min. The subtraction q-unit (Å⁻¹ vs nm⁻¹) is unrelated to
  flow calibration.
- Where applied: `src/reactor/hardware.py` — `_fit_flow_power`, `_to_setpt`, `_to_true`.
  The flow-OK safety check compares the **true** flow to the **true** setpoint.
- To fall back from table → factor, remove/comment the `flowrate_table`.
- `cal ×` is live in the app; the table is config + restart. If you want the table
  editable live too (in the Pump-limits card), ask and it can be added.
