"""
src/simulator/writer.py — write synthetic frames in the beamline's own layout.

Produces exactly what the reduction app expects to find:

    <2D>/SAXS/{prefix}_scan1_0000.raw      int32, row-major, detector shape
    <2D>/{prefix}.csv                      one row per frame  (metadata_format: csv)
    <2D>/SAXS/{prefix}_scan1_0000.raw.pdi  sidecar            (metadata_format: pdi)

CSV placement and the ``_NNNN`` row-index convention follow
``src/reduction/process_metadata.py``: the CSV lives one level above SAXS/ and
its stem must prefix-match the raw stem; the 4-digit frame number selects the row.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path

import numpy as np


#: dropped into any folder the simulator writes to, so synthetic frames can
#: always be told apart from real detector output
MARKER = "SIMULATED_DATA.txt"

MARKER_TEXT = (
    "This folder contains SYNTHETIC SAXS frames written by the SWAXS 2D\n"
    "simulator (src/simulator) running with spec.backend = mock.\n"
    "They are NOT real detector data. Do not mix real beamtime data into\n"
    "this folder — the simulator refuses to write where unmarked .raw files\n"
    "already exist.\n"
)


def is_simulated_dir(path) -> bool:
    return (Path(path) / MARKER).is_file()


def mark_simulated_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    m = p / MARKER
    if not m.is_file():
        m.write_text(MARKER_TEXT, encoding="utf-8")
    return m


def assert_safe_to_simulate(det_dir) -> None:
    """Refuse to write synthetic frames into a folder holding real data.

    The simulator reproduces the beamline's filename convention exactly, so a
    collision would silently overwrite a genuine .raw. A folder is considered
    safe only if it is empty of .raw files or already carries our marker.
    """
    d = Path(det_dir)
    if not d.exists() or is_simulated_dir(d.parent) or is_simulated_dir(d):
        return
    existing = sorted(p.name for p in d.glob("*.raw"))
    if existing:
        raise RuntimeError(
            f"refusing to write synthetic frames into {d}: it already contains "
            f"{len(existing)} .raw file(s) (e.g. {existing[0]}) that were not "
            f"written by the simulator. Point spec.mock_data_dir at a scratch "
            f"folder instead — synthetic and real data must never share a "
            f"directory.")


def frame_name(prefix: str, index: int, scan: int = 1, template: str = "") -> str:
    """Default '{prefix}_scan1_0000.raw' — matches the SSRL BL1-5 convention."""
    if template:
        return template.format(prefix=prefix, scan=scan, index=index)
    return f"{prefix}_scan{scan}_{index:04d}.raw"


def write_raw(path: Path, image: np.ndarray) -> Path:
    """Write the int32 binary the reader in read_raw_file.py expects.

    Written to a .part file and renamed, so a file watcher never sees a
    half-written frame — the real detector behaves the same way.

    The frame and the file on disk are both verified: a 0-byte or short .raw is
    useless to the reduction app and must fail loudly here rather than surface
    later as "file exists but is empty".
    """
    path = Path(path)
    arr = np.asarray(image, dtype=np.int32)
    if arr.size == 0:
        raise ValueError(
            f"refusing to write an EMPTY frame to {path.name} — the detector "
            f"shape resolved to {arr.shape}. Check detector_shapes in the "
            f"project config.yml and simulator.shape in reactor/config.yml.")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        arr.tofile(str(tmp))
        written = tmp.stat().st_size
        expected = arr.size * 4                    # int32
        if written != expected:
            raise IOError(f"wrote {written} of {expected} bytes (disk full?)")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)                # never leave a stub behind
        raise
    return path


def counters(i0=1.0e6, transmission=0.62, temperature=25.0, drift=0.0):
    """Diode/temperature counters for one frame. bstop = i0 · T (so the
    reduction app's bstop/i0 ratio reproduces the transmission we intended)."""
    i0_v = float(i0) * (1.0 + drift)
    return {"i0": round(i0_v, 1),
            "bstop": round(i0_v * float(transmission), 1),
            "temp": round(float(temperature), 2)}


def write_csv_metadata(two_d_dir: Path, prefix: str, rows: list) -> Path:
    """One CSV per acquisition, one row per frame, indexed by the _NNNN suffix."""
    two_d_dir = Path(two_d_dir)
    two_d_dir.mkdir(parents=True, exist_ok=True)
    out = two_d_dir / f"{prefix}.csv"
    # `simulated` marks every row as synthetic, so a .dat reduced from these
    # frames carries the provenance all the way downstream.
    cols = ["i0", "bstop", "temp", "simulated"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            row = {k: r.get(k, 0) for k in cols}
            row["simulated"] = 1
            w.writerow(row)
    return out


def write_pdi_metadata(raw_path: Path, ctr: dict, motors: dict | None = None) -> Path:
    """'.raw.pdi' sidecar in the 'All Counters' format get_meta_from_pdi parses."""
    raw_path = Path(raw_path)
    out = raw_path.with_suffix(raw_path.suffix + ".pdi")
    motors = motors or {"TwoTheta": 0.0, "Theta": 0.0}
    ctr_s = "\n".join(f"{k}={v}" for k, v in ctr.items())
    mot_s = "\n".join(f"{k}={v}" for k, v in motors.items())
    out.write_text(
        f"# Diffractometer Motor Positions for image\n"
        f"# All Counters\n{ctr_s}\n\n# All Motors\n{mot_s}\n#\n",
        encoding="utf-8")
    return out


class AcquisitionWriter:
    """Writes one acquisition (N frames) with optional real-time staggering.

    ``speed_factor`` scales the wall-clock delay between frames: 1.0 honours the
    real exposure (the interview choice), 10.0 runs 10× faster, 0 writes
    instantly. Frames appear one at a time so file watchers see incremental
    arrival exactly as during a real run.
    """

    def __init__(self, two_d_dir, detector="SAXS", metadata_format="csv",
                 speed_factor=1.0, name_template="", log=None, stop_event=None):
        self.two_d_dir = Path(two_d_dir)
        self.detector = detector
        self.metadata_format = str(metadata_format or "csv").lower()
        self.speed_factor = float(speed_factor)
        self.name_template = name_template
        self._log = log or (lambda msg: None)
        self._stop = stop_event

    @property
    def det_dir(self) -> Path:
        return self.two_d_dir / self.detector

    def _sleep(self, seconds):
        if self.speed_factor <= 0 or seconds <= 0:
            return
        remaining = seconds / self.speed_factor
        while remaining > 0:
            if self._stop is not None and self._stop.is_set():
                return
            step = min(0.25, remaining)
            time.sleep(step)
            remaining -= step

    def write_acquisition(self, prefix, frames, make_image, *, exposure_s=1.0,
                          transmission=0.62, temperature=25.0, i0=1.0e6):
        """``make_image(i)`` returns the int32 array for frame i."""
        written, rows = [], []
        peak, filled = 0, 0.0
        for i in range(int(frames)):
            if self._stop is not None and self._stop.is_set():
                self._log(f"simulator: acquisition '{prefix}' cancelled at frame {i}")
                break
            self._sleep(exposure_s)                     # exposure happens first
            img = make_image(i)
            peak = max(peak, int(np.max(img)) if img.size else 0)
            filled = max(filled, float((img > 0).mean()) if img.size else 0.0)
            path = self.det_dir / frame_name(prefix, i, template=self.name_template)
            write_raw(path, img)
            # mild beam decay so transmission/normalisation see realistic variation
            ctr = counters(i0=i0, transmission=transmission,
                           temperature=temperature, drift=-0.002 * i)
            rows.append(ctr)
            if self.metadata_format == "pdi":
                write_pdi_metadata(path, ctr)
            written.append(path)
        if self.metadata_format == "csv" and rows:
            write_csv_metadata(self.two_d_dir, prefix, rows)
        nbytes = sum(p.stat().st_size for p in written if p.is_file())
        # Report the CONTENT, not just the file count: "empty-looking" frames are
        # almost always dim rather than absent, and the peak count says which.
        self._log(f"simulator: wrote {len(written)} frame(s), {nbytes/1e6:.1f} MB, "
                  f"peak {peak:,} counts, {filled*100:.0f}% of pixels non-zero "
                  f"→ {self.det_dir}/{prefix}_*")
        if written and nbytes == 0:
            raise IOError(f"every frame written for '{prefix}' is 0 bytes — "
                          f"check free disk space and folder permissions")
        if written and peak < 100:
            self._log(f"simulator: ⚠ peak is only {peak} counts — frames will look "
                      f"BLACK on a linear display. Raise simulator.scale / flux "
                      f"(or the exposure) in reactor/config.yml.")
        return written
