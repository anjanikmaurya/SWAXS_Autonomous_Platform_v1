# Quality Gate — knowledge

The Quality Gate app (port 5006) sits between Background Subtraction (5003) and
Analysis (5004). It grades background-subtracted 1-D scattering profiles and
decides which are good enough for downstream analysis.

## What it does

- Watches the `Subtracted/` folder(s) (event-driven on `file.subtracted` with a
  periodic folder-poll backstop) and grades each profile automatically.
- Scores every profile **0–100** with a **good/bad** verdict (good ≥ threshold,
  default 60). Scoring is rule-based and reproducible; borderline scores may be
  adjudicated by an LLM (reusing the assistant's `ANTHROPIC_API_KEY`/model), which
  degrades gracefully to rules-only when no key is present.
- Sorts profiles into `Good/` (analysis-ready) and `NeedsReview/` subfolders, and
  records each verdict in the manifest under the `quality` key, mirroring
  `quality_score` and `quality_flags` onto the file entry and `ai_memory`.
- Emits a `file.classified` bus event; exposes good/bad counts on `/api/health`
  for the Hub badge; the Assistant can read the `quality` manifest section.

## Quality signals (penalties subtracted from 100)

- **Over-subtraction** — fraction of negative intensities (`pct_negative`).
- **Low SNR** — median I/σ over the usable range (`snr`).
- **Coverage** — usable q-decades and point count.
- **Smoothness** — spike/outlier fraction from a robust z-score on Δ²(log I).
- **Featureless** — low dynamic range *and* low SNR ⇒ the subtracted curve
  resembles the background with no structural features (shape test only).

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
  `1D/QualityReports/`.
