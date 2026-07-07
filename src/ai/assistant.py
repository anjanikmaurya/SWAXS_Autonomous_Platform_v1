"""
src/ai/assistant.py — SWAXS AI Assistant Core
===============================================
Claude API client with:
  • Expert SAXS/WAXS system prompt built from layered memory + KB retrieval
  • Tool dispatch: generate_plot, run_analysis, query_manifest,
                   add_note, flag_quality, ingest_pdf
  • Conversation history management (per session, in-memory)
  • Graceful degradation if ANTHROPIC_API_KEY is absent

Usage
-----
    from src.ai.assistant import SWAXSAssistant

    assistant = SWAXSAssistant(
        ai_knowledge_dir = "/abs/path/ai_knowledge",
        user_id          = "albert",
    )

    result = assistant.chat(
        message      = "What is my Guinier Rg for BSA_10mg_avg.dat?",
        user_id      = "albert",
        project_root = "/abs/path/experiment",
        app_id       = "analysis",
        history      = [],           # list[dict] — previous turns
    )
    # result = {"text": "...", "plot": "<base64>|None", "tool_calls": [...]}

    # Update history for the next turn:
    history += result.get("_history_delta", [])
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("swaxs_platform")

# Thread-local carrier for an optional interactive (Plotly) figure produced by a
# plot tool during the current request. Thread-local keeps concurrent Flask
# requests isolated. The static PNG remains the guaranteed fallback.
import threading as _threading
_PLOT_TL = _threading.local()


def _emit_interactive(fig: dict | None) -> None:
    _PLOT_TL.fig = fig

# ── Model config ───────────────────────────────────────────────────────────────
_DEFAULT_MODEL      = "claude-sonnet-4-6"
_MAX_TOKENS         = 4096
_KB_TOP_K           = 6          # knowledge-base hits to include
_MAX_TOOL_ROUNDS    = 5          # max recursive tool-use loops per chat turn
# ── Cost / context controls ───────────────────────────────────────────────────
# The full conversation history is re-sent on every turn, so unbounded history
# means ever-growing input-token cost. We keep only the most recent user turns
# (with their tool exchanges) and bound each tool result's size.
_MAX_HISTORY_USER_TURNS = 6      # how many recent user prompts to retain
_MAX_TOOL_RESULT_CHARS  = 8000   # truncate any single tool result beyond this

# ── Tool definitions for Claude API ──────────────────────────────────────────
_TOOLS: list[dict] = [
    {
        "name":        "generate_plot",
        "description": (
            "Generate a SAXS/WAXS analysis plot (Guinier, Kratky, Porod, "
            "p(r), multi-curve overlay, or plain curve) and return it as a "
            "base64 PNG for inline display. Pass q, I, sigma arrays as JSON "
            "lists alongside the plot type and any fit parameters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plot_type": {
                    "type":        "string",
                    "enum":        ["curve", "guinier", "kratky", "porod",
                                   "pair_distance", "multi"],
                    "description": "Which plot to generate.",
                },
                "file_path": {
                    "type":        "string",
                    "description": (
                        "Absolute path to a .dat file. If provided the "
                        "assistant loads q/I/sigma from it automatically."
                    ),
                },
                "q":     {"type": "array", "items": {"type": "number"},
                          "description": "q array (nm⁻¹). Required if file_path not given."},
                "I":     {"type": "array", "items": {"type": "number"},
                          "description": "I(q) array (a.u.). Required if file_path not given."},
                "sigma": {"type": "array", "items": {"type": "number"},
                          "description": "σ(q) error array (optional)."},
                "q_min": {"type": "number", "description": "Guinier fit q_min (nm⁻¹)."},
                "q_max": {"type": "number", "description": "Guinier fit q_max (nm⁻¹)."},
                "Rg":    {"type": "number", "description": "Radius of gyration (nm)."},
                "I0":    {"type": "number", "description": "Forward scattering intensity."},
                "Dmax":  {"type": "number", "description": "Maximum particle dimension (nm)."},
                "r":     {"type": "array", "items": {"type": "number"},
                          "description": "r array for p(r) plot (nm)."},
                "pr":    {"type": "array", "items": {"type": "number"},
                          "description": "p(r) array."},
                "datasets": {
                    "type":  "array",
                    "description": "List of {q, I, sigma?, label?} for multi-curve plot.",
                },
                "label": {"type": "string", "description": "Curve label."},
                "title": {"type": "string", "description": "Plot title."},
                "loglog":{"type": "boolean", "description": "Log-log scale (default true)."},
            },
            "required": ["plot_type"],
        },
    },
    {
        "name":        "plot_metadata",
        "description": (
            "Plot per-frame acquisition metadata (I0, bstop, transmission, "
            "thickness, normalization factor, sample temperature CTEMP/TEMP) "
            "over time for each averaged sample, and display it inline in the "
            "chat. Use when the user asks to plot/track/monitor beam metrics, "
            "transmission, temperature, or I0/bstop across frames or samples "
            "(beam-stability / dosing / temperature-series checks). Reads the "
            "manifest and .dat footers automatically — no file paths needed. "
            "Choose the source set with `stage` (averaged or subtracted) and "
            "narrow it with `keyword`; the x-axis is the beamline Timer clock."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parameters": {
                    "type":  "array",
                    "items": {"type": "string",
                              "enum": ["i0", "bstop", "transmission",
                                       "thickness_m", "normalization_factor",
                                       "ctemp", "temp"]},
                    "description": "Metadata fields to plot (default ['i0','bstop']).",
                },
                "stage": {
                    "type":        "string",
                    "enum":        ["averaged", "subtracted"],
                    "description": "Which folder/stage of samples to read metadata "
                                   "for (default averaged). 'subtracted' selects the "
                                   "subtracted curves instead.",
                },
                "detector": {
                    "type":        "string",
                    "enum":        ["SAXS", "WAXS", "both"],
                    "description": "Which detector(s) to include (default both).",
                },
                "keyword": {
                    "type":        "string",
                    "description": "Only include samples whose name contains this (optional).",
                },
            },
            "required": [],
        },
    },
    {
        "name":        "fit_model",
        "description": (
            "Run a sasmodels form-factor fit on ONE averaged sample and return "
            "the fitted parameters, reduced chi-square, and an inline data+fit "
            "plot with a residuals panel. Only call this AFTER the user has "
            "agreed to a recommended model and starting parameters (recommend → "
            "confirm → fit → iterate). To iterate, call again with adjusted "
            "values/bounds. Reads the .dat from the manifest (read-only)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":    {"type": "string",
                               "description": "Substring selecting the sample (averaged file)."},
                "model_name": {"type": "string",
                               "description": "sasmodels model (e.g. 'sphere', 'lamellar', 'broad_peak')."},
                "params": {
                    "type": "object",
                    "description": ("Initial parameter values as NUMBERS, e.g. "
                                    "{'radius':50,'scale':1e-2,'background':1e-4}. "
                                    "Give realistic starting guesses for every "
                                    "parameter you set."),
                    "additionalProperties": {"type": ["number", "string"]},
                },
                "free": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("Names of parameters to OPTIMISE (others held "
                                    "fixed at their value). e.g. "
                                    "['radius','scale','background']. If omitted, "
                                    "all provided numeric params are optimised."),
                },
                "q_min": {"type": "number", "description": "Lower q bound for the fit (nm⁻¹)."},
                "q_max": {"type": "number", "description": "Upper q bound for the fit (nm⁻¹)."},
                "detector": {"type": "string", "enum": ["SAXS", "WAXS"],
                             "description": "Detector (default SAXS)."},
                "axis": {"type": "string", "enum": ["loglog", "semilog", "linear"],
                         "description": "Axis scaling (default loglog)."},
            },
            "required": ["keyword", "model_name", "params"],
        },
    },
    {
        "name":        "assess_quality",
        "description": (
            "Run quality control on ONE averaged sample: frame-outlier detection "
            "(I0/intensity via robust MAD) and beam/transmission sanity "
            "(transmission in physical range, positive I0/bstop, beam stability). "
            "Returns a per-check verdict and the indices of any outlier frames. "
            "Use for 'is this sample OK?', QC, or before averaging/fitting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":  {"type": "string",
                             "description": "Substring selecting the sample (averaged file)."},
                "detector": {"type": "string", "enum": ["SAXS", "WAXS"],
                             "description": "Detector (default SAXS)."},
            },
            "required": ["keyword"],
        },
    },
    {
        "name":        "list_saxs_models",
        "description": (
            "List SAXS form-factor models available for fitting in the Analysis "
            "app (the sasmodels library catalog). Use when recommending a model "
            "to fit the data, or when the user asks which models are available. "
            "Optionally filter by a keyword (e.g. 'sphere', 'cylinder', "
            "'lamellar', 'fractal', 'peak')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string",
                            "description": "Filter model names containing this substring."},
            },
            "required": [],
        },
    },
    {
        "name":        "overlay_curves",
        "description": (
            "Load and OVERLAY multiple processed 1D curves (.dat) on one plot, "
            "matched by keyword(s) — e.g. compare all '12C' samples against "
            "'air'. Use when the user asks to compare/overlay/superimpose "
            "averaged, subtracted, or reduced curves. Reads files from the "
            "manifest automatically (no paths). Renders SAXS and WAXS in "
            "separate panels and displays inline in the chat. Optional q_min/"
            "q_max truncate every curve to a q-range before plotting (e.g. when "
            "the user wants to cut the SAXS beamstop/low-q or noisy high-q)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type":  "array",
                    "items": {"type": "string"},
                    "description": "Substrings to match in file names (e.g. ['12C','air']).",
                },
                "stage": {
                    "type":        "string",
                    "enum":        ["averaged", "subtracted", "reduced"],
                    "description": "Which processing stage to pull curves from (default averaged).",
                },
                "detector": {
                    "type":        "string",
                    "enum":        ["SAXS", "WAXS", "both"],
                    "description": "Detector(s) to include (default both).",
                },
                "axis": {
                    "type":        "string",
                    "enum":        ["loglog", "semilog", "linear"],
                    "description": "Axis scaling (default loglog).",
                },
                "q_min": {
                    "type":        "number",
                    "description": "Truncate every curve BELOW this q (nm⁻¹) before "
                                   "plotting — e.g. drop the low-q beamstop region for "
                                   "SAXS. Optional.",
                },
                "q_max": {
                    "type":        "number",
                    "description": "Truncate every curve ABOVE this q (nm⁻¹) before "
                                   "plotting — e.g. drop noisy high-q. Optional.",
                },
            },
            "required": ["keywords"],
        },
    },
    {
        "name":        "export",
        "description": (
            "Write an output file to the project's dedicated `assistant_outputs/` "
            "folder — the ONLY place the assistant may write. Kinds: "
            "'session_report' (HTML summary of samples, stages, QC, analyses) or "
            "'fit_results' (CSV of recorded analyses/fits). NEVER touches "
            "experiment data. Call ONLY after the user confirms they want the "
            "file saved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["session_report", "fit_results", "notes"],
                         "description": "What to export."},
                "format": {"type": "string",
                           "enum": ["html", "pdf", "csv", "xlsx", "md"],
                           "description": "session_report: html|pdf; fit_results: csv|xlsx; notes: md."},
                "content": {"type": "string",
                            "description": "For kind 'notes': the text to save (captions/methods/summary)."},
                "filename": {"type": "string",
                             "description": "Optional output filename."},
                "keyword": {"type": "string",
                            "description": "Optional sample-name filter."},
            },
            "required": ["kind"],
        },
    },
    {
        "name":        "compute_pr",
        "description": (
            "Compute the pair-distance distribution p(r) for ONE curve via a "
            "regularized indirect Fourier transform, and return Rg, Dmax, I0, "
            "and an inline p(r) plot. Use when the user asks for p(r), the "
            "pair-distance distribution, the maximum dimension Dmax, or a "
            "real-space size/shape readout. Locate the file by `keyword`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":  {"type": "string",
                             "description": "Sample name substring (matches an averaged sample)."},
                "detector": {"type": "string", "enum": ["SAXS", "WAXS"]},
                "dmax":     {"type": "number",
                             "description": "Optional Dmax hint (nm); auto-estimated if omitted."},
            },
            "required": ["keyword"],
        },
    },
    {
        "name":        "run_analysis",
        "description": (
            "Run a SAXS/WAXS analysis (Guinier, Kratky, Porod, pair-distance) "
            "on a .dat file using the analysis app algorithms and return the "
            "numeric results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "analysis_type": {
                    "type":   "string",
                    "enum":   ["guinier", "kratky", "porod", "pair_distance"],
                },
                "file_path":  {"type": "string", "description": "Absolute path to .dat file."},
                "q_min":      {"type": "number"},
                "q_max":      {"type": "number"},
                "Dmax_hint":  {"type": "number",
                               "description": "Dmax hint for BIFT / p(r) estimation."},
            },
            "required": ["analysis_type", "file_path"],
        },
    },
    {
        "name":        "query_manifest",
        "description": (
            "Query the experiment manifest for processed files, reduction "
            "status, averaged/background/analysis records, and quality flags.\n"
            "COST NOTE: a project can hold thousands of files. ALWAYS start with "
            "query_type='summary' — it returns totals, per-stage and per-detector "
            "counts, and quality-flag counts in a few tokens, and answers most "
            "'what has been processed / any quality issues' questions on its own. "
            "Only use 'files'/'averaged' when the user asks about SPECIFIC files, "
            "and ALWAYS pass a `keyword` or `detector` filter to narrow the result "
            "(list queries are compacted and capped, never the full manifest). "
            "Do not call this tool more than once per turn unless the user asks "
            "for details a prior summary did not contain."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type":  "string",
                    "enum":  ["summary", "files", "averaged", "background",
                              "analysis", "quality_flags", "events"],
                    "description": "Which section to inspect. Prefer 'summary' "
                                   "for overview questions (cheapest).",
                },
                "keyword": {
                    "type":        "string",
                    "description": "Filter by keyword/sample name. Strongly "
                                   "recommended for 'files'/'averaged' queries.",
                },
                "detector": {
                    "type":        "string",
                    "enum":        ["SAXS", "WAXS"],
                    "description": "Filter by detector (optional).",
                },
            },
            "required": ["query_type"],
        },
    },
    {
        "name":        "add_note",
        "description": (
            "Attach a plain-text note to a specific file entry in the manifest. "
            "Use when the user wants to record an observation about a file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "note":      {"type": "string"},
            },
            "required": ["file_path", "note"],
        },
    },
    {
        "name":        "flag_quality",
        "description": (
            "Set a quality flag on a processed file in the manifest. "
            "Valid flags: 'good', 'marginal', 'bad', 'radiation_damage', "
            "'aggregated', 'low_snr', 'outlier', 'needs_review'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "flag":      {
                    "type": "string",
                    "enum": ["good", "marginal", "bad", "radiation_damage",
                             "aggregated", "low_snr", "outlier", "needs_review"],
                },
                "reason":    {"type": "string"},
            },
            "required": ["file_path", "flag"],
        },
    },
    {
        "name":        "set_preferences",
        "description": (
            "Save the user's PERSISTENT preferences (apply across all projects "
            "and sessions): audience level, verbosity, default fit model, units, "
            "citation style. Use when the user states a lasting preference — e.g. "
            "'explain like I'm new', 'keep it terse', 'always use nm', 'default "
            "to correlation_length'. Confirm what you saved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "audience":  {"type": "string", "enum": ["expert", "student", "mixed"]},
                "verbosity": {"type": "string", "enum": ["concise", "detailed"]},
                "default_model":  {"type": "string"},
                "units":          {"type": "string"},
                "citation_style": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name":        "group_sops",
        "description": (
            "View, add, or remove the group's shared SOPs / conventions (naming "
            "schemes, default models, analysis defaults, buffer-matching rules, "
            "etc.). These apply across ALL projects and users and are always "
            "loaded into context. Actions: 'list', 'add' (title + text), "
            "'remove' (by id or title). Confirm before removing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "title":  {"type": "string", "description": "Short label (add) or title to remove."},
                "text":   {"type": "string", "description": "The convention/SOP text (add)."},
                "id":     {"type": "string", "description": "SOP id to remove (alternative to title)."},
            },
            "required": ["action"],
        },
    },
    {
        "name":        "web_search",
        "description": (
            "Search the scholarly literature online (Crossref) for papers "
            "relevant to a query — returns title, authors, year, venue, DOI. "
            "Use to find references the local knowledge base lacks (e.g. recent "
            "papers on a model/system). ONLINE only — it reports clearly if the "
            "beamline network is offline. Offer to ingest a found paper if the "
            "user supplies its PDF."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string",
                                "description": "Search terms (e.g. 'SAXS lamellar membrane TFC')."},
                "max_results": {"type": "integer",
                                "description": "How many papers to return (default 5, max 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name":        "manage_knowledge",
        "description": (
            "Visualise, add, or remove items in the assistant's knowledge base "
            "(your literature & papers). Actions: 'list' (show all ingested "
            "papers/notes with counts), 'add_pdf' (index a PDF by path), "
            "'add_note' (save a text snippet/fact), 'ingest_folder' (index all "
            "PDFs in a folder), 'remove' (delete a source by name). Use 'list' "
            "when the user asks what the assistant knows or which papers are "
            "loaded. Confirm with the user before 'remove'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["list", "add_pdf", "add_note",
                                    "ingest_folder", "remove"]},
                "collection": {"type": "string",
                               "enum": ["user_papers", "literature"],
                               "description": "Target collection (default user_papers)."},
                "path": {"type": "string",
                         "description": "PDF path (add_pdf) or folder path (ingest_folder)."},
                "text": {"type": "string",
                         "description": "Note text for add_note."},
                "name": {"type": "string",
                         "description": "Note title (add_note) or source name to remove."},
            },
            "required": ["action"],
        },
    },
    {
        "name":        "run_python",
        "description": (
            "Run a SHORT Python snippet for ad-hoc analysis/plots in a guarded "
            "sandbox, and show stdout + any matplotlib figure inline. Available: "
            "numpy (np), matplotlib (plt), scipy, pandas, pathlib, and "
            "`load_dat(path)` → (q, I, sigma). `load_dat` accepts a BARE FILENAME "
            "(it searches the project), so you don't need to find paths yourself. "
            "The sandbox is READ-ONLY on data (blocks os/network/file-deletion); "
            "the only writable folder is assistant_outputs/. "
            "ALWAYS show the code and get the user's explicit confirmation BEFORE "
            "calling this. Prefer the dedicated tools when they already do the job."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code":    {"type": "string", "description": "The Python snippet to run."},
                "save_as": {"type": "string",
                            "description": "If set, also save the produced figure to assistant_outputs/."},
            },
            "required": ["code"],
        },
    },
    {
        "name":        "ingest_pdf",
        "description": (
            "Ingest a PDF file (paper, manual, protocol) into the AI knowledge "
            "base so it can be retrieved in future conversations. "
            "Use when the user uploads or mentions a reference document."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pdf_path":   {"type": "string",
                               "description": "Absolute path to the PDF."},
                "collection": {"type": "string",
                               "enum": ["literature", "user_papers"],
                               "description": "Target collection (default: user_papers)."},
            },
            "required": ["pdf_path"],
        },
    },
]

# ── Base system prompt (static) ────────────────────────────────────────────────
_SYSTEM_BASE = """\
You are the Tassone Group Assistant — the Tassone group's expert in small-angle
(SAXS) and wide-angle (WAXS) X-ray scattering data processing and analysis.  You
are embedded in the SWAXS Platform, a hub-and-spoke Flask application that guides
scientists from raw 2D detector images through reduction, averaging, background
subtraction, and structural analysis. When you introduce yourself, do so as
"the Tassone Group Assistant, your SAXS/WAXS research assistant."

## Your expertise
• 2D→1D azimuthal integration (PyFAI, SWAXS/SAXS/WAXS modes)
• Absolute-scale normalisation, transmission correction, solvent subtraction
• Guinier analysis (Rg, I₀, qRg validity range)
• Kratky and dimensionless Kratky plots (fold/flexibility assessment)
• Porod analysis (surface area, excluded volume)
• p(r) pair-distance distribution and Dmax
• SEC-SAXS, GISAXS, anomalous SAXS concepts
• Common artefacts: aggregation, radiation damage, beamstop shadows,
  parasitic scattering, hot pixels, multiple scattering

## Behaviour rules
1. Always refer to the experiment manifest (use `query_manifest`) before making
   claims about what has been processed or what the results are. Be token-frugal:
   call `query_manifest` with query_type='summary' first — it answers most
   "what's processed / any quality issues" questions by itself. Only request file
   lists ('files'/'averaged') when the user asks about specific files, and always
   pass a keyword/detector filter. Avoid repeated/redundant manifest calls in one
   turn; these queries consume API tokens.
2. When the user asks for a plot, generate it immediately. Use `generate_plot`
   for scattering curves/fits (Guinier, Kratky, Porod, p(r), overlays), and
   `plot_metadata` to chart acquisition metadata (I0, bstop, transmission,
   thickness) over time per averaged sample — it reads the manifest itself, so
   you don't need file paths.
3. Before recommending a Guinier range, check qRg. The upper limit is
   shape-dependent: ≈1.3 for globular (sphere/disc-like) particles, ≈1.0 for
   extended/rod-like particles, up to ~1.7 for flat discs. Use a lower q_min·Rg
   bound of ~0.3 to avoid beamstop/beam-divergence artefacts. If unsure of the
   shape, start at qRg_max ≈ 1.3 and lower it if the residuals are not flat.
4. If you detect a potential quality issue, add a note via `add_note` or set a
   flag via `flag_quality` — do NOT just mention it in text and move on.
5. Be concise and scientific, but approachable.  Tailor responses to the
   specific files and numbers in the experiment, and briefly explain any jargon
   or acronym the first time you use it so a newcomer can follow along.
6. When you are uncertain, say so.  Recommend SEC-SAXS, dilution series, or
   background matching experiments when appropriate.
7. Unit conventions: q in nm⁻¹, r in nm, Rg in nm, I in absolute cm⁻¹·sr⁻¹
   if the data are on absolute scale, otherwise a.u.
8. End substantive answers with a short, concrete "next step" the user can take
   in the platform (which app, which action), so they always know what to do next.

## Comparing samples & recommending a fit
When the user asks to compare samples or overlay profiles:
1. Plot them with `overlay_curves` (keywords, both detectors). For trends across
   frames/samples use `plot_metadata`.
2. Interpret what DIFFERS, region by region, and say what it physically means:
   • Low-q (q ≲ 0.1 nm⁻¹): upturn/steeper slope ⇒ larger structures or
     aggregation; a plateau ⇒ finite size (extract $R_g$ via `run_analysis`
     guinier). Compare $I_0$ ∝ contrast²·volume·concentration.
   • Mid-q: a peak/shoulder ⇒ a characteristic spacing $d = 2\\pi/q^*$
     (lamellar period, mesh/correlation length, inter-particle distance). Track
     how $q^*$ and peak width shift between samples.
   • High-q (Porod): slope $-4$ ⇒ smooth sharp interface; between $-3$ and $-4$
     ⇒ rough/fractal surface; $-2$ ⇒ Gaussian chains/2D sheets (`run_analysis`
     porod gives the exponent).
   • WAXS: sharp peaks ⇒ crystalline order ($d = 2\\pi/q$); broad halos ⇒
     amorphous packing. Compare crystallinity/peak position between samples.
3. Ground the interpretation in the literature: the system prompt already
   surfaces relevant Knowledge-Base excerpts, INCLUDING the user's own ingested
   papers (collection `user_papers`). Cite them by source name. If a paper would
   help and isn't indexed, tell the user they can add it (you can call
   `ingest_pdf` on a PDF path).
4. Recommend a fitting model: call `list_saxs_models` (the Analysis app's
   sasmodels range), pick the model whose form matches the observed features,
   and give concrete INITIAL GUESSES derived from the data:
   • sphere: $R \\approx R_g\\sqrt{5/3}$  • cylinder: radius from high-q, length
     from the low-q rod slope  • lamellar/broad_peak/correlation_length: spacing
     $d = 2\\pi/q^*$ from the mid-q peak  • power_law/Porod exponent from the
     high-q slope  • scale and background from the data magnitude/baseline.
   State your uncertainty and offer 1–2 alternatives.
5. Then ASK whether to run the fit (do NOT fit unprompted). On a yes, call
   `fit_model` with NUMERIC initial values in `params` (realistic starting
   guesses for every parameter) and a `free` list naming which to optimise;
   optionally `q_min`/`q_max` to restrict the fit range. Review the returned
   reduced-χ² and **residuals**: if they are not flat,
   propose iterating — adjust guesses/bounds, free/fix parameters, or switch to an
   alternative model — and re-run `fit_model`. Summarise the final parameters and
   their physical meaning when done.
6. For data quality, use `assess_quality` (frame-outlier + transmission/beam
   sanity for one sample) before averaging/fitting; surface any WARN/FAIL. For
   real-space size/shape, use `compute_pr` (returns Rg, Dmax, I0 + p(r) plot).

## Saving outputs (read-only data rule)
NEVER modify the user's experiment data (raw, `.dat`, `manifest.json`,
`config.yml`). Your only writable location is `<project>/assistant_outputs/`.
ALWAYS get a clear yes before writing anything, then report the saved path.
- Reports/tables: `export` — kind 'session_report' (format 'html' or 'pdf') or
  'fit_results' (format 'csv' or 'xlsx'); kind 'notes' with `content` + optional
  `filename` to save figure captions, methods text, or summaries as Markdown.
- Figures: any plot tool (`overlay_curves`, `plot_metadata`, `fit_model`,
  `compute_pr`) accepts an optional `save_as` filename — pass it to also write
  that figure PNG to assistant_outputs/.

## Ad-hoc code (`run_python`)
For analysis the dedicated tools don't cover, you may use `run_python` (numpy,
matplotlib, scipy, pandas, and `load_dat(path)`), but ONLY after you SHOW the
exact code and the user clearly approves running it. The sandbox is read-only on
data and blocks os/network/file-deletion; the only writable folder is
assistant_outputs/. Keep snippets short; prefer the dedicated tools when they
already do the job.

## Response formatting
Your replies render as GitHub-flavoured Markdown with KaTeX math, so format for
readability:
• Use `##`/`###` headings to organise longer answers, **bold** for key terms and
  values, and bullet lists for scannable points. Keep it clean — don't over-format
  short replies.
• Put tabular results (file lists, metric comparisons, QC summaries) in Markdown
  tables.
• Write ALL equations and math in LaTeX: inline as $...$ (e.g. $q = 4\\pi\\sin\\theta/\\lambda$,
  $R_g$, $I_0$) and display as $$...$$ for derivations
  (e.g. $$\\ln I(q) = \\ln I_0 - \\frac{R_g^2 q^2}{3}$$). Use proper subscripts/symbols
  ($R_g$, $q_{\\min}$, $\\sigma$, $\\AA^{-1}$) rather than plain text.
• Use `inline code` for file names, config keys, and app/tool names.

## Platform context
The platform runs locally. Files are stored in a user-selected project folder.
The user works through the apps roughly in order: Reduction → Viewer (average) →
Background subtraction → Analysis, with you available throughout.
Current app: {app_id}
"""


class SWAXSAssistant:
    """
    Claude API client for the SWAXS AI Assistant.

    Parameters
    ----------
    ai_knowledge_dir : str | Path
        Root of the ``ai_knowledge/`` directory (for KB + memory layers).
    user_id : str
        Identifier for the current user (used by LayeredMemory).
    model : str
        Anthropic model string.
    beamline_id : str
        Beamline identifier used to load facility notes (Layer 3 memory).
    """

    def __init__(
        self,
        ai_knowledge_dir: str | Path,
        user_id:          str = "default",
        model:            str = _DEFAULT_MODEL,
        beamline_id:      str = "ssrl_1-5",
    ) -> None:
        # Pull gateway config (token/endpoint/model) from ~/.claude/settings.json
        # if not already in the environment — single source of truth (SLAC).
        _load_claude_settings_into_env()
        self._kb_dir      = Path(ai_knowledge_dir)
        self._user_id     = user_id
        # ANTHROPIC_MODEL lets the deployment pick the gateway's model id
        # (e.g. SLAC: "us.anthropic.claude-sonnet-4-6") without code changes.
        self._model       = os.environ.get("ANTHROPIC_MODEL", "").strip() or model
        self._beamline_id = beamline_id

        # Lazy init — avoid import errors if optional packages are missing
        self._kb:  Any = None
        self._mem: Any = None
        self._anthropic_client: Any = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def chat(
        self,
        message:      str,
        user_id:      str | None = None,
        project_root: str | Path | None = None,
        app_id:       str = "assistant",
        history:      list[dict] | None = None,
        emit:         "callable | None" = None,
    ) -> dict:
        """
        Send a message to the assistant and return the response.

        Parameters
        ----------
        message      : user's message text
        user_id      : override instance user_id for this call
        project_root : experiment folder (needed for manifest queries)
        app_id       : which app is making the request
        history      : list of prior {"role": ..., "content": ...} dicts

        Returns
        -------
        dict with keys:
            text           : str   — assistant reply text
            plot           : str|None — base64 PNG if a plot was generated
            tool_calls     : list[dict] — tools that were called
            hints          : list[str] — proactive hints (from HintChecker)
            _history_delta : list[dict] — new messages to append to history
        """
        uid = user_id or self._user_id
        client = self._get_client()

        if client is None:
            return {
                "text": (
                    "No AI credentials are configured. Set ANTHROPIC_AUTH_TOKEN "
                    "(plus ANTHROPIC_BASE_URL for the SLAC gateway) — or "
                    "ANTHROPIC_API_KEY for the direct API — in the environment, "
                    "then restart the assistant."
                ),
                "plot":           None,
                "tool_calls":     [],
                "hints":          [],
                "_history_delta": [],
            }

        system_prompt = self._build_system_prompt(
            message      = message,
            user_id      = uid,
            project_root = project_root,
            app_id       = app_id,
        )

        messages = _trim_history(history or []) + [
            {"role": "user", "content": message}
        ]

        result_text    = ""
        result_plot    = None
        result_plot_fig = None      # optional interactive Plotly figure
        tool_calls_log: list[dict] = []
        history_delta: list[dict] = [{"role": "user", "content": message}]

        def _step(kind: str, **data) -> None:
            """Push a progress event to the streaming caller (no-op if none)."""
            if emit:
                try:
                    emit({"type": kind, **data})
                except Exception:
                    pass

        def _api_error(exc: Exception) -> dict:
            logger.error("[Assistant] Claude API error: %s", exc)
            msg = str(exc)
            if "authentication" in msg.lower() or "api_key" in msg.lower() or "401" in msg:
                friendly = (
                    "I couldn't authenticate to the AI service — the token/key "
                    "looks invalid or missing. Check ANTHROPIC_AUTH_TOKEN (SLAC "
                    "gateway) or ANTHROPIC_API_KEY, confirm you're on the SLAC "
                    "network/VPN, then restart the assistant."
                )
            elif "rate" in msg.lower() and "limit" in msg.lower():
                friendly = (
                    "The Claude API is rate-limiting requests right now. "
                    "Please wait a moment and try again."
                )
            else:
                friendly = (
                    "Something went wrong talking to the Claude API. "
                    f"Details: {msg}"
                )
            # Do NOT persist a partial tool-use exchange: if the last recorded
            # turn is an assistant `tool_use` without its matching `tool_result`,
            # saving it would corrupt the next request. Only keep a balanced delta.
            safe_delta = history_delta if _delta_is_balanced(history_delta) else [
                {"role": "user", "content": message}
            ]
            return {
                "text":           friendly,
                "plot":           result_plot,
                "tool_calls":     tool_calls_log,
                "hints":          [],
                "_history_delta": safe_delta,
            }

        # Agentic loop — handle multi-turn tool use
        for _round in range(_MAX_TOOL_ROUNDS):
            try:
                response = client.messages.create(
                    model      = self._model,
                    max_tokens = _MAX_TOKENS,
                    system     = system_prompt,
                    tools      = _TOOLS,
                    messages   = messages,
                )
            except Exception as exc:
                return _api_error(exc)

            # Collect text blocks from this response
            round_text = ""
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    round_text += block.text
            result_text += round_text

            # If Claude wants to use tools
            if response.stop_reason == "tool_use":
                # Stream the model's interim narration ("Let me check…").
                if round_text.strip():
                    _step("thinking", text=round_text.strip())
                tool_results: list[dict] = []

                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue

                    tool_name  = block.name
                    tool_input = block.input or {}

                    logger.info("[Assistant] Tool call: %s %s",
                                tool_name, json.dumps(tool_input)[:200])

                    _step("tool", name=tool_name,
                          label=_tool_label(tool_name, tool_input))
                    _emit_interactive(None)   # clear before each tool call
                    tool_output, plot_b64 = self._dispatch_tool(
                        tool_name,
                        tool_input,
                        project_root = project_root,
                        user_id      = uid,
                    )

                    if plot_b64:
                        result_plot = plot_b64
                    _fig = getattr(_PLOT_TL, "fig", None)
                    if _fig:
                        result_plot_fig = _fig

                    tool_calls_log.append({
                        "name":   tool_name,
                        "input":  tool_input,
                        "output": tool_output[:500] if isinstance(tool_output, str) else tool_output,
                    })

                    tool_content = (
                        tool_output if isinstance(tool_output, str)
                        else json.dumps(tool_output)
                    )
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     _truncate(tool_content, _MAX_TOOL_RESULT_CHARS),
                    })

                # Append the assistant `tool_use` turn AND its `tool_result`
                # user turn — to BOTH the live messages and the saved history
                # delta. They must always travel together, or the next request
                # will contain a `tool_use` with no matching `tool_result`.
                # Strip empty text blocks: the API rejects messages whose text
                # content blocks are empty ("text content blocks must be non-empty").
                clean = _clean_content(response.content)
                messages.append({"role": "assistant", "content": clean})
                messages.append({"role": "user",      "content": tool_results})
                history_delta.append({"role": "assistant", "content": clean})
                history_delta.append({"role": "user",      "content": tool_results})
                # Continue loop
                continue

            # Normal end — no more tool calls. Record the final assistant turn.
            history_delta.append({"role": "assistant", "content": _clean_content(response.content)})
            break
        else:
            # Loop exhausted while Claude still wanted to use tools. Make one
            # final call WITHOUT tools to force a clean textual answer, so the
            # turn never ends on an unanswered tool exchange.
            try:
                response = client.messages.create(
                    model      = self._model,
                    max_tokens = _MAX_TOKENS,
                    system     = system_prompt,
                    messages   = messages,
                )
                for block in response.content:
                    if getattr(block, "type", None) == "text":
                        result_text += block.text
                history_delta.append({"role": "assistant",
                                      "content": _clean_content(response.content)})
            except Exception as exc:
                logger.warning("[Assistant] Final wrap-up call failed: %s", exc)
                if not result_text:
                    result_text = (
                        "I reached the tool-use limit before finishing. "
                        "Please narrow the request or try again."
                    )
                history_delta.append({"role": "assistant", "content": result_text})

        # Run proactive hints on this turn
        hints = self._run_hints(
            message      = message,
            project_root = project_root,
            tool_calls   = tool_calls_log,
        )

        # Persist correction if user explicitly corrects the AI
        self._maybe_save_correction(message, result_text, history or [], uid)

        return {
            "text":             result_text,
            "plot":             result_plot,
            "plot_interactive": result_plot_fig,
            "tool_calls":       tool_calls_log,
            "hints":            hints,
            "_history_delta":   history_delta,
        }

    # ── System prompt builder ─────────────────────────────────────────────────

    def _build_system_prompt(
        self,
        message:      str,
        user_id:      str,
        project_root: str | Path | None,
        app_id:       str,
    ) -> str:
        # NOTE: use .replace (not .format) — the prompt contains LaTeX examples
        # with curly braces (e.g. \frac{R_g^2 q^2}{3}) that .format would try to
        # parse as replacement fields and crash on.
        parts: list[str] = [_SYSTEM_BASE.replace("{app_id}", app_id)]

        # Layer 3 + 2 + 1 memory context
        mem = self._get_memory(user_id)
        if mem:
            try:
                ctx      = mem.load_context(
                    project_root = project_root,
                    beamline_id  = self._beamline_id,
                )
                ctx_text = mem.format_for_prompt(ctx)
                if ctx_text.strip():
                    parts.append(ctx_text)
                # Adaptive verbosity from saved preferences (cross-project).
                prefs = ctx.get("user_preferences") or {}
                directive = _audience_directive(prefs)
                if directive:
                    parts.append(directive)
            except Exception as exc:
                logger.debug("[Assistant] Memory load error: %s", exc)

        # KB retrieval — find relevant knowledge chunks
        kb = self._get_knowledge_base()
        if kb:
            try:
                hits = kb.retrieve(message, top_k=_KB_TOP_K)
                if hits:
                    # Number the sources so the model can cite as [n] with a
                    # reference list (user's chosen citation style).
                    seen: dict[str, int] = {}
                    snippet_lines = [
                        "## Relevant Knowledge Base Excerpts",
                        "(Cite these with numbered markers [n] and end your "
                        "answer with a 'References' list mapping [n] → source.)",
                    ]
                    for h in hits:
                        src = h["source"]
                        if src not in seen:
                            seen[src] = len(seen) + 1
                        snippet_lines.append(
                            f"\n[{seen[src]}] ({h['collection']}: {src})\n{h['text']}"
                        )
                    refs = "  ".join(f"[{i}] {s}" for s, i in seen.items())
                    snippet_lines.append(f"\nReference key: {refs}")
                    parts.append("\n".join(snippet_lines))
            except Exception as exc:
                logger.debug("[Assistant] KB retrieval error: %s", exc)

        return "\n\n---\n\n".join(parts)

    # ── Tool dispatcher ───────────────────────────────────────────────────────

    def _dispatch_tool(
        self,
        name:         str,
        inputs:       dict,
        project_root: str | Path | None,
        user_id:      str,
    ) -> tuple[Any, str | None]:
        """Execute a tool call; if it returned a figure and the caller passed
        `save_as`, also write that PNG to assistant_outputs/."""
        output, plot = self._run_tool(name, inputs, project_root, user_id)
        if (plot and isinstance(inputs, dict) and inputs.get("save_as")
                and project_root):
            try:
                fn = _save_png(plot, project_root, inputs["save_as"])
                if isinstance(output, str):
                    output += f"  (figure saved to assistant_outputs/{fn})"
            except Exception as exc:
                logger.warning("[Assistant] save_as failed: %s", exc)
        return output, plot

    def _run_tool(
        self,
        name:         str,
        inputs:       dict,
        project_root: str | Path | None,
        user_id:      str,
    ) -> tuple[Any, str | None]:
        """
        Execute a tool call.
        Returns (tool_output_str, optional_base64_plot).
        """
        try:
            if name == "generate_plot":
                return self._tool_generate_plot(inputs)

            if name == "plot_metadata":
                return self._tool_plot_metadata(inputs, project_root)

            if name == "overlay_curves":
                return self._tool_overlay_curves(inputs, project_root)

            if name == "list_saxs_models":
                return self._tool_list_saxs_models(inputs)

            if name == "compute_pr":
                return self._tool_compute_pr(inputs, project_root)

            if name == "export":
                return self._tool_export(inputs, project_root)

            if name == "fit_model":
                return self._tool_fit_model(inputs, project_root)

            if name == "assess_quality":
                return self._tool_assess_quality(inputs, project_root)

            if name == "run_analysis":
                return self._tool_run_analysis(inputs)

            if name == "query_manifest":
                return self._tool_query_manifest(inputs, project_root)

            if name == "add_note":
                return self._tool_add_note(inputs, project_root)

            if name == "flag_quality":
                return self._tool_flag_quality(inputs, project_root)

            if name == "run_python":
                return self._tool_run_python(inputs, project_root)

            if name == "ingest_pdf":
                return self._tool_ingest_pdf(inputs)

            if name == "manage_knowledge":
                return self._tool_manage_knowledge(inputs, project_root)

            if name == "web_search":
                return self._tool_web_search(inputs)

            if name == "group_sops":
                return self._tool_group_sops(inputs, user_id)

            if name == "set_preferences":
                return self._tool_set_preferences(inputs, user_id)

            return f"Unknown tool: {name}", None

        except Exception as exc:
            logger.warning("[Assistant] Tool '%s' failed: %s", name, exc)
            return f"Tool error ({name}): {exc}", None

    # ── Tool implementations ──────────────────────────────────────────────────

    def _tool_generate_plot(self, inp: dict) -> tuple[str, str | None]:
        from src.ai.plots import generate_plot

        plot_type = inp.pop("plot_type")

        # Auto-load from file if provided
        file_path = inp.pop("file_path", None)
        if file_path:
            q, I, sigma = _load_dat(file_path)
            if q is not None:
                inp.setdefault("q",     q.tolist())
                inp.setdefault("I",     I.tolist())
                if sigma is not None:
                    inp.setdefault("sigma", sigma.tolist())

        b64 = generate_plot(plot_type, **inp)
        return f"Plot '{plot_type}' generated successfully.", b64

    def _tool_plot_metadata(
        self,
        inp:          dict,
        project_root: str | Path | None,
    ) -> tuple[str, str | None]:
        """Plot per-frame metadata (I0/bstop/transmission/…) over time for each
        averaged sample and return it as an inline base64 PNG."""
        if not project_root:
            return "No project root set — ask the user to select a project folder.", None

        from src.manifest import load_manifest, manifest_path_for
        from src.ai.plots import plot_metric_timeseries

        params = inp.get("parameters") or ["i0", "bstop"]
        det_in = (inp.get("detector") or "both").lower()
        dets   = ["saxs", "waxs"] if det_in == "both" else [det_in]
        kw_fil = (inp.get("keyword") or "").lower()
        stage  = (inp.get("stage") or "averaged").lower()
        if stage not in ("averaged", "subtracted"):
            stage = "averaged"

        root  = Path(project_root)
        mpath = root if root.is_file() else manifest_path_for(root)
        mf    = load_manifest(mpath)
        files = mf.get("files", {})
        reduced  = [v for v in files.values() if v.get("stage") == "reduced"]
        # Source samples come from either the averaged or the subtracted folder;
        # in both cases the per-frame acquisition metadata is read from the
        # underlying reduced frames (subtraction doesn't change beam metrics).
        sources  = [v for v in files.values() if v.get("stage") == stage]
        if not sources:
            return (f"No {stage} samples in the manifest yet — "
                    f"{'average some scans in the Viewer' if stage=='averaged' else 'run background subtraction'} first."), None

        series: list[dict] = []
        for v in sources:
            det = (v.get("detector") or "saxs").lower()
            if det not in dets:
                continue
            kw = v.get("keyword", "")
            if kw_fil and kw_fil not in kw.lower():
                continue
            # Subtracted keywords carry a trailing '_sub'; strip it so the same
            # reduced-frame matching (and labels) work for either stage.
            kw_match = kw[:-4] if kw.lower().endswith("_sub") else kw
            rows = _frames_for_averaged(reduced, kw_match, det, params)  # [(t, {param:val})]
            if not rows:
                continue
            # Sort frames by their time (Timer) value so the plotted series is
            # strictly monotonic in time (nulls last, just in case).
            rows = sorted(rows, key=lambda r: (r[0] is None, r[0] if r[0] is not None else 0.0))
            t0 = rows[0][0]
            entry = {
                "label":    kw_match.replace("Run1_", "").replace("_PES_support", ""),
                "detector": det,
                "t":        [r[0] - t0 for r in rows],
                "values":   {p: [r[1].get(p) for r in rows] for p in params},
            }
            series.append(entry)

        if not series:
            return (f"Couldn't match any frames to the {stage} samples for the "
                    "requested detector/keyword."), None

        # Flag parameters that carry no real signal (e.g. CTEMP = -1.0 sentinel
        # when the temperature controller is off), so the model can caveat it.
        notes = []
        for p in params:
            allvals = [x for s in series for x in s["values"].get(p, []) if x is not None]
            if allvals and all(abs(x - (-1.0)) < 1e-9 for x in allvals):
                notes.append(f"{p.upper()} is -1.0 for every frame "
                             "(sensor off / not recorded — no real values to show)")
            elif allvals and len(set(round(x, 6) for x in allvals)) == 1:
                notes.append(f"{p.upper()} is constant at {allvals[0]:g}")

        pretty = ", ".join(params)
        b64 = plot_metric_timeseries(
            series, params,
            title=f"{pretty} vs Timer — {len(series)} {stage} sample(s)",
        )
        summary = (f"Plotted {pretty} vs Timer for {len(series)} {stage} "
                   f"sample(s) across {len(dets)} detector(s).")
        if notes:
            summary += " Note: " + "; ".join(notes) + "."
        return summary, b64

    def _tool_overlay_curves(
        self,
        inp:          dict,
        project_root: str | Path | None,
    ) -> tuple[str, str | None]:
        """Overlay processed 1D curves (.dat) matched by keyword(s), one panel
        per detector. Returns an inline base64 PNG."""
        if not project_root:
            return "No project root set — ask the user to select a project folder.", None

        from src.manifest import load_manifest, manifest_path_for
        from src.ai.plots import plot_overlay

        keywords = [k.lower() for k in (inp.get("keywords") or []) if k]
        if not keywords:
            return "Provide at least one keyword to match (e.g. ['12C','air']).", None
        stage  = (inp.get("stage") or "averaged").lower()
        det_in = (inp.get("detector") or "both").lower()
        dets   = ["saxs", "waxs"] if det_in == "both" else [det_in]
        axis   = (inp.get("axis") or "loglog").lower()
        qmin   = inp.get("q_min")
        qmax   = inp.get("q_max")
        qmin   = float(qmin) if qmin is not None else None
        qmax   = float(qmax) if qmax is not None else None
        _MAX   = 16   # cap overlaid curves to keep the plot readable

        root  = Path(project_root)
        mpath = root if root.is_file() else manifest_path_for(root)
        mf    = load_manifest(mpath)
        files = mf.get("files", {})

        groups: dict[str, list] = {"saxs": [], "waxs": []}
        n = 0
        for key, v in files.items():
            if v.get("stage") != stage:
                continue
            det = (v.get("detector") or "saxs").lower()
            if det not in dets:
                continue
            name = Path(key).name
            if not any(kw in name.lower() for kw in keywords):
                continue
            if n >= _MAX:
                break
            q, I, sigma = _load_dat(v.get("path", key))
            if q is None:
                continue
            # Truncate to the user-requested q-range before plotting (SAXS: drop
            # beamstop/low-q or noisy high-q). q is in nm⁻¹.
            if qmin is not None or qmax is not None:
                import numpy as _np
                m = _np.ones(len(q), dtype=bool)
                if qmin is not None:
                    m &= (q >= qmin)
                if qmax is not None:
                    m &= (q <= qmax)
                if not m.any():
                    continue
                q, I = q[m], I[m]
                if sigma is not None:
                    sigma = sigma[m]
            # short label: which keyword + x-position
            import re as _re
            xm = _re.search(r"x-?[\d.]+", name)
            lab = (next((kw for kw in keywords if kw in name.lower()), "") +
                   (" " + xm.group(0) if xm else "")).strip() or name[:24]
            groups[det].append({"q": q.tolist(), "I": I.tolist(),
                                 "sigma": sigma.tolist() if sigma is not None else None,
                                 "label": lab})
            n += 1

        if n == 0:
            return (f"No {stage} files matched {keywords} for "
                    f"{'/'.join(dets).upper()}."), None

        qtxt = ""
        if qmin is not None or qmax is not None:
            lo = f"{qmin:g}" if qmin is not None else "min"
            hi = f"{qmax:g}" if qmax is not None else "max"
            qtxt = f"q {lo}–{hi} nm⁻¹"
        title = f"Overlay — {', '.join(keywords)} ({stage})" + (f"  ·  {qtxt}" if qtxt else "")
        b64 = plot_overlay(groups, axis=axis, title=title)
        try:
            from src.ai.plots import overlay_plotly
            _emit_interactive(overlay_plotly(groups, axis=axis, title=title))
        except Exception as exc:
            logger.debug("[Assistant] interactive overlay failed: %s", exc)
        per_det = ", ".join(f"{len(groups[d])} {d.upper()}" for d in dets if groups.get(d))
        msg = (f"Overlaid {n} {stage} curve(s) [{per_det}] matching {keywords} "
               f"on {axis} axes")
        if qtxt:
            msg += f", truncated to {qtxt}"
        return msg + ".", b64

    def _find_averaged_sample(self, project_root, keyword: str, detector: str):
        """Locate ONE averaged file entry matching keyword+detector.
        Returns (entry_dict, manifest, all_matches) or (None, manifest, matches)."""
        from src.manifest import load_manifest, manifest_path_for
        root  = Path(project_root)
        mpath = root if root.is_file() else manifest_path_for(root)
        mf    = load_manifest(mpath)
        kw    = (keyword or "").lower()
        det   = (detector or "saxs").lower()
        matches = [
            v for k, v in mf.get("files", {}).items()
            if v.get("stage") == "averaged"
            and (v.get("detector") or "").lower() == det
            and (not kw or kw in Path(k).name.lower())
        ]
        return (matches[0] if matches else None), mf, matches

    def _tool_fit_model(
        self, inp: dict, project_root: str | Path | None,
    ) -> tuple[str, str | None]:
        if not project_root:
            return "No project root set — ask the user to select a project folder.", None
        from src.ai.plots import plot_fit_residuals

        keyword = inp.get("keyword", "")
        model   = inp.get("model_name", "")
        params  = inp.get("params") or {}
        free    = inp.get("free")          # optional list of params to optimise
        det     = (inp.get("detector") or "SAXS").lower()
        axis    = (inp.get("axis") or "loglog").lower()
        if not keyword or not model or not params:
            return "fit_model needs keyword, model_name, and params.", None

        entry, _mf, matches = self._find_averaged_sample(project_root, keyword, det)
        if entry is None:
            return (f"No averaged {det.upper()} sample matched '{keyword}'."), None
        if len(matches) > 1:
            names = ", ".join(Path(m["path"]).name for m in matches[:5])
            return (f"'{keyword}' matched {len(matches)} {det.upper()} samples "
                    f"({names}…). Please narrow the keyword to one sample."), None

        q, I, sigma = _load_dat(entry.get("path", ""))
        if q is None:
            return f"Could not load data for {Path(entry.get('path','')).name}.", None
        import numpy as _np
        if sigma is None:
            sigma = _np.ones_like(I)

        # optional q-range trim
        qmin, qmax = inp.get("q_min"), inp.get("q_max")
        if qmin is not None or qmax is not None:
            m = _np.ones_like(q, dtype=bool)
            if qmin is not None:
                m &= q >= float(qmin)
            if qmax is not None:
                m &= q <= float(qmax)
            if m.sum() >= 10:
                q, I, sigma = q[m], I[m], sigma[m]

        try:
            from src.analysis.core import sasmodels_fit
        except Exception as exc:
            return (f"Model fitting is unavailable here ({exc}). Install the fitting "
                    "stack in the platform venv: `pip install scipy sasmodels`."), None
        res = sasmodels_fit(q, I, sigma, model, params, free=free)
        if "error" in res:
            return (f"Fit failed: {res['error']}. "
                    "If sasmodels is missing, install it in the platform venv."), None

        p = res.get("plot", {})
        b64 = plot_fit_residuals(
            p.get("q_data", q), p.get("I_data", I),
            p.get("q_fit", q),  p.get("I_fit", I),
            sigma=sigma, model=model, chi2=res.get("chi2"), axis=axis)
        pstr = ", ".join(f"{k}={v:.4g}" for k, v in res.get("params", {}).items())
        return (f"Fitted '{model}' to {Path(entry['path']).name}: "
                f"reduced χ² = {res.get('chi2')}. Parameters: {pstr}. "
                "Review the residuals; tell me to iterate (adjust guesses/free "
                "params) or try another model if they're not flat."), b64

    def _tool_assess_quality(
        self, inp: dict, project_root: str | Path | None,
    ) -> tuple[str, None]:
        if not project_root:
            return "No project root set — ask the user to select a project folder.", None
        import numpy as _np

        keyword = inp.get("keyword", "")
        det     = (inp.get("detector") or "SAXS").lower()
        entry, _mf, matches = self._find_averaged_sample(project_root, keyword, det)
        if entry is None:
            return (f"No averaged {det.upper()} sample matched '{keyword}'."), None

        reduced = [v for v in _mf.get("files", {}).values() if v.get("stage") == "reduced"]
        rows = _frames_for_averaged(reduced, entry.get("keyword", ""), det,
                                    ["transmission"])
        if not rows:
            return (f"Found the sample but couldn't locate its frames to QC."), None

        i0  = _np.array([r[1].get("i0") for r in rows], float)
        bst = _np.array([r[1].get("bstop") for r in rows], float)
        T   = _np.array([r[1].get("transmission") for r in rows], float)
        n   = len(rows)

        # Frame-outlier rejection via robust MAD on I0.
        med = float(_np.median(i0)); mad = float(_np.median(_np.abs(i0 - med))) or 1e-12
        z   = 0.6745 * (i0 - med) / mad        # robust z-score
        outliers = [int(i) for i in _np.where(_np.abs(z) > 3.5)[0]]

        # Beam / transmission sanity.
        n_T_bad  = int(_np.sum((T <= 0) | (T > 1.0)))
        n_i0_bad = int(_np.sum(i0 <= 0))
        n_bs_bad = int(_np.sum(bst <= 0))
        cv = float(_np.std(i0) / med * 100) if med else 0.0  # I0 stability %

        def verdict(ok, warn):
            return "PASS" if ok else ("WARN" if warn else "FAIL")

        report = {
            "sample":   Path(entry["path"]).name,
            "n_frames": n,
            "frame_outliers": {
                "verdict": verdict(not outliers, len(outliers) <= max(1, n // 20)),
                "count":   len(outliers),
                "indices": outliers[:20],
                "note":    "I0 robust-MAD |z|>3.5",
            },
            "transmission_sanity": {
                "verdict": verdict(n_T_bad == 0, False),
                "median_T": round(float(_np.median(T)), 4),
                "out_of_range_frames": n_T_bad,
            },
            "beam_sanity": {
                "verdict": verdict(n_i0_bad == 0 and n_bs_bad == 0, False),
                "i0_nonpositive": n_i0_bad, "bstop_nonpositive": n_bs_bad,
                "i0_stability_cv_pct": round(cv, 2),
            },
        }
        return json.dumps(report, indent=2), None

    def _tool_compute_pr(
        self, inp: dict, project_root: str | Path | None,
    ) -> tuple[str, str | None]:
        """Compute p(r) via IFT for one sample; return Rg/Dmax/I0 + p(r) plot."""
        if not project_root:
            return "No project root set — ask the user to select a project folder.", None

        keyword = inp.get("keyword", "")
        det     = (inp.get("detector") or "SAXS").lower()
        dmax    = inp.get("dmax")
        if not keyword:
            return "compute_pr needs a keyword to locate the sample.", None

        entry, _mf, matches = self._find_averaged_sample(project_root, keyword, det)
        if entry is None:
            return (f"No averaged {det.upper()} sample matched '{keyword}'."), None
        if len(matches) > 1:
            names = ", ".join(Path(m["path"]).name for m in matches[:5])
            return (f"'{keyword}' matched {len(matches)} {det.upper()} samples "
                    f"({names}…). Narrow the keyword to one sample."), None

        q, I, sigma = _load_dat(entry.get("path", ""))
        if q is None:
            return f"Could not load data for {Path(entry.get('path','')).name}.", None

        try:
            from src.analysis.core import pair_distance_ift
        except Exception as exc:
            return (f"p(r) is unavailable here ({exc}). Install the platform "
                    "analysis stack (numpy/scipy)."), None
        res = pair_distance_ift(q, I, sigma, dmax=dmax)
        if "error" in res:
            return (f"p(r) failed: {res['error']}"), None

        from src.ai.plots import plot_pair_distance
        b64 = plot_pair_distance(res["r"], res["pr"], Dmax=res.get("Dmax"),
                                 title=f"p(r): {Path(entry['path']).name}")
        return (f"p(r) for {Path(entry['path']).name}: "
                f"Rg = {res['Rg']} nm, Dmax = {res['Dmax']} nm, I0 = {res['I0']} "
                f"(IFT reduced χ² = {res['chi2']}). "
                "Check that p(r) returns smoothly to zero at Dmax — if it's "
                "truncated or dips negative, tell me to adjust Dmax."), b64

    def _tool_export(
        self, inp: dict, project_root: str | Path | None,
    ) -> tuple[str, None]:
        """Write a report/table/notes to <project>/assistant_outputs/ — the ONLY
        writable path. Never modifies experiment data."""
        if not project_root:
            return "No project root set — ask the user to select a project folder.", None

        from datetime import datetime
        try:
            out_dir = _assistant_outputs_dir(project_root)
        except Exception as exc:
            return f"Could not create assistant_outputs/: {exc}", None

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        kind  = inp.get("kind", "session_report")
        fmt   = (inp.get("format") or "").lower()

        # ── Free-text notes (captions / methods / summaries) ──────────────────
        if kind == "notes":
            content = inp.get("content") or ""
            if not content.strip():
                return "Nothing to save — provide `content` for the notes.", None
            name = _safe_filename(inp.get("filename"), f"notes_{stamp}", ".md")
            (out_dir / name).write_text(content)
            return (f"Saved notes to assistant_outputs/{name} "
                    f"({len(content)} chars)."), None

        from src.manifest import load_manifest, manifest_path_for
        import collections
        root  = Path(project_root); root = root.parent if root.is_file() else root
        mf    = load_manifest(manifest_path_for(root))
        files = mf.get("files", {})
        kw    = (inp.get("keyword") or "").lower()
        analyses = mf.get("analyses", {})

        def _match(name: str) -> bool:
            return (not kw) or kw in name.lower()

        # ── Fit-results table (CSV or XLSX) ───────────────────────────────────
        if kind == "fit_results":
            rows = [[aid, v.get("analysis_type", ""), str(v.get("file_path", "")),
                     json.dumps(v.get("results", v.get("params", {})))]
                    for aid, v in analyses.items()
                    if _match(str(v.get("file_path", "")))]
            header = ["analysis_id", "type", "file", "results_json"]
            if fmt == "xlsx":
                try:
                    from openpyxl import Workbook
                    wb = Workbook(); ws = wb.active; ws.title = "fits"
                    ws.append(header)
                    for r in rows:
                        ws.append(r)
                    name = _safe_filename(inp.get("filename"), f"fit_results_{stamp}", ".xlsx")
                    wb.save(out_dir / name)
                    return (f"Wrote {len(rows)} record(s) to assistant_outputs/{name}."
                            + (" (empty — run fits first.)" if not rows else "")), None
                except Exception as exc:
                    fmt = "csv"  # graceful fallback
                    note = f" (xlsx unavailable: {exc}; saved CSV instead)"
            else:
                note = ""
            import csv
            name = _safe_filename(inp.get("filename"), f"fit_results_{stamp}", ".csv")
            with (out_dir / name).open("w", newline="") as fh:
                w = csv.writer(fh); w.writerow(header); w.writerows(rows)
            return (f"Wrote {len(rows)} record(s) to assistant_outputs/{name}.{note}"
                    + (" (empty — run fits first.)" if not rows else "")), None

        # ── Session report (HTML or PDF) ──────────────────────────────────────
        by_stage = collections.Counter(v.get("stage", "?") for v in files.values())
        by_det   = collections.Counter(str(v.get("detector", "?")).upper()
                                       for v in files.values())
        averaged = [(k, v) for k, v in files.items()
                    if v.get("stage") == "averaged" and _match(Path(k).name)]
        summary = (f"Total files: {len(files)} — reduced {by_stage.get('reduced',0)}, "
                   f"averaged {by_stage.get('averaged',0)}, subtracted "
                   f"{by_stage.get('subtracted',0)}; SAXS {by_det.get('SAXS',0)} / "
                   f"WAXS {by_det.get('WAXS',0)}")

        if fmt == "pdf":
            try:
                import matplotlib; matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                from matplotlib.backends.backend_pdf import PdfPages
                name = _safe_filename(inp.get("filename"), f"session_report_{stamp}", ".pdf")
                with PdfPages(out_dir / name) as pdf:
                    fig = plt.figure(figsize=(8.27, 11.69))  # A4 portrait
                    fig.text(0.08, 0.94, "SWAXS Session Report", fontsize=20,
                             fontweight="bold", color="#8C1515")
                    fig.text(0.08, 0.90, f"Project: {root}", fontsize=8, color="#444")
                    fig.text(0.08, 0.88, f"Generated: {stamp}"
                             + (f"   filter: {kw}" if kw else ""), fontsize=8, color="#444")
                    fig.text(0.08, 0.84, summary, fontsize=10)
                    fig.text(0.08, 0.79, f"Averaged samples ({len(averaged)}):",
                             fontsize=11, fontweight="bold")
                    y = 0.76
                    for k, v in averaged[:40]:
                        fig.text(0.10, y, f"• {Path(k).name}  [{str(v.get('detector','')).upper()}]",
                                 fontsize=7, family="monospace")
                        y -= 0.018
                        if y < 0.08:
                            break
                    fig.text(0.08, 0.04, "Generated by the Tassone Group Assistant. "
                             "Experiment data was not modified.", fontsize=7, color="#888")
                    plt.axis("off"); pdf.savefig(fig); plt.close(fig)
                return (f"Wrote PDF session report to assistant_outputs/{name} "
                        f"({len(files)} files, {len(averaged)} averaged)."), None
            except Exception as exc:
                fmt = "html"  # graceful fallback to HTML

        import html as _html
        def _esc(s):
            return _html.escape(str(s))
        rows_avg = "\n".join(
            f"<tr><td>{_esc(Path(k).name)}</td><td>{_esc(str(v.get('detector','')).upper())}</td>"
            f"<td>{_esc(v.get('keyword',''))}</td></tr>" for k, v in averaged[:500])
        rows_an = "\n".join(
            f"<tr><td>{_esc(v.get('analysis_type',''))}</td>"
            f"<td>{_esc(Path(str(v.get('file_path',''))).name)}</td></tr>"
            for v in analyses.values()) or "<tr><td colspan=2>none yet</td></tr>"
        doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>SWAXS session report</title><style>
body{{font-family:Inter,system-ui,sans-serif;margin:2rem;color:#111827;max-width:900px}}
h1{{color:#8C1515}} h2{{border-bottom:2px solid #fde8ea;padding-bottom:3px}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0;font-size:.9rem}}
th{{background:#8C1515;color:#fff;text-align:left;padding:6px 10px}}
td{{padding:5px 10px;border-top:1px solid #e5e7eb}}
.kv{{color:#4b5563}}</style></head><body>
<h1>SWAXS Session Report</h1>
<p class="kv">Project: {_esc(root)}<br>Generated: {_esc(stamp)}
{(' &middot; filter: ' + _esc(kw)) if kw else ''}</p>
<h2>Processing summary</h2><p>{_esc(summary)}</p>
<h2>Averaged samples ({len(averaged)})</h2>
<table><tr><th>File</th><th>Detector</th><th>Keyword</th></tr>{rows_avg}</table>
<h2>Analyses ({len(analyses)})</h2>
<table><tr><th>Type</th><th>File</th></tr>{rows_an}</table>
<p class="kv" style="margin-top:2rem">Generated by the Tassone Group Assistant.
Experiment data was not modified.</p></body></html>"""
        name = _safe_filename(inp.get("filename"), f"session_report_{stamp}", ".html")
        (out_dir / name).write_text(doc)
        return (f"Wrote session report to assistant_outputs/{name} "
                f"({len(files)} files, {len(averaged)} averaged samples, "
                f"{len(analyses)} analyses). Open it in a browser."), None

    def _tool_list_saxs_models(self, inp: dict) -> tuple[str, None]:
        """Return the SAXS model catalog (sasmodels), with a curated fallback."""
        keyword = (inp.get("keyword") or "").lower()

        models: list[str] = []
        source = ""
        # 1. Direct import (works without the Analysis app running)
        try:
            import sasmodels.core as _sm
            models = sorted(_sm.list_models())
            source = "sasmodels"
        except Exception:
            # 2. Ask the Analysis app, if it's up
            try:
                import urllib.request
                base = os.environ.get("SWAXS_ANALYSIS_API", "http://localhost:5004")
                url = base.rstrip("/") + "/api/sasmodels/list"
                with urllib.request.urlopen(url, timeout=1) as r:
                    models = json.loads(r.read()).get("models", [])
                    source = "analysis app"
            except Exception:
                models = []

        if models:
            if keyword:
                models = [m for m in models if keyword in m.lower()]
            return json.dumps({
                "source": source,
                "count":  len(models),
                "models": models[:120],
            }, indent=2), None

        # 3. Curated fallback (sasmodels not installed) — common SAXS models,
        #    their use-case and the feature that sets the initial guess.
        curated = _CURATED_SAXS_MODELS
        if keyword:
            curated = {k: v for k, v in curated.items()
                       if keyword in k.lower() or keyword in v.lower()}
        return json.dumps({
            "source": "curated (install `sasmodels` for the full ~80-model list)",
            "count":  len(curated),
            "models": curated,
        }, indent=2), None

    def _tool_run_analysis(self, inp: dict) -> tuple[str, None]:
        """
        Dispatch a numeric analysis using src.analysis.core functions.
        Returns a JSON string with results (no plot — use generate_plot for that).
        """
        analysis_type = inp.get("analysis_type")
        file_path     = inp.get("file_path", "")

        q, I, sigma = _load_dat(file_path)
        if q is None:
            return f"Cannot load file: {file_path}", None

        # Strip the verbose plot sub-dict so the result stays concise for
        # the model context window — the assistant uses generate_plot for visuals.
        def _strip_plot(d: dict) -> dict:
            return {k: v for k, v in d.items() if k != "plot"}

        results: dict = {"file": file_path, "analysis": analysis_type}

        try:
            from src.analysis.core import (
                guinier_fit, porod_fit, kratky_plot,
            )

            if analysis_type == "guinier":
                res = guinier_fit(
                    q, I, sigma,
                    q_min      = inp.get("q_min"),
                    q_max      = inp.get("q_max"),
                    auto_range = bool(inp.get("auto_range", True)),
                )
                results.update(_strip_plot(res))

            elif analysis_type == "porod":
                res = porod_fit(
                    q, I, sigma,
                    q_min = inp.get("q_min"),
                    q_max = inp.get("q_max"),
                )
                results.update(_strip_plot(res))

            elif analysis_type == "kratky":
                # Kratky is display-only; return a data summary
                res = kratky_plot(q, I,
                                  q_min=inp.get("q_min"),
                                  q_max=inp.get("q_max"))
                results["n_points"] = len(res.get("q", []))
                results["description"] = (
                    "Kratky data computed. Use generate_plot(kratky, "
                    f"file_path='{file_path}') to visualise."
                )

            elif analysis_type == "pair_distance":
                from src.analysis.core import pair_distance_ift
                pr = pair_distance_ift(q, I, sigma,
                                       dmax=inp.get("Dmax_hint") or inp.get("q_max"))
                if "error" in pr:
                    results["error"] = pr["error"]
                else:
                    results.update({k: pr[k] for k in
                                    ("Rg", "Dmax", "I0", "chi2")})
                    results["note"] = ("p(r) computed by regularized IFT. "
                                       "Use compute_pr for the inline plot.")

            else:
                results["error"] = f"Unknown analysis_type: {analysis_type}"

        except Exception as exc:
            results["error"] = str(exc)

        return json.dumps(results, indent=2), None

    def _tool_query_manifest(
        self,
        inp:          dict,
        project_root: str | Path | None,
    ) -> tuple[str, None]:
        if not project_root:
            return "No project root set — ask the user to select a project folder.", None

        from src.manifest import load_manifest, manifest_path_for

        try:
            root  = Path(project_root)
            mpath = root if root.is_file() else manifest_path_for(root)
            mf    = load_manifest(mpath)
        except Exception as exc:
            return f"Cannot load manifest: {exc}", None

        query_type = inp.get("query_type", "summary")
        keyword    = inp.get("keyword", "").lower()
        detector   = inp.get("detector", "").upper()

        if query_type == "summary":
            files = mf.get("files", {})
            # Files are tracked by processing stage, not a nested "averaged" list.
            by_stage: dict[str, int] = {}
            for v in files.values():
                st = v.get("stage", "unknown")
                by_stage[st] = by_stage.get(st, 0) + 1
            return json.dumps({
                "version":          mf.get("version", "unknown"),
                "total_files":      len(files),
                "reduced_files":    by_stage.get("reduced", 0),
                "averaged_files":   by_stage.get("averaged", 0),
                "subtracted_files": by_stage.get("subtracted", 0),
                "background_count": len(mf.get("background", {})),
                "analysis_count":   len(mf.get("analyses", {})),
                "events_count":     len(mf.get("events", [])),
            }), None

        if query_type == "files":
            entries  = mf.get("files", {})
            filtered = _filter_manifest_entries(entries, keyword, detector)
            return json.dumps(_files_response(filtered), indent=2), None

        if query_type == "averaged":
            # Averaged outputs are file entries with stage == "averaged".
            out = {k: v for k, v in mf.get("files", {}).items()
                   if v.get("stage") == "averaged"
                   and (not keyword or keyword in k.lower())
                   and (not detector or detector in k.upper())}
            return json.dumps(_files_response(out), indent=2), None

        if query_type == "background":
            entries  = mf.get("background", {})
            filtered = {k: v for k, v in entries.items()
                        if (not keyword or keyword in k.lower())
                        and (not detector or detector in k.upper())}
            return json.dumps(_capped_dict(filtered), indent=2), None

        if query_type == "analysis":
            # Analyses are keyed by uuid; filter on the entry's file_path.
            entries = mf.get("analyses", {})
            if keyword:
                filtered = {k: v for k, v in entries.items()
                            if keyword in str(v.get("file_path", "")).lower()}
            else:
                filtered = entries
            return json.dumps(_capped_dict(filtered), indent=2), None

        if query_type == "quality_flags":
            ai_mem = mf.get("ai_memory", {})
            return json.dumps(ai_mem.get("quality_flags", {}), indent=2), None

        if query_type == "events":
            events = mf.get("events", [])[-20:]  # last 20 events
            return json.dumps(events, indent=2), None

        return f"Unknown query_type: {query_type}", None

    def _tool_add_note(
        self,
        inp:          dict,
        project_root: str | Path | None,
    ) -> tuple[str, None]:
        if not project_root:
            return "No project root — cannot update manifest.", None

        from src.manifest import add_file_note

        file_path = inp.get("file_path", "")
        note      = inp.get("note", "")

        if not file_path or not note:
            return "file_path and note are required.", None

        try:
            from src.manifest import update_manifest
            found = update_manifest(project_root, lambda m: add_file_note(m, file_path, note))
            if found:
                return f"Note added to {Path(file_path).name}.", None
            return (f"File {Path(file_path).name} not in manifest yet; "
                    "note not saved (reduce the file first).", None)
        except Exception as exc:
            return f"Failed to add note: {exc}", None

    def _tool_flag_quality(
        self,
        inp:          dict,
        project_root: str | Path | None,
    ) -> tuple[str, None]:
        if not project_root:
            return "No project root — cannot update manifest.", None

        file_path = inp.get("file_path", "")
        flag      = inp.get("flag", "")

        if not file_path or not flag:
            return "file_path and flag are required.", None

        try:
            from src.manifest import update_manifest, add_quality_flag
            found = update_manifest(
                project_root,
                lambda m: add_quality_flag(m, file_path, flag, source="ai"),
            )
            if found:
                return f"Quality flag '{flag}' set on {Path(file_path).name}.", None
            return (f"File {Path(file_path).name} not in manifest yet; "
                    "flag not saved (reduce the file first).", None)
        except Exception as exc:
            return f"Failed to flag quality: {exc}", None

    def _tool_run_python(
        self, inp: dict, project_root: str | Path | None,
    ) -> tuple[str, str | None]:
        """Run a guarded Python snippet; return stdout/errors + an inline figure."""
        code = inp.get("code", "")
        if not code.strip():
            return "Provide `code` to run.", None
        try:
            from src.ai.code_exec import run_user_code
        except Exception as exc:
            return f"Code execution unavailable: {exc}", None

        res = run_user_code(code, project_root=project_root)
        fig = res.get("figure")
        if not res.get("ok"):
            return (f"Code did not run cleanly. {res.get('error','')}").strip(), fig
        parts = []
        if res.get("stdout", "").strip():
            parts.append("Output:\n" + res["stdout"].strip())
        if fig:
            parts.append("(figure shown below)")
        if not parts:
            parts.append("Ran successfully (no output).")
        return "\n".join(parts), fig

    def _tool_ingest_pdf(self, inp: dict) -> tuple[str, None]:
        pdf_path   = inp.get("pdf_path", "")
        collection = inp.get("collection", "user_papers")

        kb = self._get_knowledge_base()
        if kb is None:
            return "Knowledge base unavailable (ChromaDB not installed).", None

        try:
            n = kb.ingest_pdf(pdf_path, collection=collection)
            if n == 0:
                return (
                    f"PDF already indexed (unchanged): {Path(pdf_path).name}."
                ), None
            return (
                f"Ingested {Path(pdf_path).name} into '{collection}' "
                f"({n} chunks)."
            ), None
        except Exception as exc:
            return f"Ingestion failed: {exc}", None

    def _tool_manage_knowledge(
        self, inp: dict, project_root: str | Path | None = None,
    ) -> tuple[str, None]:
        """Visualise / add / remove items in the assistant's knowledge base."""
        kb = self._get_knowledge_base()
        if kb is None:
            return ("Knowledge base unavailable — install it in the platform "
                    "venv: `pip install chromadb sentence-transformers`. "
                    "(The list of indexed papers may still be readable from "
                    "ai_knowledge/ingestion_log.json.)"), None

        action = (inp.get("action") or "list").lower()
        col    = inp.get("collection") or "user_papers"

        try:
            if action == "list":
                items = kb.list_ingested()
                try:
                    stats = kb.collection_stats()
                except Exception:
                    stats = {}
                view = [{"name": it.get("name", it.get("source")),
                         "collection": it["collection"],
                         "chunks": it.get("chunks"),
                         "added": it.get("ingested_at", "")[:10]}
                        for it in items]
                return json.dumps({"counts": stats, "items": view,
                                   "total": len(view)}, indent=2), None

            if action == "add_pdf":
                path = inp.get("path", "")
                if not path:
                    return "Provide the PDF `path` to add.", None
                n = kb.ingest_pdf(path, collection=col)
                return (f"Added {Path(path).name} to '{col}' ({n} chunks)."
                        if n else f"{Path(path).name} already indexed (unchanged)."), None

            if action == "add_note":
                text = inp.get("text", "")
                nm   = inp.get("name") or "note"
                if not text.strip():
                    return "Provide `text` for the note.", None
                n = kb.ingest_text(text, name=nm, collection=col)
                return f"Saved note '{nm}' to '{col}' ({n} chunks).", None

            if action == "ingest_folder":
                # Explicit path, else scan BOTH the per-project <project>/papers/
                # and the global ai_knowledge/user_papers/ folders.
                folders = []
                if inp.get("path"):
                    folders = [Path(inp["path"])]
                else:
                    if project_root:
                        pj = (Path(project_root).parent if Path(project_root).is_file()
                              else Path(project_root)) / "papers"
                        if pj.is_dir():
                            folders.append(pj)
                    folders.append(self._kb_dir / "user_papers")
                folders = [f for f in folders if f.is_dir()]
                if not folders:
                    return ("No papers folder found. Add PDFs to "
                            "<project>/papers/ or ai_knowledge/user_papers/."), None
                added, skipped = 0, 0
                for folder in folders:
                    for pdf in sorted(folder.glob("*.pdf")):
                        n = kb.ingest_pdf(pdf, collection=col)
                        added += 1 if n else 0
                        skipped += 0 if n else 1
                where = ", ".join(f.name for f in folders)
                return (f"Ingested from [{where}]: {added} new/updated, "
                        f"{skipped} unchanged into '{col}'."), None

            if action == "remove":
                nm = inp.get("name", "")
                if not nm:
                    return "Provide the `name` of the source to remove.", None
                res = kb.remove_source(nm)
                if "error" in res:
                    return res["error"], None
                return (f"Removed {res['removed']} ({res['chunks']} chunks) from "
                        "the knowledge base. Re-add it anytime to restore."), None

            return f"Unknown action: {action}", None
        except Exception as exc:
            return f"manage_knowledge failed: {exc}", None

    def _tool_web_search(self, inp: dict) -> tuple[str, None]:
        """Search Crossref for scholarly papers (online; graceful offline)."""
        import urllib.request
        import urllib.parse

        q = (inp.get("query") or "").strip()
        if not q:
            return "Provide a search query.", None
        try:
            n = max(1, min(int(inp.get("max_results") or 5), 10))
        except (TypeError, ValueError):
            n = 5

        url = "https://api.crossref.org/works?" + urllib.parse.urlencode({
            "query":  q,
            "rows":   n,
            "select": "title,author,issued,DOI,container-title",
        })
        req = urllib.request.Request(url, headers={
            "User-Agent": "SWAXS-Assistant/1.0 (mailto:akmaurya@stanford.edu)",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
        except Exception as exc:
            return (f"Web search is unavailable right now (offline or blocked: "
                    f"{exc}). I can still use the local knowledge base and your "
                    "ingested papers — say the word."), None

        items = (data.get("message", {}) or {}).get("items", []) or []
        results = []
        for it in items:
            title = (it.get("title") or ["(untitled)"])[0]
            authors = it.get("author", []) or []
            a = ", ".join(x.get("family", "") for x in authors[:3] if x.get("family"))
            if len(authors) > 3:
                a += " et al."
            try:
                year = it.get("issued", {}).get("date-parts", [[None]])[0][0]
            except Exception:
                year = None
            results.append({
                "title":   title,
                "authors": a,
                "year":    year,
                "venue":   (it.get("container-title") or [""])[0],
                "doi":     it.get("DOI", ""),
            })
        return json.dumps({"query": q, "n": len(results),
                           "results": results}, indent=2), None

    def _tool_set_preferences(self, inp: dict, user_id: str) -> tuple[str, None]:
        """Persist user preferences (cross-project) into Layer-1 memory."""
        mem = self._get_memory(user_id)
        if mem is None:
            return "Memory system unavailable — cannot save preferences.", None
        fields = {k: v for k, v in inp.items()
                  if k in ("audience", "verbosity", "default_model",
                           "units", "citation_style") and v not in (None, "")}
        if not fields:
            return "No recognised preferences to set.", None
        try:
            mem.update_preferences(**fields)
            saved = ", ".join(f"{k}={v}" for k, v in fields.items())
            return (f"Saved preferences ({saved}). These now apply across all "
                    "your projects."), None
        except Exception as exc:
            return f"Could not save preferences: {exc}", None

    def _tool_group_sops(self, inp: dict, user_id: str) -> tuple[str, None]:
        """View/add/remove shared group SOPs & conventions (always-loaded memory)."""
        mem = self._get_memory(user_id)
        if mem is None:
            return "Memory system unavailable — cannot manage group SOPs.", None
        action = (inp.get("action") or "list").lower()
        try:
            if action == "list":
                sops = mem.load_group_sops()
                if not sops:
                    return "No group SOPs/conventions saved yet.", None
                return json.dumps([{"id": s["id"], "title": s["title"],
                                    "text": s["text"]} for s in sops], indent=2), None
            if action == "add":
                title = inp.get("title", ""); text = inp.get("text", "")
                if not text.strip():
                    return "Provide the SOP `text` (and a short `title`).", None
                e = mem.add_group_sop(title, text)
                return (f"Added group SOP '{e['title']}' (id {e['id']}). "
                        "It now applies across all projects."), None
            if action == "remove":
                ident = inp.get("id") or inp.get("title") or ""
                if not ident:
                    return "Provide the SOP `id` or `title` to remove.", None
                ok = mem.remove_group_sop(ident)
                return (f"Removed group SOP '{ident}'." if ok else
                        f"No group SOP matched '{ident}'."), None
            return f"Unknown action: {action}", None
        except Exception as exc:
            return f"group_sops failed: {exc}", None

    # ── Proactive hints ───────────────────────────────────────────────────────

    def _run_hints(
        self,
        message:      str,
        project_root: str | Path | None,
        tool_calls:   list[dict],
    ) -> list[str]:
        """
        Run HintChecker on any analysis results returned by tool calls.
        Returns a list of plain-text hint strings.
        """
        try:
            from src.ai.hints import HintChecker
            checker = HintChecker()
            hint_texts: list[str] = []

            for tc in tool_calls:
                if tc["name"] != "run_analysis":
                    continue
                output_raw = tc.get("output", "")
                try:
                    result_dict = json.loads(output_raw) if isinstance(output_raw, str) else output_raw
                except Exception:
                    continue

                hints = checker.on_analysis({
                    "analysis_type": result_dict.get("analysis", ""),
                    "file_path":     result_dict.get("file"),
                    "results":       result_dict,
                })
                hint_texts += [h.message for h in hints]

            return hint_texts
        except Exception as exc:
            logger.debug("[Assistant] Hints error: %s", exc)
            return []

    # ── Correction detection ──────────────────────────────────────────────────

    def _maybe_save_correction(
        self,
        message:  str,
        response: str,
        history:  list[dict],
        user_id:  str,
    ) -> None:
        """
        Heuristic: if the message looks like a correction of the prior AI turn,
        save it to LayeredMemory so future sessions don't repeat the mistake.
        """
        _CORRECTION_TRIGGERS = [
            "no, ", "actually,", "that's wrong", "incorrect",
            "not right", "you said", "you got", "wrong rg", "wrong range",
        ]
        msg_lo = message.lower()
        if not any(t in msg_lo for t in _CORRECTION_TRIGGERS):
            return

        prior_text = ""
        for turn in reversed(history):
            if turn.get("role") == "assistant":
                content = turn.get("content", "")
                if isinstance(content, str):
                    prior_text = content
                break

        if prior_text:
            mem = self._get_memory(user_id)
            if mem:
                try:
                    mem.save_correction(
                        turn      = len(history),
                        original  = prior_text[:500],
                        corrected = message[:500],
                    )
                except Exception as exc:
                    logger.debug("[Assistant] Could not save correction: %s", exc)

    # ── Lazy initialisers ─────────────────────────────────────────────────────

    def _get_client(self):
        if self._anthropic_client is not None:
            return self._anthropic_client

        # Reuse SLAC gateway config from ~/.claude/settings.json if not in env.
        _load_claude_settings_into_env()

        # ── Credentials & endpoint ────────────────────────────────────────────
        # Two supported auth modes (both read from the environment — never from
        # the repo). Prefer the SLAC/enterprise gateway when configured:
        #   • Gateway (SLAC, KB0015379): ANTHROPIC_AUTH_TOKEN (Bearer) +
        #     ANTHROPIC_BASE_URL = https://ai-api.slac.stanford.edu
        #   • Direct Anthropic API:      ANTHROPIC_API_KEY (x-api-key)
        # Secrets must come from a secure vault/keychain exported into the
        # environment; .env is only a last-resort local fallback for the key.
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        base_url   = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        api_key    = os.environ.get("ANTHROPIC_API_KEY", "").strip()

        # Fallback: read ANTHROPIC_API_KEY directly from .env in the project root
        if not auth_token and not api_key:
            _root = Path(__file__).resolve().parent.parent.parent
            _dotenv = _root / ".env"
            if _dotenv.is_file():
                try:
                    for _line in _dotenv.read_text().splitlines():
                        _line = _line.strip()
                        if _line.startswith("ANTHROPIC_API_KEY="):
                            api_key = _line.split("=", 1)[1].strip().strip('"').strip("'")
                            if api_key:
                                os.environ["ANTHROPIC_API_KEY"] = api_key
                                logger.info("[Assistant] Loaded ANTHROPIC_API_KEY from .env")
                            break
                except Exception as _e:
                    logger.warning("[Assistant] Could not read .env: %s", _e)

        if not auth_token and not api_key:
            logger.warning(
                "[Assistant] No credentials found. Set ANTHROPIC_AUTH_TOKEN (+ "
                "ANTHROPIC_BASE_URL for the SLAC gateway) or ANTHROPIC_API_KEY in "
                "your environment, then restart."
            )
            return None

        try:
            import anthropic
            kwargs: dict = {}
            if base_url:
                kwargs["base_url"] = base_url
            if auth_token:
                # Gateway / Bearer auth (SLAC enterprise gateway)
                kwargs["auth_token"] = auth_token
                logger.info("[Assistant] Using gateway auth (base_url=%s, model=%s)",
                            base_url or "default", self._model)
            else:
                kwargs["api_key"] = api_key
                logger.info("[Assistant] Using direct API key (base_url=%s, model=%s)",
                            base_url or "default", self._model)
            self._anthropic_client = anthropic.Anthropic(**kwargs)
            return self._anthropic_client
        except ImportError:
            logger.error(
                "[Assistant] anthropic package not installed. "
                "Run: pip install anthropic"
            )
            return None

    def _get_knowledge_base(self):
        if self._kb is not None:
            return self._kb
        try:
            from src.ai.knowledge import KnowledgeBase
            self._kb = KnowledgeBase(self._kb_dir)
            return self._kb
        except Exception as exc:
            logger.debug("[Assistant] KB unavailable: %s", exc)
            return None

    def _get_memory(self, user_id: str | None = None):
        uid = user_id or self._user_id
        if self._mem is not None and self._mem._user_id == uid:
            return self._mem
        try:
            from src.ai.memory import LayeredMemory
            self._mem = LayeredMemory(
                ai_knowledge_dir = self._kb_dir,
                user_id          = uid,
            )
            return self._mem
        except Exception as exc:
            logger.debug("[Assistant] Memory unavailable: %s", exc)
            return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_claude_settings_into_env() -> None:
    """
    Populate ANTHROPIC_* environment variables from ``~/.claude/settings.json``
    (SLAC's sanctioned gateway config; see KB0015379) when they are not already
    set. This lets the SWAXS app reuse the SAME token/endpoint as the Claude Code
    CLI — one place to maintain, nothing in the repo.

    Real environment variables always win (we only fill what's missing), and the
    documentation placeholder token is ignored.
    """
    path = Path.home() / ".claude" / "settings.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text())
        env  = data.get("env", {}) if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("[Assistant] Could not read %s: %s", path, exc)
        return

    base = str(env.get("ANTHROPIC_BASE_URL", "")).strip()
    if base and not os.environ.get("ANTHROPIC_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = base

    tok = str(env.get("ANTHROPIC_AUTH_TOKEN", "")).strip()
    if tok and tok != "yourSlacApiKeyHere" and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        os.environ["ANTHROPIC_AUTH_TOKEN"] = tok

    if not os.environ.get("ANTHROPIC_MODEL"):
        model = str(env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")).strip()
        if model:
            os.environ["ANTHROPIC_MODEL"] = model


def _clean_content(content):
    """
    Remove EMPTY text blocks from an assistant message's content. The Anthropic
    API rejects a stored message whose text content block is empty
    ("text content blocks must be non-empty"), which can happen when the model
    emits a tool_use with a blank text block. Tool_use/other blocks are kept.
    Falls back to a single space if everything would be removed.
    """
    if not isinstance(content, list):
        return content
    kept = []
    for b in content:
        btype = getattr(b, "type", None)
        if btype is None and isinstance(b, dict):
            btype = b.get("type")
        if btype == "text":
            txt = getattr(b, "text", None)
            if txt is None and isinstance(b, dict):
                txt = b.get("text", "")
            if not (txt or "").strip():
                continue          # drop empty text block
        kept.append(b)
    return kept or [{"type": "text", "text": " "}]


def _delta_is_balanced(delta: list[dict]) -> bool:
    """
    True if every assistant turn containing `tool_use` blocks is immediately
    followed by a user turn carrying the matching `tool_result` ids. Used to
    avoid persisting a half-finished tool exchange into the session history,
    which would make the *next* API request invalid (the 400 error
    "tool_use ids were found without tool_result blocks immediately after").
    """
    for i, turn in enumerate(delta):
        if turn.get("role") != "assistant":
            continue
        content = turn.get("content")
        if not isinstance(content, list):
            continue
        tool_ids = {
            getattr(b, "id", None) or (b.get("id") if isinstance(b, dict) else None)
            for b in content
            if (getattr(b, "type", None) == "tool_use")
            or (isinstance(b, dict) and b.get("type") == "tool_use")
        }
        tool_ids.discard(None)
        if not tool_ids:
            continue
        # The next turn must be a user message answering all of these ids.
        nxt = delta[i + 1] if i + 1 < len(delta) else None
        if not nxt or nxt.get("role") != "user" or not isinstance(nxt.get("content"), list):
            return False
        answered = {
            b.get("tool_use_id") for b in nxt["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        if not tool_ids.issubset(answered):
            return False
    return True


def _load_dat(file_path: str):
    """Load q, I, sigma from a .dat file. Returns (None, None, None) on error."""
    import numpy as np
    try:
        from src.utils.read_dat_metadata import read_dat_data_metadata
        _, q, I, sigma, _ = read_dat_data_metadata(file_path)
        return np.asarray(q), np.asarray(I), (np.asarray(sigma) if sigma is not None else None)
    except Exception as exc:
        logger.debug("[Assistant] Cannot load %s: %s", file_path, exc)
        return None, None, None


def _filter_manifest_entries(
    entries: dict,
    keyword: str,
    detector: str,
) -> dict:
    out = {}
    for key, val in entries.items():
        if keyword and keyword not in key.lower():
            continue
        if detector and detector not in key.upper():
            continue
        out[key] = val
    return out


# Curated common SAXS form-factor models (fallback when sasmodels isn't
# installed). Value = use-case + the data feature that sets the initial guess.
_CURATED_SAXS_MODELS = {
    "sphere":              "Globular particles/nanoparticles. radius ≈ Rg·√(5/3).",
    "ellipsoid":           "Anisotropic globular particles. Re, Rp from Rg + aspect.",
    "core_shell_sphere":   "Micelles/vesicles/coated NPs. core radius + shell thickness.",
    "cylinder":            "Rod-like particles. radius (high-q), length (low-q slope≈−1).",
    "core_shell_cylinder": "Coated rods/fibres. radius + shell thickness + length.",
    "flexible_cylinder":   "Worm-like polymers. kuhn_length + contour length.",
    "lamellar":            "Single bilayer/membrane. thickness = 2π/q of high-q falloff.",
    "lamellar_stack_paracrystal": "Multilamellar stacks. d-spacing = 2π/q_peak, N layers.",
    "guinier_porod":       "General start: Rg + dimensionality s (0 sphere,1 rod,2 lamellar).",
    "broad_peak":          "Amorphous correlation peak (mesh/d-spacing). d = 2π/q_peak.",
    "correlation_length":  "Polymer gels/networks (Ornstein–Zernike + Porod). ξ = 1/q_knee.",
    "power_law":           "Fractal/rough interface or background. exponent = high-q slope.",
    "mass_fractal":        "Aggregates/gels. fractal_dim from mid-q slope, cutoff length.",
    "gaussian_peak":       "Single correlation peak (position, width) — quick d-spacing.",
}

# Parameters that are NOT stored in the manifest and must be read from the
# .dat footer instead (key map: tool-param -> footer key).
_FOOTER_PARAMS = {"ctemp": "CTEMP", "temp": "TEMP"}


def _read_dat_footer_values(path: str, footer_keys: set) -> dict:
    """Read selected scalar values from a .dat footer (comment lines only)."""
    out: dict[str, float] = {}
    if not footer_keys:
        return out
    want = {k.upper() for k in footer_keys}
    try:
        with open(path) as fh:
            for line in fh:
                if not line.startswith("#"):
                    continue
                s = line[1:].strip()
                if ":" not in s:
                    continue
                k, v = s.split(":", 1)
                if k.strip().upper() in want:
                    try:
                        out[k.strip().upper()] = float(v.strip())
                    except ValueError:
                        pass
    except Exception:
        pass
    return out


def _frames_for_averaged(reduced: list[dict], keyword: str, detector: str,
                         params: list[str] | None = None) -> list:
    """
    Return the reduced frames that make up one averaged sample, ordered by
    acquisition timestamp. Each item is (epoch_seconds, metadata_dict).

    Matching: same detector + the sample's x-position token + all base name
    tokens, with the dark/non-dark set chosen to match the averaged keyword.

    Any requested `params` that live only in the .dat footer (CTEMP/TEMP) are
    read from the file and merged into each frame's metadata dict.
    """
    import os
    import re
    from datetime import datetime

    params = params or []
    # Always read the beamline 'Timer' clock so it can serve as the x-axis.
    footer_keys = {_FOOTER_PARAMS[p] for p in params if p in _FOOTER_PARAMS} | {"TIMER"}

    m    = re.search(r"x-?[\d.]+", keyword)
    xtok = m.group(0) if m else None
    base = keyword[: m.start()] if m else keyword
    toks = [t for t in base.split("_") if t]
    want_dark = "dark" in keyword.lower()

    def _ts(s: str) -> float:
        try:
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0

    rows = []
    for v in reduced:
        if (v.get("detector") or "").lower() != detector.lower():
            continue
        name = os.path.basename(v.get("path", ""))
        if xtok and xtok not in name:
            continue
        if not all(t in name for t in toks):
            continue
        if ("dark" in name.lower()) != want_dark:
            continue
        md = dict(v.get("metadata") or {})
        if md.get("i0") is None:
            continue
        timer = None
        if footer_keys:
            vals = _read_dat_footer_values(v.get("path", ""), footer_keys)
            for p, fkey in _FOOTER_PARAMS.items():
                if fkey in vals:
                    md[p] = vals[fkey]
            timer = vals.get("TIMER")
        # x-axis = beamline 'Timer' clock (from footer, else manifest metadata);
        # fall back to the provenance acquisition timestamp if Timer is absent.
        if timer is None:
            try:
                tm = md.get("Timer", md.get("timer"))
                timer = float(tm) if tm is not None else None
            except (TypeError, ValueError):
                timer = None
        xval = timer if timer is not None else _ts((v.get("provenance") or {}).get("timestamp", ""))
        rows.append((xval, md))
    rows.sort(key=lambda r: r[0])
    return rows


def _truncate(text: str, limit: int) -> str:
    """Bound a tool-result string so a single huge result can't blow up cost."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit:,} chars to save tokens]"


_TOOL_LABELS = {
    "query_manifest":  "Checking the experiment manifest",
    "overlay_curves":  "Overlaying curves",
    "plot_metadata":   "Plotting acquisition metadata",
    "fit_model":       "Running the model fit",
    "compute_pr":      "Computing p(r) / Dmax",
    "assess_quality":  "Running quality checks",
    "run_analysis":    "Running analysis",
    "list_saxs_models":"Looking up SAXS models",
    "generate_plot":   "Generating a plot",
    "web_search":      "Searching the literature",
    "manage_knowledge":"Updating the knowledge base",
    "group_sops":      "Updating group SOPs",
    "set_preferences": "Saving your preferences",
    "run_python":      "Running code",
    "export":          "Writing the export",
    "add_note":        "Adding a note",
    "flag_quality":    "Flagging quality",
    "ingest_pdf":      "Indexing the PDF",
}


def _tool_label(name: str, inp: dict) -> str:
    """A short human-friendly progress label for a tool call."""
    base = _TOOL_LABELS.get(name, name.replace("_", " "))
    kw = ""
    if isinstance(inp, dict):
        kw = (inp.get("keyword") or (", ".join(inp.get("keywords", []))
              if isinstance(inp.get("keywords"), list) else "")
              or inp.get("query") or inp.get("model_name") or "")
    return f"{base} — {kw}" if kw else base


def _audience_directive(prefs: dict) -> str:
    """Build a verbosity/audience behavior directive from saved preferences."""
    aud = str(prefs.get("audience", "")).lower()
    verb = str(prefs.get("verbosity", "")).lower()
    bits = []
    if aud == "expert":
        bits.append("Audience is EXPERT: be terse and technical, skip basics, "
                    "use standard SAXS/WAXS terminology without explaining it.")
    elif aud == "student":
        bits.append("Audience is a STUDENT/newcomer: explain jargon and each "
                    "step, give brief reasoning, and be encouraging.")
    elif aud == "mixed":
        bits.append("Audience is MIXED: lead with the answer, then add a short "
                    "plain-language explanation for newcomers.")
    if verb == "concise":
        bits.append("Keep answers concise.")
    elif verb == "detailed":
        bits.append("Provide thorough, detailed answers.")
    return ("## Audience & verbosity\n" + " ".join(bits)) if bits else ""


def _assistant_outputs_dir(project_root) -> "Path":
    """The ONE writable location for the assistant: <project>/assistant_outputs/.
    Created on demand. Experiment data lives elsewhere and is never written."""
    root = Path(project_root)
    if root.is_file():
        root = root.parent
    d = root / "assistant_outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(name: str, default: str, suffix: str) -> str:
    """Sanitize a user/model-supplied filename to a basename with the suffix."""
    import re
    base = Path(str(name or default)).name        # strip any path components
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or default
    if not base.lower().endswith(suffix.lower()):
        base += suffix
    return base


def _save_png(b64: str, project_root, save_as: str) -> str:
    """Write a base64 PNG into assistant_outputs/. Returns the saved filename."""
    import base64 as _b64
    from datetime import datetime
    out = _assistant_outputs_dir(project_root)
    name = _safe_filename(save_as, f"figure_{datetime.now():%Y%m%d-%H%M%S}", ".png")
    (out / name).write_bytes(_b64.b64decode(b64))
    return name


def _trim_history(history: list[dict]) -> list[dict]:
    """
    Keep only the most recent `_MAX_HISTORY_USER_TURNS` user prompts and their
    following assistant/tool turns. Trimming on user-text boundaries guarantees
    we never split a tool_use from its tool_result (which would 400), while
    bounding the input tokens re-sent on every turn.
    """
    if not history:
        return []
    # Indices of "real" user prompts (string content == a typed message).
    starts = [
        i for i, m in enumerate(history)
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    if len(starts) <= _MAX_HISTORY_USER_TURNS:
        return list(history)
    cut = starts[-_MAX_HISTORY_USER_TURNS]
    return list(history[cut:])


# Max individual entries returned by a manifest list query. A project can hold
# thousands of files whose full entries (metadata + provenance + input_files)
# easily exceed the model's context window, so list queries are compacted and
# capped — the model gets aggregate counts plus a small representative sample.
# Kept low to minimise token cost; use keyword/detector filters for specifics.
_MAX_LIST_ENTRIES = 15


def _compact_file_entry(key: str, val: dict) -> dict:
    """Light, token-cheap view of a file entry (no provenance/input_files)."""
    m = val.get("metadata", {}) or {}
    out: dict[str, Any] = {
        "name":     Path(key).name,
        "stage":    val.get("stage"),
        "detector": val.get("detector"),
    }
    for f in ("transmission", "i0", "bstop", "thickness_m"):
        if f in m:
            out[f] = m[f]
    if val.get("status") and val.get("status") != "ok":
        out["status"] = val["status"]
    qf = val.get("quality_flag") or val.get("flag")
    if qf:
        out["quality_flag"] = qf
    return out


def _files_response(filtered: dict) -> dict:
    """Aggregate + capped sample for a (possibly huge) set of file entries."""
    items = list(filtered.items())
    by_stage: dict[str, int] = {}
    by_det:   dict[str, int] = {}
    flags:    dict[str, int] = {}
    for _k, v in items:
        by_stage[v.get("stage", "?")] = by_stage.get(v.get("stage", "?"), 0) + 1
        d = str(v.get("detector", "?")).upper()
        by_det[d] = by_det.get(d, 0) + 1
        qf = v.get("quality_flag") or v.get("flag")
        if qf:
            flags[qf] = flags.get(qf, 0) + 1
    resp: dict[str, Any] = {
        "matched":     len(items),
        "by_stage":    by_stage,
        "by_detector": by_det,
        "quality_flags": flags or "none",
        "files":       [_compact_file_entry(k, v) for k, v in items[:_MAX_LIST_ENTRIES]],
    }
    if len(items) > _MAX_LIST_ENTRIES:
        resp["note"] = (
            f"Showing {_MAX_LIST_ENTRIES} of {len(items)} matches. Use a `keyword` "
            "or `detector` filter to narrow, or `summary` for totals only."
        )
    return resp


def _capped_dict(entries: dict) -> dict:
    """Cap a dict of records (background/analyses), dropping heavy provenance."""
    items = list(entries.items())
    out: dict[str, Any] = {}
    for k, v in items[:_MAX_LIST_ENTRIES]:
        if isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items()
                      if kk not in ("provenance", "input_files")}
        else:
            out[k] = v
    result: dict[str, Any] = {"matched": len(items), "entries": out}
    if len(items) > _MAX_LIST_ENTRIES:
        result["note"] = f"Showing {_MAX_LIST_ENTRIES} of {len(items)} entries."
    return result
