# Reactor app — software setup

How to install, configure, and run the Flow-Synthesis reactor app (port **5007**).
For the physical rig (pumps, temperature, beamline components + wiring) see
`docs/REACTOR_HARDWARE_SETUP.md`; for "where is what" while troubleshooting,
`docs/REACTOR_MAP.md`; for the beamline bench tests, `tools/BEAMLINE_TESTING.md`.

---

## 1. Prerequisites

- **Python 3.12** (matches the pinned `requirements.txt`).
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
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
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
  `max_flow` are HARD limits from the installed LG16 flow sensor. Map serials↔ports
  with `tools/map_pumps.py`.
- **bounds** / **safety** — recipe validation ranges and absolute caps (`T_max`,
  `F_tot_max`, `per_pump_max`, `max_pressure`). A breach is rejected at submit and
  trips E-stop at runtime.
- **temperature** — `tolerance`, `read_interval_s`, and `cooldown_c` (°C the reactor
  is set to the moment a synthesis run ends; `null` = leave as-is).
- **arming** — `default_mode` (temperature / timed / ramp), `default_ramp_rate`.
- **flush** — flush pump rate and duration.
- **spec** (beamline) — the block you validated on the rig:
  - `backend`, `base_url` (bServer, `http://127.0.0.1:18085/SIS/`)
  - `temp_counter: CTEMP`, `bstop_counter`, `i0_counter`
  - `read_refresh_cmd: "ct 0.1"` (makes the live plot live; blank = stale last-count)
  - `set_temp_cmd: "csettemp {T}"`, `open_shutter_cmd`, `close_shutter_cmd`
  - `macro_file: reactor/macros/Singlesnapshot.flat.template.txt`, `collect_mode: commands`
  - `data_dir` — the SPEC (Linux) folder shots are saved to = the pipeline's 2D base
  - `spec_lead_s`, `exposure_s`, `frames`, `sample_tag`, `bkg_tag`

## 6. Run

**Via the hub (recommended):**
```bash
python hub/app.py          # http://localhost:5000 — launch "Flow Synthesis" from the hub
```
The hub passes the selected project folder to the reactor and manages its process
(and frees the port on hub exit).

**Standalone (development):**
```bash
python reactor/app.py      # http://localhost:5007
# real hardware:
SWAXS_REACTOR_BACKEND=real python reactor/app.py
```
(Repo convention elsewhere is `uv run`, but the hub launches apps with the plain
interpreter so they share the installed environment — use `python` here too.)

## 7. First-run checklist

1. `pip install -r requirements-core.txt` succeeds; add `requirements-hardware.txt` for real.
2. App opens at `http://localhost:5007`, backend pill shows the expected mode.
3. **Mock:** submit a recipe, Start — watch it arm → run → flush; live plot animates.
4. **Real (on the rig):** bench-test the beamline in isolation FIRST with the app
   stopped — `tools/beamline_read_test.py`, `…_temp_test.py`, `…_shutter_test.py`,
   `…_collect_test.py` (see `tools/BEAMLINE_TESTING.md`), then run one **📷 Collect
   now** from the app before an autonomous run.
5. Confirm the reduction pipeline sees new `.raw` under `<data_dir>/2D/SAXS`.

## 8. Ports

hub 5000 · reduction 5001 · viewer 5002 · background 5003 · analysis 5004 ·
assistant 5005 · quality 5006 · **reactor 5007** · analyzer 5008.
Closing an app frees its port; closing the hub stops all its sub-apps.

## 9. Safety notes (real mode)

- Stop / E-stop act on **pumps only** and never interrupt an in-progress X-ray
  collection.
- While the reactor app is open in Real mode it **holds SPEC remote control** (for
  the `ct` live-plot refresh); close the app or switch to Mock to hand control back.
- Run **one SPEC client at a time** — don't run the bench tools and the app against
  Real simultaneously.
- Full safety review: `docs/audits/BEAMLINE_SAFETY_AUDIT.md`; readiness checklist:
  `docs/audits/PRE_BEAMTIME_READINESS.md`.
