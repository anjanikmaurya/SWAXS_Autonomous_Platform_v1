# Geometry Calibration: knowledge

## What this app is for
Geometry Calibration (port 5101) is the pre-reduction utility. It does two jobs:

1. **Copy data**: pull raw data from the beamline machine to a local folder over SFTP.
2. **Calibrate**: turn calibrant `.raw` images (AgBh, LaB6, ...) into CBF and open the
   pyFAI-calib2 GUI so the user can produce the `.poni` geometry files that the
   Reduction app (5102) needs.

## Where it sits in the pipeline
- **Reads:** calibrant `.raw` files from any folder you choose; the project
  `config.yml` (`detector_shapes`, `energy_keV`, `poni_directory`).
- **Writes:** CBF files to `<raw folder>/cbf_output/`; `.poni` files are saved by
  you from the pyFAI-calib2 GUI, into the project poni folder (`poni_directory`
  from `config.yml`, else `<project_root>/poni/`).
- It has no `manifest_key` in `apps.yml` and writes nothing to the manifest.
  Reduction picks the `.poni` files up by path through `poni_directory` plus
  `poni_files.saxs` / `poni_files.waxs` in `config.yml`; detector masks
  (`mask_files`, `.edf`) live in the same folder.
- Reduction reads `.raw` directly. The CBF conversion exists only because
  pyFAI-calib2 needs a standard image format, and for quick QA.

## How to use it
The sidebar has two tabs: **Copy data** and **Calibrate**.

### Copy data tab (SFTP to local)
1. Fill in **Host**, **Port** (22), **Username**, and either **Password** or
   **SSH key (optional)**.
2. Set **Remote folder** and **Local folder** (use **Browse...**; the local folder
   must already exist).
3. Choose **Copy mode**:
   - **Watch**: polls the remote folder every **Check interval (s)** (default 60)
     and copies anything new or changed. Use during beamtime. Changing the
     interval needs Stop, then Start again.
   - **One time**: copies the whole remote tree once, then stops (post-beamtime).
4. Set **Parallel transfers** (1 to 16, default 4). Use 8 for many small files on a
   fast link, 1 or 2 if the server throttles.
5. **Test connection** to check credentials, then **Start copy**. **Stop** ends it.
   The progress bar shows files, data, speed, elapsed time, ETA and failures.

Behaviour: the remote sub-folder tree is preserved; a file already present locally
with the same size is skipped. Settings persist to `~/.swaxs_sftp_sync.json`, but
the **password is never saved**, so re-enter it each session (or use an SSH key).
Transfers share one ssh connection with a large flow-control window and 32 KB
packets, which is much faster than paramiko's defaults.

### Calibrate tab
**1 · Convert calibrant raw → CBF**
1. **Raw folder**: the folder of calibrant `.raw` files.
2. **Keywords**: comma separated, case insensitive, matches any; blank = all files.
3. **List** shows the matching files with pixel count and detector (SAXS, WAXS or
   `?`), and fills the Image menu with the `.raw` files.
4. **Convert → CBF** writes one CBF per file to `<raw folder>/cbf_output/`, shows a
   log-scale preview of each frame, and fills the Image menu with the CBFs. A file
   that cannot be read is reported and the rest of the batch continues. Each
   result carries QA stats: `min`, `max`, `total_counts`, `hot_pixels` (pixels
   above 1e6) and `shape`.

**2 · Generate .poni (pyFAI)**
1. **Calibrant**: see the list below (default AgBh).
2. **Energy (keV)**: prefilled from `energy_keV` in `config.yml`; must be positive.
3. **Detector**: **Auto (from the image size)** by default, or any detector model
   pyFAI knows, grouped by manufacturer with image size and pixel size. pyFAI
   already knows each model's pixel size, image size and module gaps.
4. **Image**: a CBF, or a `.raw` from the List step (a `.raw` is converted to CBF
   into `cbf_output/` first, because pyFAI cannot read headerless `.raw`).
5. **Open pyFAI-calib2**. In the GUI pick rings, refine, then **Save as...**; the
   dialog starts in the project poni folder.

**3 · PONI files in project**: **Refresh** lists the `.poni` files in the poni folder.

## Settings and defaults

### Raw format and detector shapes
SSRL BL1-5 `.raw` files are headerless little-endian **int32**, row-major. The
detector is inferred from the pixel count (file size / 4):

```
DEFAULT_SHAPES = {"SAXS": (1043, 981), "WAXS": (195, 487)}     # (rows, cols)
```

`detector_shapes` in the project `config.yml` wins; the defaults apply only when
the config has no usable entry. If the pixel count matches no shape, a filename
containing `waxs`, `100k` or `si` is treated as WAXS; otherwise the file is
rejected with `"<file>: unexpected size <N> (expected <M> or <M>)"`.

### Detector selection
- **Auto** picks the Pilatus model whose shape matches the image:
  1043 × 981 → `pilatus1m` (SAXS), 195 × 487 → `pilatus100k` (WAXS); other Pilatus
  sizes (200k, 300k, 300kw, 900k, 2M, 6M) are also recognised. With no match the
  GUI opens without a detector and asks for one.
- A model chosen by hand is checked against the image first. A mismatch (e.g. a
  Pilatus 2M for a 1043 × 981 image) is refused and nothing starts; a binned
  image (integer fraction of the model) is accepted.
- A `.poni` saved this way records `Detector: Pilatus1M` (or 100k), so pyFAI also
  knows the Pilatus module gaps when it integrates.
- A bare `--pixel` is never passed, because on current pyFAI it builds a detector
  with no shape and the GUI crashes. API callers may still send `pixel_um`; the
  auto-chosen model must then agree with it within 1 µm.

### Energy and wavelength
```
λ[Å] = 12.39842 / E[keV]
```
`energy_to_wavelength_m(energy_keV)` returns metres (`12.39842 / E * 1e-10`).

### Supported calibrants
pyFAI names, case sensitive:
```
AgBh, LaB6, CeO2, Si, Cr2O3, Au, Ni, alpha_Al2O3
```
Silver behenate is **AgBh** in pyFAI; "AgBehenate" and "AgBeh" are mapped to AgBh.
AgBh suits transmission SAXS; LaB6, CeO2 and Si suit the WAXS detector's higher q.

### How pyFAI-calib2 is launched
Command:
```
<launcher> --calibrant <name> --energy <keV> [--detector <model>] <image>
```
- Launcher: `python -m pyFAI.app.calib2` with the platform's own interpreter
  (preferred, because a `pyFAI-calib2` script on PATH may belong to another
  environment without Qt), else `pyFAI-calib2` from PATH.
- The GUI runs detached in its own session: stopping or restarting this app or
  the hub does not close a calibration in progress. The launch writes no `.poni`,
  config or manifest entry.
- Its working directory is the project poni folder. `MPLBACKEND` and
  `QT_QPA_PLATFORM` are removed from its environment so it does not inherit the
  app's `Agg` backend or an `offscreen` Qt platform.
- The launch is verified: the GUI is watched for 6 s and, if it closes, the app
  reports its stderr tail ("pyFAI-calib2 closed right after starting (rc=...)").
  The response always includes the full `command`, so it can be run by hand.
- pyFAI-calib2 needs a Qt binding. **PySide6** is in `requirements-core.txt`
  (PyQt6 or PyQt5 also work) and needs a display to open the window.

### 2D preview
Log-scale PNG (matplotlib `LogNorm`, `hot` colormap), `vmin` clamped to at least 1
so zero or negative pixels do not break the scale. Send `preview: false` to
`/api/convert` to skip it. Good rings are continuous, concentric and evenly
spaced; broken or elliptical rings mean a tilted or off-centre detector; missing
rings mean too short an exposure.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Garbled or striped preview | Wrong detector shape: the headerless int32 data is reshaped into nonsense instead of failing. Check `detector_shapes` in `config.yml`; the `pixels` count from List must equal rows × cols. |
| "unexpected size N" | The pixel count matches no configured shape and the filename gives no WAXS hint. The file is truncated or not a `.raw`, or `detector_shapes` does not describe this detector. |
| Detector refused ("the image is ... pixels but a ... is ...") | The chosen model does not fit the image. Pick the detector the image came from, or Auto. |
| pyFAI-calib2 closes right after starting, "No Qt wrapper found" | The environment that runs the platform lacks Qt: `pip install -r requirements-core.txt` (or `pip install PySide6`) in that same environment. |
| pyFAI-calib2 fails with a display or `xcb` error | No display (headless or remote session). Run the returned `command` in a terminal on a machine with a display and the platform environment active. |
| Fitted sample-to-detector distance is systematically off | Wrong energy: distance and wavelength are coupled in the refinement, so a wrong `energy_keV` shifts the distance while the rings still appear to fit. Confirm the energy with the beamline. |
| SFTP will not start | Host, Username, Remote folder and Local folder are required, and the local folder must exist. Only one copy runs at a time. |
| SFTP fails after restarting the app | The password is never saved; re-enter it, or use an SSH key. |

## API routes

| Route | Purpose |
|---|---|
| `GET /api/health` | `{"status": "ok", "app": "calibration"}` |
| `POST /api/set_project` | `{path}` pushed by the hub; also sets `SWAXS_PROJECT` |
| `GET /api/project` | `{project_root, poni_dir, shapes, energy_keV}` |
| `GET /api/calibrants` | Calibrant list and display labels |
| `POST /api/list_raw` | `{raw_dir, keywords}` → `{files: [{name, path, pixels, detector}], count}` |
| `POST /api/convert` | `{raw_dir, keywords, preview}` → per-file results with QA stats and preview PNG |
| `GET /api/detectors` | Every pyFAI detector model (id, name, manufacturer, shape, pixel) |
| `POST /api/calibrate/launch` | `{cbf, calibrant, energy_keV, detector}` (`detector` defaults to `auto`; optional `pixel_um`) → `{ok, message, command, image, poni_dir}` |
| `GET /api/poni` | `.poni` files in the poni folder |
| `GET /api/browse?path=` | Folder browser; defaults to the project root, then home |
| `GET /api/sftp/config` | Saved SFTP settings and whether a copy is running |
| `POST /api/sftp/test` | Test the connection → `{ok, message}` |
| `POST /api/sftp/start` | Start a copy (`host`, `username`, `remote_dir`, `local_dir` required) |
| `POST /api/sftp/stop` | Stop it (waits up to 3 s for the loop to exit, so two copies never overlap) |
| `GET /api/sftp/status?since=<seq>` | Running flag, status, new log lines, progress |
