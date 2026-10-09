# Auto-Fit & Optimiser — methods refactor (design / plan)

Status: **plan only — no code written yet.** This is the agreed design before any
change. It turns the analyzer (port 5107) from a single hardcoded pipeline into a
set of selectable **fitting** and **optimising** methods, using the same
left-rail + tabs shell the analysis app (5106) already ships.

---

## 1. Why

Today the analyzer fuses two capabilities, both hardcoded:

- **Fitting** (per profile): `src/analysis/nanoparticle.py::analyze_profile` — a
  polydisperse-sphere form-factor fit by scipy `least_squares` (trf, bounded),
  Schulz or log-normal distribution (`auto` fits both, keeps the better), Guinier
  fallback; returns size / PDI / invariant / 0–1 confidence.
- **Optimising** (per campaign): `src/optimizer/` — `CampaignController.ask()/tell()`
  over a numpy GP (RBF) surrogate with Expected-Improvement acquisition, Sobol
  cold-start, loss `((size − target)/tolerance)² + w·(PDI/PDI_cap)`.

Neither can be swapped or compared. The goal: make each a **registered method**
the operator picks from a left rail, so new fitters (Guinier/Porod, sasmodels)
and new optimisers (random/grid, CMA-ES, **BAX**) are additive, not rewrites.

---

## 2. The shell (reuse the analysis app pattern)

The analysis app already implements exactly what we want; copy its primitives
(verbatim CSS, matching the design system):

- **Left `.nav` rail** (250px): `.navtab` buttons grouped under `.lbl` section
  labels; `.active` = cardinal left-border; `.soon` badge for not-yet-built
  methods; `:disabled` for those.
- **`.toptabbar` / `.toptab`** for the sub-views of the selected method, with
  `showTab()` toggling `.page` panels.

Left rail layout for the analyzer:

```
METHODS
  Fitting
    ▸ Least-squares sphere      (current; default)
      Guinier / Porod           soon
      sasmodels form+structure  soon
  Optimising
    ▸ Bayesian — GP + EI        (current; default)
      Random / grid search      soon
      CMA-ES                    soon
      BAX (Bayesian Alg. eXec.) soon
```

Selecting a Fitting method sets what runs on each profile; selecting an
Optimising method sets what drives the closed loop. The main area shows that
method's panels (the existing Profiles+Fit-plot for fitting; Campaign +
Parameter-space for optimising).

---

## 3. The method interfaces (the core of the refactor)

Two small registries in `src/`, each a dict of `id → class`. A method is a class
implementing a fixed protocol; registering it is one line.

### 3.1 Fitting methods — `src/fitting/`

```python
class Fitter(Protocol):
    id: str                      # "ls_sphere"
    label: str                   # "Least-squares sphere"
    params_schema: list[dict]    # UI controls this method needs (optional)

    def fit(self, q, I, sigma=None, **opts) -> dict:
        """Return the standard result dict: distribution, size, pdi, invariant,
        confidence, guinier, fit{scale,background}, diagnostics. Never raises —
        on failure returns confidence 0.0 (matches analyze_profile today)."""
```

- The current code becomes `src/fitting/ls_sphere.py`, a thin wrapper that calls
  the existing `analyze_profile` — **no maths moves or changes**, so the fit the
  loop sees is byte-identical on day one.
- `src/fitting/__init__.py` exposes `REGISTRY: dict[str, Fitter]` and
  `get(id)`; the analyzer asks the registry rather than importing `analyze_profile`
  directly.
- Result dict shape is frozen as the contract (it is already what `_analyze_file`,
  the manifest writer, and the plot all consume), so new fitters must return it.

### 3.2 Optimising methods — `src/optimizer/methods/`

```python
class Optimiser(Protocol):
    id: str                      # "gp_ei"
    label: str                   # "Bayesian — GP + EI"

    def start(self, space, **cfg) -> None: ...
    def ask(self) -> dict | None: ...                 # next recipe, or None
    def tell(self, params, size, pdi, confidence, recipe_id="") -> None: ...
    @property
    def history(self) -> list: ...
    @property
    def status_str(self) -> str: ...
    # best / budget / converged_condition — as CampaignController exposes today
```

- `CampaignController` **already is** this interface. It becomes the `gp_ei`
  method (register it; keep the class). `ask/tell/history/budget/status_str/best`
  are unchanged, so `_feed_campaign`, `_advance_campaign`, `_restore_campaign`,
  `_continue_run` keep working.
- New optimisers live beside it: `random.py`, `cmaes.py`, `bax.py`. They share
  `ParameterSpace` and the loss in `campaign.py` (factor the loss into a small
  `objective(result, cfg)` helper both can import, so every method optimises the
  SAME scalar).

### 3.3 BAX — explicitly a distinct method, not GP+EI

BAX (Bayesian Algorithm eXecution) is **not** the same as Expected-Improvement
optimisation and will be its own `src/optimizer/methods/bax.py`:

- EI asks "where is the optimum?" and maximises expected improvement in the loss.
- BAX asks "what measurement most reduces my uncertainty about a target
  *property* of the function?" (e.g. the whole acceptance-band contour, or the
  level set `size = target`), and picks the point of highest expected information
  gain about that property — not necessarily near the optimum.
- Practical consequence for us: BAX can map the band `|size − target| ≤ tol`
  across the recipe space, not just chase one recipe. It reuses the same GP
  surrogate (`src/optimizer/gp.py`) but a different acquisition. Flagged `soon`
  until implemented and validated in silico against `ground_truth`.

---

## 4. What stays the same (de-risking)

- The result-dict contract, the `.dat`/manifest/`fit.complete` outputs, the
  watcher, `_startup_present` freeze, backfill, retention, run-tag scoping, and
  the matplotlib lock — all unchanged. Methods plug in beneath them.
- Defaults reproduce today's behaviour exactly: fitting = `ls_sphere`,
  optimising = `gp_ei`. A fresh project behaves identically; the only visible
  change is that the method is now shown (and selectable) rather than implicit.
- The closed loop's correctness checks (confidence gate, budget, convergence)
  live in the optimiser method, so swapping fitters never changes loop logic.

---

## 5. Migration steps (ordered, each testable)

1. **Shell, no behaviour change.** Add the left `.nav` rail + `.toptab`s to the
   analyzer template; one Fitting entry (`Least-squares sphere`, active) and one
   Optimising entry (`Bayesian — GP + EI`, active), the rest `soon`/disabled.
   Panels are the current ones, just reorganised under tabs. Test: UI test + a
   smoke check that selecting the only methods changes nothing.
2. **Fitting registry.** Create `src/fitting/` with the `Fitter` protocol,
   `ls_sphere.py` wrapping `analyze_profile`, and `REGISTRY`. Point `_analyze_file`
   at `fitting.get(active_id).fit(...)`. Test: existing analyzer tests stay green
   (byte-identical fit).
3. **Optimiser registry.** Create `src/optimizer/methods/` with the `Optimiser`
   protocol and register `CampaignController` as `gp_ei`; factor the loss into
   `objective()`. Point the campaign routes at `optimisers.get(active_id)`. Test:
   `test_optimizer*`, resume/continue tests stay green.
4. **Expose selection + persist it.** `GET/POST /api/method` to read/set the
   active fitter and optimiser; remember per project in the manifest/config.
   Test: a new `test_method_registry.py`.
5. **First new method (proof the plug works).** Add `random`/`grid` optimiser
   (cheapest), wire the rail entry live. Test: it drives a mock campaign.
6. **BAX** as its own method, GP surrogate reused, new acquisition; validate in
   silico against `ground_truth` before un-`soon`-ing it.

---

## 6. Open questions to settle before build

- One active optimiser at a time, or allow comparing two on the same history?
  (Start with one; comparison is a later feature.)
- Should the fitter be selectable **per profile** or **per project**? (Per
  project to start — simplest, matches the loop's single-method assumption.)
- Where to persist the chosen methods — `manifest.project_meta`, or the
  analyzer's own config? (Lean `project_meta`, so a resume restores the method.)
