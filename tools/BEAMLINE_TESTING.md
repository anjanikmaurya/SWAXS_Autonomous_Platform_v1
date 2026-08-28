# Beamline testing runbook (Windows / conda)

Test the SPEC beamline link — temperature, counters, shutter, and 2D data
collection — in isolation, **before** running the whole autonomous flow. Each
tool uses the same driver the reactor app uses, so what you verify here is
exactly what the app will do. All five are **safe by default**: reads send
nothing, temperature / shutter / collection are confirmation-gated, and
collection is dry-run unless you pass `--fire`.

The five tools (in `tools\`):
- `beamline_read_test.py` — read-only temperature / bstop / I₀ from SPEC counters
- `beamline_epics_test.py` — read-only temperature / i0 / bstop from EPICS (caget);
  the read path used when `spec.read_source: epics`. **Needs `pyepics`** — the
  other four need only pyyaml + requests.
- `beamline_temp_test.py` — set a temperature and watch the readback
- `beamline_shutter_test.py` — **opens the fast shutter**, holds it, closes it
  (`sopen`/`sclose` only — no detector, no pumps). X-rays onto the sample:
  confirmation-gated, and it always closes the shutter on exit, Ctrl-C included.
- `beamline_collect_test.py` — trigger a 2D acquisition (renders the macro first)

---

## 0. One-time setup (Windows + conda)

```bat
:: open Anaconda Prompt
conda activate swaxs                 :: your platform environment
cd C:\path\to\SWAXS_Autonomous_Platform_v1

:: the SPEC tools only need pyyaml + requests (already in the platform env);
:: if using a fresh env:  pip install pyyaml requests
:: beamline_epics_test.py additionally needs pyepics:
::   pip install -r requirements-hardware.txt      (pyserial + pyepics)
```

> **One SPEC client at a time.** The tools and the reactor app both take SPEC
> remote control and fire `ct`. SPEC is single-threaded, so running a tool *and*
> the reactor app against the **real** backend simultaneously makes them fight
> over control and muddies the readings. While script-testing on real hardware,
> **stop the reactor app** (or switch it to the **Mock** backend) so the script is
> the sole SPEC client. Reopen / switch back to Real when you're done.
> (`beamline_epics_test.py` is the exception — it never touches SPEC, so it can run
> alongside the app.)

Run every tool with `python` (not `uv`) and Windows backslash paths, e.g.:
```bat
python tools\beamline_read_test.py --mock
```

Add `--mock` to any tool to dry-run against the simulator with no hardware —
do this once first to confirm the tools launch in your env.

### Point the tools at your SPEC bServer

Edit `reactor\config.yml` → `spec:` (the tools read it):
- `base_url:` — the bServer address. `http://127.0.0.1:18085/SIS/` if the bServer
  runs on this same PC; otherwise `http://<bserver-host>:18085/SIS/`.
- `read_source:` — where the live monitors come from: `spec` (counters + `ct`
  refresh, shipped default) or `epics` (caget, keeps reading during a collection).
  See step 2.
- `epics_pvs:` — temperature / i0 / bstop PV names, used only when
  `read_source: epics`. ⚠ confirm them with the beamline engineer.
- `temp_counter:` / `bstop_counter:` / `i0_counter:` — the real counter names
  (step 1 helps you discover them), used only when `read_source: spec`.
- `set_temp_cmd:` — the ramp command (default `csettemp {T}`).
- `background_when:` — `before` (shipped) collects the blank on the clean
  capillary *before* the synthesis; `after` is the legacy post-run flush blank.
  Affects the app, not these tools, but it decides which shot you are debugging.
- `macro_file:` — path (on THIS PC) to your collection macro template. For the
  default `commands` mode this must be the **flat** macro
  `reactor\macros\Singlesnapshot.flat.template.txt` (see step 5).
- `collect_mode:` — how the macro reaches SPEC. **Leave at the default `commands`**
  unless you know SPEC shares a filesystem with this PC (see below).
- `data_dir:` — the base folder that contains `2D\SAXS` (the `main_folder`).

### Which `collect_mode`? (the one thing to decide for collection)

The bServer runs on this Windows PC, but SPEC itself may run elsewhere. That only
matters for **collection**, and `collect_mode` handles both cases:

- **`commands` (default, recommended):** the reactor reads the macro **on this PC**,
  fills the `{{markers}}`, and sends the lines to SPEC one at a time through the
  bServer. **No file is written anywhere**, and SPEC saves the detector frames
  itself using the paths already inside your macro. This works whether SPEC is
  local or a different Linux host — nothing has to be shared. Start here. Requires
  the **flat** macro: streamed line by line, SPEC variable assignments and
  `eval(sprintf)` do not run (step 5).
- **`qdo`:** the reactor writes the filled macro to `macro_out_file` and tells SPEC
  `qdo` it. Only works if `macro_out_file` is a path **SPEC itself can open** (i.e.
  a shared mount, written using the path SPEC sees — the Linux path, not a Windows
  drive letter). Use only if you specifically want file-based `qdo`.

> Don't know where SPEC runs or whether anything is shared? Use `commands`. Reads
> and temperature don't depend on any of this — only collection does.

---

## 1. Reads first — temperature / bstop / I₀ from SPEC counters (safe, sends nothing)

```bat
python tools\beamline_read_test.py                 :: polls ~1/s, Ctrl-C to stop
python tools\beamline_read_test.py --all           :: dump every counter each poll
python tools\beamline_read_test.py --count 1       :: single read then exit
```

- It prints the **list of available counters** first. On BL1-5 the temperature is
  `CTEMP` (already the default). Override with `--temp-counter NAME` if needed.
- **If the values look frozen** (identical every poll, `CTEMP = -1`): that's
  expected. `get_all_counters` returns SPEC's **last-count** values, which are
  stale until SPEC counts again. Make them live by refreshing before each read:
  ```bat
  python tools\beamline_read_test.py --refresh "ct 0.1"
  ```
  `ct` refreshes **all** counters (temperature/bstop/I₀) at once. **⚠ `ct` may open
  the shutter** — if your beamline has a shutter-free temperature-query macro,
  pass that to `--refresh` instead. Once you find what makes `CTEMP` track the
  controller, set it in `reactor\config.yml`:
  ```yaml
  spec:
    temp_counter:     "CTEMP"
    read_refresh_cmd: "ct 0.1"   # or your shutter-free query macro; "" = no refresh
  ```
- **Check:** with the refresh, `CTEMP` matches the number on the temperature
  controller and bstop/I₀ move. (This mirrors the group's own
  `MSD.execute_and_read_count`, which also `ct`s before reading.)
- **Shutter:** `ct` obeys `sauto`. To poll temperature during a long ramp WITHOUT
  exposing the sample, run `sauto off` first — the collection macro opens/closes
  the shutter itself (`sopen`/`sclose`), so collection still works with `sauto off`.
- If it can't reach the bServer it says so — confirm the bServer is running and
  `spec.base_url` is correct.

> Path note (from the real macro): SPEC saves to a **Linux** path like
> `/msd_data/checkout/bl1-5/.../Auto_Test`, which the Windows PC sees as
> `X:\bl1-5\...`. In `commands` mode SPEC writes the frames itself, so `--data-dir`
> is the **Linux** `/msd_data/...` path; the **pipeline** (reduction app) then
> reads those `.raw` files from the `X:\...` mount.

## 2. Reads from EPICS — the fix for the collection blackout (safe, sends nothing)

With `read_source: "spec"` (the shipped default) the live monitors go dark for the
whole of every acquisition. A collection holds the SPEC lock for its duration and
counter reads are deliberately non-blocking (`src/beamline/driver.py`
`read_state`/`read_counters`), so temperature / i0 / bstop stop updating from the
first frame to the last: with `exposure_s: 10` and `frames: 10` that is **100 s
per acquisition with no fresh temperature**, and for that window the
over-temperature interlock has nothing to compare against. The app logs this once
per run instead of raising a sensor fault.

Reading from EPICS instead avoids it entirely — caget needs no remote control, no
`ct`, and keeps working during a collection:

```bat
python tools\beamline_epics_test.py                 :: poll the config PVs ~1/s
python tools\beamline_epics_test.py --count 1       :: one read then exit
python tools\beamline_epics_test.py --temp BL01-5:Aux1Temp.G ^
       --i0 BL01-5:AuxInput.A --bstop BL01-5:AuxInput.B
```

- Needs `pyepics` (`pip install -r requirements-hardware.txt`) and channel access
  to the beamline.
- If a PV returns `None` / won't connect, pass `--ca-addr <ip>` (the IOC or CA
  gateway for BL1-5, from the beamline engineer) and then set
  `spec.epics_ca_addr_list` to the same value.
- **Check:** the three values track the controller and move between polls. Then
  set `read_source: "epics"` in `reactor\config.yml`.
- Second-best mitigation, if EPICS is not available: `read_during_collect: true`,
  which polls the SPEC counters concurrently with a collection. Only use it if the
  bServer tolerates counter reads mid-scan.

| Symptom | Fix |
|---|---|
| Live plot frozen **only during a collection**, fine otherwise | `spec.read_source: "epics"` (or `read_during_collect: true`) — the SPEC read path is blocked by the acquisition |
| Live plot frozen **all the time** on `read_source: spec` | `read_refresh_cmd` / remote control — see step 1 |
| EPICS values all `None` | wrong PV names, or no channel access — `--ca-addr` / `epics_ca_addr_list` |

## 3. Temperature — set + readback (confirmation-gated)

```bat
python tools\beamline_temp_test.py --read-only     :: just read current temp
python tools\beamline_temp_test.py 60              :: asks y/N, ramps to 60 C, prints readback
```

- Sends only `csettemp` (nothing else). Ctrl-C stops monitoring; the setpoint is
  left as-is.
- **Check:** after confirming, the readback climbs toward the target and prints
  `✓ reached` within tolerance.

## 4. Shutter — open, hold, close (confirmation-gated, X-rays)

The smallest possible beamline action: `sopen`, hold, `sclose`. Nothing else is
touched — no temperature, no detector, no pumps, no files.

```bat
python tools\beamline_shutter_test.py --close-only  :: just make sure it is CLOSED (safe)
python tools\beamline_shutter_test.py --mock        :: dry-run against the simulator
python tools\beamline_shutter_test.py               :: asks y/N, opens 2 s, closes
python tools\beamline_shutter_test.py --hold 5      :: hold open 5 s
```

- **Watch the hutch / shutter status while it runs.** This puts X-rays on the
  sample; make sure the hutch is searched and locked and nobody is inside.
- The close is in a `finally` block, so the shutter is closed on a normal exit, an
  error, and Ctrl-C. If the tool is killed outright (task manager, power loss),
  run `--close-only` to be sure.
- Commands come from `spec.open_shutter_cmd` / `close_shutter_cmd`; the tool takes
  SPEC remote control first and reports if it cannot.
- **Check:** the shutter status readback (or the hutch indicator) goes open and
  then closed, and `bstop` in step 1 jumps while it is open.

## 5. Data collection — dry-run, then fire

Point `--macro-file` at your macro **on this PC**. In the default `commands` mode
use the **FLAT** macro `reactor\macros\Singlesnapshot.flat.template.txt` — which
is what `reactor\config.yml` ships — and use the markers
`{{sample}} {{frames}} {{exposure}} {{main_folder}}`. The file only needs to be
readable **here** (the reactor sends its lines to SPEC), so a plain Windows path
is fine.

> ⚠ **Do not pass `Singlesnapshot.template.txt` in `commands` mode.** That is the
> `qdo` variant: it sets SPEC variables (`sample = "…"`, `n_images = …`) and wraps
> its real work in `eval(sprintf(...))`, and neither runs reliably through the
> interactive `execute_command` path (`src/beamline/driver.py` `_do_collect`, and
> the flat macro's own header). Streamed line by line, the plain commands `sopen`
> and `sclose` still fire while `newfile` / `pd savepath` / the `loopscan` inside
> the `eval` may not — X-rays on the sample with the frames unsaved or written to
> whatever path SPEC was last pointed at. Use the flat macro; `qdo` mode is the
> only place the variable macro belongs.

```bat
:: 5a. DRY-RUN — shows the exact lines that would be sent to SPEC, sends nothing
python tools\beamline_collect_test.py --id test1 --frames 2 --exposure 30 ^
       --macro-file reactor\macros\Singlesnapshot.flat.template.txt ^
       --data-dir /msd_data/checkout/bl1-5/.../Auto_Test

:: 5b. FIRE — actually collects (asks y/N; OPENS SHUTTER, X-rays)
python tools\beamline_collect_test.py --id test1 --frames 2 --exposure 30 ^
       --macro-file reactor\macros\Singlesnapshot.flat.template.txt ^
       --data-dir /msd_data/checkout/bl1-5/.../Auto_Test --fire
```

(`^` is the Windows line-continuation; or put it all on one line. `--data-dir` is
the `main_folder` your macro writes into — use the path **SPEC** saves to, since
SPEC creates the files.)

- **Dry-run check:** `collect_mode = commands` is printed, followed by the list of
  SPEC commands with your values filled in — every line a plain action command
  (`newfile test1_sample`, `pd savepath '…/2D/SAXS'`, `loopscan 2 30 0`, …), with
  no `sample =` assignment and no `sprintf` anywhere. If you see either of those,
  you are pointing at the `qdo` macro — switch to the flat one. Nothing is sent.
- **Fire check:** after confirming, `.raw` frames appear in
  `<data_dir>/2D/SAXS/` named `test1_sample_*`. That confirms the whole
  collect → save path the pipeline reads from. If nothing lands, check that
  `--data-dir` is the path SPEC writes to (not a Windows drive letter SPEC can't
  see), then re-run the dry-run to inspect the commands.
- Use `--role background` to test the background acquisition (files named
  `test1_bkg_*`). In the app the background is collected **before** its synthesis
  (`spec.background_when: "before"`, the shipped default), not during the post-run
  flush — but the acquisition this tool fires is identical either way.

---

## 6. Once they all pass

Set the confirmed values in `reactor\config.yml` → `spec:` (`read_source` and
either `epics_pvs` or the counter names, `macro_file` — the **flat** one for
`commands` mode, `data_dir`, exposure/frames), then start the reactor app and use
the **Data collection** card + **📷 Collect now** button to repeat step 5 from the
UI. After that you're ready to run the full loop.

## Safety recap
- Reads never send commands (both the SPEC and the EPICS read tools).
- Temperature, shutter and collection always confirm before acting (skip with
  `--yes`).
- Collection is dry-run unless `--fire`.
- The shutter tool always closes the shutter on the way out; `--close-only` is the
  safe way to force it shut.
- In `commands` mode always use the flat macro — the `qdo` macro can open the
  shutter without setting up the save path.
- In the app, Stop / E-stop act on the pumps only and never interrupt a running
  collection (the SPEC link is guarded so no command overlaps an acquisition).
- With `read_source: "spec"` the temperature interlock has no fresh reading for
  the whole of each acquisition (step 2).
