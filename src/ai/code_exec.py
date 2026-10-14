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
  4. RUNTIME AUDIT HOOK (Q2): before the preamble and the user code, the child
     installs a ``sys.addaudithook`` hook that raises PermissionError on network
     calls (socket connect/getaddrinfo/bind), starting programs (subprocess,
     os.system/exec*/spawn*/posix_spawn/fork), ctypes.dlopen, reads or folder
     listings outside the project and the Python install (sys.path), and writes,
     deletes or renames outside the temp work folder, assistant_outputs/ and the
     matplotlib cache (MPLCONFIGDIR, in the temp folder).
  5. The caller (assistant) must show the code and get user confirmation first.

This is a guard, not an OS sandbox: C extensions can still do I/O the audit hook
does not see. The human confirmation remains the real control.
"""
from __future__ import annotations

import ast
import base64
import os
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
    # block the raw-file-read + home-resolution vectors that let a snippet (or a
    # prompt-injected KB/PDF) exfiltrate secrets, e.g.
    #   pathlib.Path.home()/".claude/settings.json").read_text()  → gateway TOKEN.
    # Data analysis still reads .dat via np.loadtxt / pd.read_csv on manifest-known
    # paths; it never needs raw .open()/.read_text() or the home directory.
    "open", "read_text", "read_bytes", "read", "readlines", "readline",
    "home", "expanduser", "expandvars", "getenv",
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


# Runtime guard (Q2): installed in the CHILD before the preamble and the user
# code. The static check above is a name deny-list and can be walked around
# (matplotlib.subprocess..., np.loadtxt('/abs/path'), pd.read_csv('http://...'),
# np.savetxt('/tmp/x'), Path('/Users').iterdir()). A sys.addaudithook hook sees
# the real operation whatever the spelling, and an audit hook cannot be removed.
_GUARD = r'''
import os as _g_os, sys as _g_sys
try:
    import ctypes as _g_ctypes  # its import dlopens libpython; do it before the hook
except Exception:
    pass
def _g_norm(p):
    return _g_os.path.normcase(_g_os.path.realpath(_g_os.path.abspath(p)))
_G_READ = []
_G_WRITE = []
for _p in {read_roots!r}:
    _G_READ.append(_g_norm(_p))
for _p in {write_roots!r}:
    _G_WRITE.append(_g_norm(_p))
for _p in list(_g_sys.path):
    if _p:
        _G_READ.append(_g_norm(_p if _g_os.path.isdir(_p) else _g_os.path.dirname(_p)))
_G_READ.extend(_G_WRITE)
_G_DENY = ("subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
           "os.fork", "os.forkpty", "os.kill", "os.killpg", "pty.spawn", "ctypes.dlopen",
           "ctypes.dlsym", "socket.connect", "socket.getaddrinfo", "socket.bind",
           "socket.sendto", "socket.sendmsg", "socket.gethostbyname",
           "socket.gethostbyaddr", "socket.gethostbyname_ex", "_winapi.CreateProcess",
           "os.startfile", "winreg.", "webbrowser.open")
_G_WRITE_EVENTS = ("os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.symlink",
                   "os.link", "os.truncate", "os.chmod", "os.chown", "os.utime",
                   "shutil.rmtree", "shutil.move", "shutil.copyfile", "os.chflags")
_G_LIST_EVENTS = ("os.listdir", "os.scandir", "glob.glob")
_G_WFLAGS = (_g_os.O_WRONLY | _g_os.O_RDWR | getattr(_g_os, "O_APPEND", 0)
             | getattr(_g_os, "O_CREAT", 0) | getattr(_g_os, "O_TRUNC", 0))
def _g_under(p, roots):
    for r in roots:
        if p == r or p.startswith(r.rstrip(_g_os.sep) + _g_os.sep):
            return True
    return False
def _g_path_ok(p, roots):
    if isinstance(p, int):
        return True          # an fd that is already open
    if isinstance(p, bytes):
        p = _g_os.fsdecode(p)
    if not isinstance(p, str):
        p = _g_os.fspath(p)
    return _g_under(_g_norm(p), roots)
_g_busy = [False]
def _g_hook(event, args):
    if _g_busy[0]:
        return
    _g_busy[0] = True
    try:
        if event.startswith(_G_DENY):
            raise PermissionError("Blocked by the run_python sandbox: " + event)
        if event == "open":
            path = args[0]
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writing = bool((isinstance(flags, int) and flags & _G_WFLAGS)
                           or (isinstance(mode, str) and any(c in mode for c in "wax+")))
            if writing:
                if not _g_path_ok(path, _G_WRITE):
                    raise PermissionError("Blocked by the run_python sandbox: "
                                          "write outside the output folder: %s" % (path,))
            elif not _g_path_ok(path, _G_READ):
                raise PermissionError("Blocked by the run_python sandbox: "
                                      "read outside the project: %s" % (path,))
        elif event.startswith(_G_WRITE_EVENTS):
            for a in args[:2]:
                if isinstance(a, (str, bytes)) or hasattr(a, "__fspath__"):
                    if not _g_path_ok(a, _G_WRITE):
                        raise PermissionError("Blocked by the run_python sandbox: "
                                              "%s outside the output folder: %s" % (event, a))
        elif event in _G_LIST_EVENTS:
            a = args[0] if args else "."
            if a is None:
                a = "."
            if (isinstance(a, (str, bytes)) or hasattr(a, "__fspath__")) \
                    and not _g_path_ok(a, _G_READ):
                raise PermissionError("Blocked by the run_python sandbox: "
                                      "listing outside the project: %s" % (a,))
    finally:
        _g_busy[0] = False
_g_sys.addaudithook(_g_hook)
del _p
'''


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
    *,
    _static_check: bool = True,
    runtime_guard: bool = True,
) -> dict:
    """
    Run *code* under the guards. Returns
        {"ok": bool, "stdout": str, "error": str|None, "figure": b64|None}
    """
    ok, reason = check_code_safety(code) if _static_check else (True, "ok")
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
    mpl_dir = Path(tempfile.gettempdir()) / "swaxs_exec_mplconfig"
    try:
        mpl_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        mpl_dir = workdir / "mplconfig"
    write_roots = [str(workdir), str(outputs), str(mpl_dir)]
    read_roots  = [str(root)]
    script  = (
        (_GUARD.format(read_roots=read_roots, write_roots=write_roots)
         if runtime_guard else "")
        + _PREAMBLE.format(project=str(root), outputs=str(outputs))
        + "\n# ── user code ──\n"
        + textwrap.dedent(code)
        + "\n"
        + _POSTAMBLE.format(figpath=str(figpath))
    )
    script_path = workdir / "snippet.py"
    script_path.write_text(script)

    # Minimal environment: no proxy/network hints, no inherited secrets. HOME is
    # remapped to the workdir so `~` / Path.home() can't reach the real home
    # (where the gateway token lives).
    env = {"HOME": str(workdir), "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(mpl_dir),
           "PYTHONHASHSEED": "0", "PYTHONUTF8": "1"}
    if sys.platform == "win32":
        # Windows Python needs SYSTEMROOT/PATH/PATHEXT/TEMP to start. Do NOT leak
        # the real USERPROFILE/HOMEDRIVE/HOMEPATH/APPDATA — Path.home() reads
        # USERPROFILE, which would defeat the HOME jail; point them at the workdir.
        for _v in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "PATHEXT"):
            if _v in os.environ:
                env[_v] = os.environ[_v]
        _wd = Path(workdir)
        env["USERPROFILE"] = str(_wd)
        env["HOMEDRIVE"]   = _wd.drive or "C:"
        env["HOMEPATH"]    = "\\" + str(_wd).split(":", 1)[-1].lstrip("\\/")
    else:
        env["PATH"] = "/usr/bin:/bin"

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
