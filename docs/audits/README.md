# Audits

Three files. Everything else that used to live here was a point-in-time report
whose findings are either fixed or carried into `OPEN_DEFECTS.md`; ten such files
were consolidated and remain in git history
(`git log --diff-filter=D -- docs/audits/`).

| File | What it is | Read it when |
|---|---|---|
| [`OPEN_DEFECTS.md`](OPEN_DEFECTS.md) | **The single register of known open defects**, grouped by owner, every entry with a severity and a `file:line`. Also keeps the three rationale blocks recorded nowhere else: the temperature-interlock trade-off, why mock mode has no time compression, and the frames-vs-batch averaging deadlock. | Deciding what to fix next, or wondering whether a symptom is already known. |
| [`PRE_BEAMTIME_READINESS.md`](PRE_BEAMTIME_READINESS.md) | Operator go/no-go checklist: what to verify before arming an autonomous run, and the data-path checks where a run silently goes nowhere. | Before every beamtime, and before pressing **▶ Run autonomously**. |
| [`BEAMLINE_SAFETY_AUDIT.md`](BEAMLINE_SAFETY_AUDIT.md) | The complete whitelist of SPEC commands the platform issues, an explicit list of what it does **not** do, and the session state it touches. | Handing the platform to beamline staff for review. |

Numbering in `OPEN_DEFECTS.md` is historical: gaps mean fixed, and an old
reference to `N7`, `O4`, `C5` or `D3` still resolves there.
