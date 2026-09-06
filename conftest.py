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

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Tests never run against a live hub, so the event-bus reconnect thread each app
# module spawns at import has nothing to connect to and just retries forever.
# Loading app modules with importlib (several tests do, some with many unique
# tags) accumulated dozens of these threads across the session; their scheduling
# jitter made wall-clock/`sleep`-based monitor tests flaky under load. Default the
# bus off for the whole suite — apps degrade gracefully (emit_* drops the event).
# A test that specifically needs the client thread can still monkeypatch it back.
os.environ.setdefault("SWAXS_NO_BUS", "1")

# Same reasoning for the apps' module-level auto-start daemons (the analyzer's
# folder _watcher and every app's _boot_resume). No test relies on them firing on
# import — they all call _boot_resume/_analyze_file/etc. directly — but left
# running they poll folders and render plots (pyplot is not thread-safe, N14),
# racing a test's own synchronous _analyze_file and flaking under load. Disable
# the auto-start for the suite; explicit /api/monitor/start still works.
os.environ.setdefault("SWAXS_NO_WATCH", "1")

# Preload the REAL scipy before any test module is collected, so the numpy-only
# tests (which stub scipy only "if scipy not in sys.modules") leave the real
# library in place. Keeps src.analysis.nanoparticle (scipy.signal/ndimage/...)
# importable in the same session. Guarded: if scipy isn't installed, the stub
# tests still run as before.
try:  # pragma: no cover - environment-dependent
    import scipy.optimize  # noqa: F401
    import scipy.signal    # noqa: F401
    import scipy.ndimage   # noqa: F401
    import scipy.stats     # noqa: F401
except Exception:
    pass

# Same guard for fabio: preload the real fabio (+ CBF/EDF submodules) so the
# raw→CBF conversion tests (src.preprocess.raw_convert) keep a real fabio in the
# shared session, rather than a numpy-only test's stub.
try:  # pragma: no cover - environment-dependent
    import fabio            # noqa: F401
    import fabio.cbfimage   # noqa: F401
    import fabio.edfimage   # noqa: F401
except Exception:
    pass

# And for pyFAI + pandas: the 2D simulator (src.simulator.pattern) builds its
# per-pixel q map with a real AzimuthalIntegrator, and the reduction pipeline
# reads CSV metadata with pandas. Without this preload a numpy-only test's stub
# wins the race and the simulator silently writes no frames.
try:  # pragma: no cover - environment-dependent
    import pyFAI                                # noqa: F401
    import pyFAI.integrator.azimuthal           # noqa: F401
except Exception:
    try:
        import pyFAI.azimuthalIntegrator        # noqa: F401  (older pyFAI)
    except Exception:
        pass
try:  # pragma: no cover - environment-dependent
    import pandas           # noqa: F401
except Exception:
    pass
