# Pump flow calibration (per-pump, for ODE / non-water fluids)

The Mitos LG16 sensors are water-calibrated and the reactor app bypasses the
Dolomite FCC, so the app must correct water→fluid flow itself. Two ways, per pump:

- **Linear factor** (`calibration_factor`) — one number, live-editable in the app
  (Pump-limits card, `cal ×`). Use when the response is just proportional.
- **Power-law table** (`flowrate_table`) — several `[setpoint, measured]` pairs,
  fitted to `measured ≈ setpoint^a`. Use when the response is nonlinear across the
  range. Config-only (edit + restart). **A table wins over the factor.**

This doc is the procedure to build a table.

---

## What you need
- The reactor app running (Real backend), the pump primed with the real fluid (ODE).
- A way to measure **true** delivered flow at a set point, one of:
  - **Gravimetric:** collect the output for a timed interval, weigh it, divide by
    density and time → µL/min (most reliable), or
  - **FCC reference:** in the Dolomite Flow Control Center with **Hexadecane**
    selected, read the flow it reports (closest preset to ODE).

> Only one program can control a pump at a time. If you use the FCC to read, you
> can't have the reactor app controlling the same pump simultaneously — measure with
> one, then configure the other.

## Step 1 — put the pump in identity mode (so you measure raw response)
In `reactor/config.yml` for that pump, temporarily set:
```yaml
    calibration_factor: 1.0
    # flowrate_table:   (absent / commented out)
```
Restart the reactor app. Now the number you command = the raw instrument setpoint.

## Step 2 — measure setpoint → true-flow pairs
Pick 3–5 setpoints spanning the pump's working range (e.g. for a 1–50 µL/min pump:
10, 20, 30, 40). For each:
1. Command that flow on the pump (app or FCC), let it stabilize (~30–60 s).
2. Measure the **true** delivered flow (gravimetric or FCC-Hexadecane reading).
3. Record the pair `[commanded_setpoint, measured_true]` in µL/min.

Example table you might get:

| commanded (µL/min) | measured true (µL/min) |
|---|---|
| 10 | 8.6 |
| 20 | 18.1 |
| 30 | 27.9 |
| 40 | 38.8 |

## Step 3 — enter the table in config
Add `flowrate_table` under that pump in `reactor/config.yml` (list of
`[setpoint, measured]`, both µL/min, **≥ 2 points**):
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
Repeat per pump (each LG16 differs). Save the file.

## Step 4 — apply it
Restart the reactor app (or flip the backend Mock↔Real toggle). The table is fitted
to a power-law exponent at startup. From now on:
- when you command a **true** flow `Q`, the driver sends `Q^(1/a)` to the pump;
- the pump's raw reading is reported back as `raw^a`;
so the app's setpoints and live flow are **true fluid µL/min**.

## Step 5 — verify
1. Command a flow that was **not** one of your calibration points (e.g. 25 µL/min).
2. Measure the true delivered flow (gravimetric / FCC).
3. It should match the commanded value within your tolerance. If it's off, add that
   point to the table and restart, or add more points across the range.

---

## Notes
- **Table vs factor:** if a `flowrate_table` is present it is used and
  `calibration_factor` is ignored. Remove/comment the table to fall back to the
  linear factor.
- **Units:** all table values are µL/min. The truncation q-unit (Å⁻¹ vs nm⁻¹) is a
  separate setting and unrelated to flow calibration.
- **Where it's applied:** `src/reactor/hardware.py` (`_fit_flow_power`, `_to_setpt`,
  `_to_true`); the flow-OK check compares the true flow to the true setpoint.
- The table is config-only today. If you want to enter/adjust it live in the app
  (like the `cal ×` factor), ask and I'll add a table editor to the Pump-limits card.
