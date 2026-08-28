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
                 ⚠ the dedicated flush line is the DESIGN, not the current rig:
                 config.yml ships flush.pump = ode_dilution (see §3).

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

| Pump (`config.yml`) | Sensor | Sensor range (µL/min) | Shipped `sensor_min`/`max_flow` | Role |
|---|---|---|---|---|
| `pd_top_precursor` | LG16-1000 | 30–1000 | **0.0** / 1000 | Pd precursor (high flow) |
| `ode_flush` | LG16-1000 | 30–1000 | **0.0** / 1000 | ODE flush/carrier |
| `oleylamine` | LG16-0480 | 1–50 | **0.0** / 50 | reagent (low flow) |
| `top` | LG16-0480 | 1–50 | **0.0** / 50 | reagent (low flow) |
| `ode_dilution` | LG16-0480 | 1–50 | **0.0** / 50 | dilution (low flow) |

The "sensor range" column is the physical sensor. The `config.yml` column is what
the software actually enforces — and **`sensor_min: 0.0` on every pump means the
lower limit is switched off in the shipped config** (`reactor/config.yml` lines
23, 36, 49, 61, 74). Until you set the real minima, a recipe whose computed
setpoint is, say, 0.3 µL/min on an LG16-0480 is **accepted and sent to hardware**,
below the sensor's usable range, and the delivered flow is unknown while the app
displays a number as if it were measured. Set them before you trust a composition.

Setup:

1. Plug each pump's USB; note it appears as a COM (Windows) / `/dev/ttyUSB*` (Linux).
2. **Map serial → port:** run `tools/map_pumps.py`. Pumps are matched by their fixed
   `serial` first (portable across PCs; COM numbers can change), with `address` as a
   fallback. Put the confirmed `serial` for each pump in `config.yml`.
3. Set each pump's `sensor_min` / `max_flow` to the installed sensor's real limits.
   A nonzero setpoint below `sensor_min` or above `max_flow` rejects the whole
   recipe, never clamps (`src/reactor/recipe.py recipe_to_setpoints`) — but with
   the shipped `sensor_min: 0.0` **the below-minimum rejection does nothing**.
   `max_pressure` (mbar) is the pump's ceiling. **⚠ do this before beamtime.**
4. Prime lines and check for leaks before any beam.

Software: `src/reactor/hardware.py` (`RealPump`, `PumpBank`) via
`src/reactor/drivers/Py_P_Pump.py` (needs `pyserial`). Diagnostics: `tools/list_pumps.py`,
`tools/pump_diag.py`, `tools/pump_flow_test.py`, `tools/tare_probe.py`.

### Real-pump bring-up (serial protocol, ports, tare)

The driver (`src/reactor/drivers/Py_P_Pump.py`) speaks the Mitos ASCII protocol
verified on the SSRL rig: **57600 baud, 8N1, no handshaking**, newline-terminated
commands — `s` (status), `A1`/`A0` (enter/leave remote control), `F<pl/s>` (flow
setpoint, **picolitres per second** on the wire — the platform works in µL/min and
`ulmin_to_pls` converts, `Py_P_Pump.py:13` and `set_flow`), `P<mbar>` / `P0`
(target pressure / idle-vent), `R0` (pressure tare), `R1` (flow-sensor tare). It
enters remote control automatically and polls status to hold it; a pump **drops out
of remote control after ~30 s** without a command. **One pump per COM/tty port**
(`pump_id` is unused).

**Find each port** (one pump connected at a time is easiest):
`Py_P_Pump.find_address(identifier="Dolomite")` prints the port (`/dev/ttyUSB0`, or
`COM3` on Windows). `tools/list_pumps.py` lists candidate ports and
`tools/pump_diag.py <port>` sanity-checks a single pump; `tools/map_pumps.py` then
records the confirmed `serial` per pump in `config.yml`.

**Tare (one-time, before a run).** There are **two** tares: `R0` zeroes the
pressure reading (air supply disconnected) and `R1` zeroes the flow sensor (no
flow through it) — `Py_P_Pump.tare` / `tare_flow`, exposed as
`PumpBank.tare(name, kind="pressure"|"flow"|"both")`.

Do it **from the reactor app**: the **🔧 Tare pumps** card has per-pump
**Pressure / Flow / Both** buttons, which POST `/api/tare` →
`controller.tare_pump` and are accepted only in idle / ready / estop, so a tare
can never land mid-run. One pump at a time; wait for it to return to IDLE.

Only use the console tools (`tools/tare_probe.py`, or `P_pump(...).tare()` then
`set_idle()`) when the app is **not** running — the app holds the COM port and the
second opener gets a port conflict.

**Windows COM notes.** `address` uses the COM name (e.g. `COM3`), read from Device
Manager → Ports (COM & LPT). If a port won't open it is usually the **wrong COM
number** or the **port is held by another program** (vendor GUI, PuTTY) — close it.
There is **no `chmod`** on Windows; on Linux/macOS use `sudo chmod 666 /dev/ttyUSB*`
for a "Permission denied" error.

### Solvent / liquid calibration (ODE vs water)

The LG16 sensors are **water-calibrated** and the reactor app bypasses Dolomite's
Flow Control Center, so under the app every pump reports water-equivalent flow
until you calibrate it. Both correction methods (`cal ×` factor, live in the app;
or a per-pump `flowrate_table` in `config.yml`), how to measure the numbers, and
how to verify them are in **`docs/PUMP_FLOW_CALIBRATION.md`**.

## 2. Temperature (heated reactor)

The reactor coil/cell is heated; temperature is **commanded and read through SPEC**,
not over a separate cable to the control PC:

- **Set:** `csettemp <T>` (config `spec.set_temp_cmd`) — sent when a run arms and for
  the end-of-run cooldown (`temperature.cooldown_c`).
- **Read:** either path, chosen by `spec.read_source`:
  - `"spec"` (shipped): the `CTEMP` counter (`spec.temp_counter`), refreshed by
    `ct 0.1` (`spec.read_refresh_cmd`) before each read.
  - `"epics"` (recommended): `caget` on `spec.epics_pvs.temperature` — no SPEC
    remote control, no `ct`, and it keeps reading during a data collection.
- There is **no independent heater control in the app** — it relies on the beamline's
  temperature controller behind `csettemp`/`CTEMP`. **⚠ confirm** the controller
  (e.g. Linkam/hot-stage) is in SPEC/remote mode and that `CTEMP` reads the sample.

**⚠ On `read_source: "spec"` the temperature reading — and with it the
over-temperature interlock — goes dark for the whole of every 2D acquisition.** A
collection holds the SPEC lock and counter reads are non-blocking by design
(`src/beamline/driver.py read_state`), so nothing refreshes from the first frame
to the last: at the shipped `exposure_s: 10 × frames: 10` that is **100 s per
acquisition with no fresh temperature**, repeated every run. The `ct 0.1` refresh
described above applies only between collections. Fix it by reading from EPICS:
set `spec.read_source: "epics"`, fill in `spec.epics_pvs` (⚠ confirm the PV names
with the beamline engineer) and `spec.epics_ca_addr_list` (the CA gateway IP),
install `pyepics` (`pip install -r requirements-hardware.txt`), and verify with
`python tools/beamline_epics_test.py` before you rely on it. If EPICS is
unavailable, `spec.read_during_collect: true` polls the SPEC counters during a
collection instead — only use it if the bServer tolerates counter reads mid-scan.

Arming (`config.yml arming`): `temperature` (wait to reach and hold T) or `timed`
(fixed wait, `default_wait_s`). Those are the only two modes — any other
`arm_mode` (e.g. the old `ramp`) is rejected by `src/reactor/recipe.py` at intake,
which stalls the queue during an unattended campaign. The shipped default is
`timed`, 120 s: **as delivered the pumps start on a timer, not on temperature.**

## 3. Flow cell in the beam

The heated flow cell (capillary/jet) sits at the sample position so the reacting
stream is measured in situ. **⚠ confirm** cell type, path length, and window material
(affects background/transmission). Reagents flow during the run; a flush clears the
cell for the background measurement.

**Which pump flushes:** `config.yml flush.pump` ships as **`ode_dilution`** — a
reagent pump on the mixer, not the dedicated `ode_flush` line ("Default is
ode_dilution (same ODE) while ode_flush is down; switch to ode_flush once fixed").
So the shipped rig flushes with the same ODE through the reagent path, at a rate
capped by that pump's own `max_flow`: the 50 µL/min limit auto-clamps the flush
rate (`controller._enter_flush` logs "clamped … flush will take longer"). Switch
`flush.pump` to `ode_flush` once the dedicated line works, and re-check
`flush.duration` for the rate you end up with.

With `spec.background_when: "before"` (shipped) the flush + background happen
**before** the synthesis, on a clean capillary — see §4 and
`docs/REACTOR_MAP.md`.

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

Counters (`i0`, `bstop`, `CTEMP`) update only on a **count** — with
`read_source: "spec"` the app issues `ct 0.1` before each read
(`read_refresh_cmd`). `ct` obeys `sauto`; run `sauto off` if you don't want
counting to open the shutter during a ramp. **This refresh is skipped for the
duration of a collection, so temperature / i0 / bstop and the live plot freeze for
every acquisition** (see §2 — the mitigation is `read_source: "epics"`).

**EPICS alternative (recommended).** With `spec.read_source: "epics"` the three
live monitors are read by channel access instead: `spec.epics_pvs` (temperature,
i0, bstop — ⚠ confirm names), `spec.epics_ca_addr_list` = the BL1-5 IOC/CA gateway
`ip:port`. No SPEC remote control is taken for reads, no `ct` fires (so no shutter
side-effect), and the readings continue during a collection. Needs `pyepics` on
the control PC; verify with `python tools/beamline_epics_test.py` before beamtime.
Temperature setting and collection still go through SPEC either way.

Collection is your `Singlesnapshot` macro streamed as SPEC commands
(`collect_mode: commands`, `macro_file: …flat.template.txt` — the **flat** macro:
plain action commands only, no `sample =` assignments and no `eval(sprintf)`,
which do not run reliably through `execute_command`): `newfile` → `sopen` →
`loopscan frames exposure` → `sclose`, saving `.raw` under `spec.data_dir/2D/SAXS`.

Ordering within a run (`spec.background_when: "before"`, the shipped value):
flush → **background** shot on the clean cell → arm → synthesis → **sample** shot
→ post-run flush. Both shots share one `recipe_id`, which is how the background app
pairs them.

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

Hardware only — the beamline link (reads, temperature, shutter, one collection) has
its own runbook: **`tools/BEAMLINE_TESTING.md`**. Do that first, with the reactor
app stopped, then come back here.

1. All 5 pumps enumerated; `tools/map_pumps.py` matches every `serial`; lines primed,
   no leaks.
2. `sensor_min` / `max_flow` / `max_pressure` set to the **real** sensors — the
   shipped `sensor_min: 0.0` leaves the low-flow rejection disabled (§1). **⚠**
3. Pumps tared (Pressure + Flow, per pump) and flow-calibrated if you need true
   ODE flow — `docs/PUMP_FLOW_CALIBRATION.md`.
4. Temperature controller in remote/SPEC mode and reading the sample.
5. `flush.pump` set to the pump actually plumbed for flushing, and `flush.duration`
   sane for its clamped rate (§3).
6. Detectors calibrated — `poni` files + masks present for the reduction step.
7. Beamline runbook passed (`tools/BEAMLINE_TESTING.md`), including `read_source`
   decided and verified.
8. Then: reactor app → **Real** → one **📷 Collect now** → confirm the pipeline sees it.

Safety review: `docs/audits/BEAMLINE_SAFETY_AUDIT.md`. Readiness:
`docs/audits/PRE_BEAMTIME_READINESS.md`. Bench runbook: `tools/BEAMLINE_TESTING.md`.
