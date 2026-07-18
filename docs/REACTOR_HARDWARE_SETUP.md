# Reactor hardware setup — fluidics, temperature & beamline

Physical setup for the autonomous flow-synthesis rig and how each component maps to
the software/config. Software install is in `docs/REACTOR_SETUP.md`; code locations
in `docs/REACTOR_MAP.md`. Items marked **⚠ confirm on rig** depend on your exact
hardware — verify before beamtime.

---

## System overview

```
 reagent pumps ─┐
 (Mitos + LG16) │  PTFE tubing
   ode_dilution ┤
   top          ├──► mixer/tee ──► heated reactor coil ──► FLOW CELL ──► waste
   oleylamine   ┤                    (temp: CTEMP)         (in X-ray beam)
   pd_top_precu ┘
   ode_flush ───────────────────────────────────────────►(flush line)

 X-ray:  source ─► [shutter] ─► [i0 monitor] ─► FLOW CELL ─► [beamstop=bstop]
                                                    │
                                        scattered X-rays ─► SAXS + WAXS detectors

 Control:  Control PC (reactor app + pumps over USB/serial)
           Control PC ─HTTP:18085─► bServer ─► SPEC ─► shutter / counters / detectors
           Detectors save .raw ─► /msd_data/.../2D/{SAXS,WAXS}  (= X:\ on the PC)
```

---

## 1. Pumps & flow sensors (fluidics)

Five Mitos pressure pumps, each with a Dolomite **LG16** inline flow sensor, connect
to the **control PC over USB/serial** (FTDI → COM/tty).

| Pump (`config.yml`) | Sensor | Range (µL/min) | Role |
|---|---|---|---|
| `pd_top_precursor` | LG16-1000 | 30–1000 | Pd precursor (high flow) |
| `ode_flush` | LG16-1000 | 30–1000 | ODE flush/carrier |
| `oleylamine` | LG16-0480 | 1–50 | reagent (low flow) |
| `top` | LG16-0480 | 1–50 | reagent (low flow) |
| `ode_dilution` | LG16-0480 | 1–50 | dilution (low flow) |

Setup:

1. Plug each pump's USB; note it appears as a COM (Windows) / `/dev/ttyUSB*` (Linux).
2. **Map serial → port:** run `tools/map_pumps.py`. Pumps are matched by their fixed
   `serial` first (portable across PCs; COM numbers can change), with `address` as a
   fallback. Put the confirmed `serial` for each pump in `config.yml`.
3. Set each pump's `sensor_min` / `max_flow` to the installed sensor's real limits —
   these are HARD limits (a setpoint below `sensor_min` or above `max_flow` is
   rejected, never clamped). `max_pressure` (mbar) is the pump's ceiling. **⚠ confirm.**
4. Prime lines and check for leaks before any beam.

Software: `src/reactor/hardware.py` (`RealPump`, `PumpBank`) via
`src/reactor/drivers/Py_P_Pump.py` (needs `pyserial`). Diagnostics: `tools/list_pumps.py`,
`tools/pump_diag.py`, `tools/pump_flow_test.py`, `tools/tare_probe.py`.

### Real-pump bring-up (serial protocol, ports, tare)

The driver (`src/reactor/drivers/Py_P_Pump.py`) speaks the Mitos ASCII protocol
verified on the SSRL rig: **57600 baud, 8N1, no handshaking**, newline-terminated
commands — `s` (status), `A1`/`A0` (enter/leave remote control), `F<µL/min>` (flow
setpoint), `P0` (idle), `R0` (tare). It enters remote control automatically and
polls status to hold it; a pump **drops out of remote control after ~30 s** without
a command. **One pump per COM/tty port** (`pump_id` is unused).

**Find each port** (one pump connected at a time is easiest):
`Py_P_Pump.find_address(identifier="Dolomite")` prints the port (`/dev/ttyUSB0`, or
`COM3` on Windows). `tools/list_pumps.py` lists candidate ports and
`tools/pump_diag.py <port>` sanity-checks a single pump; `tools/map_pumps.py` then
records the confirmed `serial` per pump in `config.yml`.

**Tare (one-time, before a run).** Tare each pump with the chamber open / air
supply disconnected per Dolomite's procedure. The command is non-interactive
(`R0`) and the web app assumes pumps are already tared, so run it from a console
(`tools/tare_probe.py`, or `P_pump(...).tare()` then `set_idle()`), one pump at a
time, and wait for it to return to IDLE.

**Windows COM notes.** `address` uses the COM name (e.g. `COM3`), read from Device
Manager → Ports (COM & LPT). If a port won't open it is usually the **wrong COM
number** or the **port is held by another program** (vendor GUI, PuTTY) — close it.
There is **no `chmod`** on Windows; on Linux/macOS use `sudo chmod 666 /dev/ttyUSB*`
for a "Permission denied" error.

### Solvent / liquid calibration (ODE vs water)

The Mitos flow sensors are **water-calibrated**; the per-liquid correction normally
lives in Dolomite's **Flow Control Center (FCC)** GUI (Info/Settings → *Fluid*:
Water, Hexadecane, FC-40, Novec 7500, Mineral oil — closest to **ODE** is
**Hexadecane**). There is **no serial command** to set the liquid, and only one host
can hold the pump at a time — so the FCC selection does **not** carry over when the
reactor app drives the pumps. Under the app the sensor reports water-equivalent flow.

To deliver true ODE flow while bypassing the FCC, set a per-pump
`calibration_factor` in `config.yml` (`cf = true_flow ÷ app_water_reading` at the
same pressure). The driver commands the pump in water units (`target/cf`) and
reports `actual = raw × cf`, so the app's setpoints/readouts are true fluid µL/min.
Default `1.0` (no correction). Get the number by comparing the FCC reading (with
Hexadecane selected) to the app's water reading at one fixed pressure.

## 2. Temperature (heated reactor)

The reactor coil/cell is heated; temperature is **commanded and read through SPEC**,
not over a separate cable to the control PC:

- **Set:** `csettemp <T>` (config `spec.set_temp_cmd`) — sent when a run arms and for
  the end-of-run cooldown (`temperature.cooldown_c`).
- **Read:** the `CTEMP` counter (config `spec.temp_counter`), refreshed by `ct 0.1`
  (`spec.read_refresh_cmd`) and shown on the live plot.
- There is **no independent heater control in the app** — it relies on the beamline's
  temperature controller behind `csettemp`/`CTEMP`. **⚠ confirm** the controller
  (e.g. Linkam/hot-stage) is in SPEC/remote mode and that `CTEMP` reads the sample.

Arming (`config.yml arming`): `temperature` (wait to reach T), `timed`, or `ramp`
(wait = (T_final − 25 °C)/rate).

## 3. Flow cell in the beam

The heated flow cell (capillary/jet) sits at the sample position so the reacting
stream is measured in situ. **⚠ confirm** cell type, path length, and window material
(affects background/transmission). Reagents flow during the run; the flush line
(`ode_flush`) clears the cell for the background measurement.

## 4. Beamline components (SSRL BL1-5, via SPEC/bServer)

| Component | What it is | Software handle | Config |
|---|---|---|---|
| Fast shutter | opens/closes the beam onto the sample | `sopen` / `sclose` | `spec.open_shutter_cmd` / `close_shutter_cmd` |
| I₀ monitor | incident-flux ion chamber (upstream) | `i0` counter | `spec.i0_counter` |
| Beamstop diode | transmitted-beam intensity (downstream) | `bstop` counter | `spec.bstop_counter` |
| Temperature | sample/stage temperature | `CTEMP` counter + `csettemp` | `spec.temp_counter` |
| SAXS detector | 2D small-angle detector (~1043×981) | `loopscan` (via macro) | `poni_files.saxs`, `detector_shapes.saxs` |
| WAXS detector | 2D wide-angle detector (~195×487) | `loopscan` (via macro) | `poni_files.waxs`, `detector_shapes.waxs` |
| SPEC host | runs SPEC; owns detectors/counters/shutter | bServer `execute_command` | — |
| bServer | HTTP bridge to SPEC (`pySSRL-bServer`) | `base_url` | `spec.base_url` (`…:18085/SIS/`) |

Counters (`i0`, `bstop`, `CTEMP`) update only on a **count** — the app issues `ct 0.1`
before each read (`read_refresh_cmd`) so the live plot is live. `ct` obeys `sauto`;
run `sauto off` if you don't want counting to open the shutter during a ramp.

Collection is your `Singlesnapshot` macro streamed as SPEC commands
(`collect_mode: commands`, `macro_file: …flat.template.txt`): `newfile` → `sopen` →
`loopscan frames exposure` → `sclose`, saving `.raw` under `spec.data_dir/2D/SAXS`.

## 5. Machines & data path (topology)

- **Control PC** — runs the hub + reactor app, USB to the pumps, HTTP to the bServer.
- **bServer** — `pySSRL-bServer` on the control PC (`127.0.0.1:18085`); relays to SPEC.
- **SPEC host** — runs SPEC, drives detectors/counters/shutter, writes detector files
  to its own filesystem (Linux `/msd_data/...`). SPEC does its own `cd`/`u mkdir`.
- **Data mount** — the SPEC `/msd_data/checkout/bl1-5/...` folder is the PC's `X:\bl1-5\...`
  mount; the reduction pipeline reads the `.raw` back through it. **⚠ confirm** the
  mapping (`spec.data_dir` uses the **Linux** path SPEC writes to).

Because collection is streamed as commands, the control PC does **not** need write
access to `/msd_data` for collection — only the pipeline (reading) uses the `X:\` mount.

## 6. Pre-run hardware checklist

1. All 5 pumps enumerated; `tools/map_pumps.py` matches every `serial`; lines primed,
   no leaks; `sensor_min`/`max_flow`/`max_pressure` set to the real sensors. **⚠**
2. Temperature controller in remote/SPEC mode; `beamline_read_test.py --refresh "ct 0.1"`
   shows `CTEMP` matching the controller display.
3. Shutter actuates: `beamline_shutter_test.py` opens then closes (watch the hutch).
4. `csettemp` reaches setpoint: `beamline_temp_test.py <T>`.
5. One 2D collection lands `.raw` in `data_dir/2D/SAXS`: `beamline_collect_test.py --fire`.
6. Detectors calibrated — `poni` files + masks present for the reduction step.
7. Then: reactor app → **Real** → one **📷 Collect now** → confirm the pipeline sees it.

Safety review: `docs/audits/BEAMLINE_SAFETY_AUDIT.md`. Readiness:
`docs/audits/PRE_BEAMTIME_READINESS.md`. Bench runbook: `tools/BEAMLINE_TESTING.md`.
