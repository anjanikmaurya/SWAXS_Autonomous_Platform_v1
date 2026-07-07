"""
tests/test_code_exec.py
=======================
Guards for the run_python sandbox (`src/ai/code_exec.py`): the static AST check
must block dangerous operations, and the isolated runner must execute safe
scientific code (producing a figure) while rejecting blocked code.

Run:
    python tests/test_code_exec.py
    uv run pytest tests/test_code_exec.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.ai.code_exec import check_code_safety, run_user_code  # noqa: E402

_DANGEROUS = [
    "import os\nos.system('echo hi')",
    "import subprocess\nsubprocess.run(['ls'])",
    "import socket",
    "import shutil\nshutil.rmtree('/tmp/x')",
    "import urllib.request",
    "open('/etc/passwd', 'w')",
    "eval('2+2')",
    "exec('x=1')",
    "().__class__.__bases__[0].__subclasses__()",
    "__import__('os')",
]

_SAFE = [
    "import numpy as np\nprint(np.arange(5).sum())",
    "import matplotlib.pyplot as plt\nimport numpy as np\nplt.plot(np.arange(3))",
    "q,I,s = load_dat  # name exists in preamble; reference only\nprint('ok')",
]


def test_dangerous_code_blocked():
    for c in _DANGEROUS:
        ok, why = check_code_safety(c)
        assert not ok, f"should block: {c!r}"


def test_safe_code_allowed():
    for c in _SAFE:
        ok, why = check_code_safety(c)
        assert ok, f"should allow: {c!r} -> {why}"


def test_runner_executes_safe_code_with_figure():
    res = run_user_code(
        "import numpy as np\nimport matplotlib.pyplot as plt\n"
        "q=np.linspace(0.02,2,100); I=q**-2\n"
        "print('pts', len(q))\nplt.loglog(q, I)")
    assert res["ok"], res.get("error")
    assert "pts 100" in res["stdout"]
    assert res["figure"], "expected a captured figure"


def test_runner_rejects_blocked_code():
    res = run_user_code("import os\nos.remove('x')")
    assert not res["ok"] and "Blocked" in (res["error"] or "")


def test_pathlib_allowed_but_writes_blocked():
    assert check_code_safety("import pathlib\np = pathlib.Path('x')")[0]
    assert check_code_safety("import glob")[0]
    ok, _ = check_code_safety("import pathlib\npathlib.Path('x').write_text('y')")
    assert not ok          # writing via pathlib is blocked


def test_load_dat_resolves_bare_filename(tmp_path=None):
    """The sandbox load_dat finds a file by bare name under the project."""
    import tempfile, os
    import numpy as np
    proj = tempfile.mkdtemp()
    sub = os.path.join(proj, "1D", "SAXS", "Averaged")
    os.makedirs(sub)
    np.savetxt(os.path.join(sub, "sample_avg.dat"),
               np.column_stack([np.linspace(0.1, 1, 20),
                                np.linspace(1, 0.1, 20),
                                np.full(20, 0.01)]))
    res = run_user_code(
        "q,I,s = load_dat('sample_avg.dat')\nprint('n', len(q))",
        project_root=proj)
    assert res["ok"], res.get("error")
    assert "n 20" in res["stdout"]


if __name__ == "__main__":
    tests = sorted(n for n in globals() if n.startswith("test_"))
    passed = failed = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {name}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed  ({len(tests)} total)")
    sys.exit(1 if failed else 0)
