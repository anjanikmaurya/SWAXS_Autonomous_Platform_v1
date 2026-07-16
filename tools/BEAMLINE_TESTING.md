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
- `macro_file:` — path to your collection macro template (see step 3).
- `macro_out_file:` — where the filled macro is written for `qdo`.
- `data_dir:` — the base folder that contains `2D\SAXS` (the `main_folder`).

> Cross-machine note: if the reactor PC (Windows) and SPEC (Linux) are different
> hosts, `base_url` points at the SPEC host, and `macro_file` / `macro_out_file` /
> `data_dir` must be on a **shared mount** using the path **SPEC** sees (the Linux
> path that `qdo` will open), not the Windows drive letter.

---

## 1. Reads first — temperature / bstop / I₀ (safe, sends nothing)

```bat
python tools\beamline_read_test.py                 :: polls ~1/s, Ctrl-C to stop
python tools\beamline_read_test.py --all           :: dump every counter each poll
python tools\beamline_read_test.py --count 1       :: single read then exit
```

- It prints the **list of available counters** first — use this to find the real
  temperature counter name. If it's `ctemp` (not `temp`):
  ```bat
  python tools\beamline_read_test.py --temp-counter ctemp
  ```
  then set `spec.temp_counter: ctemp` in `reactor\config.yml`.
- **Check:** temperature / bstop / I₀ print sensible live values.
- If it can't reach the bServer it says so — confirm the bServer is running and
  `spec.base_url` is correct.

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

Put your macro template on a SPEC-readable path (a templatized copy of
`Singlesnapshot.txt` lives at `reactor\macros\Singlesnapshot.template.txt`; it
uses the markers `{{sample}} {{frames}} {{exposure}} {{main_folder}}`).

```bat
:: 3a. DRY-RUN — renders the filled macro + shows the save path, sends nothing
python tools\beamline_collect_test.py --id test1 --frames 2 --exposure 30 ^
       --macro-file X:\bl1-5\...\Singlesnapshot.template.txt ^
       --data-dir X:\bl1-5\...\Auto_Test

:: 3b. FIRE — actually collects (asks y/N; OPENS SHUTTER, X-rays)
python tools\beamline_collect_test.py --id test1 --frames 2 --exposure 30 ^
       --macro-file X:\bl1-5\...\Singlesnapshot.template.txt ^
       --data-dir X:\bl1-5\...\Auto_Test --fire
```

(`^` is the Windows line-continuation; or put it all on one line.)

- **Dry-run check:** the printed macro has your values filled in
  (`sample = "test1_sample"`, `n_images = 2`, …) and the SPEC `sprintf`/`%s`
  lines are untouched. Nothing is sent.
- **Fire check:** after confirming, `.raw` frames appear in
  `<data_dir>\2D\SAXS\` named `test1_sample_*`. That confirms the whole
  collect → save path the pipeline reads from.
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
