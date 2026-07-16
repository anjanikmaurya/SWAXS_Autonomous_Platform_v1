# Beamline testing runbook (Windows / conda)

Test the SPEC beamline link — temperature, counters, and 2D data collection — in
isolation, **before** running the whole autonomous flow. Each tool uses the same
driver the reactor app uses, so what you verify here is exactly what the app will
do. All three are **safe by default**: reads send nothing, temperature and
collection are confirmation-gated, and collection is dry-run unless you pass
`--fire`.

The three tools (in `tools\`):
- `beamline_read_test.py` — read-only temperature / bstop / I₀ (the live-plot data)
- `beamline_temp_test.py` — set a temperature and watch the readback
- `beamline_collect_test.py` — trigger a 2D acquisition (renders the macro first)

---

## 0. One-time setup (Windows + conda)

```bat
:: open Anaconda Prompt
conda activate swaxs                 :: your platform environment
cd C:\path\to\SWAXS_Autonomous_Platform_v1

:: these tools only need pyyaml + requests (already in the platform env);
:: if using a fresh env:  pip install pyyaml requests
```

> **One SPEC client at a time.** The tools and the reactor app both take SPEC
> remote control and fire `ct`. SPEC is single-threaded, so running a tool *and*
> the reactor app against the **real** backend simultaneously makes them fight
> over control and muddies the readings. While script-testing on real hardware,
> **stop the reactor app** (or switch it to the **Mock** backend) so the script is
> the sole SPEC client. Reopen / switch back to Real when you're done.

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
- `temp_counter:` / `bstop_counter:` / `i0_counter:` — the real counter names
  (step 1 helps you discover them).
- `set_temp_cmd:` — the ramp command (default `csettemp {T}`).
- `macro_file:` — path (on THIS PC) to your collection macro template (see step 3).
- `collect_mode:` — how the macro reaches SPEC. **Leave at the default `commands`**
  unless you know SPEC shares a filesystem with this PC (see below).
- `data_dir:` — the base folder that contains `2D\SAXS` (the `main_folder`).

### Which `collect_mode`? (the one thing to decide for collection)

The bServer runs on this Windows PC, but SPEC itself may run elsewhere. That only
matters for **collection**, and `collect_mode` handles both cases:

- **`commands` (default, recommended):** the reactor reads the macro **on this PC**,
  fills the `{{markers}}`, and sends the lines to SPEC one at a time through the
  bServer — the same statements `qdo` would run. **No file is written anywhere**,
  and SPEC saves the detector frames itself using the paths already inside your
  macro. This works whether SPEC is local or a different Linux host — nothing has
  to be shared. Start here.
- **`qdo`:** the reactor writes the filled macro to `macro_out_file` and tells SPEC
  `qdo` it. Only works if `macro_out_file` is a path **SPEC itself can open** (i.e.
  a shared mount, written using the path SPEC sees — the Linux path, not a Windows
  drive letter). Use only if you specifically want file-based `qdo`.

> Don't know where SPEC runs or whether anything is shared? Use `commands`. Reads
> and temperature don't depend on any of this — only collection does.

---

## 1. Reads first — temperature / bstop / I₀ (safe, sends nothing)

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

## 2. Temperature — set + readback (confirmation-gated)

```bat
python tools\beamline_temp_test.py --read-only     :: just read current temp
python tools\beamline_temp_test.py 60              :: asks y/N, ramps to 60 C, prints readback
```

- Sends only `csettemp` (nothing else). Ctrl-C stops monitoring; the setpoint is
  left as-is.
- **Check:** after confirming, the readback climbs toward the target and prints
  `✓ reached` within tolerance.

## 3. Data collection — dry-run, then fire

Point `--macro-file` at your macro **on this PC** — a templatized copy of
`Singlesnapshot.txt` lives at `reactor\macros\Singlesnapshot.template.txt`; it
uses the markers `{{sample}} {{frames}} {{exposure}} {{main_folder}}`. In the
default `commands` mode this file only needs to be readable **here** (the reactor
sends its lines to SPEC), so a plain Windows path is fine.

```bat
:: 3a. DRY-RUN — shows the exact lines that would be sent to SPEC, sends nothing
python tools\beamline_collect_test.py --id test1 --frames 2 --exposure 30 ^
       --macro-file reactor\macros\Singlesnapshot.template.txt ^
       --data-dir /msd_data\...\Auto_Test

:: 3b. FIRE — actually collects (asks y/N; OPENS SHUTTER, X-rays)
python tools\beamline_collect_test.py --id test1 --frames 2 --exposure 30 ^
       --macro-file reactor\macros\Singlesnapshot.template.txt ^
       --data-dir /msd_data\...\Auto_Test --fire
```

(`^` is the Windows line-continuation; or put it all on one line. `--data-dir` is
the `main_folder` your macro writes into — use the path **SPEC** saves to, since
SPEC creates the files.)

- **Dry-run check:** `collect_mode = commands` is printed, followed by the list of
  SPEC commands with your values filled in (`sample = "test1_sample"`,
  `n_images = 2`, …) and the `sprintf`/`%s` lines untouched. Nothing is sent.
- **Fire check:** after confirming, `.raw` frames appear in
  `<data_dir>\2D\SAXS\` named `test1_sample_*`. That confirms the whole
  collect → save path the pipeline reads from. If nothing lands, check that
  `--data-dir` is the path SPEC writes to (not a Windows drive letter SPEC can't
  see), then re-run the dry-run to inspect the commands.
- Use `--role background` to test the background acquisition (files named
  `test1_bkg_*`).

---

## 4. Once all three pass

Set the confirmed values in `reactor\config.yml` → `spec:` (counter names,
`macro_file`, `data_dir`, exposure/frames), then start the reactor app and use
the **Data collection** card + **📷 Collect now** button to repeat step 3 from the
UI. After that you're ready to run the full loop.

## Safety recap
- Reads never send commands.
- Temperature and collection always confirm before acting (skip with `--yes`).
- Collection is dry-run unless `--fire`.
- In the app, Stop / E-stop act on the pumps only and never interrupt a running
  collection (the SPEC link is guarded so no command overlaps an acquisition).
```
