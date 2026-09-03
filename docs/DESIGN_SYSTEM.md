# SWAXS Platform — Design System

The typography, spacing, and color tokens the platform's apps are *meant* to
share. It is **not** a single source of truth: each app owns its own `:root`
block in `<app>/templates/index.html`, and they have diverged. Section 0 says
exactly how far, per app, so you know whether a token you reference will
actually resolve.

The canonical, most complete implementation is
`reduction/templates/index.html:9-51` (light) and `:366-372` (dark). When this
document and an app disagree, the app wins — fix the app or fix this file, but
do not assume conformance.

---

## 0. Per-app conformance

| App | Port | Type scale | Weights / line-heights / spacing | `--font` | `--radius` | Base size | Dark mode | `:focus-visible` |
|---|---|---|---|---|---|---|---|---|
| reduction | 5001 | all 8 `--fs-*` | yes | Inter | 8px | 16px | toggle + OS | yes |
| average | 5002 | all 8 | yes | Inter | 8px | (inherits 16px) | toggle + OS | yes |
| assistant | 5005 | all 8 | yes | Inter | 8px | **18px** | toggle + OS | yes |
| hub | 5000 | all 8 | yes | Inter | 8px | 16px | dark only, no toggle | yes |
| background | 5003 | 7 of 8 (`--fs-2xl` missing) | `--fw-*` partial; no `--lh-*`, no `--sp-*` | Inter | 8px | **18px** | toggle + OS | 1 rule |
| analysis | 5004 | 5 of 8 | none | Inter | 8px | **18px** | toggle + OS | **none** |
| quality | 5006 | 6 of 8 | none | **unset** (system-ui literal) | **9px** | 16px | toggle + OS | **none** |
| reactor | 5007 | 4 tokens, **different values** | none | **unset** | **9px** | 1.02rem | toggle, defaults dark | **none** |
| analyzer | 5008 | 4 tokens, **different values** | none | **unset** | **9px** | 1.02rem | toggle, defaults dark | **none** |
| calibration | 5009 | **none** | none | Inter | 8px | 16px | light only, no toggle | **none** |

Consequences worth knowing before you write CSS:

- `var(--sp-4)`, `var(--lh-snug)`, `var(--fw-bold)`, `var(--ls-label)` resolve in
  four apps only (reduction, average, assistant, hub). Elsewhere they are unset
  and the declaration is dropped.
- reactor and analyzer define `--fs-lg:1.45rem`, `--fs-md:1.05rem`,
  `--fs-sm:.94rem`, `--fs-xs:.82rem` — the same *names* as section 1 with
  different *values*. A component moved between apps changes size silently.
  This is worse than the tokens being absent.
- Text-token names have forked: `--txt` / `--txt-strong` in calibration,
  reduction, average; `--text` / `--text-strong` in analysis, assistant,
  background; `--text` with no strong variant in analyzer, quality, reactor,
  hub. There is no name that works everywhere.

---

## 1. Typography

| Token | Value | Role |
|---|---|---|
| `--fs-2xs` | 0.6875rem (11px) | Eyebrow labels, table headers, micro-meta |
| `--fs-xs` | 0.75rem (12px) | Captions, helper text |
| `--fs-sm` | 0.8125rem (13px) | Dense text, table cells |
| `--fs-md` | 0.875rem (14px) | Controls, secondary text |
| `--fs-base` | 1rem (16px) | Body / default reading size |
| `--fs-lg` | 1.125rem (18px) | Subsection headings |
| `--fs-xl` | 1.375rem (22px) | Section headings |
| `--fs-2xl` | 1.75rem (28px) | Page titles |

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

- **Base size is not uniform.** 16px in reduction, average, hub, quality,
  calibration; **18px** in analysis, background, assistant; `1.02rem` in
  reactor and analyzer. Because `--fs-*` are `rem`-based, the 18px apps render
  the whole scale ~12% larger than the table above.
- **Eyebrow labels** (section titles): `--fs-2xs`, weight 700,
  `letter-spacing: var(--ls-label)` (0.06em), uppercase, color `--muted`.
- **Font family** (`--font`): `'Inter','Segoe UI',system-ui,-apple-system,sans-serif`.
  Inter is chosen for its tall x-height and crisp rendering in dense data
  layouts. quality, reactor, and analyzer do not define `--font` and use a
  `system-ui` literal in `body` instead.
- **Monospace** (`--mono`): `'JetBrains Mono','Fira Code',monospace` for q/I
  values, file names, numeric data. quality/reactor/analyzer use
  `ui-monospace,SFMono-Regular,Menlo,monospace`.

Rationale follows established guidance: ~1.5 line-height for body, ~1.4 for
tables, a consistent modular scale, and a high-x-height sans-serif for
data-dense displays.

---

## 2. Spacing & radius

| Token | Value |
|---|---|
| `--sp-1` … `--sp-6` | 4 / 8 / 12 / 16 / 24 / 32 px |
| `--radius` | 8px in seven apps; **9px** in quality, reactor, analyzer |

`--sp-*` is defined in reduction, average, assistant, and hub only.

---

## 3. Color — light palette

Ratios are vs white (`--surface`) and were computed from the shipped hex
values, not carried over from an older revision.

| Token | Hex | Ratio | Role |
|---|---|---|---|
| `--txt` / `--text` | `#1f2937` | 14.7:1 | Primary text |
| `--txt-strong` / `--text-strong` | `#111827` | 16.5:1 | Emphasized headings |
| `--muted` | `#4b5563` | 7.6:1 | Secondary text |
| `--faint` | `#6b7280` | 4.8:1 | Tertiary text |
| `--disabled` | `#9ca3af` | 2.5:1 | **Disabled/decorative only — never essential text** |
| `--border` | `#e5e7eb` | — | Subtle dividers (decorative) |
| `--border-strong` | `#c3c9d1` / `#b9c0ca` | **1.7:1 / 1.8:1** | Control outlines — **fails 3:1**, see section 6 |
| `--ok` / `--green` | `#16a34a` | — | Success fills / dots / icons |
| `--ok-text` | `#15803d` | 5.0:1 | Success **text** |
| `--warn` / `--yellow` | `#d97706` | — | Warning fills |
| `--warn-text` | `#b45309` | 5.0:1 | Warning **text** |
| `--err` / `--red` | `#dc2626` | — | Error fills |
| `--err-text` | `#b91c1c` | 6.5:1 | Error **text** |
| `--info-text` / `--blue` | `#1d4ed8` | 6.7:1 | Info **text** / links |

**Key rule:** bright semantic colors (`--ok`, `--warn`, `--err`) are for
*fills, dots, borders, and icons* (3:1 needed). When the same status appears as
**text**, use the `-text` variant (4.5:1+).

### Three accents ship, not one

| `--accent` | Ratio on white | Apps |
|---|---|---|
| `#B1040E` | 7.3:1 | analysis, assistant, background, calibration, reduction, average |
| `#B83A4B` | 5.6:1 | analyzer, quality, reactor |
| `#C5202C` | 5.8:1 | hub |

`--accent-d` is `#8C1515` in every app (the deep-cardinal hover). `#B83A4B`,
which earlier revisions of this document named as *the* accent, has been
demoted in the reduction app to `--teal`
(`reduction/templates/index.html:22`) — it is no longer that app's brand color
at all.

### The sidebar is a light rail, not a cardinal one

The rail is **light with dark text**, the inverse of what earlier revisions
described:

| Token | Value | Apps |
|---|---|---|
| `--sidebar` | `#eef1f5` | analysis, background, calibration, reduction, average |
| `--sidebar` | `#27272d` | assistant |
| `--sidebar` | `#23262d` | quality |
| `--sidebar-h` | `#e3e7ed` (light) / `#34343c` (assistant) | hover row |
| `--sf` | `31,41,55` (light rail) / `231,233,238` (dark rail) | rail foreground, as an RGB triple for `rgba()` |
| `--sidebar-active-bg` | `rgba(177,4,14,.10)` | active row tint |
| `--sidebar-active-fg` | `#8C1515` | active row text |

`--sf` is a bare `r,g,b` triple, not a color — it exists so a rail can tint its
own foreground at partial opacity and flip between light and dark themes with
one token. Use it as `rgb(var(--sf))` / `rgba(var(--sf),.7)`.

There is no `#8C1515` cardinal sidebar with white text anywhere in the
platform.

---

## 4. Color — dark palette

Eight apps ship a dark theme. It was undocumented until now.

### The contract

Every dark-capable app implements the same four pieces:

1. A `:root[data-theme="dark"]` block that re-declares the palette tokens
   (`reduction/templates/index.html:366-372`).
2. A `.theme-toggle` button, `id="themeToggle"` or `id="themeBtn"`
   (`reduction/templates/index.html:424`).
3. An IIFE that reads `localStorage['swaxs-theme']` (`'dark'` | `'light'`) and
   sets `document.documentElement.dataset.theme`
   (`reduction/templates/index.html:1470-1474`).
4. `window.toggleTheme()` writing the choice back to that key.

The key name `swaxs-theme` is shared across all apps, so the choice follows the
user from app to app on the same `localhost` origin.

**Where the default comes from differs:**

| Apps | Initial theme when `localStorage` is empty |
|---|---|
| reduction, average, analysis, background, quality, assistant | `matchMedia('(prefers-color-scheme:dark)')` — follows the OS |
| reactor, analyzer | **dark**, unconditionally (`<html data-theme="dark">`) |
| hub | dark only; no toggle, no light palette |
| calibration | light only; no toggle, no dark palette |

New apps should follow the OS. Hardcoding dark means a user on a light desktop
gets a dark panel next to nine light ones.

### Dark palette

Identical in all eight apps. Ratios vs `--surface` `#1f2126`.

| Token | Hex | Ratio | Role |
|---|---|---|---|
| `--bg` | `#15161a` | — | Page background |
| `--surface` | `#1f2126` | — | Card / panel |
| `--surface2` | `#272a31` | — | Inset / secondary panel |
| `--border` | `#343842` | — | Subtle dividers |
| `--border-strong` | `#4c515c` | **2.0:1** | Control outlines — **fails 3:1** |
| `--txt` / `--text` | `#e7e9ee` | 13.4:1 | Primary text |
| `--txt-strong` / `--text-strong` | `#f5f7fa` | 15.2:1 | Emphasized headings |
| `--muted` | `#a4abb6` | 7.0:1 | Secondary text |
| `--faint` | `#7d848f` | **4.3:1** | Tertiary text — marginally under AA for body text |
| `--disabled` | `#5a606b` | — | Disabled/decorative only |
| `--accent` | `#d44a55` | 3.8:1 | Brand fill / large text only |
| `--accent-d` | `#b1040e` | — | Accent hover/active |
| `--accent-lt` | `#3a2023` | — | Accent tint background |
| `--sidebar` | `#1f2126` | — | Rail collapses into the surface |
| `--sidebar-h` | `#2a2d34` | — | Rail hover |
| `--sf` | `231,233,238` | — | Rail foreground triple |
| `--sidebar-active-bg` | `rgba(212,74,85,.22)` | — | Active row tint |
| `--sidebar-active-fg` | `#f0a6ab` | — | Active row text |

### Dark palette — hub

The hub predates the shared dark palette and uses its own, darker one.

| Token | Hex | Role |
|---|---|---|
| `--bg` | `#0d1117` | Page background |
| `--surface` | `#161b22` | Card |
| `--surface2` | `#1c2128` | Inset |
| `--border` | `#30363d` | Dividers |
| `--border-strong` | `#6b7280` | Control outlines — the only `--border-strong` in the platform that still clears 3:1 |
| `--text` | `#e6edf3` | Primary text |
| `--muted` | `#8b949e` | Secondary text |
| `--accent` | `#C5202C` | Brand fill / large text only |
| `--accent-text` | `#ff7b8a` | **Small accent text / links** |
| `--ok-text` | `#3fb950` | Success |
| `--warn-text` | `#d29922` | Warning |
| `--err-text` | `#f85149` | Error |
| `--info-text` | `#58a6ff` | Info |

---

## 5. Components

- **Buttons** — primary: `--accent` fill, white text, `--accent-d` border;
  ghost: transparent, `--border-strong` outline. Radius `--radius`.
- **Form controls** — `--border-strong` 1px outline; focus adds
  `border-color: var(--accent)` + a 3px translucent ring.
- **Focus ring (keyboard)** — where present:
  `outline: 2px solid var(--accent)` (`--accent-text` in the hub), 2px offset,
  applied to `a`, `button`, `[role="button"]`, `input`, `select`, `textarea`
  (`reduction/templates/index.html:418-422`). **Five apps have no
  `:focus-visible` rule at all** — analysis, analyzer, calibration, quality,
  reactor. Add one when you touch those apps.
- **Badges / status** — bright color for the border, `-text` variant for the label.
- **Hints** — 3px left border in the semantic color, tinted background, `--fs-sm` text.
- **Tables** — `--fs-sm`, `--lh-snug`; headers use the eyebrow label style +
  `--border-strong` underline.
- **Tabs** — active tab uses `--accent` text + 2px underline.
- **Sidebar** — `--sidebar` background with `rgb(var(--sf))` text; active row
  gets `--sidebar-active-bg` tint and `--sidebar-active-fg` text. In light
  themes this is a pale rail with dark text; in dark themes it merges into
  `--surface`.
- **Plots** — Plotly/matplotlib render on light backgrounds regardless of
  theme, so plot contrast and exported figures stay correct. Dark-mode panels
  therefore contain light plot canvases by design.

---

## 6. Accessibility status — read this before citing conformance

**This document no longer claims WCAG 2.2 AA conformance for the platform.**
Earlier revisions asserted that `--border-strong` was `#828c99` at 3.4:1 and
"meets 3:1, SC 1.4.11", and that every app carried a global `:focus-visible`
rule (SC 2.4.7). Neither is true of the shipped CSS.

Known, unresolved failures:

| Issue | Detail |
|---|---|
| `--border-strong` fails SC 1.4.11 (non-text contrast, 3:1) | Ships as `#c3c9d1` (~1.7:1 on white) in analysis, assistant, background, calibration, reduction, average; `#b9c0ca` (~1.8:1) in analyzer, quality, reactor; `#4c515c` (~2.0:1) in the shared dark palette. Only the hub's `#6b7280` passes. Every form-control outline on the platform is therefore below the threshold. |
| No keyboard focus indicator (SC 2.4.7) | analysis, analyzer, calibration, quality, reactor have zero `:focus-visible` rules. |
| `--faint` in dark mode | `#7d848f` is ~4.3:1 on `--surface` — under 4.5:1 for body-size text. |
| Never contrast-checked | quality, reactor, analyzer, and calibration were all added *after* the 2026-06-15 accessibility audit that produced these tokens, and have never been contrast-checked at all. The ratios quoted in sections 3 and 4 are computed from hex values, not measured against rendered screenshots. |
| Per-element literals | A few hundred per-element `font-size` literals remain in the templates. They sit *within* the unified base/scale and were left in place to avoid layout risk. New components should use the scale tokens. |

The text/`-text` pairs in sections 3 and 4 do meet 4.5:1 as tabulated; the
failures above are non-text contrast and focus indication.

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
