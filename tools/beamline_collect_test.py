#!/usr/bin/env python
"""
beamline_collect_test.py — test the 2D data-collection trigger in isolation.

Uses the SAME driver/macro path the reactor uses, but on its own — no pumps, no
run, no pipeline. SAFE BY DEFAULT: it only RENDERS the filled SPEC macro and
shows you exactly what would run (and where it saves). It fires a real
acquisition (shutter opens, X-rays) ONLY with --fire and a confirmation.

Recommended order:
    # 1) dry-run: see the filled macro + save path, send nothing
    uv run tools/beamline_collect_test.py --id test1 --frames 2 --exposure 30

    # 2) simulate end-to-end (no hardware)
    uv run tools/beamline_collect_test.py --id test1 --mock --fire --yes

    # 3) on the rig, actually collect (asks y/N; opens shutter):
    uv run tools/beamline_collect_test.py --id test1 --fire

Config (bServer URL, macro_file, data_dir, tags, exposure/frames defaults) comes
from reactor/config.yml (spec:). --macro-file / --data-dir override for testing.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.reactor import load_config                          # noqa: E402
from src.beamline import make_beamline                       # noqa: E402
from src.beamline.driver import render_macro                 # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", default="test_" + time.strftime("%Y%m%d_%H%M%S"),
                    help="recipe/condition id (goes into the filename)")
    ap.add_argument("--role", choices=["sample", "background"], default="sample")
    ap.add_argument("--frames", type=int, default=None, help="override spec.frames")
    ap.add_argument("--exposure", type=float, default=None, help="override spec.exposure_s")
    ap.add_argument("--data-dir", default=None, help="override spec.data_dir (main_folder)")
    ap.add_argument("--macro-file", default=None, help="override spec.macro_file")
    ap.add_argument("--mock", action="store_true", help="use the simulator, not real SPEC")
    ap.add_argument("--fire", action="store_true", help="ACTUALLY run the collection (else dry-run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    cfg = load_config()
    spec = cfg.setdefault("spec", {})
    spec["backend"] = "mock" if args.mock else "real"
    if args.macro_file:
        spec["macro_file"] = args.macro_file
    if args.data_dir is not None:
        spec["data_dir"] = args.data_dir

    frames = args.frames if args.frames is not None else int(spec.get("frames", 1))
    exposure = args.exposure if args.exposure is not None else float(spec.get("exposure_s", 1.0))
    tag = spec.get("sample_tag", "sample") if args.role == "sample" else spec.get("bkg_tag", "bkg")
    data_dir = str(spec.get("data_dir", ""))
    sample = f"{args.id}_{tag}"
    path = f"{data_dir.rstrip('/')}/{sample}" if data_dir else sample
    params = dict(recipe_id=args.id, role=args.role, sample=sample, main_folder=data_dir,
                  path=path, temperature=None, exposure=exposure, frames=frames)

    print(f"# backend={spec['backend']}  role={args.role}")
    print(f"# sample(filename) = {sample}")
    print(f"# frames = {frames}   exposure = {exposure} s/frame")
    print(f"# main_folder (data_dir) = {data_dir or '(unset!)'}")

    macro_file = spec.get("macro_file")
    if macro_file:
        try:
            rendered = render_macro(Path(macro_file).read_text(), params)
        except Exception as exc:
            print(f"!! could not read/render macro_file {macro_file!r}: {exc}")
            return 1
        out = spec.get("macro_out_file") or str(Path(macro_file).parent / "_autopilot_run.mac")
        qdo = spec.get("qdo_cmd", 'qdo "{file}"').format(file=out)
        print(f"# macro_file    = {macro_file}")
        print(f"# will write to = {out}")
        print(f"# will run      = {qdo}")
        print("# ---- filled macro ----")
        print(rendered)
        print("# -----------------------")
    else:
        print("# no macro_file set → named-command mode:")
        print(f"#   {spec.get('newfile_cmd','newfile {path}').format(path=path)}")
        print(f"#   {spec.get('collect_cmd','ct {exposure}').format(exposure=exposure, frames=frames, **params)}")

    if not args.fire:
        print("\nDRY-RUN — nothing sent. Re-run with --fire to actually collect.")
        return 0

    if not args.yes:
        warn = "" if args.mock else "  ⚠ THIS OPENS THE SHUTTER AND COLLECTS X-RAYS."
        ans = input(f"Fire the collection now?{warn} [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted — nothing sent.")
            return 0

    bl = make_beamline(cfg)
    print("# firing…")
    t0 = time.time()
    try:
        bl.collect(**params)
    except Exception as exc:
        print(f"!! collection failed: {exc}")
        return 1
    print(f"# done in {time.time()-t0:.1f}s. Saved under {data_dir}/2D/…  as {sample}_*")
    if args.mock and getattr(bl, "collections", None):
        print("# (mock) recorded:", bl.collections[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
