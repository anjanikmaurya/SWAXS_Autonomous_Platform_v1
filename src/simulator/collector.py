"""
src/simulator/collector.py — turns a triggered "collect" into synthetic files.

This is the object MockBeamline holds. It owns the geometry/mask caches and the
current recipe, so a collect() call needs no extra plumbing:

    sim = SimulatedCollector(cfg, project_root, log=...)
    sim.set_recipe(recipe)                       # from the controller
    sim.collect(prefix="r001_sample", role="sample", exposure=10, frames=10)

Role handling:
  • "sample"      → particles from the hidden ground-truth model
  • "background"  → particle-free solvent frame (matched pair for subtraction)
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import yaml

from .ground_truth import truth_from_recipe, describe
from .pattern import (q_map, synthetic_q_map, load_mask, beamstop_mask,
                      simulate_frame)
from .writer import (AcquisitionWriter, assert_safe_to_simulate,
                     mark_simulated_dir)

DEFAULTS: dict = {
    "enabled":        False,
    "detector":       "SAXS",
    "shape":          [1043, 981],
    "poni":           "",         # blank → resolve from the project config
    "mask":           "",         # blank → resolve from the project config
    "speed_factor":   1.0,        # 1.0 = real time (honours exposure × frames)
    "flux":           1.0e6,      # counts/s scale at I(0)
    #: Particle signal at I(0), in counts per second of exposure. 800 gave a
    #: peak of only ~900 counts, so the frame rendered black on any linear
    #: display; 2e4 puts a 1 s frame in the 10^4 range like real detector data.
    "scale":          20000.0,
    #: sample-detector distance used ONLY by the synthetic fallback geometry
    "fallback_dist_m": 3.0,
    "solvent_bkg":    50.0,       # flat incoherent solvent level (counts/s)
    "capillary":      120.0,      # capillary q⁻² upturn, quoted at q = 0.1 nm⁻¹
    "porod":          0.0,
    "q_beamstop":     0.02,       # nm⁻¹ — beamstop shadow radius in q
    "transmission":   0.62,
    "name_template":  "",
    "metadata_format": "",        # blank → from the project config.yml
    #: "auto" → put frames under <folder>/2D/SAXS when <folder> is a project
    #: root; "" → treat <folder> itself as the 2D base (the rig convention);
    #: any other string → force that sub-directory name.
    "two_d_subdir":   "auto",
    "truth":          {},         # overrides for ground_truth.DEFAULTS
}


def _merge(cfg: dict | None) -> dict:
    out = dict(DEFAULTS)
    out["truth"] = dict(DEFAULTS["truth"])
    for k, v in (cfg or {}).items():
        if k == "truth" and isinstance(v, dict):
            out["truth"].update(v)
        elif k in DEFAULTS:
            out[k] = v
    return out


class SimulatedCollector:
    """Generates .raw + metadata for a triggered acquisition."""

    def __init__(self, cfg: dict | None = None, project_root: str = "",
                 log=None, stop_event=None):
        self.cfg = _merge(cfg)
        self.project_root = str(project_root or "")
        self._log = log or (lambda msg: None)
        self._stop = stop_event or threading.Event()
        self._recipe = None
        self.last: dict | None = None          # ground truth of the last collect
        self.history: list = []
        self._last_data_dir = ""               # widens the config.yml search
        self._cfg_source = ""                  # which config.yml supplied geometry

    def stop(self) -> None:
        """Cancel an in-flight acquisition (called from MockBeamline.close())."""
        self._stop.set()

    def resume(self) -> None:
        self._stop = threading.Event()

    # ── configuration ────────────────────────────────────────────────────────
    def set_recipe(self, recipe) -> None:
        """Called by the controller when a run starts, so the simulated particles
        reflect the recipe actually being executed."""
        self._recipe = recipe

    def set_project_root(self, path: str) -> None:
        self.project_root = str(path or "")

    def _config_search_roots(self):
        """Folders that may hold the project config.yml, most specific first.

        The reactor knows two different paths — the hub PROJECT ROOT (where
        config.yml lives) and the 2D SAVE FOLDER — and they are not the same.
        Walking up from the save folder as well means the geometry is found even
        if only one of them was wired up.
        """
        roots = []
        if self.project_root:
            roots.append(Path(self.project_root))
        if self._last_data_dir:
            d = Path(self._last_data_dir)
            roots.extend([d, *list(d.parents)[:3]])   # e.g. …/2D/SAXS → …/2D → …
        seen, out = set(), []
        for r in roots:
            s = str(r)
            if s not in seen:
                seen.add(s); out.append(r)
        return out

    def _project_cfg(self) -> dict:
        for root in self._config_search_roots():
            p = root / "config.yml"
            if p.is_file():
                try:
                    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                if cfg.get("poni_files") or cfg.get("detector_shapes"):
                    self._cfg_source = str(p)
                    return cfg
        return {}

    def _discover_poni(self, det: str):
        """Last resort: find a .poni on disk when config.yml doesn't name one,
        preferring a filename that mentions this detector."""
        for root in self._config_search_roots():
            for d in (root / "poni", root):
                if not d.is_dir():
                    continue
                files = sorted(d.glob("*.poni"))
                if not files:
                    continue
                named = [f for f in files if det.lower() in f.name.lower()]
                return str((named or files)[0])
        return ""

    def _resolve(self):
        """Detector shape / poni / mask / metadata format — config first, then
        the project's own config.yml so the mock matches the real reduction."""
        pc = self._project_cfg()
        det = str(self.cfg["detector"]).upper()

        shape = self.cfg["shape"]
        ds = (pc.get("detector_shapes") or {})
        for k, v in ds.items():
            if k.upper() == det and v:
                shape = [int(v[0]), int(v[1])]
        shape = (int(shape[0]), int(shape[1]))

        poni = str(self.cfg["poni"] or "").strip()
        if poni:
            pp = Path(poni).expanduser()
            if pp.is_dir():
                # A DIRECTORY is allowed: pick the .poni whose name mentions this
                # detector (else the first), so you can point at poni/ and forget
                # the exact filename.
                files = sorted(pp.glob("*.poni"))
                named = [f for f in files if det.lower() in f.name.lower()]
                poni = str((named or files)[0]) if files else ""
                if not files:
                    self._log(f"simulator: ⚠ no .poni files in {pp}")
            else:
                poni = str(pp)
        if not poni:
            pdir = pc.get("poni_directory") or (Path(self.project_root) / "poni"
                                                if self.project_root else "")
            pfile = (pc.get("poni_files") or {}).get(det.lower())
            if pdir and pfile:
                poni = str(Path(pdir) / pfile)
            if not poni or not Path(poni).is_file():
                poni = self._discover_poni(det) or poni

        mask = str(self.cfg["mask"] or "")
        if not mask:
            pdir = pc.get("poni_directory") or (Path(self.project_root) / "poni"
                                                if self.project_root else "")
            mfile = (pc.get("mask_files") or {}).get(det.lower())
            if pdir and mfile:
                mask = str(Path(pdir) / mfile)

        meta = str(self.cfg["metadata_format"] or pc.get("metadata_format") or "csv")
        return det, shape, poni, mask, meta.lower()

    def _two_d_base(self, folder: Path, detector: str) -> Path:
        """Where the 2D tree lives, given the configured save folder.

        The rig points ``data_dir`` straight at the 2D base (it already holds
        SAXS/). The hub, by contrast, hands us a PROJECT ROOT, whose documented
        layout is ``<project>/2D/SAXS``. Guessing wrong scatters frames where
        the reduction app will never look, so resolve it explicitly:

          • already named "2D"            → use as-is
          • already contains <detector>/  → use as-is (rig convention)
          • contains a "2D" sub-dir       → use that
          • otherwise                     → create and use <folder>/2D
        """
        mode = str(self.cfg.get("two_d_subdir", "auto") or "").strip()
        if mode and mode.lower() != "auto":
            return folder / mode                     # explicit override
        if not mode:
            return folder                            # forced flat / rig style
        if folder.name == "2D":
            return folder
        if (folder / detector).is_dir():
            return folder
        if (folder / "2D").is_dir():
            return folder / "2D"
        return folder / "2D"

    # ── main entry point ─────────────────────────────────────────────────────
    def collect(self, *, prefix: str, role: str = "sample", data_dir: str = "",
                exposure: float = 1.0, frames: int = 1,
                temperature: float = 25.0, recipe_id: str = "") -> dict:
        self._last_data_dir = str(data_dir or "")     # widen the config.yml search
        det, shape, poni, mask_path, meta_fmt = self._resolve()
        if shape[0] <= 0 or shape[1] <= 0:
            raise ValueError(
                f"detector shape for {det} resolved to {shape} — cannot generate "
                f"frames. Fix detector_shapes in the project config.yml or "
                f"simulator.shape in reactor/config.yml.")
        # A stop event left set by a previous close()/backend switch would
        # silently produce zero or partial output on every later acquisition.
        self._stop = threading.Event()
        # NEVER default to the process CWD: an unset save folder used to scatter
        # 4 MB frames into whatever directory the app happened to start in.
        base = str(data_dir or self.project_root or "").strip()
        if not base:
            raise ValueError(
                "no save folder given for the simulated acquisition — set the "
                "Save folder in the reactor app (spec.data_dir), or "
                "spec.mock_data_dir. Refusing to write into the current "
                "working directory.")
        folder = Path(base).expanduser()
        two_d = self._two_d_base(folder, det)

        # SAFETY INTERLOCK: never write synthetic frames where real detector
        # data lives. The filename convention is identical, so a collision would
        # silently overwrite genuine beamtime data.
        assert_safe_to_simulate(two_d / det)

        # Create the 2D/<detector> tree up front so the folders exist as soon as
        # a collection starts — not only once the first frame lands.
        created = not (two_d / det).is_dir()
        (two_d / det).mkdir(parents=True, exist_ok=True)
        mark_simulated_dir(two_d)          # stamp so the data is always traceable
        mark_simulated_dir(two_d / det)
        if created:
            self._log(f"simulator: created {two_d / det}")

        # geometry: real .poni when available, otherwise a synthetic fallback so
        # the simulator still works before calibration has been done
        if poni and Path(poni).is_file():
            q = q_map(poni, shape)
            geom = f"poni {Path(poni).name}"
        else:
            q = synthetic_q_map(shape, dist_m=float(self.cfg["fallback_dist_m"]))
            why = (f"'{poni}' not found" if poni else
                   "no poni_directory/poni_files.saxs in the project config.yml")
            geom = f"SYNTHETIC geometry ({why})"
            self._log(f"simulator: ⚠ using {geom} at "
                      f"{self.cfg['fallback_dist_m']}m — the pattern will NOT match "
                      f"your real detector distance. Set poni_files.saxs in the "
                      f"project config.yml (or simulator.poni) to use the real one.")

        mask = load_mask(mask_path, shape)
        bstop = beamstop_mask(shape, q, q_beamstop=float(self.cfg["q_beamstop"]))

        is_bkg = str(role).lower().startswith("b")
        truth = truth_from_recipe(self._recipe or {}, self.cfg["truth"],
                                  seed_key=recipe_id or prefix)
        rng = np.random.default_rng(abs(hash((recipe_id or prefix, role))) % 2**32)

        if is_bkg:
            self._log(f"simulator: background acquisition '{prefix}' "
                      f"({frames}×{exposure:g}s, {geom}) — solvent only")
        else:
            self._log(f"simulator: sample acquisition '{prefix}' "
                      f"({frames}×{exposure:g}s, {geom}) — "
                      f"TRUE R={truth['R_nm']:.2f} nm, PDI={truth['pdi']:.3f}")

        def make_image(_i):
            return simulate_frame(
                q, truth["R_nm"], truth["pdi"],
                exposure_s=exposure, flux=float(self.cfg["flux"]),
                scale=float(self.cfg["scale"]),
                solvent_bkg=float(self.cfg["solvent_bkg"]),
                capillary=float(self.cfg["capillary"]),
                porod=float(self.cfg["porod"]),
                beamstop=bstop, mask=mask, rng=rng, particles=not is_bkg)

        writer = AcquisitionWriter(
            two_d, detector=det, metadata_format=meta_fmt,
            speed_factor=float(self.cfg["speed_factor"]),
            name_template=str(self.cfg["name_template"]),
            log=self._log, stop_event=self._stop)

        files = writer.write_acquisition(
            prefix, frames, make_image, exposure_s=exposure,
            transmission=float(self.cfg["transmission"]), temperature=temperature)

        rec = {"prefix": prefix, "role": role, "files": [str(f) for f in files],
               "n_frames": len(files), "detector": det, "geometry": geom,
               "two_d_dir": str(two_d), "detector_dir": str(two_d / det),
               "created_dirs": created,
               "truth": None if is_bkg else truth, "recipe_id": recipe_id}
        self.last = rec
        self.history.append(rec)
        return rec

    def describe_truth(self) -> str:
        return describe(self.cfg["truth"])
