# SWAXS Platform — Full Pipeline Audit (2026-06-18)

Scope: correctness and consistency review across the whole hub-and-spoke
pipeline (reduction → viewer → background → analysis → assistant), the shared
`src/` package, the manifest data contract, the hub launcher, and
security/secret handling. Triggered by an AI-assistant crash report.

Verification was static only (no app launched, per CLAUDE.md): `py_compile` on
every module, stub-import numeric checks, and a live functional test of the
manifest concurrency/self-heal path. **All modules compile.**

---

## 1. Summary

| Severity | Finding | Status |
|---|---|---|
| **High (correctness)** | C1 — Assistant tool-loop saved `tool_use` turns without their `tool_result` turns → next request 400 (`tool_use ids … without tool_result`) | **Fixed** |
| **High (correctness)** | C2 — Scan averaging divided by full scan count even when scans were skipped → averaged I biased low, σ under-estimated | **Fixed** |
| **Medium (robustness)** | C3 — Assistant tool loop hitting its round cap left a dangling tool exchange | **Fixed** |
| **Medium (robustness)** | C4 — Assistant API-error path could persist an unbalanced history delta | **Fixed** |
| **Low (robustness)** | C5 — `average_and_save` metadata carry crashed on any non-numeric metadata value | **Fixed** |
| **Low (cosmetic)** | C6 — `/api/history` rendered tool-plumbing turns as blank/garbled bubbles | **Fixed** |
| Info | D1 — `check_imports.py` false positives + stale module list | Open (tooling) |
| Info | D2 — CLAUDE.md import table out of date (hub, analysis) | Open (doc) |
| Info | D3 — Background subtraction logic lives in `app.py`, not `src/` | Open (architecture) |
| Info | D4 — Averaging uses unweighted mean; `np.interp` edge-clamps out-of-range q | Open (enhancement) |

Everything else audited — reduction physics, background subtraction math,
analysis fits, manifest locking, security — was found **correct and consistent**
and required no change.

---

## 2. Correctness findings (fixed)

### C1 — Assistant conversation-history imbalance (the reported crash)
`src/ai/assistant.py`, agentic loop. Each round that used tools appended the
assistant `tool_use` turn to the saved `_history_delta` **but not** the matching
user `tool_result` turn. On the *next* user message the reconstructed history
therefore contained `tool_use` ids with no results immediately after — exactly
the API error reported (`messages.1: tool_use ids were found without
tool_result blocks`). With multiple tool calls in the prior turn, all of their
ids were orphaned.

Fix: the `tool_use` turn and its `tool_result` turn are now always appended
**together** to both the live `messages` and the saved `_history_delta`, so the
persisted history is always a valid, balanced sequence.

### C2 — Scan-averaging low-bias when a scan is dropped
`src/plot_reduction.py :: average_and_save`. Intensities were stacked into a
pre-zeroed array `I_stack[len(files)]`; a scan failing the `valid.sum() < 3`
check was `continue`d, leaving its row all zeros, yet the mean still divided by
`n = len(kw_files)` (and σ by the same `n`). Result: every dropped scan pulled
the averaged intensity toward zero and shrank the error bars.

Demonstration (1 of 4 scans dropped, true I = 2.0):
old mean → **1.5 (−25 %)**; fixed mean → **2.0**. σ-of-mean propagation
verified (`√Σσ²/n = 0.1732` for 3×σ=0.3).

Fix: only scans that actually contribute are stacked (`np.vstack` of collected
rows); `n`, the file count in the output filename, and the metadata carry all
use the contributing set.

### C3 / C4 — Assistant loop hardening
- C3: if the tool loop exhausts `_MAX_TOOL_ROUNDS` while Claude still wants
  tools, a final **tool-free** call now forces a clean textual answer instead of
  ending on an unanswered tool exchange.
- C4: the API-error return path now runs a `_delta_is_balanced()` guard and
  refuses to persist a half-finished tool exchange into the session, so one
  transient error can't poison every subsequent request.

### C5 / C6 — Robustness/cosmetic
- C5: metadata median now computes over numeric values only; a non-numeric
  field carries its first value instead of raising and aborting the average.
- C6: the history endpoint extracts only displayable `text` blocks and skips
  empty turns.

---

## 3. Verified correct (no change required)

**Reduction & correction (`src/reduction/core.py`).** Transmission
`T = bstop/i0` with optional air-path correction
`T = (bstop/i0)/(bstop_air/i0_air)`; Beer-Lambert thickness with correct
cm⁻¹→m⁻¹ unit handling; normalization collapse (`resolve_normalization`) for the
overlapping bstop/i0/absolute terms; absolute scale
`NF = bstop·d_cm/K → I = K·counts/(I0·T·d)`. Non-positive corrected diodes are
skipped rather than silently defaulted. `integrate1d` receives
`normalization_factor`, `polarization_factor`, `mask`, and `error_model`
correctly. Matches the prior reduction audit; all fixes still present.

**Background subtraction (`background/app.py`).**
`I_sub = I_sam − c·I_b`, `σ_sub = √(σ_sam² + (c·σ_b)²)` — correct propagation.
Background is log-interpolated onto the sample grid from positive points only.
High-q weighted least-squares scale with a sigma-clip pass; QC metrics
(% negatives, high-q residual ratio, low-q slope) are sound.

**Analysis (`src/analysis/core.py`).** Guinier `Rg = √(−3·slope)`,
`I0 = exp(intercept)`, with `qRg ≤ 1.3` auto-refinement and a positive-slope
guard; Porod power-law on ln–ln; Kratky `I·q²`. All correct.

**Manifest (`src/manifest.py`).** Cross-process `fcntl` lock
(`manifest_lock` / `update_manifest`), atomic unique-temp writes
(`pid + uuid`), and corrupt-file self-heal (backs up then recreates). Live test:
5 serialized updates all persisted; a deliberately corrupted manifest was backed
up to `manifest.corrupt-*.json` and recreated cleanly.

**Hub & security.** `hub/app.py` loads the app registry from `apps.yml`
(ports 5001–5005, hub 5000) — no hardcoding. No secrets in tracked files;
`.env` contains only comments (token comes from `~/.claude/settings.json`), is
git-ignored, and is `0600`.

---

## 4. Open, non-blocking items

- **D1 — `check_imports.py`:** reports false positives (flags background/analysis
  as importing `src.reduction.core` when they import `src.analysis.core`) and its
  `SRC_MODULES` map omits `src.analysis.core`, `src.events`, and `src.ai.*`.
  The actual app imports were verified clean by hand.
- **D2 — CLAUDE.md import table:** says "hub — nothing from src/", but hub now
  imports `src.manifest` (`update_manifest`, `add_event`); the analysis row omits
  `src.analysis.core`. Worth a doc refresh.
- **D3 — Architecture:** background subtraction science lives in
  `background/app.py` rather than `src/`, unlike every other app. Consider
  extracting to `src/background/core.py` to honor the "logic in src/" rule.
- **D4 — Averaging enhancements:** the scan average is unweighted; inverse-variance
  (or I0) weighting would be statistically optimal for unequal-quality scans.
  Also `np.interp` clamps to edge values outside a file's q-range, so a scan with
  a shorter range contributes its boundary value rather than being excluded there.
  Both are acceptable for typical repeat scans; flagged for awareness.

---

## 5. Files changed in this audit

- `src/ai/assistant.py` — balanced tool-use/tool-result history; round-cap
  wrap-up call; `_delta_is_balanced` guard on the error path.
- `assistant/app.py` — history endpoint skips tool-plumbing/empty turns.
- `src/plot_reduction.py` — average only contributing scans; numeric-safe
  metadata carry.

All changed modules compile; numeric behavior verified by stub tests.
