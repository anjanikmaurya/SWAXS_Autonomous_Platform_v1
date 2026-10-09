# Geometry Calibration — knowledge

## Purpose and position in the pipeline
The Geometry Calibration app (port 5101) is a **pre-reduction utility** — the
step BEFORE the Reduction app (5102). Its job is to produce the `.poni` PyFAI
calibration files that reduction consumes, and to pull raw data across from the
beamline machine.

It has **no `manifest_key`** in `apps.yml` and writes nothing to
`manifest.json`. Its outputs are files on disk: `.cbf` conversions and `.poni`
geometries. Nothing downstream reads its state; reduction simply picks up the
`.poni` files by path.

src/ imports: see CLAUDE.md.

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
`CALIBRANTS` (pyFAI calibrant names, case sensitive):

```
AgBh, LaB6, CeO2, Si, Cr2O3, Au, Ni, alpha_Al2O3
```

Silver behenate is **AgBh** in pyFAI. "AgBehenate" is not a pyFAI name (passing it
opened the GUI with no calibrant), so `resolve_calibrant` maps AgBehenate / AgBeh
to AgBh. `GET /api/calibrants` returns this list plus display labels. AgBh is the
default in the launch route and the usual choice for transmission SAXS; LaB6 / CeO2 / Si suit the WAXS
detector's higher q range.

## Calibration route — interactive pyFAI-calib2
`launch_calib2` spawns the standard pyFAI-calib2 GUI preloaded with the CBF, the
calibrant, the energy and (optionally) the pixel size and a starting geometry.
The user picks rings, refines, and saves the `.poni` themselves.

Command construction (`build_calib2_command`):
```
<launcher> --calibrant <name> --energy <keV> [--detector <model>] [--poni <init>] <image>
```
**Detector menu** (replaces the old Pixel field, October 2026): every detector
model pyFAI knows (`GET /api/detectors`, from `list_detectors`), grouped by
manufacturer, with its image size and pixel size. pyFAI already knows each
model's pixel size, image size and module gaps, so nothing has to be typed. A
chosen model is checked against the image first (`check_detector`): a mismatch
(e.g. a Pilatus 2M for a 1043 × 981 image) is refused with a message and nothing
is started; a binned image is accepted. With **Auto** (the default) the
detector model is chosen from the image shape (`detector_for_image`):
1043 × 981 → `pilatus1m` (SAXS), 195 × 487 → `pilatus100k` (WAXS), and other
Pilatus sizes; the model must also match the Pixel (µm) field (172 µm). A bare
`--pixel` is never sent: on pyFAI 2024 it builds a detector with no shape and the
GUI crashes on start (`detector.max_shape[1] … 'NoneType' object is not
subscriptable`). With no matching model the GUI opens without a detector and asks
for one. A .poni saved this way records `Detector: Pilatus1M` (or 100k), so pyFAI
also knows the module gaps of the Pilatus when it integrates.

How the launcher is resolved (`_calib2_launcher`):
1. `[sys.executable, "-m", "pyFAI.app.calib2"]`: the CURRENT interpreter, i.e. the
   environment with the platform's pyFAI and PySide6. Preferred, because a
   `pyFAI-calib2` script on PATH may belong to another environment without Qt.
2. Otherwise `shutil.which("pyFAI-calib2")`, the console script on PATH.

**Image.** Pick a CBF, or a `.raw` straight from the List step: a `.raw` is
converted to CBF (into `cbf_output/` next to it) before the GUI opens, because
pyFAI cannot read the headerless `.raw`. Energy must be a positive keV value.

**Isolated from the rest of the platform.** The launch writes no `.poni`, config
or manifest entry and touches no other app. The GUI is started through a short
helper process in its own session, so it is not a child of the calibration app:
stopping or restarting the app (or the hub) does not close a calibration in
progress.

The GUI's working directory is set to the project's poni folder, so its
"Save as…" dialog already points there. The environment passed to the GUI has
`MPLBACKEND` and `QT_QPA_PLATFORM` REMOVED, so the GUI does not inherit the
app's forced `Agg` backend or an `offscreen` Qt platform.

The launch is verified: the helper watches the GUI for 6 s (a cold silx + Qt
import can take several seconds) and, if it closed, the app reports its stderr
tail instead of falsely claiming success ("pyFAI-calib2 closed right after
starting (rc=…)"). The response always includes the full
`command` string, so the user can run it by hand in a terminal with the platform
environment active.

**Qt binding required.** pyFAI-calib2 is a Qt GUI (via silx.gui). The Qt binding
**PySide6** now ships in `requirements-core.txt`, so a normal install has it. If it
still exits with `ImportError: No Qt wrapper found` (a stale/partial environment),
reinstall into the SAME environment that runs the platform:
`pip install -r requirements-core.txt` (or `pip install PySide6`; PyQt6 / PyQt5
also work). The launcher says exactly this when it detects the Qt-wrapper error.
PySide6 installs from binary wheels everywhere; it only needs a display to open the
window (a headless box installs it fine and simply never launches the GUI).

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
