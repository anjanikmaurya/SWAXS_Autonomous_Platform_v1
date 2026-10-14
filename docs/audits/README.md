# Audits

One register, one operator checklist, and the completed audit write-ups. Open
items from every audit are carried into [`OPEN_DEFECTS.md`](OPEN_DEFECTS.md), so
that is the only file to read when deciding what to fix next. Older
point-in-time reports were consolidated into it and remain in git history
(`git log --diff-filter=D -- docs/audits/`).

## Living documents

| File | What it is | Read it when |
|---|---|---|
| [`OPEN_DEFECTS.md`](OPEN_DEFECTS.md) | **The single register of known open defects**, grouped by owner, each with a severity and status. Also keeps three rationale blocks recorded nowhere else: the temperature-interlock trade-off, why mock mode has no time compression, and the frames-vs-batch averaging deadlock. | Deciding what to fix next, or checking whether a symptom is already known. |
| [`PRE_BEAMTIME_READINESS.md`](PRE_BEAMTIME_READINESS.md) | Operator go/no-go checklist: what to verify before arming an autonomous run, and the data-path checks where a run silently goes nowhere. | Before every beamtime, and before pressing **Run autonomously**. |

## Completed audits

Kept as written; each starts with a status block. Do not update the body, file
new findings in the register instead.

| File | Scope | When | Result |
|---|---|---|---|
| [`PLATFORM_QUICK_AUDIT_2026_10.md`](PLATFORM_QUICK_AUDIT_2026_10.md) | every app and the shared platform code (Q1 to Q37) | 2026-10-14 | all 37 fixed the same day; one residual |
| [`REACTOR_AUDIT.md`](REACTOR_AUDIT.md) | reactor app, `src/reactor/`, `src/beamline/` (R1 to R28) | Sept 2026 | 25 of 27 fixed, R4 accepted, **R12 open** |
| [`AUTO_WATCH_AUDIT.md`](AUTO_WATCH_AUDIT.md) | watchdog app, `src/watchdog/` (W1 to W25) | 2026-09-11 | 14 fixed, **11 open** |
| [`ASSISTANT_AUDIT.md`](ASSISTANT_AUDIT.md) | assistant app, `src/ai/` (A1 to A10) | Sept 2026, two passes | 6 fixed, A4 accepted, A10 by design, **A9 open** |
| [`AUTOFIT_AUDIT.md`](AUTOFIT_AUDIT.md) | analyzer app and its `src/` deps (A1 to A9) | Oct 2026 | all 9 fixed; **L1, L2 open** (LOW) |
| [`BEAMLINE_SAFETY_AUDIT.md`](BEAMLINE_SAFETY_AUDIT.md) | every SPEC/bServer command the platform issues | 2026 (date not recorded) | 1 gap found and fixed, nothing open |

Numbering in `OPEN_DEFECTS.md` is historical: gaps mean fixed, and an old
reference to `N7`, `O4`, `C5` or `D3` still resolves there. The assistant and
analyzer audits both use `A` numbers; the register names the audit with each one.
