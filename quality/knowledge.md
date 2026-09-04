# Quality Gate — knowledge

The Quality Gate app (port 5105) sits between Background Subtraction (5104) and
Analysis (5106). It grades background-subtracted 1-D scattering profiles and
decides which are good enough for downstream analysis.

## What it does

- Watches the `Subtracted/` folder(s) (event-driven on `file.subtracted` with a
  periodic folder-poll backstop) and grades each profile automatically.
- Scores every profile **0–100** with a **good/bad** verdict (good ≥ threshold,
  default 60). Scoring is rule-based and reproducible; borderline scores may be
  adjudicated by an LLM (reusing the assistant's `ANTHROPIC_API_KEY`/model), which
  degrades gracefully to rules-only when no key is present.
- Sorts profiles into `Good/` (analysis-ready) and `NeedsReview/` subfolders
  created BESIDE the graded file — so for the standard layout they are
  `1D/<DET>/Subtracted/Good/` and `1D/<DET>/Subtracted/NeedsReview/`. The file is
  **COPIED**, not moved: the original stays in the flat `Subtracted/` folder, and
  a stale copy in the other verdict folder is deleted. The operation is
  idempotent, so a re-grade that changes the verdict moves the copy across.
  Downstream apps that must respect the gate therefore have to read `Good/`
  explicitly — reading the flat folder sees rejected profiles too.
- Records each verdict in the manifest under the `quality` key, mirroring
  `quality_score` and `quality_flags` onto the file entry and `ai_memory`.
- Emits a `file.classified` bus event; exposes good/bad counts on `/api/health`
  for the Hub badge; the Assistant can read the `quality` manifest section.

## Quality signals (penalties subtracted from 100)

There are SIX signals. The score is `100 − (p_neg + p_snr + p_cov + p_spike +
p_feat + p_aggr)`, clamped to [0, 100].

- **Over-subtraction** (flag `over_subtraction`, weight `w_neg` = 40) — fraction of
  negative intensities (`pct_negative`). Ramps linearly from `neg_warn_pct` (5%)
  to `neg_fail_pct` (25%).
- **Low SNR** (flag `low_snr`, `w_snr` = 40) — median I/σ over the usable range
  (`snr`). Ramps from `snr_good` (10, no penalty) down to `snr_floor` (2, full).
- **Coverage** (flags `narrow_q` / `sparse`, `w_cov` = 15) — 60% of the weight for
  fewer than `min_decades` (1.0) usable log₁₀(q) decades, 40% for fewer than
  `min_points` (50) usable points.
- **Smoothness** (flag `spikes`, `w_spike` = 15) — spike/outlier fraction from a
  robust z-score (|z| > 5) on Δ²(log I); ramps from 0 to `spike_fail_frac` (0.10).
  Flagged above 2%.
- **Featureless** (flag `featureless`, `w_featureless` = 55) — fires on EITHER of
  two conditions: `dyn_range < dyn_range_min` (0.5) **AND** `snr < snr_good` (10),
  **OR** `dyn_range < dyn_range_hard` (0.2) on its own, regardless of SNR. A
  hard-flat curve is therefore flagged even if its SNR is excellent. This is an
  all-or-nothing 55-point penalty, so it alone flunks a profile that would
  otherwise be clean (100 − 55 = 45 < the 60 cutoff).
- **Low-q aggregation** (flag `aggregation`, `w_aggr` = 20) — the ln I vs ln q
  slope over the lowest ~15% of positive points. If that slope is steeper (more
  negative) than `aggr_slope` (−3.2), the full 20 points are removed.

All of the numbers above are `DEFAULT_THRESHOLDS` in `src/quality/core.py` and all
are editable in the UI / `quality_config.json`.

## Per-detector thresholds (SAXS vs WAXS are graded differently)

WAXS profiles are peak-dominated: low dynamic range and lower SNR are normal, and
a steep low-q rise is not aggregation. `thresholds_for(detector)` merges these
WAXS overrides on top of the defaults:

| Key | SAXS (default) | WAXS |
|---|---|---|
| `snr_good` | 10.0 | **6.0** |
| `dyn_range_min` | 0.5 | **0.3** |
| `dyn_range_hard` | 0.2 | **0.12** |
| `aggr_slope` | −3.2 | **−6.0** |
| `w_aggr` | 20.0 | **0.0** |

Because `w_aggr` is 0 for WAXS, the `aggregation` flag is disabled entirely on
WAXS — it is never raised and never costs points. So the same curve can score
differently on the two detectors, and a WAXS profile will never be marked as
aggregating.

Everything else (`score_pass`, `w_neg`, `w_snr`, `w_cov`, `w_spike`,
`w_featureless`, negatives, coverage and spike thresholds) is shared.

## Tunable scoring + AI refine

Every scoring weight and threshold is editable in the UI (the "🎚 Scoring
parameters" panel). Changes re-score all cached profiles instantly (from stored
metrics, no file re-read) and are saved to `quality_config.json` in the project
folder so they persist and are shareable. "Refine with AI" asks the model to
suggest weight adjustments from the profiles you've overridden (labeled good/bad)
and shows them for you to Apply. The detail pane also has "Re-grade this profile
with AI" to force a full LLM judgment on a single profile.

## Interaction

- Threshold slider re-labels instantly (no recompute). One-click overrides
  (good/bad + note) are logged and gently **adapt the threshold** toward the
  user's judgment.
- Exports a QC summary CSV and an accepted-profiles list to
  `1D/SAXS/Results/QualityReports/` — moved here (from a bare `1D/QualityReports/`)
  so everything the platform keeps after the fact lives under `Results/`,
  alongside the analyzer's `Fit/` records and campaign figures.
