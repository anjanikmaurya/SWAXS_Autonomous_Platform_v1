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
from .writer import AcquisitionWriter

DEFAULTS: dict = {
    "enabled":        False,
    "detector":       "SAXS",
    "shape":          [1043, 981],
    "poni":           "",         # blank → resolve from the project config
    "mask":           "",         # blank → resolve from the project config
    "speed_factor":   1.0,        # 1.0 = real time (honours exposure × frames)
    "flux":           1.0e6,      # counts/s scale at I(0)
    "scale":          800.0,      # particle signal strength
    "solvent_bkg":    2.0,        # flat incoherent solvent level
    "capillary":      5.0,        # capillary q⁻² upturn, quoted at q = 0.1 nm⁻¹
    "porod":          0.0,
    "q_beamstop":     0.02,       # nm⁻¹ — beamstop shadow radius in q
    "transmission":   0.62,
    "name_template":  "",
    "metadata_format": "",        # blank → from the project config.yml
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

    # ── configuration ────────────────────────────────────────────────────────
    def set_recipe(self, recipe) -> None:
        """Called by the controller when a run starts, so the simulated particles
        reflect the recipe actually being executed."""
        self._recipe = recipe

    def set_project_root(self, path: str) -> None:
        self.project_root = str(path or "")

    def _project_cfg(self) -> dict:
        if not self.project_root:
            return {}
        p = Path(self.project_root) / "config.yml"
        if not p.is_file():
            return {}
        try:
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

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

        poni = str(self.cfg["poni"] or "")
        if not poni:
            pdir = pc.get("poni_directory") or (Path(self.project_root) / "poni"
                                                if self.project_root else "")
            pfile = (pc.get("poni_files") or {}).get(det.lower())
            if pdir and pfile:
                poni = str(Path(pdir) / pfile)

        mask = str(self.cfg["mask"] or "")
        if not mask:
            pdir = pc.get("poni_directory") or (Path(self.project_root) / "poni"
                                                if self.project_root else "")
            mfile = (pc.get("mask_files") or {}).get(det.lower())
            if pdir and mfile:
                mask = str(Path(pdir) / mfile)

        meta = str(self.cfg["metadata_format"] or pc.get("metadata_format") or "csv")
        return det, shape, poni, mask, meta.lower()

    # ── main entry point ─────────────────────────────────────────────────────
    def collect(self, *, prefix: str, role: str = "sample", data_dir: str = "",
                exposure: float = 1.0, frames: int = 1,
                temperature: float = 25.0, recipe_id: str = "") -> dict:
        det, shape, poni, mask_path, meta_fmt = self._resolve()
        two_d = Path(data_dir or self.project_root or ".")

        # geometry: real .poni when available, otherwise a synthetic fallback so
        # the simulator still works before calibration has been done
        if poni and Path(poni).is_file():
            q = q_map(poni, shape)
            geom = f"poni {Path(poni).name}"
        else:
            q = synthetic_q_map(shape)
            geom = "synthetic geometry (no .poni found)"

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
               "truth": None if is_bkg else truth, "recipe_id": recipe_id}
        self.last = rec
        self.history.append(rec)
        return rec

    def describe_truth(self) -> str:
        return describe(self.cfg["truth"])
