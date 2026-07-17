# Audits

Consolidated audit reports for the SWAXS Platform.

| File | Scope | Date |
|---|---|---|
| `PIPELINE_AUDIT_2026-06-18.md` | Full pipeline correctness & consistency sweep (all apps + src + manifest + hub + security); fixed an assistant tool-loop crash and a scan-averaging low-bias bug | 2026-06-18 |
| `SUBTRACTION_APP_AUDIT.md` | Background-subtraction app — functionality, correctness (with numeric validation), and UX | 2026-06-18 |
| `REDUCTION_CORRECTION_AUDIT.md` | Reduction/correction pipeline — physics correctness & normalization math (with fixes applied) | 2026-06-15 |
| `DESIGN_AUDIT.md` | Platform-wide typography & color accessibility (WCAG 2.2 AA) | 2026-06-15 |
| `REACTOR_CONTROLS_AUDIT.md` | Reactor app control panel — what each button does, valid states, and overlaps/unreachable endpoints | 2026-07 |
| `REACTOR_APP_AUDIT.md` | Flow Synthesis app end-to-end — correctness, safety, closed-loop role, config surface, and test coverage | 2026-07 |
| `BEAMLINE_SAFETY_AUDIT.md` | Confirms the platform only sends commands and reads from SPEC/bServer, never reconfiguring the beamline | 2026-07 |
| `PRE_BEAMTIME_READINESS.md` | Pre-beamtime checklist — bench-validated beamline tests and dev-decision coverage (118 tests passing) | 2026-07 |

Each report lists findings with severity and recommendations. Audits are static
code review + unit-level/numeric validation; the live Flask apps are not launched
as part of auditing (per the project's "don't test unless asked" rule).
