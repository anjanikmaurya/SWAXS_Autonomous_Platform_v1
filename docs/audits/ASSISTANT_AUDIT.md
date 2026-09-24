# Assistant (Guinier) app — audit (September 2026)

Scope: `assistant/app.py` (11 routes) and all of `src/ai/` — `assistant.py`
(19 tools, the chat loop, secret loading), `code_exec.py` (run_python sandbox),
`knowledge.py`, `memory.py`, `plots.py`, `hints.py`, `loop_advice.py`.

Lenses: secret/credential handling, the code-execution and web surfaces,
prompt-injection exposure, error containment, multi-user isolation, unbounded
growth, and every tool traced to its dispatch.

> **STATUS.** One functional bug found and FIXED this session — **A6, P(r)
> auto-Dmax returned nonsense** (the "enable Pr analysis" request; it ran but
> produced garbage). Three hardening findings (A1–A3) are recorded for review,
> not yet changed. The rest of the app is soundly built — see
> [§ Checked and sound](#sound).

Severity: **HIGH** = data loss or a real security hole on the beamline LAN ·
**MED** = a plausible local exploit or a leak of internal detail · **LOW** =
defence-in-depth / cost.

---

## Summary

| # | Severity | Area | One line |
|---|---|---|---|
| [A6](#a6) | HIGH (fixed) | correctness | P(r) auto-Dmax used π/q_min → Dmax and Rg meaningless; now scanned |
| [A1](#a1) | MED | security | PDF ingest writes the raw client filename — path traversal |
| [A2](#a2) | MED | stability | PDF ingest has no size cap — a large upload can fill disk / OOM the indexer |
| [A3](#a3) | MED | leak | Nine routes return `str(exc)` to the client (chat, ingest, memory, knowledge…) |
| [A4](#a4) | LOW | — | `run_python` is a guard, not a jail (documented, human-gated) — accepted |

---

## A6 — P(r) auto-Dmax returned meaningless Dmax and Rg (FIXED) {#a6}

**Proven.** This is what "P(r) is broken in the assistant" was. `compute_pr`
runs without error but returned garbage: a 4 nm sphere came back as **Dmax ≈
157 nm, Rg ≈ 5.7** (truth: Dmax = 8 nm, Rg = 3.1), reduced χ² ≈ 10⁴.

Cause — `src/analysis/core.py::pair_distance_ift`, the auto-Dmax line:

```python
if dmax is None or dmax <= 0:
    dmax = float(np.pi / q.min())     # ← the largest RESOLVABLE size, not Dmax
```

π/q_min is the coarse resolution ceiling (157 nm at q_min = 0.02), not the
particle's maximum dimension. The IFT then spread p(r) over 0–157 nm and every
derived quantity (Dmax, Rg, I0) was nonsense. It was masked because the Data
Analysis app lets you *type* a Dmax (manual path works), and the only test on
the auto path asserted `Dmax > 0` — which the bug satisfied.

> **Fixed.** Auto-Dmax now SCANS: a coarse grid from ~2π/q_max up to the old
> π/q_min ceiling, then a fine grid around the χ²-minimum region, picking the
> smallest Dmax whose fit is within a tight band of the best reduced χ² and
> without heavy pre-clip negativity — the standard GNOM-style criterion done
> numerically. A supplied Dmax is still honoured exactly (single solve,
> unchanged). This fixes the Data Analysis app's auto path too, since both call
> the same function.
>
> Verified against solid spheres across sizes and realistic weightings
> (scaled + background + Poisson-like σ, the regime real subtracted data lives
> in): Rg is now recovered to a few percent and Dmax to a sane multiple of 2R,
> versus 40× off before. Held by `tests/test_pr_ift.py::
> test_pr_auto_dmax_is_accurate_not_the_qmin_ceiling`, which fails against the
> old code.
>
> Honest caveat: Dmax is the least-determined quantity in any IFT, and on a
> *noise-free unit-scale* curve with σ ∝ I (an artificial input) the scan can
> still over- or under-shoot Dmax while Rg stays right. For real data it is
> good; for a hard number, supply Dmax or use ATSAS GNOM.

---

## A1 — PDF ingest writes the raw client filename (path traversal) {#a1}

`assistant/app.py:653`

```python
save_dir  = _ROOT / "ai_knowledge" / collection      # collection is allow-listed ✓
save_path = save_dir / f.filename                     # f.filename is NOT sanitised
f.save(str(save_path))
```

`collection` is validated against `{user_papers, literature}` (good), but
`f.filename` is the client-supplied upload name used verbatim. Werkzeug does
**not** sanitise it; that is exactly what `secure_filename` is for. A filename
like `../../../../tmp/x.pdf` (it only has to end `.pdf`) resolves outside
`ai_knowledge/`, so a crafted upload can write a `.pdf` anywhere the process can
write. On a single-user beamline box the blast radius is small, but it is a
genuine write-primitive on an HTTP route.

> Fix: `from werkzeug.utils import secure_filename; name =
> secure_filename(f.filename) or "upload.pdf"`, then `save_dir / name`, and
> reject a name that still contains a separator.

---

## A2 — No size cap on the PDF upload {#a2}

Same route: `f.save(...)` then `kb.ingest_pdf(...)` with no bound on the upload
size or page count. A multi-GB upload fills the disk (where the whole
experiment's data also lives) and the chunk/embed step can blow memory. Flask's
`MAX_CONTENT_LENGTH` is not set anywhere in the app.

> Fix: set `app.config["MAX_CONTENT_LENGTH"]` (say 50 MB) and/or a page cap in
> `ingest_pdf`, and return 413 rather than crashing.

---

## A3 — Raw exception text returned to the client {#a3}

`assistant/app.py:472` (`/api/chat`), `:675` (`/api/ingest/pdf`), and **seven
more** (memory, knowledge, project routes) — nine occurrences of:

```python
except Exception as exc:
    logger.exception(...)
    return jsonify({"error": str(exc)}), 500
```

`str(exc)` goes straight to the browser. For most errors that is only untidy,
but a misconfigured gateway can raise with the base URL or auth context in the
message, and file errors echo absolute server paths. The full detail is already
in the log via `logger.exception`; the client only needs a generic message.
Because the pattern is repeated in nine routes, a small shared
`_fail(exc)` helper is the tidy fix.

> Fix: return a fixed `"internal error — see the app log"` (plus an error id)
> and keep `str(exc)` in the log only.

---

## A4 — `run_python` is a guard, not a jail (accepted) {#a4}

`src/ai/code_exec.py` is a well-built defence: an AST allowlist of scientific
modules, a denylist that explicitly covers the secret-exfiltration vectors a
prompt-injected KB/PDF would reach for (`getenv`, `home`, `expanduser`,
`read_text`, `open`, dunder tricks, `subprocess`/socket attrs), execution in an
isolated `python -I` subprocess with CPU/memory rlimits and a wall-clock
timeout, writes confined to `assistant_outputs/`, and **mandatory human
confirmation before any snippet runs**. Its own docstring states it is "a guard,
not a perfect jail … the human confirmation is the real control." That is the
right posture and is recorded here as an accepted, understood residual risk, not
a defect — the human-in-the-loop gate is what makes it safe.

---

## Checked and sound {#sound}

Examined closely and correct:

* **Secrets never leave the process.** The gateway token is loaded from
  `~/.claude/settings.json` into the environment only if not already set, the
  documentation placeholder is ignored, and `/api/health` reports credentials
  as **booleans** (`api_key_set`, `credentials: gateway-token|api-key|none`) —
  never the value. The token is not logged.
* **All 19 tools are wired.** Every tool in `_TOOLS` has a dispatch branch and
  vice-versa — no orphaned tool, no dead branch — and the whole dispatch is
  wrapped so a tool that raises returns a contained tool-error and the chat
  turn survives.
* **web_search cannot SSRF.** It hits a single hard-coded endpoint
  (`api.crossref.org`), not a caller-supplied URL.
* **Multi-user isolation.** Memory and analysis modality are per-`user_id`
  dicts under locks (`_mems`/`_mem_lock`, `_modality`/`_modality_lock`),
  fixing the earlier cross-user thrash; the assistant is a threaded singleton.
* **Bounded growth.** Sessions carry a 2 h TTL and are actively swept
  (`_expire_sessions`), history is capped per session (60) and again trimmed to
  the last 6 user turns before each model call, so input-token cost and memory
  are both bounded.
* **The tool loop is bounded** (`_MAX_TOOL_ROUNDS = 5`) — no runaway recursion.

---

*Method: static read of all ~5,200 lines in scope, plus executable probes of
the P(r) path against analytic solid spheres of several sizes and weightings.
A6 is proven by that output; A1–A3 are read from the routes. No secret value
was printed at any point.*
