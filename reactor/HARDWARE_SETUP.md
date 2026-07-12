# Flow Synthesis — Hardware Setup & Testing Guide

How to test the pump-control app in **mock** mode, then connect it to the real
5-pump Dolomite Mitos rig and run it safely.

The app has two backends, chosen by the `SWAXS_REACTOR_BACKEND` environment
variable:

| Backend | When | What it drives |
|---|---|---|
| `mock` (default) | development, UI testing, dry runs | in-memory pumps; flows converge so the dashboard & run/flush logic behave realistically |
| `real` | on the instrument | the Dolomite Mitos P-pumps via `src/reactor/drivers/Py_P_Pump.py` |

The single hardware call point is `PumpBank.set_pump_flow(pump, rate)` in
`src/reactor/hardware.py`; the real SDK calls are marked `⟵ REAL DRIVER`.

---

## Part A — Test in MOCK mode first (no hardware)

Always shake the app out in mock mode before touching the rig.

### A1. Install dependencies
```bash
# from the project root
uv pip install flask pyyaml pyserial          # pyserial only needed for real mode
```

### A2. Launch the app
```bash
# standalone
uv run reactor/app.py
#   → http://localhost:5007   (banner shows "backend = mock")

# OR via the hub (recommended) — start the hub, then click ▶ Start on the
# "Flow Synthesis" card:
uv run hub/app.py             # http://localhost:5000
```

### A3. Submit a recipe three ways

**Manual form:** open http://localhost:5007, fill T_reac / F_tot / x_ODE / x_TOP /
x_oley, click **Queue recipe**, then **Start** (or flip **Auto-run** on).

**JSON API** (what the BO/SAXS side will use):
```bash
curl -X POST http://localhost:5007/api/recipe \
  -H "Content-Type: application/json" \
  -d '{"recipe_id":"demo1","T_reac":240,"F_tot":80,
       "x_ODE":0.30,"x_TOP":0.20,"x_oley":0.20,"run_duration":20}'
```

**Watched folder:** drop a `*.json` recipe into `<project>/reactor/recipes/`
(the app moves it to `recipes/done/` after intake):
```bash
mkdir -p reactor/recipes
echo '{"recipe_id":"demo2","T_reac":250,"F_tot":100,
       "x_ODE":0.25,"x_TOP":0.20,"x_oley":0.20,"run_duration":20}' \
  > reactor/recipes/demo2.json
```

> **Note on hard limits:** a recipe is **rejected** if any pump's computed flow is
> nonzero-but-below its `sensor_min` or above its `max_flow` (see `config.yml`).
> With the placeholder mins, low-fraction recipes will be refused — that's
> expected. Use values whose setpoints clear the minimums (e.g. the JSON above)
> for the mock walk-through.

### A4. Watch the lifecycle on the dashboard
You should see the state pill move **idle → arming → running → flushing → ready**:
- *arming* — the mock reactor temperature ramps to `T_reac` and the run waits
  until it is stable (±`tolerance` for `stable_hold` seconds).
- *running* — the 4 reagent pumps show their target/actual flows; `ode_flush` = 0.
- *flushing* — reagent pumps drop to 0, `ode_flush` runs at the config rate.
- *ready* — auto-advances to the next queued recipe, or waits.

### A5. Exercise the controls
- **Stop** ends the run early → flush. **Abort** stops reagents → flush.
- **🛑 Emergency Stop** idles everything immediately; **Reset** returns to idle.
- **Flush now** / **Prime lines** run `ode_flush` on demand when idle.

### A6. Verify the closed-loop signals
- A finished run writes `<project>/reactor/feedback/<recipe_id>.done.json` and
  emits a `reactor.run_complete` event on the hub bus.
- It also records the run in `manifest.json` under the `reactor` key.
- Run-end on **SAXS measurement**: when the viewer's auto-averager emits a
  `file.averaged` event (or you simulate one), a *running* recipe ends
  immediately; the fixed `run.default_duration` is the fallback.

```bash
# confirm feedback was written
cat reactor/feedback/demo1.done.json
```

---

## Part B — Connect the real hardware

### B1. Wire and power up
1. Connect each Mitos P-pump via its Dolomite USB-to-serial cable.
2. Connect the compressed-air supply and the Dolomite **LG16 flow sensor** in
   line with each pump (flow-control mode needs the sensor).
3. Power the pumps and confirm the OS sees the serial devices
   (`/dev/ttyUSB*` on Linux/macOS, `COM*` on Windows).

### B2. Find each pump's serial address
From a Python console in the project (one pump connected at a time is easiest):
```python
import sys; sys.path.insert(0, "src/reactor/drivers")
import Py_P_Pump
Py_P_Pump.find_address(identifier="Dolomite")   # prints e.g. /dev/ttyUSB0
#   or call find_address() and unplug/replug to identify by elimination
```
Linux/macOS permissions (if "Permission denied"):
```bash
sudo chmod 666 /dev/ttyUSB0
```

### B3. Fill in `reactor/config.yml`
For **each** of the 5 pumps set the real values:
```yaml
pumps:
  pd_top_precursor:
    address:   "/dev/ttyUSB0"     # from find_address()
    pump_id:   0                  # 0 = broadcast; set a unique id if on a shared bus
    sensor:    "LG16-1000"        # the installed sensor model
    sensor_min: 30.0              # HARD min — its real LG16 minimum (µL/min)
    max_flow:   1000.0            # HARD max — its real LG16 maximum (µL/min)
  # … repeat for oleylamine, top, ode_dilution, ode_flush …
```
Also review `bounds`, `safety` (T_max, F_tot_max, per_pump_max), `temperature`
(tolerance/timeout) and `flush` (rate/duration). The min/max you enter are the
hard accept/reject limits for every recipe.

### B4. Pre-tare the pumps (one-time, from a console — NOT the web app)
The SDK tare is interactive (it asks you to open the chamber), so the web app
**assumes pumps are already tared**. Do it once before running:
```python
import sys; sys.path.insert(0, "src/reactor/drivers")
import Py_P_Pump
p = Py_P_Pump.P_pump("/dev/ttyUSB0", name="pd_top_precursor", pump_id=0)
p.tare_pump()        # follow the on-screen instructions
p.set_idle()
```
Repeat for each pump.

### B5. Temperature reading — or use timed arming instead
This app is **gate-only** — it never commands the heater. It supports two
arming modes (per recipe, or set the default in `config.yml → arming`):

- **`temperature`** — arming waits for the reactor to reach/hold `T_reac`. The
  reading comes from `TempController.read()` in `src/reactor/hardware.py`, which
  is currently a stub. **Edit `read()` to return your thermocouple / external
  controller value** (the hook is documented inline), otherwise arming sits
  until `temperature.timeout` and aborts.
- **`timed`** — arming just waits a fixed number of seconds, then starts the
  pumps regardless of temperature. **Use this when no temperature reading is
  wired to this machine** (it's the default: `arming.default_mode: timed`,
  `arming.default_wait_s: 120`). In the web form pick *"after a fixed wait"*;
  via JSON add `"arm_mode": "timed"` and optionally `"arm_wait_s": 90`.

So you can run real pumps immediately in timed mode, and switch to the
temperature gate later once `read()` is wired.

### B6. Launch in real mode
```bash
# macOS / Linux
SWAXS_REACTOR_BACKEND=real uv run reactor/app.py
#   banner shows "backend = real"
```

---

## Windows notes (COM ports)

Everything above applies on Windows too; only the serial-port details and the
launch command differ.

1. **Driver + pyserial.** Install the Dolomite USB-to-serial driver (FTDI), then
   `pip install pyserial` in your venv.
2. **Find the ports.** Plug pumps in one at a time and read the assigned port
   from **Device Manager → Ports (COM & LPT)** — e.g. `COM3`, `COM4`. Or from a
   Python console:
   ```python
   import sys; sys.path.insert(0, "src/reactor/drivers")
   import Py_P_Pump
   Py_P_Pump.find_address(identifier="Dolomite")   # prints e.g. COM3
   ```
3. **config.yml addresses** use the COM name (not `/dev/ttyUSB*`):
   ```yaml
   pumps:
     pd_top_precursor:
       address: "COM3"
   ```
   There is **no `chmod`** on Windows — if a port won't open, close any other
   program using it (vendor GUI, PuTTY) and check the COM number.
4. **Launch in real mode (PowerShell):**
   ```powershell
   $env:SWAXS_REACTOR_BACKEND="real"
   python reactor/app.py
   #   banner shows "backend = real"
   ```
   To launch via the hub instead, set `$env:SWAXS_REACTOR_BACKEND="real"` in the
   same PowerShell session *before* `python hub/app.py`, then Start the Flow
   Synthesis card — the hub passes its environment to the app.

| Symptom (Windows) | Fix |
|---|---|
| `could not open port 'COM3'` | wrong COM number, cable unplugged, or the port is held by another program |
| Banner says `backend = mock` | `$env:SWAXS_REACTOR_BACKEND` not set in the same PowerShell session |
| Pump not found by `find_address` | FTDI driver not installed, or check Device Manager for the COM number |

---

## Part C — First real run (safety-first checklist)

Do a controlled bring-up before any autonomous campaign:

1. **E-stop test (dry):** with pumps idle, click **🛑 Emergency Stop** and
   confirm every pump reads idle, then **Reset**.
2. **Single-pump, low-flow check:** temporarily set all `x_*` to 0 so only
   `pd_top_precursor` flows at a modest `F_tot`; submit, **Start**, and confirm
   the dashboard's *actual* flow tracks *target* and there are no leaks/errors.
   Watch the app's console — the SDK prints pump errors (e.g. *"Flow sensor lost
   during flow control"*, *"Target too high"*).
3. **Flush check:** click **Flush now** and confirm only `ode_flush` runs.
4. **Full recipe at low F_tot:** run a complete recipe with a short
   `run_duration`, verify arming→running→flushing→ready and the feedback file.
5. **Ramp up** to real conditions only after the above pass.

Keep the console visible during real runs — pump faults are printed there and a
detected over-limit at runtime trips the emergency stop automatically.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Banner says `backend = mock` on the rig | `SWAXS_REACTOR_BACKEND=real` not set in the launching shell |
| `serial.SerialException: could not open port` | wrong `address` in `config.yml`, cable unplugged, or no permissions (`chmod 666`) |
| Recipe rejected immediately | a pump setpoint is below `sensor_min` or above `max_flow` — check the log message; tune `config.yml` or the recipe |
| Stuck in **arming**, then aborts | temperature never reached `T_reac`; in real mode wire `TempController.read()` (B5); check tolerance/timeout |
| Run never ends on its own | no `file.averaged` event arriving; it will still end at `run.default_duration` (fallback) or on manual **Stop** |
| Pump faults in console | follow the SDK's printed error (supply pressure, sensor lost, target too high/low); re-tare if needed |
| "Permission denied" on `/dev/ttyUSB*` | `sudo chmod 666 /dev/ttyUSBX` (or add your user to the `dialout` group) |

---

## Where things live

- `reactor/app.py` — Flask app, routes, SSE, folder watch, bus wiring (port 5007)
- `reactor/config.yml` — all tunables (pump addresses, hard min/max, bounds, caps, flush, temperature)
- `src/reactor/hardware.py` — `MockPump` / `RealPump` / `TempController` (the `⟵ REAL DRIVER` hooks)
- `src/reactor/recipe.py` — validation + fraction→flow conversion + hard-limit rejection
- `src/reactor/controller.py` — the run/flush state machine
- `src/reactor/drivers/Py_P_Pump.py` — the vendored Dolomite Mitos SDK
