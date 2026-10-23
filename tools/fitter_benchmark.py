#!/usr/bin/env python3
"""fitter_benchmark.py: score the fitting methods on simulated curves of known shape.

    python tools/fitter_benchmark.py --n 50 --split practice
    python tools/fitter_benchmark.py --n 50 --split heldout --methods catalogue_bic
    python tools/fitter_benchmark.py --report-only --out logs/fitter_benchmark/practice.jsonl
    python tools/fitter_benchmark.py --n 40 --split hard_heldout --methods catalogue_bic llm_assisted
                                     (real-data problems; llm_assisted needs the AI login)

Results are appended to a JSONL file one case at a time and finished cases are
skipped on the next run, so a long benchmark can be stopped (Ctrl-C, or
--time-limit) and resumed. A Markdown report is written next to it.
See src/fitting/benchmark.py for the cases and the scores.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fitting import benchmark as B  # noqa: E402

DEFAULT_METHODS = ["ls_sphere", "ml_regressor", "catalogue_bic"]


def _one(args):
    case, methods = args
    return B.run_case(case, methods)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=50, help="number of cases (cycled over the shapes)")
    ap.add_argument("--split", choices=["practice", "heldout", "hard", "hard_heldout"], default="practice")
    ap.add_argument("--methods", nargs="+", default=DEFAULT_METHODS)
    ap.add_argument("--out", type=Path, default=None, help="results JSONL")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--time-limit", type=float, default=0, help="stop starting new cases after N s")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args(argv)
    out = a.out or ROOT / "logs" / "fitter_benchmark" / f"{a.split}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    if not a.report_only:
        done = set()
        if out.exists():
            for line in out.read_text().splitlines():
                try:
                    r = json.loads(line)
                    done.add((r["case"], r["method"]))
                except Exception:
                    pass
        todo = []
        for i in range(a.n):
            c = B.make_case(a.split, i)
            ms = [m for m in a.methods if (c["id"], m) not in done]
            if ms:
                todo.append((c, ms))
        print(f"{len(todo)} case(s) to run, {len(done)} result(s) already in {out}")
        t0 = time.time()
        # Hand out a few cases at a time and stop handing out more once the
        # time limit is reached, so a limited run ends soon after it.
        queue = list(todo)
        if a.workers <= 1:                       # single process: simple, easy to stop
            with out.open("a") as fh:
                while queue and not (a.time_limit and time.time() - t0 > a.time_limit):
                    for r in _one(queue.pop(0)):
                        fh.write(json.dumps(r, default=float) + "\n")
                    fh.flush()
            if queue:
                print(f"time limit reached; {len(queue)} case(s) left for the next run")
            queue = []
        with ProcessPoolExecutor(max_workers=max(a.workers, 1)) as pool, out.open("a") as fh:
            running = set()
            while queue or running:
                while queue and len(running) < a.workers and not (
                        a.time_limit and time.time() - t0 > a.time_limit):
                    running.add(pool.submit(_one, queue.pop(0)))
                if not running:
                    break
                done_now, running = wait(running, return_when=FIRST_COMPLETED)
                for f in done_now:
                    for r in f.result():
                        fh.write(json.dumps(r, default=float) + "\n")
                    fh.flush()
            if queue:
                print(f"time limit reached; {len(queue)} case(s) left for the next run")
        print(f"ran for {time.time() - t0:.0f} s")

    rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()] if out.exists() else []
    scores = B.score(rows)
    md = B.report_markdown(scores, title=f"Fitter benchmark ({out.stem}, {len({r['case'] for r in rows})} cases)")
    out.with_suffix(".md").write_text(md)
    out.with_suffix(".scores.json").write_text(json.dumps(scores, indent=2))
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
