# The assistant's knowledge (Guinier)

Everything the AI assistant knows, and in which order it is told. The model
itself is never retrained; what makes it expert is what it reads before it
answers. Paths are defined in one place, `src/ai/layout.py`, and every source
is listed in [`sources.yml`](sources.yml) (a test checks that the two agree).

## Folders

| Folder | What | In git |
|---|---|---|
| `rules/` | Instructions given on **every** turn: corrections, group SOPs, the base prompt; plus `saxs_models.yml`, the structure fitter's model catalogue | yes |
| `reference/` | Documents **searched by meaning** for each question: `literature/`, `user_papers/`, `beamline/` | yes (literature, beamline) |
| `cases/` | Expert-judged worked examples (the knowledge pack; to be filled) | yes |
| `evals/` | Exam questions with marking points, to score the assistant (to be filled) | yes |
| `design/` | The Analyser's design knowledge sets, one per chemistry (Knowledge tab); **not** read by the assistant | yes |
| `index/` | The search index (`vector_db/`) and its `ingestion_log.json` | **no**, rebuilt per machine |

Knowledge that lives elsewhere, beside the code it describes:

| Where | What |
|---|---|
| `<app>/knowledge.md` | How each app works; the in-scope app's file is given every turn |
| `analysis/saxs_knowledge.yaml`, `analysis/waxs_knowledge.yaml` (via `src/analysis/guidelines.py`) | The analysis ladder for SAXS / WAXS (analysis, analyser and assistant pages) |
| `src/ai/hints.py` | Rules for the proactive hints (Guinier range, upturn, I₀ outliers …) |
| `~/.swaxs/memory/users/<user>/` | Personal memory: preferences, chat corrections (`corrections.jsonl`), session summaries. **Never shared.** |
| `<project>/.swaxs/memory/` and `manifest.db` (`ai_memory`) | What the assistant noted about this experiment: chat history, experiment history, quality log |

## What the model sees, in order

The prompt has a **static** part (identical every turn, so it is cached) and a
**dynamic** part (built per question). Earlier items win when they disagree.

| # | Part | Source | When |
|---|---|---|---|
| 1 | Base instructions | `rules/system_prompt.md` | always |
| 2 | **Operator corrections** (authoritative) | `rules/corrections.json` | always |
| 3 | The current app's manual | `<app>/knowledge.md` | always |
| 4 | Analysis ladder + guideline YAML | `src/analysis/guidelines.py` | analysis, analyser, assistant |
| 5 | Memory: group SOPs, beamline, project, personal | `rules/sops.json`, `reference/beamline/`, manifest, `~/.swaxs/memory/` | dynamic |
| 6 | The 6 most relevant passages | `index/` over `reference/` | dynamic |

## Changing what the assistant knows

| To | Do |
|---|---|
| Fix a wrong answer for everyone | Add a correction (assistant page, or `add_correction`); it lands in `rules/corrections.json` |
| Add a group convention | Add an SOP (`group_sops` tool); it lands in `rules/sops.json` |
| Add a paper | Upload it on the assistant page (`reference/user_papers/`), or drop a PDF in `reference/literature/` and ingest it |
| Change the base instructions | Edit `rules/system_prompt.md` (keep the `{app_id}` placeholder) |
| Document an app | Edit that app's `knowledge.md` |

Every answer also writes a trace to `logs/assistant_traces/` (see
`src/ai/traces.py`): which parts above were used, the passages retrieved, the
tools called and any errors. That is how a wrong answer is diagnosed, and the
raw material for improving the assistant (Meta-Harness, arXiv 2603.28052).

Older clones kept these files at the `ai_knowledge/` root; the assistant moves
them into this layout automatically on first start (`src/ai/layout.py::migrate`).
