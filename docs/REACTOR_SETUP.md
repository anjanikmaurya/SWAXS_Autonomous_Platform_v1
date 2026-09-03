# Reactor app — software setup

How to install, configure, and run the Flow-Synthesis reactor app (port **5108**).
For the physical rig (pumps, temperature, beamline components + wiring) see
`docs/REACTOR_HARDWARE_SETUP.md`; for "where is what" while troubleshooting,
`docs/REACTOR_MAP.md`; for the beamline bench tests, `tools/BEAMLINE_TESTING.md`.

---

## 1. Prerequisites

- **Python 3.10 or newer** (3.11 / 3.12 recommended) — see
  [QUICKSTART.md](../QUICKSTART.md).
- **git**.
- For **real hardware only** (see §5): USB access to the Mitos pumps and network
  access to the SPEC bServer (default `http://127.0.0.1:18085`).

The app runs on Windows, macOS, or Linux. Mock mode needs no hardware at all.

## 2. Get the code and a Python environment

```bash
git clone https://github.com/anjanikmaurya/SWAXS_Autonomous_Platform_v1.git
cd SWAXS_Autonomous_Platform_v1

# conda (used on the beamtime PC)
conda create -n swaxs python=3.12 -y
conda activate swaxs
# — or venv —
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
```

## 3. Install dependencies

```bash
pip install -r requirements-core.txt
```

(Use `requirements-core.txt`, not `requirements.txt` — see
[QUICKSTART.md](../QUICKSTART.md). `requirements.txt` is a `pip freeze` of a Mac
and fails to install on Windows.)

**Extra packages for REAL hardware** (not in `requirements-core.txt`, imported
lazily so Mock mode works without them):

```bash
pip install -r requirements-hardware.txt   # pyserial (pumps) + pyepics (temperature)
```

Mock mode (default) needs neither — good for developing the loop off the rig.

## 4. Backends: Mock vs Real

The reactor talks to two hardware layers — **pumps** and **beamline** — and one
toggle covers both.

- **Startup default:** environment variable `SWAXS_REACTOR_BACKEND` = `mock`
  (default) or `real`. e.g. `SWAXS_REACTOR_BACKEND=real`.
- **At runtime:** the Mock/Real pill in the app UI (`/api/backend`) switches both
  pumps and beamline live and re-wires everything.

Start in **mock** to learn the UI; switch to **real** only on the rig.

## 5. Configure `reactor/config.yml`

Everything rig-specific lives here; no code changes needed. Key sections:

- **pumps** — one entry per Mitos pump. Matched by `serial` first (portable across
  PCs — COM numbers can differ), `address` (COM/tty) is a fallback. `sensor_min` /
  `max_flow` are HARD limits from the installed LG16 flow sensor: a nonzero
  setpoint outside that window rejects the whole recipe. **The shipped config sets
  `sensor_min: 0.0` on all five pumps, which disables the lower half of that
  check** — set each pump's real sensor minimum or a below-range setpoint is
  accepted and the delivered flow is unknown. Map serials↔ports with
  `tools/map_pumps.py`.
- **bounds** / **safety** — recipe validation ranges and absolute caps (`T_max`,
  `F_tot_max`, `per_pump_max`, `max_pressure`). A breach is rejected at submit and
  trips E-stop at runtime.
- **temperature** — `tolerance`, `read_interval_s`, and `cooldown_c` (°C the reactor
  is set to the moment a synthesis run ends; `null` = leave as-is).
- **arming** — `default_mode` (`temperature` or `timed` — **those are the only two
  accepted values**; `src/reactor/recipe.py` rejects anything else, so a condition
  file carrying `arm_mode: ramp` fails at intake and the queue stalls) and
  `default_wait_s` (the timed wait when a recipe omits `arm_wait_s`). The shipped
  default is `timed`, 120 s — the temperature gate is OFF as delivered.
- **flush** — flush pump rate, duration, and `pump` (which pump flushes; the
  shipped value is `ode_dilution`, a reagent pump, not `ode_flush`).
- **spec** (beamline) — the block you validated on the rig:
  - `backend`, `base_url` (bServer, `http://127.0.0.1:18085/SIS/`), `enabled`
    (false = run the loop with no 2D collection at all)
  - `read_source` — `"epics"` (caget; keeps reading during a collection) or
    `"spec"` (counters; shipped default). See §9.
  - `epics_pvs` (temperature / i0 / bstop PV names), `epics_ca_addr_list` +
    `epics_ca_auto_addr_list` (CA gateway) — used only by `read_source: "epics"`
  - `read_during_collect` — `true` = keep polling SPEC counters during an
    acquisition (only if the bServer allows counter reads mid-scan)
  - `temp_counter: CTEMP`, `bstop_counter`, `i0_counter` — `read_source: "spec"` only
  - `read_refresh_cmd: "ct 0.1"` (makes the live plot live; blank = stale last-count)
  - `set_temp_cmd: "csettemp {T}"`, `open_shutter_cmd`, `close_shutter_cmd`
  - `macro_file: reactor/macros/Singlesnapshot.flat.template.txt`, `collect_mode: commands`
  - `data_dir` — the SPEC (Linux) folder shots are saved to = the pipeline's 2D base
  - `data_dir_from_hub` + `hub_path_map` — follow the hub's project folder into
    `data_dir`, translating the Windows prefix to the Linux one SPEC writes to
  - `mock_data_dir` — where the mock backend writes instead (blank = the hub folder)
  - `background_when` — `"before"` (shipped) or `"after"`; see §7 step 3
  - `spec_lead_s`, `exposure_s`, `frames`, `sample_tag`, `bkg_tag`
  - `simulator.*` — mock-only synthetic 2D data generator (poni/mask, brightness,
    `truth.*` ground truth). Cannot produce data on the real backend.

## 6. Run

**Via the hub (recommended):**
```bash
python hub/app.py          # http://localhost:5100 — launch "Flow Synthesis" from the hub
```
The hub passes the selected project folder to the reactor and manages its process
(and frees the port on hub exit).

**Standalone (development):**
```bash
python reactor/app.py      # http://localhost:5108
# real hardware:
SWAXS_REACTOR_BACKEND=real python reactor/app.py
```
The hub launches apps with the plain interpreter so they share the installed
environment — use `python` here too.

## 7. First-run checklist

1. `pip install -r requirements-core.txt` succeeds; add `requirements-hardware.txt` for real.
2. App opens at `http://localhost:5108`, backend pill shows the expected mode.
3. **Mock:** submit a recipe, Start — with the shipped `background_when: "before"`
   the sequence is **flush → background shot → arm → run → sample shot → flush**.
   The first thing you see is a flush (20 min at the shipped `flush.duration`), not
   arming; that is correct, the app has not hung. Live plot animates throughout.
4. **Real (on the rig):** bench-test the beamline in isolation FIRST with the app
   stopped — `tools/beamline_read_test.py`, `…_epics_test.py`, `…_temp_test.py`,
   `…_shutter_test.py`, `…_collect_test.py` (see `tools/BEAMLINE_TESTING.md`), then
   run one **📷 Collect now** from the app before an autonomous run.
5. Confirm the reduction pipeline sees new `.raw` under `<data_dir>/2D/SAXS`.

## 8. Ports

hub 5100 · reduction 5102 · average 5103 · background 5104 · analysis 5106 ·
assistant 5109 · quality 5105 · **reactor 5108** · analyzer 5107 ·
calibration 5101.
Closing an app frees its port; closing the hub stops all its sub-apps.

## 9. Safety notes (real mode)

- Stop / E-stop act on **pumps only** and never interrupt an in-progress X-ray
  collection.
- **E-stop also turns auto-run OFF** (`controller.estop`), so the folder watcher
  cannot restart the rig into an unresolved fault. After clearing the fault and
  pressing **Reset** you must re-arm **▶ Run autonomously** deliberately —
  otherwise queued conditions sit there and nothing runs.
- **The over-temperature interlock goes blind during every acquisition when
  `spec.read_source: "spec"`** (the shipped value). A collection holds the SPEC
  lock and counter reads are non-blocking, so temperature / i0 / bstop stop
  updating for the whole shot — `exposure_s: 10 × frames: 10` = **100 s per
  acquisition with no fresh temperature**, and the live plot freezes for exactly
  that window. The app logs it once per run. Fixes, in order of preference:
  `read_source: "epics"` (caget, independent of SPEC — verify the PVs with
  `tools/beamline_epics_test.py`, needs `pyepics` from
  `requirements-hardware.txt`), or `read_during_collect: true` if the bServer
  tolerates counter reads mid-scan.
- With `read_source: "spec"`, while the reactor app is open in Real mode it
  **holds SPEC remote control** (for the `ct` live-plot refresh); close the app or
  switch to Mock to hand control back. `read_source: "epics"` never takes SPEC
  control for reads.
- Run **one SPEC client at a time** — don't run the bench tools and the app against
  Real simultaneously.
- Full safety review: `docs/audits/BEAMLINE_SAFETY_AUDIT.md`; readiness checklist:
  `docs/audits/PRE_BEAMTIME_READINESS.md`.
