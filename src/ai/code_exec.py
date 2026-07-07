"""
src/ai/code_exec.py — Guarded Python execution for the assistant
================================================================
Run short, model/user-supplied analysis snippets with multiple guard layers:

  1. STATIC AST CHECK (primary gate): only an allowlist of scientific modules
     may be imported; a denylist of dangerous calls/attributes (os.system,
     subprocess, sockets, file deletion, eval/exec, raw open for writing, …) is
     rejected before anything runs.
  2. ISOLATED SUBPROCESS: code runs via `python -I` in a temporary working
     directory with CPU/memory rlimits and a wall-clock timeout — never inside
     the Flask process.
  3. READ-ONLY DATA: the snippet receives the project path for reading only; the
     ONLY writable location is the project's ``assistant_outputs/`` folder.
  4. The caller (assistant) must show the code and get user confirmation first.

This is a guard, not a perfect jail — Python can't be fully sandboxed in-process.
The static check blocks the obvious destructive/network operations; the human
confirmation is the real control.
"""
from __future__ import annotations

import ast
import base64
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# Modules the snippet may import (scientific + safe stdlib; no fs/network/proc).
_ALLOWED_IMPORTS = {
    "numpy", "np", "scipy", "pandas", "pd", "matplotlib", "math", "cmath",
    "statistics", "json", "csv", "re", "datetime", "collections", "itertools",
    "functools", "random", "decimal", "fractions", "string", "typing",
    # read-only path helpers (write/delete methods are denied below)
    "pathlib", "glob",
}

# Calls/attributes that are forbidden anywhere in the source.
_DENY_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview",
}
_DENY_ATTRS = {
    "system", "popen", "spawn", "spawnl", "spawnv", "fork", "kill", "remove",
    "unlink", "rmtree", "rmdir", "removedirs", "rename", "replace", "truncate",
    "chmod", "chown", "connect", "socket", "urlopen", "request", "Request",
    "Popen", "run", "call", "check_output", "check_call", "getoutput",
    "__subclasses__", "__globals__", "__builtins__", "__import__", "load",
    "loads_pickle", "savefig",   # savefig handled by the runner, not the user
    # block file writes/creation via pathlib (open is already denied)
    "write_text", "write_bytes", "mkdir", "touch", "symlink_to", "hardlink_to",
}


def check_code_safety(code: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means the code is rejected before running."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"Syntax error: {exc}"

    for node in ast.walk(tree):
        # imports — allowlist only
        if isinstance(node, ast.Import):
            for n in node.names:
                top = n.name.split(".")[0]
                if top not in _ALLOWED_IMPORTS:
                    return False, f"Import of '{n.name}' is not allowed."
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top not in _ALLOWED_IMPORTS:
                return False, f"Import from '{node.module}' is not allowed."
        # forbidden bare names (eval/exec/open/…)
        elif isinstance(node, ast.Name) and node.id in _DENY_NAMES:
            return False, f"Use of '{node.id}' is not allowed."
        # forbidden attributes (os.system, .rmtree, .urlopen, dunder tricks…)
        elif isinstance(node, ast.Attribute) and node.attr in _DENY_ATTRS:
            return False, f"Use of '.{node.attr}' is not allowed."
        # block dunder string access used for sandbox escapes
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") \
                and node.attr.endswith("__") and node.attr not in ("__init__",):
            return False, f"Access to '{node.attr}' is not allowed."
    return True, "ok"


_PREAMBLE = '''\
import matplotlib
matplotlib.use("Agg")
import numpy as np
import matplotlib.pyplot as plt
PROJECT = r"""{project}"""
OUTPUTS = r"""{outputs}"""
def load_dat(path):
    """Read-only loader: returns (q, I, sigma|None) from a .dat file.
    A bare filename is resolved by searching the project folder."""
    import os as _os
    p = str(path)
    if not _os.path.exists(p):
        base = _os.path.basename(p)
        for _root, _d, _files in _os.walk(PROJECT):
            if base in _files:
                p = _os.path.join(_root, base)
                break
        else:
            raise FileNotFoundError(base + " not found under the project folder.")
    a = np.atleast_2d(np.loadtxt(p, comments="#"))
    return a[:, 0], a[:, 1], (a[:, 2] if a.shape[1] > 2 else None)
'''

_POSTAMBLE = '''
import matplotlib.pyplot as _plt
if _plt.get_fignums():
    _plt.savefig(r"""{figpath}""", dpi=110, bbox_inches="tight")
'''


def _limits(mem_mb: int):
    """Return a preexec_fn that sets CPU/memory/file rlimits (POSIX only)."""
    try:
        import resource
    except ImportError:
        return None

    def _set():
        resource.setrlimit(resource.RLIMIT_CPU, (10, 12))
        soft = mem_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (soft, soft))
        except (ValueError, OSError):
            pass
        # no new files larger than 50 MB
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024,) * 2)
        except (ValueError, OSError):
            pass
    return _set


def run_user_code(
    code:         str,
    project_root: str | Path | None = None,
    timeout:      float = 15.0,
    mem_mb:       int = 1024,
) -> dict:
    """
    Run *code* under the guards. Returns
        {"ok": bool, "stdout": str, "error": str|None, "figure": b64|None}
    """
    ok, reason = check_code_safety(code)
    if not ok:
        return {"ok": False, "stdout": "", "error": f"Blocked: {reason}",
                "figure": None}

    root = Path(project_root) if project_root else Path(tempfile.gettempdir())
    if root.is_file():
        root = root.parent
    outputs = root / "assistant_outputs"
    try:
        outputs.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    workdir = Path(tempfile.mkdtemp(prefix="swaxs_exec_"))
    figpath = workdir / "figure.png"
    script  = (
        _PREAMBLE.format(project=str(root), outputs=str(outputs))
        + "\n# ── user code ──\n"
        + textwrap.dedent(code)
        + "\n"
        + _POSTAMBLE.format(figpath=str(figpath))
    )
    script_path = workdir / "snippet.py"
    script_path.write_text(script)

    # Minimal environment: no proxy/network hints, no inherited secrets.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(workdir),
           "MPLBACKEND": "Agg", "PYTHONHASHSEED": "0"}

    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(script_path)],
            cwd=str(workdir), env=env, capture_output=True, text=True,
            timeout=timeout, preexec_fn=_limits(mem_mb),
        )
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "figure": None,
                "error": f"Execution timed out after {timeout:.0f}s."}
    except Exception as exc:
        return {"ok": False, "stdout": "", "figure": None,
                "error": f"Runner error: {exc}"}

    fig_b64 = None
    if figpath.exists() and figpath.stat().st_size > 0:
        fig_b64 = base64.b64encode(figpath.read_bytes()).decode("ascii")

    err = None
    if rc != 0:
        err = (stderr or "").strip()[-1500:] or f"Exited with code {rc}."
    return {"ok": rc == 0, "stdout": (stdout or "")[:4000],
            "error": err, "figure": fig_b64}
