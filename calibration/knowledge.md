# Calibration & Raw Prep — knowledge

## Purpose and position in the pipeline
The Calibration & Raw Prep app (port 5009) is a **pre-reduction utility** — the
step BEFORE the Reduction app (5001). Its job is to produce the `.poni` PyFAI
calibration files that reduction consumes, and to pull raw data across from the
beamline machine.

It has **no `manifest_key`** in `apps.yml` and writes nothing to
`manifest.json`. Its outputs are files on disk: `.cbf` conversions and `.poni`
geometries. Nothing downstream reads its state; reduction simply picks up the
`.poni` files by path.

All logic lives in `src/preprocess/` (`raw_convert.py`, `calib.py`,
`sftp_sync.py`); `calibration/app.py` is a thin Flask shell.

## Three-stage workflow

### 1. Find calibrant `.raw` files by keyword
`POST /api/list_raw` with `{raw_dir, keywords}`. `find_raw_files` lists every
`*.raw` in the folder whose name contains ANY of the keywords
(case-insensitive); an empty keyword list returns all of them. For each file the
app divides the byte size by 4 (int32 pixels) and matches the pixel count against
the known detector shapes, so the response carries
`{name, path, pixels, detector}` with `detector` = `SAXS`, `WAXS` or `?`.

### 2. Convert `.raw` → CBF
`POST /api/convert` with `{raw_dir, keywords, preview}`. `convert_dir` writes a
true CBF for each matching file via **fabio**
(`fabio.cbfimage.CbfImage(...).write()`) into `<raw_dir>/cbf_output/`. It is
fail-soft: a file that cannot be read is recorded with `ok: false` and an error
message rather than aborting the batch.

Each result also carries QA stats from `frame_stats`: `min`, `max`,
`total_counts`, `hot_pixels` (count of pixels above 1e6) and `shape`.

Reduction still reads `.raw` directly — the CBF conversion exists for
calibration (pyFAI-calib2 wants a standard image format) and for quick QA.

### 3. Generate the `.poni`
`POST /api/calibrate/launch` with `{cbf, calibrant, energy_keV, pixel_um}`.
See "Calibration route" below.

`GET /api/poni` lists the `.poni` files already in the project's poni folder as
`{name, path}`.

## Raw file format and detector shapes
The SSRL BL1-5 `.raw` files are headerless little-endian **int32**, row-major.
The detector is inferred from the pixel count.

```
DEFAULT_SHAPES = {"SAXS": (1043, 981), "WAXS": (195, 487)}     # (rows, cols)
```

These are the same values as `detector_shapes` in the project `config.yml`, and
the project config WINS: `_shapes()` reads `detector_shapes` from
`<project_root>/config.yml` and falls back to `DEFAULT_SHAPES` only when the
config has no usable entry.

`detect_shape(size, shapes, name)` matches the pixel count against the known
shapes. If the count is unknown or ambiguous it falls back to a filename hint —
a name containing `waxs`, `100k` or `si` is treated as WAXS — and otherwise
returns no shape, which makes `read_raw` raise
`"<file>: unexpected size <N> (expected <M> or <M>)"`.

## Energy and wavelength
Energy in **keV** is the required input. The conversion is

```
λ[Å] = 12.39842 / E[keV]
```

`energy_to_wavelength_m(energy_keV)` returns the wavelength in **metres**
(`12.39842 / E * 1e-10`). The default energy shown in the UI comes from
`energy_keV` in the project `config.yml` (surfaced by `GET /api/project`).

## Supported calibrants
`CALIBRANTS` (pyFAI `get_calibrant` names):

```
AgBehenate, LaB6, CeO2, Si, Cr2O3, Au, Ni, alpha_Al2O3
```

`GET /api/calibrants` returns this list. AgBehenate is the default in the launch
route and the usual choice for transmission SAXS; LaB6 / CeO2 / Si suit the WAXS
detector's higher q range.

## Calibration route — interactive pyFAI-calib2
`launch_calib2` spawns the standard pyFAI-calib2 GUI preloaded with the CBF, the
calibrant, the energy and (optionally) the pixel size and a starting geometry.
The user picks rings, refines, and saves the `.poni` themselves.

Command construction (`build_calib2_command`):
```
<launcher> --calibrant <name> --energy <keV> [--pixel <microns>] [--poni <init>] <image>
```
`--pixel` is in **MICRONS**, not metres (the app's default is 172.0 µm).

How the launcher is resolved (`_calib2_launcher`):
1. `shutil.which("pyFAI-calib2")` — the console script, if it is on PATH.
2. Otherwise `[sys.executable, "-m", "pyFAI.app.calib2"]` — the same module run
   through the CURRENT interpreter. This fallback is what makes the app work when
   the hub spawns it without the virtualenv's `bin/` on PATH.

The GUI's working directory is set to the project's poni folder, so its
"Save as…" dialog already points there. The environment passed to the GUI has
`MPLBACKEND` and `QT_QPA_PLATFORM` REMOVED, so the GUI does not inherit the
app's forced `Agg` backend or an `offscreen` Qt platform.

The launch is verified: after 1.5 s the app checks whether the process already
exited and, if so, reports its stderr tail instead of falsely claiming success
("pyFAI-calib2 exited immediately (rc=…). Usually a missing Qt or no display.").
The response always includes the full `command` string, so the user can run it by
hand in a terminal with the platform environment active.

## Poni folder
The output folder is `poni_directory` from the project `config.yml`, falling back
to `<project_root>/poni/` (or `./poni` with no project). Reduction then references
these files through `poni_directory` plus `poni_files.saxs` / `poni_files.waxs`
in its own `config.yml`, and the detector masks (`mask_files`, `.edf`) live
alongside them.

## 2D preview
Every converted frame can be rendered as a **log-scale** PNG thumbnail
(matplotlib `LogNorm`, `hot` colormap, base64 data URI) so ring quality can be
checked before refining. `vmin` is clamped to at least 1.0 so zero/negative
pixels do not break the log scale. Set `preview: false` in the convert request to
skip it.

Look for: continuous, concentric, evenly spaced rings. Discontinuous or
elliptical rings mean a tilted or badly centred detector; missing rings mean the
exposure was too short.

## SFTP data sync (left panel)
`src/preprocess/sftp_sync.py` pulls raw data from the beamline machine into a
local folder (for example a Google-Drive-synced directory), preserving the remote
sub-directory tree.

Endpoints:
- `GET  /api/sftp/config` — the saved configuration and whether a sync is running
- `POST /api/sftp/test` — test the connection, returns `{ok, message}`
- `POST /api/sftp/start` — start a sync. `host`, `username`, `remote_dir` and
  `local_dir` are all required, and `local_dir` must already exist.
- `POST /api/sftp/stop` — stop it (waits up to 3 s for the poll loop to exit
  before releasing the handle, so two syncs can never overlap)
- `GET  /api/sftp/status?since=<seq>` — running flag, status text, new log lines
  since a sequence number, and transfer progress

Two modes via `cfg["mode"]`:
- **watch** — keep polling every `interval` seconds and copy anything new (use
  during beamtime; the ssh connection is reused across polls)
- **once** — walk the whole remote tree a single time, then stop (post-beamtime)

Credentials persist to `~/.swaxs_sftp_sync.json`. The **password is never
written to disk** — `save_config` strips it — so it must be re-entered after a
restart.

Throughput is tuned deliberately: a large SFTP flow-control window
(`2**31 - 1`) and 32 KB packets (paramiko's defaults are tiny and are the usual
reason "python sftp" feels ~10× slower than a real client), `workers` parallel
transfers (default 4) on channels of ONE ssh connection, and file sizes taken
from the directory listing so there is no extra `stat()` per file. paramiko is
imported lazily, so the module loads without it installed.

## Other endpoints
- `GET  /api/health` — `{"status": "ok", "app": "calibration"}`
- `POST /api/set_project` — accepts `{path}` pushed by the hub; also sets
  `SWAXS_PROJECT` in this process
- `GET  /api/project` — `{project_root, poni_dir, shapes, energy_keV}`
- `GET  /api/browse?path=` — directory browser (dirs and files), defaulting to
  the project root then the user's home

## Troubleshooting

### Garbled or striped preview
The detector shape is wrong. The `.raw` is headerless int32, so a shape mismatch
reshapes the data into nonsense rather than failing. Check `detector_shapes` in
the project `config.yml` against the actual detector, and check the reported
`pixels` count in `/api/list_raw` — it must equal rows × cols.

### Systematically wrong sample-to-detector distance
The energy is wrong. Distance and wavelength are coupled in the refinement, so an
incorrect `energy_keV` is absorbed into a shifted distance while the rings still
appear to fit. Confirm the energy with the beamline before refining.

### pyFAI-calib2 will not launch
Usually PATH: the hub spawns each app with `sys.executable`, so the
virtualenv's `bin/` may not be on PATH and the `pyFAI-calib2` console script is
not found. That is exactly why the `python -m pyFAI.app.calib2` fallback exists.
If it still fails, the reported stderr almost always says missing Qt or no
display — run the returned `command` string manually in a terminal with the
platform environment active.

### "unexpected size N"
`read_raw` could not match the pixel count to any configured detector shape and
the filename gave no WAXS hint. Either the file is truncated/not a `.raw`, or
`detector_shapes` does not describe this detector.
