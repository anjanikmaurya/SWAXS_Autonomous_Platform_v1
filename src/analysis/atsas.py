"""
src/analysis/atsas.py — ATSAS command-line wrappers
===================================================
Thin, defensive wrappers around the ATSAS suite (must be installed and on PATH).
Each runner shells out, captures stdout/stderr, parses the numbers we need with
tolerant regexes, and ALWAYS returns the raw output too — so nothing is lost if
an ATSAS version formats things slightly differently.

Tools wrapped:
  • autorg    → Rg, I(0), qRg range, quality            (run_autorg)
  • datgnom   → GNOM p(r), Dmax, real-space Rg/I0        (run_datgnom)
  • datporod  → Porod volume (from a GNOM .out)          (run_datporod)
  • datvc     → volume of correlation + MW               (run_datvc)
  • datmw     → molecular-weight estimate                (run_datmw)
  • dammif    → ab-initio bead model (slow; launched)    (run_dammif)

If a binary is missing, the runner returns {"error": "<tool> not found …"} so
the app degrades gracefully.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_TOOLS = ("autorg", "datgnom", "datporod", "datvc", "datmw", "dammif", "gnom")


def available() -> dict:
    """Return {tool: path|None} for each ATSAS binary on PATH."""
    return {t: shutil.which(t) for t in _TOOLS}


def _run(args: list[str], timeout: float = 60.0, cwd: str | None = None):
    """Run a command; return (rc, stdout, stderr). rc=-1 on launch failure."""
    try:
        p = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -2, "", f"timed out after {timeout:.0f}s"
    except FileNotFoundError:
        return -1, "", "binary not found"
    except Exception as exc:                       # noqa: BLE001
        return -1, "", str(exc)


def _floats(text: str):
    return [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)]


def _first_float_after(text: str, *keys: str):
    """First number appearing after any of the key labels (case-insensitive)."""
    for k in keys:
        m = re.search(re.escape(k) + r"\s*[:=]?\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)",
                      text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


# ── autorg ────────────────────────────────────────────────────────────────────

def run_autorg(path: str | Path, timeout: float = 60.0) -> dict:
    if not shutil.which("autorg"):
        return {"error": "autorg not found in PATH (is ATSAS installed/activated?)."}
    # Try CSV format first (stable, machine-readable in ATSAS 3.x).
    rc, out, err = _run(["autorg", "--format", "csv", str(path)], timeout)
    res: dict = {"tool": "autorg", "raw": (out or err)[:2000]}
    rows = [ln for ln in out.splitlines() if "," in ln and "Rg" not in ln]
    if rc == 0 and rows:
        c = rows[-1].split(",")
        try:
            res.update({"Rg": float(c[1]), "Rg_err": float(c[2]),
                        "I0": float(c[3]), "I0_err": float(c[4]),
                        "first_point": int(float(c[5])), "last_point": int(float(c[6])),
                        "quality": float(c[7])})
            return res
        except (IndexError, ValueError):
            pass
    # Fallback: parse the human-readable output.
    rc, out, err = _run(["autorg", str(path)], timeout)
    res["raw"] = (out or err)[:2000]
    rg = _first_float_after(out, "Rg")
    i0 = _first_float_after(out, "I(0)", "I0")
    if rg is None:
        return {"error": f"autorg failed: {(err or out).strip()[:200]}", "raw": res["raw"]}
    res.update({"Rg": rg, "I0": i0, "quality": _first_float_after(out, "Quality")})
    return res


# ── datgnom (p(r)) ──────────────────────────────────────────────────────────────

def _parse_gnom_out(text: str) -> dict:
    """Extract Dmax, real-space Rg/I0, and the p(r) table from a GNOM .out."""
    res: dict = {}
    res["Rg_real"] = _first_float_after(text, "Real space: Rg", "Real space Rg", "Rg =")
    res["I0_real"] = _first_float_after(text, "I(0) =", "I(0)")
    # p(r) table: lines with two/three numeric columns after the distribution header
    r, pr = [], []
    started = False
    for ln in text.splitlines():
        if re.search(r"Distance distribution", ln, re.IGNORECASE):
            started = True
            continue
        if started:
            nums = _floats(ln)
            if len(nums) >= 2 and ln.strip() and not re.search(r"[A-Za-z]{3,}", ln):
                r.append(nums[0]); pr.append(nums[1])
    if r:
        res["r"] = r; res["pr"] = pr
        res["Dmax"] = float(r[-1])
    return res


def run_datgnom(path: str | Path, rg: float | None = None,
                timeout: float = 90.0) -> dict:
    if not shutil.which("datgnom"):
        return {"error": "datgnom not found in PATH."}
    out_file = Path(tempfile.mkdtemp()) / (Path(path).stem + ".out")
    args = ["datgnom", str(path), "-o", str(out_file)]
    if rg:
        args += ["-r", str(rg)]
    rc, out, err = _run(args, timeout)
    if not out_file.is_file():
        return {"error": f"datgnom produced no output: {(err or out).strip()[:200]}"}
    parsed = _parse_gnom_out(out_file.read_text(errors="ignore"))
    if "Dmax" not in parsed:
        return {"error": "Could not parse GNOM .out (Dmax/p(r) not found).",
                "out_file": str(out_file)}
    parsed.update({"tool": "datgnom", "out_file": str(out_file)})
    return parsed


def run_datporod(gnom_out: str | Path, timeout: float = 60.0) -> dict:
    if not shutil.which("datporod"):
        return {"error": "datporod not found in PATH."}
    rc, out, err = _run(["datporod", str(gnom_out)], timeout)
    nums = _floats(out)
    if rc != 0 or not nums:
        return {"error": f"datporod failed: {(err or out).strip()[:200]}", "raw": out[:1000]}
    # datporod prints the Porod volume (last/Largest number on the line typically)
    return {"tool": "datporod", "porod_volume": nums[-1], "raw": out[:1000]}


# ── datvc / datmw ───────────────────────────────────────────────────────────────

def run_datvc(path: str | Path, timeout: float = 60.0) -> dict:
    if not shutil.which("datvc"):
        return {"error": "datvc not found in PATH."}
    rc, out, err = _run(["datvc", str(path)], timeout)
    if rc != 0:
        return {"error": f"datvc failed: {(err or out).strip()[:200]}", "raw": out[:1000]}
    return {"tool": "datvc",
            "Vc": _first_float_after(out, "Vc", "Volume of correlation"),
            "MW": _first_float_after(out, "MW", "Molecular weight"),
            "raw": out[:1500]}


def run_datmw(path: str | Path, method: str = "vc", timeout: float = 60.0) -> dict:
    if not shutil.which("datmw"):
        return {"error": "datmw not found in PATH."}
    rc, out, err = _run(["datmw", method, str(path)], timeout)
    if rc != 0:
        # some versions: datmw <file> -m <method>
        rc, out, err = _run(["datmw", str(path), "-m", method], timeout)
    if rc != 0:
        return {"error": f"datmw failed: {(err or out).strip()[:200]}", "raw": out[:1000]}
    return {"tool": "datmw", "method": method,
            "MW": _first_float_after(out, "MW", "Molecular weight", "Mass"),
            "raw": out[:1500]}


# ── dammif (slow ab-initio bead model) ──────────────────────────────────────────

def run_dammif(gnom_out: str | Path, out_dir: str | Path,
               mode: str = "fast", timeout: float = 900.0) -> dict:
    if not shutil.which("dammif"):
        return {"error": "dammif not found in PATH."}
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run(["dammif", str(gnom_out), "--mode", mode,
                         "--prefix", "dammif"], timeout, cwd=str(out_dir))
    if rc == -2:
        return {"error": f"dammif timed out (mode={mode}). Run heavier modes offline.",
                "raw": out[:1000]}
    pdbs = sorted(str(p) for p in out_dir.glob("dammif*.pdb"))
    if not pdbs:
        return {"error": f"dammif produced no model: {(err or out).strip()[:200]}",
                "raw": out[:1000]}
    return {"tool": "dammif", "mode": mode, "models": pdbs, "raw": out[:1500]}
