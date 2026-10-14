# Quality Gate: Knowledge Base

## 1. What it is for

The Quality Gate (port 5105) grades every background-subtracted 1D profile with a
**0 to 100 score** and a **good / bad** verdict (good = score ≥ threshold, default
60), and sorts the files so downstream analysis can use only the good ones.
Scoring is rule-based and reproducible (`src/quality/core.py`). Optionally, an
LLM adjudicates borderline scores; without `ANTHROPIC_API_KEY` the rules alone
decide.

## 2. Where it sits in the pipeline

Background (5104) → **Quality Gate (5105)** → Analysis (5106).

| Reads | Writes |
|---|---|
| `<project>/1D/<DET>/Subtracted/*.dat` | `Subtracted/Good/` and `Subtracted/NeedsReview/` (copies) |
| | `<project>/1D/<DET>/Results/QualityReports/` (exported reports) |
| | `<project>/quality_config.json` (your scoring parameters) |

- Files are **copied**, not moved: the original stays in the flat `Subtracted/`
  folder. A re-grade that changes the verdict moves the copy to the other folder.
  Anything that must respect the gate has to read `Good/`; the flat folder still
  contains rejected profiles.
- Each verdict is stored in the manifest `quality` section; `quality_score` and
  `quality_flags` are mirrored onto the file entry and into `ai_memory`, so the
  Assistant can see them. A re-grade replaces the previous grade's flags (flags
  added by the Assistant are kept).
- A `file.classified` bus event is emitted (`file_path`, `score`, `verdict`,
  `detector`, `flags`). `/api/health` reports good/bad counts for the hub badge.

## 3. How to use it

1. **Watch & grade**: set **SAXS Subtracted** and/or **WAXS Subtracted** folders
   (**Browse…**), and **Interval (s)** (default 10).
2. **Start grading** watches the folders: it grades at once on each
   `file.subtracted` event and also polls the folders every interval as a
   backstop. **Grade now** grades the folders once without monitoring.
   **Stop** ends monitoring.
3. **Good ≥ threshold** slider: re-labels all profiles instantly (no re-read)
   and re-sorts the copies.
4. **LLM adjudication (borderline only)** checkbox and **Model** (default
   `claude-sonnet-4-6`): only scores within ±10 of the threshold go to the LLM,
   and its verdict then decides.
5. Gallery: search by name or sample; filter by verdict, detector or flag; sort
   by score, name or sample. Click a profile for its plot, metrics and reasons.
6. In the detail pane: **Good** / **Bad** override (with an optional note) or
   **Re-grade with AI** (forces a full LLM judgment, recorded as an override).
   Overrides gently move the threshold toward your judgment: halfway to the
   midpoint between your lowest "good" score and highest "bad" score, clamped to
   20 to 90, and only if those do not overlap.
7. **Scoring parameters** (**Show/hide**): edit any weight or threshold, then
   **Apply**. All cached profiles are re-scored from stored metrics and the
   values are saved to `quality_config.json`. **Reset defaults** clears them.
   **Refine with AI** suggests changes from your overrides (needs an API key and
   at least 3 overrides including both good and bad); review, then
   **Apply suggestions**.
8. **Export report** writes `quality_report_<timestamp>.csv` (name, detector,
   sample, score, verdict, flags, overridden, reasons) and
   `accepted_<timestamp>.txt` (good files) per detector to
   `1D/<DET>/Results/QualityReports/`.
9. **Activity log** shows each grade and override.

## 4. Scoring

`score = 100 − (p_neg + p_snr + p_cov + p_spike + p_feat + p_aggr)`, clamped to
[0, 100]. Fewer than 3 usable points (or an unreadable file) gives score 0, bad
(flags `empty` / `unreadable`).

| Signal (flag) | Metric | Penalty | Default |
|---|---|---|---|
| Over-subtraction (`over_subtraction`) | % negative points | linear from `neg_warn_pct` to `neg_fail_pct` | 5% → 25%, `w_neg` = 40 |
| Low SNR (`low_snr`) | median I/σ | linear from `snr_good` (none) down to `snr_floor` (full) | 10 → 2, `w_snr` = 40 |
| Coverage (`narrow_q`, `sparse`) | usable log₁₀ q decades, point count | 60% of `w_cov` if < `min_decades`, 40% if < `min_points` | 1.0 decade, 50 points, `w_cov` = 15 |
| Smoothness (`spikes`) | fraction with robust \|z\| > 5 on Δ²(ln I) | linear from 0 to `spike_fail_frac`; flagged above 2% | 0.10, `w_spike` = 15 |
| Featureless (`featureless`) | dynamic range = log₁₀(I₉₅/I₅) in decades | all or nothing | `w_featureless` = 55 |
| Low-q aggregation (`aggregation`) | ln I vs ln q slope, lowest ~15% of positive points | all or nothing if steeper than `aggr_slope` | −3.2, `w_aggr` = 20 |

**Featureless** fires if `dyn_range < dyn_range_min` (0.5) **and**
`snr < snr_good`, **or** `dyn_range < dyn_range_hard` (0.2) regardless of SNR.
Its 55 points alone fail an otherwise clean profile (100 − 55 = 45 < 60).

Other parameters: `score_pass` = 60 (verdict cutoff), `borderline` = 10 (LLM
band ±).

### SAXS vs WAXS
The detector is taken from a `SAXS` / `WAXS` token in the filename, else from
the `SAXS` / `WAXS` folder in the path, else SAXS. WAXS is peak-dominated, so
these defaults change for WAXS:

| Key | SAXS | WAXS |
|---|---|---|
| `snr_good` | 10.0 | 6.0 |
| `dyn_range_min` | 0.5 | 0.3 |
| `dyn_range_hard` | 0.2 | 0.12 |
| `aggr_slope` | −3.2 | −6.0 |
| `w_aggr` | 20 | 0 |

With `w_aggr` = 0, WAXS profiles are never flagged for aggregation. Parameters
you set in the UI apply to both detectors and replace these defaults.

### Verdict order
A user override wins, then an LLM verdict for a borderline profile, then the
threshold. If a threshold change or a rescore moves a profile out of the
borderline band, its LLM verdict is cleared and the threshold decides.

A monitor resumed after a restart keeps the pass threshold saved in
`quality_config.json`; it does not take the threshold from the replayed Start
request.

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Good-looking profile marked bad | Look at the flags. `featureless` (55 points) is the usual cause for weak or flat curves; `low_snr` for short exposures. Override, or tune the parameters. |
| Every WAXS profile graded like SAXS | The path has no `WAXS` token or folder. Use the standard `1D/WAXS/Subtracted/` layout. |
| Rejected profiles still analysed | The reader uses the flat `Subtracted/` folder; point it at `Subtracted/Good/`. |
| LLM notes never appear | No `ANTHROPIC_API_KEY`, the checkbox is off, or no score is within ±10 of the threshold. |
| "Refine with AI" returns nothing | Needs an API key and at least 3 overrides with both verdicts. |
| Banner: saved parameters could not be read | `quality_config.json` is unreadable, so defaults are in use. Set the parameters again. |

## 6. API routes

| Route | Method | Purpose |
|---|---|---|
| `/api/health` | GET | Health and good/bad/graded counts |
| `/api/set_project`, `/api/project` | POST / GET | Project root from the hub |
| `/api/browse` | GET | Folder browser |
| `/api/grade` | POST | Grade folders once |
| `/api/results` | GET | All graded records |
| `/api/profile` | GET | One profile's curve and record |
| `/api/threshold` | POST | Set the pass threshold |
| `/api/override` | POST | Manual good/bad override |
| `/api/params`, `/api/params/reset` | GET, POST / POST | Scoring parameters |
| `/api/llm_grade` | POST | Force an LLM re-grade of one profile |
| `/api/refine_ai` | POST | LLM-suggested parameters (not applied) |
| `/api/report` | GET | Write and return the QC report |
| `/api/monitor/start`, `/api/monitor/stop` | POST | Watch the folders |
| `/api/monitor/status`, `/api/monitor/stream` | GET | Monitor state, log (SSE) |
| `/api/restart_notice` | GET | Restored/lost parameters banner |
