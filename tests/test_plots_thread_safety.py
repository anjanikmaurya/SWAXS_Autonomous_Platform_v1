"""Plotting must be thread-safe and must not leak figures/file descriptors.

The assistant serves chat turns on a threaded Flask server. pyplot keeps global
state and is not thread-safe, so two turns plotting at once corrupted matplotlib
(`has no attribute 'get_data_path'`) and a figure orphaned by an error leaked
file descriptors until the process hit "Too many open files". src/ai/plots.py now
serialises every entry point and closes half-built figures on error.
"""
import base64
import threading

import numpy as np
import matplotlib.pyplot as plt

from src.ai import plots


def _q_I():
    q = np.linspace(0.05, 3.0, 200)
    x = q * 4.0
    I = 1e4 * (3 * (np.sin(x) - x * np.cos(x)) / x**3) ** 2 + 1.0
    return q, I


def test_concurrent_plotting_is_safe_and_leaks_no_figures():
    q, I = _q_I()
    errs = []

    def work():
        for _ in range(10):
            try:
                b = plots.plot_guinier(q=q, I=I, sigma=np.sqrt(I),
                                       q_min=0.05, q_max=0.2, Rg=3.1, I0=1e4)
                assert len(base64.b64decode(b)) > 1000
                plots.plot_pair_distance(np.linspace(0, 8, 80),
                                         np.exp(-(np.linspace(0, 8, 80) - 4) ** 2),
                                         Dmax=8)
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))

    threads = [threading.Thread(target=work) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errs, f"concurrent plotting raised: {errs[:3]}"
    assert plt.get_fignums() == [], "figures were left open (fd/memory leak)"


def test_a_failing_plot_closes_its_figure():
    # bad input (mismatched arrays) should raise but not leave a figure open
    before = list(plt.get_fignums())
    try:
        plots.plot_curve(q=np.array([1.0, 2.0]), I=np.array([1.0]))  # length mismatch
    except Exception:
        pass
    assert plt.get_fignums() == before, "a failed plot leaked its figure"
