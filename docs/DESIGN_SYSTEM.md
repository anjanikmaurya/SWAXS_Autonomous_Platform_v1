# SWAXS Platform — Design System

The typography, spacing, and color tokens the platform's apps share.

**Colours and the light/dark theme have ONE source of truth (October 2026):**
`assets/icons/swaxs-tokens.css`, shipped to every app's `static/` by
`python tools/build_icon_sprite.py`. Its palette blocks
(`html:root:not([data-theme="dark"])` and `html:root[data-theme="dark"]`) are
one specificity step above an app's own `:root`, so they win over any leftover
app palette. To change a colour, change it there and rebuild — never in an app.
The same file carries the page chrome (top bar, theme toggle, tab rows). Every
page includes `_theme_boot.html` in `<head>` (saved choice under the shared key
`swaxs-theme`, else the OS), so apps start with no light/dark flash. Each app runs
on its own port and the browser keeps saved settings PER PORT, so links between
apps (hub Open buttons, every ← Hub) carry `?theme=…`, which the boot script
adopts and saves; two open tabs of the SAME app also follow each other live.

**Top row, same order in every app:** logo + name │ status │ project folder │
app extras (one fixed slot) │ theme switch │ Port · ← Hub. Apps without their own
status or project display carry `#barStatus` / `#barProject`, filled by the
shared helper from the app's `/api/health` and `/api/project`. The **Operator**
is entered once, in the hub only; `src/operator_id.py` passes it to every app
(project manifest + `SWAXS_USER_ID`). Plotly charts take colours
from `swaxsPlotTheme()` and re-colour themselves on a theme change
(`swaxsRethemePlots`, in `_icon_helpers.html`). Component colours use tokens,
not hex: text on a tint pairs `--x-text` with `--x-lt` (ok/warn/err/info/violet);
text in the brand colour uses `--accent-text` (`--accent` is a fill — white
labels on it). Contrast floors (WCAG AA) are enforced by
`tests/test_ui_theme_uniform.py`.

Typography scale, spacing tokens, radius and focus rules are still per app;
section 0 says how far they have diverged. When this document and the code
disagree, the code wins: fix the code or fix this file, but do not assume
conformance.

---

## The shared layout spec (reduction · average · background · watchdog)

These apps carry one byte-identical panel spec. The first three are the same
kind of page — a column of panels, each a
heading over a form — and now carry one byte-identical CSS block, checked by
`tests/test_shared_layout_spec.py`:

```css
.card, .section          { padding:16px 18px; … }
.card-hd, .section-title { font-size:15px; uppercase; accent; … }
.two-col, .row           { grid, auto-fit minmax(240px, 1fr) }
```

Three things it fixed:

- **background rendered every token ~11% larger** than the other apps,
  because it set its own `html` font size. Since October 2026 the root size is
  set once for every app in the shared stylesheet (18px), so this cannot recur.
- **`.row` had no CSS rule.** background's markup used it in four places to put
  two fields side by side; with no rule they stacked, silently, costing about
  160px of scrolling.
- **Panels are tighter** — 16/18px padding rather than reduction's original
  20/22px, and a 12/8px heading rule rather than 16/10px. About 14px back per
  panel, so roughly 140–250px per screen depending on the app. No text got
  smaller to achieve it.

watchdog's template still sets 17px on `html, body` for a wall display; the
shared 18px root overrides it, so it has no effect.

**Guideline for any new app:** include `swaxs-tokens.css`, do not set a root
font size, use the `--fs-*` scale as defined in section 1, and copy this layout
block verbatim.

---

### Forms

Every app now uses one shared form rule: **label beside its box** (see "Forms"
under page chrome below). The earlier label-above `.field` spec shared by
average and background is superseded. `--fs-form: .9375rem` remains the size
for labels, inputs and selects in average, background and reactor. Focus rules
must restate `background:var(--surface2)`: average once had
`input:focus{background:#fff}` with no dark override, which put near-white
text on a white box in dark mode.

---

## Icons, fonts and page chrome (October 2026)

**Icons come from the platform sprite only. No emoji.** Emoji render differently
on every OS, ignore the light/dark theme, and do not match the drawn icon set.

* The set lives in `assets/icons/swaxs-icons-svg/` — 24×24, `stroke="currentColor"`,
  2.0 main stroke, 1.5 detail stroke, round caps/joins, 1.8 corner radii, a filled
  accent dot where useful. 11 app marks + 62 UI icons (`swaxs-ui-*`).
* `python tools/build_icon_sprite.py` builds the sprite and ships, to every app:
  `templates/_icon_sprite.svg` (inlined after `<body>`), `templates/_icon_helpers.html`
  (included right after it) and `static/swaxs-tokens.css` (which carries `.ico`).
* In markup: `<svg class="ico" aria-hidden="true"><use href="#swaxs-ui-NAME"/></svg>`.
  `.ico` sizes to the text (1em) and takes its colour from `color`.
* In script: `icoSvg('NAME')` returns that markup; `setIco(el, 'NAME', text)` sets an
  icon plus text, with the text in a TEXT node so a server string can never be
  parsed as HTML. Use these instead of writing glyphs into `textContent`.
* Theme toggle: every themed app shows `swaxs-ui-sun` in dark mode (click to go
  light) and `swaxs-ui-moon` in light mode.
* Plain text marks (✓ ✗ ⚠, arrows, ▲▼ sort, ▾ disclosure) are typography and fine.
* Guarded by `tests/test_ui_icon_policy.py` (no emoji, sprite + helpers included),
  `tests/test_app_marks.py` / `tests/test_hub_icon_sprite.py` (every static AND
  runtime icon name resolves to a symbol) and `tests/test_page_scripts_parse.py`
  (every page script parses).

**Fonts.** Every app: `--font: 'Inter','Segoe UI',system-ui,-apple-system,sans-serif`
and `--mono: 'JetBrains Mono','Fira Code','Cascadia Code',ui-monospace,Menlo,Consolas,monospace`,
and nothing names a font family directly — always `var(--font)` / `var(--mono)`.

**Page chrome.** The header title is the app's name exactly as in `apps.yml` (two-tone:
first word plain, the rest in the accent span). The right edge of a top bar is the
theme toggle followed by `Port NNNN · ← Hub`. Every app links back to the hub.

**One top bar everywhere; a left column for multi-section apps (operator's
choice, October 2026).** Every app has the same full-width `header.topbar`: logo
and name, live status, project path, theme switch, `Port NNNN · ← Hub`. The
multi-section apps (reduction, calibration, watchdog, average, analysis,
background, reactor) add a left column UNDER it holding only their sections (`#sidebar` /
`.nav-btn`), with each section's task tabs (`.toptabbar`) inside the page; their
`<body class="swaxs-shell">` stacks bar over column + content. In the reactor the
column lists the synthesis routes, with the pump controls and emergency stop at
its foot (`#sidebar-controls`), and the Activity log is docked under the tabs.
Single-workspace apps (analyzer, quality, assistant) have the top bar only. All of it is
styled in the shared stylesheet. Side panels that hold
CONTENT (the assistant's knowledge base) are fine. Guarded by `tests/test_ui_icon_policy.py`.

**Forms: label beside its box** (October 2026). Every setting is one line, label
on the left and box on the right, in every app. One shared rule in
`assets/icons/swaxs-tokens.css` covers the three patterns the apps use: `.fg`
label/cell pairs, a `.field` holding a label and one box, and `.grid2`/`.grid3`
cells (two or three settings per line, with a fixed label column so the boxes
line up). Checkboxes, radios and sliders keep their inline layout; below
1100 px wide the label goes back above the box.

---

## 0. Per-app conformance

What is **shared** (one definition in `assets/icons/swaxs-tokens.css`, applied to
the hub and all ten apps): the whole light and dark colour palette, the root
font size (`html:root { font-size:18px }`, body `1rem`), card and section
heading style, top bar, theme toggle, tab rows, sidebar rail, folder picker and
form layout. These rules use `html:root` / `html body` selectors, which outrank
an app's own `:root`, `html` or `body` rules, so a leftover app value is
overridden whatever it says. An app's own palette or base size in its template
is therefore dead code, not a divergence.

What is still **per app** (checked against the templates, 2026-10-14):

| App | Port | `--fs-*` scale | `--fw-*` / `--lh-*` / `--sp-*` | `--radius` | `:focus-visible` |
|---|---|---|---|---|---|
| hub | 5100 | all 8 | yes / yes / yes | 8px | yes |
| calibration | 5101 | **none** | no / no / no | 8px | **none** |
| reduction | 5102 | all 8 | yes / yes / yes | 8px | yes |
| average | 5103 | all 8 + `--fs-form` | yes / yes / yes | 8px | yes |
| background | 5104 | 7 of 8 (`--fs-2xl` missing) + `--fs-form` | yes / no / no | 8px | 1 rule |
| quality | 5105 | 6 of 8 | no / no / no | **9px** | **none** |
| analysis | 5106 | 5 of 8 | no / no / no | 8px | **none** |
| analyzer | 5107 | all 8, **different values** | yes / yes / yes | 8px | **none** |
| reactor | 5108 | 4 of 8 + `--fs-form` (section 1 values) | no / no / no | **9px** | **none** |
| assistant | 5109 | all 8 | yes / yes / yes | 8px | yes |
| watchdog | 5110 | all 8 | yes / yes / yes | 8px | **none** |

Every app defines `--font` and `--mono`, includes `_theme_boot.html` and has a
theme toggle (`tests/test_ui_theme_uniform.py`, `tests/test_theme_and_naming.py`).

Consequences worth knowing before you write CSS:

- `var(--sp-4)`, `var(--lh-snug)` resolve only in hub, reduction, average,
  analyzer, assistant and watchdog. Elsewhere they are unset and the
  declaration is dropped.
- analyzer defines `--fs-lg:1.45rem`, `--fs-md:1.05rem`, `--fs-sm:.94rem`,
  `--fs-xs:.82rem`, `--fs-xl:1.6rem`, `--fs-2xl:1.9rem`: the same *names* as
  section 1 with different *values*. A component moved into or out of the
  analyzer changes size silently. (reactor had the same fork; a later `:root`
  in its template now restores the section 1 values.)
- `--txt` / `--text` and `--txt-strong` / `--text-strong` are both defined by
  the shared palette (`--text` aliases `--txt`), so either name works in every
  app.

---

## 1. Typography

| Token | Value | Role |
|---|---|---|
| `--fs-2xs` | 0.6875rem | Eyebrow labels, table headers, micro-meta |
| `--fs-xs` | 0.75rem | Captions, helper text |
| `--fs-sm` | 0.8125rem | Dense text, table cells |
| `--fs-md` | 0.875rem | Controls, secondary text |
| `--fs-base` | 1rem | Body / default reading size |
| `--fs-lg` | 1.125rem | Subsection headings |
| `--fs-xl` | 1.375rem | Section headings |
| `--fs-2xl` | 1.75rem | Page titles |

| Weight token | Value |
|---|---|
| `--fw-regular` | 400 |
| `--fw-medium` | 500 |
| `--fw-semibold` | 600 |
| `--fw-bold` | 700 |

| Line-height | Value | Use |
|---|---|---|
| `--lh-tight` | 1.25 | Headings |
| `--lh-snug` | 1.4 | Tables / dense rows |
| `--lh-normal` | 1.5 | Body copy |

- **Base size is uniform: 18px**, set once in the shared stylesheet (operator's
  choice, October 2026). The `--fs-*` tokens are `rem`-based, so at 18px
  `--fs-sm` renders at ~14.6px, not the 13px of a 16px base. Card and section
  headings are fixed at 15px. Some templates still set `font-size:16px`,
  `17px` (watchdog, "wall display") or `18px` on `html`/`body`; those rules are
  overridden and can be deleted.
- `--fs-form` (0.9375rem) fills the gap between `--fs-md` and `--fs-base`.
  Defined in average, background and reactor.
- **Eyebrow labels** (section titles): `--fs-2xs`, weight 700,
  `letter-spacing: var(--ls-label)` (0.06em), uppercase, color `--muted`.
- **Font family** (`--font`): `'Inter','Segoe UI',system-ui,-apple-system,sans-serif`,
  defined in every app. Inter is chosen for its tall x-height and crisp
  rendering in dense data layouts.
- **Monospace** (`--mono`): for q/I values, file names, numeric data; defined in
  every app.

Rationale follows established guidance: ~1.5 line-height for body, ~1.4 for
tables, a consistent modular scale, and a high-x-height sans-serif for
data-dense displays.

---

## 2. Spacing & radius

| Token | Value |
|---|---|
| `--sp-1` … `--sp-6` | 4 / 8 / 12 / 16 / 24 / 32 px |
| `--radius` | 8px; **9px** in quality and reactor |

`--sp-*` is defined in hub, reduction, average, analyzer, assistant and watchdog.

---

## 3. Color — light palette

One palette for every page (`html:root:not([data-theme="dark"])` in
`swaxs-tokens.css`). Ratios are vs white (`--surface`), computed from the hex
values.

| Token | Hex | Ratio | Role |
|---|---|---|---|
| `--txt` / `--text` | `#1f2937` | 14.7:1 | Primary text |
| `--txt-strong` / `--text-strong` | `#111827` | 16.5:1 | Emphasized headings |
| `--muted` | `#4b5563` | 7.6:1 | Secondary text |
| `--faint` | `#6b7280` | 4.8:1 | Tertiary text |
| `--disabled` | `#9ca3af` | 2.5:1 | **Disabled/decorative only, never essential text** |
| `--border` | `#e5e7eb` | n/a | Subtle dividers (decorative) |
| `--border-strong` | `#c3c9d1` | **1.7:1** | Control outlines. **Fails 3:1**, see section 6 |
| `--accent` | `#B1040E` | 7.3:1 | Brand fill; white labels on it |
| `--accent-text` | `#B1040E` | 7.3:1 | Text in the brand colour |
| `--accent-d` | `#8C1515` | n/a | Hover / active |
| `--ok` / `--green` | `#16a34a` | n/a | Success fills / dots / icons |
| `--ok-text` | `#15803d` | 5.0:1 | Success **text** |
| `--warn` | `#d97706` | n/a | Warning fills |
| `--warn-text` / `--yellow` | `#b45309` | 5.0:1 | Warning **text** |
| `--err` / `--red` | `#dc2626` | n/a | Error fills |
| `--err-text` | `#b91c1c` | 6.5:1 | Error **text** |
| `--info-text` / `--blue` | `#1d4ed8` | 6.7:1 | Info **text** / links |

**Key rule:** bright semantic colors (`--ok`, `--warn`, `--err`) are for
*fills, dots, borders, and icons* (3:1 needed). When the same status appears as
**text**, use the `-text` variant (4.5:1+), paired with its `-lt` tint for a
background. `--teal` (`#B83A4B`) is a secondary colour, not the brand accent.

**Sidebar rail (light):** `--sidebar:#eef1f5`, hover `--sidebar-h:#e3e7ed`,
foreground `--sf:31,41,55`, active row `--sidebar-active-bg:rgba(177,4,14,.10)`
with `--sidebar-active-fg:#8C1515`. `--sf` is a bare `r,g,b` triple, not a
color, so a rail can tint its foreground at partial opacity: use it as
`rgb(var(--sf))` / `rgba(var(--sf),.7)`.

---

## 4. Color — dark palette

### The contract

1. The palette is `html:root[data-theme="dark"]` in `swaxs-tokens.css`. Apps do
   not declare their own; any leftover `:root[data-theme="dark"]` block in a
   template is overridden.
2. Every page includes `_theme_boot.html` in `<head>` (generated by
   `tools/build_icon_sprite.py`, do not edit). It applies the saved choice in
   `localStorage['swaxs-theme']`, else the OS `prefers-color-scheme`, before
   first paint.
3. A `.theme-toggle` button writes the choice back to `swaxs-theme`.

Each app runs on its own port, so the browser keeps a separate `localStorage`
per app; links between apps therefore carry `?theme=dark|light`, which the
receiving app adopts and saves. watchdog used `swaxs_theme` (underscore) until
September 2026; if you add an app, use the hyphen
(`tests/test_theme_and_naming.py` checks this).

### Dark palette

The same in the hub and all ten apps. Ratios vs `--surface` `#1f2126`.

| Token | Hex | Ratio | Role |
|---|---|---|---|
| `--bg` | `#15161a` | n/a | Page background |
| `--surface` | `#1f2126` | n/a | Card / panel |
| `--surface2` | `#272a31` | n/a | Inset / secondary panel |
| `--border` | `#343842` | n/a | Subtle dividers |
| `--border-strong` | `#4c515c` | **2.0:1** | Control outlines. **Fails 3:1** |
| `--txt` / `--text` | `#e7e9ee` | 13.3:1 | Primary text |
| `--txt-strong` / `--text-strong` | `#f5f7fa` | 15.0:1 | Emphasized headings |
| `--muted` | `#a4abb6` | 7.0:1 | Secondary text |
| `--faint` | `#8a919c` | 5.1:1 | Tertiary text |
| `--disabled` | `#5a606b` | n/a | Disabled/decorative only |
| `--accent` | `#cc4450` | 3.5:1 | Brand fill (white labels on it: 4.66:1) |
| `--accent-text` | `#ff7b8a` | 6.5:1 | Text in the brand colour, links |
| `--accent-d` | `#b1040e` | n/a | Accent hover/active |
| `--accent-lt` | `#3a2023` | n/a | Accent tint background |
| `--sidebar` | `#1f2126` | n/a | Rail collapses into the surface |
| `--sidebar-h` | `#2a2d34` | n/a | Rail hover |
| `--sf` | `231,233,238` | n/a | Rail foreground triple |
| `--sidebar-active-bg` | `rgba(212,74,85,.22)` | n/a | Active row tint |
| `--sidebar-active-fg` | `#f0a6ab` | n/a | Active row text |

The hub had its own darker palette (`#0d1117` background) until October 2026;
it now uses this one.

---

## 5. Components

- **Buttons**: primary has an `--accent` fill, white text, `--accent-d` border;
  ghost is transparent with a `--border-strong` outline. Radius `--radius`.
- **Form controls**: `--border-strong` 1px outline; focus adds
  `border-color: var(--accent)` and a 3px translucent ring. Label beside the
  box (shared rule, see above).
- **Focus ring (keyboard)**, where present: `outline: 2px solid var(--accent)`,
  2px offset, on `a`, `button`, `[role="button"]`, `input`, `select`,
  `textarea`. **Six apps have no `:focus-visible` rule**: analysis, analyzer,
  calibration, quality, reactor, watchdog. Moving the reduction rule into the
  shared stylesheet would fix all of them at once.
- **Badges / status**: bright color for the border, `-text` variant for the label.
- **Hints**: 3px left border in the semantic color, tinted background, `--fs-sm` text.
- **Tables**: `--fs-sm`, `--lh-snug`; headers use the eyebrow label style and a
  `--border-strong` underline.
- **Tabs**: active tab uses `--accent` text and a 2px underline (shared rule).
- **Sidebar**: `--sidebar` background with `rgb(var(--sf))` text; active row
  gets `--sidebar-active-bg` tint and `--sidebar-active-fg` text.
- **Plots**: Plotly charts take colours from `swaxsPlotTheme()` and re-colour on
  a theme change (`swaxsRethemePlots`). Server-side matplotlib PNGs render on a
  light background regardless of theme, so exported figures stay correct.

---

## 6. Accessibility status — read this before citing conformance

**This document does not claim WCAG 2.2 AA conformance for the platform.**
Text contrast is enforced: `tests/test_ui_theme_uniform.py` checks the text
pairs and white-on-accent labels of the shared palette in both themes against
the AA floors. What is still failing:

| Issue | Detail |
|---|---|
| `--border-strong` fails SC 1.4.11 (non-text contrast, 3:1) | `#c3c9d1` (1.7:1 on white) light, `#4c515c` (2.0:1 on `--surface`) dark. Every form-control outline on the platform is below the threshold. One change in `swaxs-tokens.css` fixes it everywhere. |
| No keyboard focus indicator (SC 2.4.7) | analysis, analyzer, calibration, quality, reactor and watchdog have no `:focus-visible` rule. |
| Rendered contrast never measured | The ratios here are computed from hex values, not measured on rendered pages (tints, opacity and plots are not covered). |
| Per-element literals | Several hundred per-element `font-size` literals remain in the templates. New components should use the scale tokens. |

---

## 7. How to use going forward

When adding UI, reference tokens instead of literals — but check section 0
first for whether the token exists in the app you are editing.

```css
.my-label   { font-size: var(--fs-2xs); font-weight: var(--fw-bold);
              letter-spacing: var(--ls-label); text-transform: uppercase; color: var(--muted); }
.my-input   { border: 1px solid var(--border-strong); border-radius: var(--radius); }
.my-status  { color: var(--ok-text); }      /* text */
.my-dot     { background: var(--ok); }      /* fill */
```

If a token is missing in the app, add the whole group to that app's `:root`
rather than inventing a local name — the fork in `--txt-strong` /
`--text-strong` is what one-off naming looks like three apps later.
