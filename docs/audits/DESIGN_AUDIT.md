# SWAXS Platform — UI Readability & Accessibility Audit

**Date:** 2026-06-15
**Scope:** All six app templates — hub, reduction, viewer, background, analysis, assistant.
**Standard:** WCAG 2.2 Level AA (contrast SC 1.4.3 / 1.4.11, focus SC 2.4.7).
**Goal:** one uniform typography system + accessible color, while keeping Stanford
Cardinal and SLAC red branding, optimized for long data-analysis sessions.

---

## 1. Methodology

1. Researched current guidance: WCAG 2.2 contrast thresholds (4.5:1 text, 3:1
   large text & UI components), and data-dense UI typography (consistent modular
   scale, ~1.5 body / ~1.4 table line-height, high-x-height sans-serif).
2. Inventoried every `:root` token, `font-size`, `font-weight`, and color usage
   across the six templates.
3. Computed exact contrast ratios for every text/UI pair (script: relative
   luminance per WCAG formula).
4. Defined one shared token system and applied it; fixed each pair that failed.
5. Re-validated all pairs (Section 5) and built a rendered reference
   (a `design_system_preview.html` preview, since removed; the token spec lives
   in `docs/DESIGN_SYSTEM.md`).

Sources: [W3C WCAG 2.2](https://www.w3.org/TR/WCAG22/) ·
[WebAIM Contrast](https://webaim.org/resources/contrastchecker/) ·
[USWDS Typography](https://designsystem.digital.gov/components/typography/) ·
[Optimal line length — UXPin](https://www.uxpin.com/studio/blog/optimal-line-length-for-readability/).

---

## 2. What was broken (before)

| Issue | Where | Before | Problem |
|---|---|---|---|
| Inconsistent base font size | all 6 apps | 13.5 / 14 / 15 / 16px | No uniform reading size |
| Inconsistent radius | all 6 | 7 / 10 / 12px | Visual inconsistency |
| No shared type scale / weights / spacing | all 6 | ad-hoc px values | No hierarchy system |
| `--faint #9ca3af` used as text | assistant | 2.5:1 | **Fails** AA |
| `--muted #6b7280` | all light apps | 4.5–4.8:1 | Passes but borderline on tints |
| Success as text `#16a34a` | viewer, assistant | 3.3:1 | **Fails** AA for text |
| Warning as text `#d97706` | assistant | 3.2:1 | **Fails** AA for text |
| Hardcoded warning `#f59e0b` | viewer | 2.1:1 | **Fails** badly |
| Accent small text on dark `#B83A4B` | hub | 3.4:1 | **Fails** AA for small text |
| Form-control borders `#e5e7eb` | all light apps | 1.2:1 | **Fails** UI 3:1 (1.4.11) |
| Weak/absent keyboard focus ring | background, analysis, assistant, hub | — | Fails 2.4.7 |

---

## 3. What changed (after)

### 3.1 Typography — uniform across all apps
- **Base size standardized to 16px** with `line-height: 1.5`.
- Added a shared scale (`--fs-2xs`…`--fs-2xl`), weights (`--fw-*`),
  line-heights (`--lh-*`), label letter-spacing (`--ls-label`), and a spacing
  scale (`--sp-1`…`--sp-6`) to **every** app's `:root`.
- Unified font family to Inter-first (`--font`); reduction & viewer previously used
  `-apple-system` and now use `var(--font)`.
- `--radius` unified to **8px** everywhere.

### 3.2 Color — accessible, brand-aligned
- Restructured the gray hierarchy so **every text token passes AA**:
  `--muted` `#6b7280 → #4b5563` (7.6:1); `--faint` now `#6b7280` (4.8:1);
  `#9ca3af` retained only as `--disabled` (decorative/disabled).
- Added accessible semantic **text** variants: `--ok-text #15803d`,
  `--warn-text #b45309`, `--err-text #b91c1c`, `--info-text #1d4ed8`
  (bright `--ok/--warn/--err` kept for fills/dots/icons).
- Added `--border-strong` for form-control outlines (`#828c99` light = 3.4:1,
  `#6b7280` dark = 3.6:1).
- Hub: added `--accent-text #ff7b8a` (7.6:1) for small accent text/links; kept
  `#B83A4B` for brand/large fills only.
- Kept **Stanford Cardinal `#8C1515`** (sidebars) and **SLAC red `#B83A4B`**
  (accent) — both verified accessible in their roles.

### 3.3 Components
- **Forms:** input/select/textarea borders → `--border-strong`; consistent focus
  state (accent border + 3px ring) added to background, analysis, assistant
  (reduction/viewer already had it).
- **Focus ring:** global `:focus-visible` outline added to all six apps (2.4.7).
- **Badges/status text:** `.badge.ok/.warn/.err` and tool-call labels switched to
  `-text` variants (assistant); viewer `.mc-cv-*` and `.save-result` switched to
  `-text` variants; viewer hardcoded `#f59e0b` → `--warn-text`.
- **Hub small accent text:** `.pchange` and `.ev-project` chip → `--accent-text`.

---

## 4. Before / after by file

| File | `:root` reworked | base font | semantic-text fixes | borders | focus ring |
|---|---|---|---|---|---|
| `hub/templates/index.html` | ✅ +tokens, +`--accent-text` | 15→16 | n/a (dark palette already AA) | +`--border-strong` token | ✅ added |
| `reduction/templates/index.html` | ✅ full token set | 13.5→16 | — | inputs → `--border-strong` | ✅ added (had ring) |
| `viewer/templates/index.html` | ✅ full token set | 16 (kept) | `.save-result`, `.mc-cv-ok/warn/bad`, `#f59e0b` | inputs → `--border-strong` | ✅ added (had ring) |
| `background/templates/index.html` | ✅ +tokens | 15→16 | — | inputs → `--border-strong` + ring | ✅ added |
| `analysis/templates/index.html` | ✅ +tokens | 15→16 | — | `.field`+`.param-table` inputs → `--border-strong` + ring | ✅ added |
| `assistant/templates/index.html` | ✅ +tokens, `--faint` fixed | 14→16 | `.badge.ok/.warn/.err`, `.tc-label` | `textarea#msg` → `--border-strong` + ring | ✅ added |

---

## 5. Validation — measured contrast (after)

All text and UI pairs meet or exceed AA. (`--disabled #9ca3af` is intentionally
below threshold and used only for disabled/decorative elements, which are exempt.)

**Light theme (vs white / page bg)**

| Pair | Ratio | Result |
|---|---|---|
| Primary `#1f2937` | 14.7:1 | ✅ AA |
| Secondary `#4b5563` | 7.6:1 | ✅ AA |
| Tertiary `#6b7280` | 4.8:1 | ✅ AA |
| Accent `#B83A4B` (text) | 5.6:1 | ✅ AA |
| White on Cardinal `#8C1515` | 9.4:1 | ✅ AA |
| `--ok-text #15803d` | 5.0:1 | ✅ AA |
| `--warn-text #b45309` | 5.0:1 | ✅ AA |
| `--err-text #b91c1c` | 6.5:1 | ✅ AA |
| `--info-text #1d4ed8` | 6.7:1 | ✅ AA |
| `--border-strong #828c99` (UI) | 3.4:1 | ✅ AA (3:1) |

**Dark theme — hub (vs `#0d1117`)**

| Pair | Ratio | Result |
|---|---|---|
| Primary `#e6edf3` | 16:1 | ✅ AA |
| Secondary `#8b949e` | 6.2:1 | ✅ AA |
| `--accent-text #ff7b8a` | 7.6:1 | ✅ AA |
| Status ok/warn/err/info | 5.7–7.5:1 | ✅ AA |
| Accent `#B83A4B` (large/brand) | 3.4:1 | ✅ AA (large/UI) |

---

## 6. Seeing the proof

- The design tokens — type scale, color swatches with ratios, buttons, forms
  (default + focus), tables, badges, hint cards, sidebar, tabs, dialog, and plot
  panel, in both themes — are specified in `docs/DESIGN_SYSTEM.md` and applied via
  each app's `templates/index.html`. (A standalone `design_system_preview.html`
  preview existed during the audit but has since been removed.)
- **Live in-app screenshots:** I couldn't auto-capture these here — the sandbox has no
  GPU/browser libraries and no Chrome extension was connected. To capture them, start the
  platform (`./start_platform.sh`, open http://localhost:5000) and I can screenshot each
  running app if you connect the Chrome extension, or you can grab them directly.

---

## 7. Notes & limitations

- Changes are **token- and component-level**; no app logic or layout structure was
  altered. Reduction is the densest panel and jumped most in base size (13.5→16px);
  worth a visual glance once running.
- A few hundred per-element `font-size` literals remain in the templates; they now sit
  *within* the unified base/scale and were left in place to avoid layout risk. New
  components should use the scale tokens (see `DESIGN_SYSTEM.md`).
- Plot rendering (Plotly/matplotlib) keeps light backgrounds by design, so the data apps
  remain light for figure contrast and export fidelity.
