"""Pytest bootstrap for the SWAXS test suite.

Path handling is configured in ``pytest.ini`` (``pythonpath = .``); this file
only guarantees the project root is importable even when pytest is invoked in
ways that bypass that setting.

Note: several numpy-only unit tests install lightweight stand-in modules for
scipy / pandas / fabio / pyFAI / xraydb into ``sys.modules`` at import time. The
full-pipeline regression test (``tests/test_demo_pipeline_regression.py``)
insulates itself from those stubs with an autouse fixture that swaps in the real
libraries for the duration of each test and restores the stubs afterwards, so
the whole suite can run in one process.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
