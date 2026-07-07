# SWAXS Platform — Design System

A single, shared specification for typography, spacing, and color across all six
apps. The goal is uniform readability and WCAG 2.2 AA accessibility for long
scientific workflows. Every app's `:root` now carries these tokens, so future
components should reference the tokens rather than hardcoding values.

Two color themes share one typographic system:

- **Light theme** — Reduction, Viewer, Background, Analysis, Assistant
- **Dark theme** — Hub (control center)

---

## 1. Typography

One scale, four weights, three line-heights — identical in every app.

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

- **Base size: 16px** in every app (`html,body`).
- **Eyebrow labels** (section titles): `--fs-2xs`, weight 700, `letter-spacing: var(--ls-label)` (0.06em), uppercase, color `--muted`.
- **Font family** (`--font`): `'Inter','Segoe UI',system-ui,-apple-system,sans-serif`. Inter is chosen for its tall x-height and crisp rendering in dense data layouts.
- **Monospace** (`--mono`): `'JetBrains Mono','Fira Code',monospace` for q/I values, file names, numeric data.

Rationale follows established guidance: ~1.5 line-height for body, ~1.4 for tables, a consistent modular scale, and a high-x-height sans-serif for data-dense displays.

---

## 2. Spacing & radius

| Token | Value |
|---|---|
| `--sp-1` … `--sp-6` | 4 / 8 / 12 / 16 / 24 / 32 px |
| `--radius` | 8px (unified; was 7–12 across apps) |

---

## 3. Color — Light theme (5 apps)

All text/UI pairs meet WCAG 2.2 AA. Ratios are vs white (`--surface`) unless noted.

| Token | Hex | Ratio | Role |
|---|---|---|---|
| `--txt` / `--text` | `#1f2937` | 14.7:1 | Primary text |
| `--text-strong` | `#111827` | 16.5:1 | Emphasized headings |
| `--muted` | `#4b5563` | 7.6:1 | Secondary text |
| `--faint` | `#6b7280` | 4.8:1 | Tertiary text (still AA) |
| `--disabled` | `#9ca3af` | 2.5:1 | **Disabled/decorative only — never essential text** |
| `--accent` | `#B83A4B` | 5.6:1 | SLAC red — links, accents, button fill |
| `--accent-d` | `#8c2a38` | — | Accent hover/active |
| `--sidebar` | `#8C1515` | 9.4:1 (white on it) | Stanford Cardinal sidebar |
| `--border` | `#e5e7eb` | — | Subtle dividers (decorative) |
| `--border-strong` | `#828c99` | 3.4:1 | **Form-control outlines (meets 3:1)** |
| `--ok` / `--green` | `#16a34a` | — | Success fills / dots / icons |
| `--ok-text` | `#15803d` | 5.0:1 | Success **text** |
| `--warn` / `--yellow` | `#d97706` | — | Warning fills |
| `--warn-text` | `#b45309` | 5.0:1 | Warning **text** |
| `--err` / `--red` | `#dc2626` | — | Error fills |
| `--err-text` | `#b91c1c` | 6.5:1 | Error **text** |
| `--info-text` / `--blue` | `#1d4ed8` | 6.7:1 | Info **text** / links |

**Key rule:** bright semantic colors (`--ok`, `--warn`, `--err`) are for *fills, dots,
borders, and icons* (3:1 needed). When the same status appears as **text**, use the
`-text` variant (4.5:1+).

---

## 4. Color — Dark theme (Hub)

Ratios vs `--bg` `#0d1117`.

| Token | Hex | Ratio | Role |
|---|---|---|---|
| `--text` | `#e6edf3` | 16:1 | Primary text |
| `--muted` | `#8b949e` | 6.2:1 | Secondary text |
| `--accent` | `#B83A4B` | 3.4:1 | Brand fill / large text only |
| `--accent-text` | `#ff7b8a` | 7.6:1 | **Small accent text / links** |
| `--ok-text` | `#3fb950` | 7.5:1 | Success |
| `--warn-text` | `#d29922` | 7.5:1 | Warning |
| `--err-text` | `#f85149` | 5.7:1 | Error |
| `--info-text` | `#58a6ff` | 7.5:1 | Info |
| `--border-strong` | `#6b7280` | 3.6:1 (vs surface) | Control outlines |

---

## 5. Components

- **Buttons** — primary: `--accent` fill, white text (5.6:1), `--accent-d` border;
  ghost: transparent, `--border-strong` outline. Radius `--radius`.
- **Form controls** — `--border-strong` 1px outline (meets 3:1, SC 1.4.11); focus adds
  `border-color: var(--accent)` + a 3px translucent ring.
- **Focus ring (keyboard)** — every app has a global `:focus-visible` rule:
  `outline: 2px solid var(--accent)` (`--accent-text` in the hub), 2px offset (SC 2.4.7).
- **Badges / status** — bright color for the border, `-text` variant for the label.
- **Hints** — 3px left border in the semantic color, tinted background, `--fs-sm` text.
- **Tables** — `--fs-sm`, `--lh-snug`; headers use the eyebrow label style + `--border-strong` underline.
- **Tabs** — active tab uses `--accent` text + 2px underline.
- **Sidebar** — Stanford Cardinal (`--sidebar`), white text (9.4:1), active item tinted.
- **Plots** — Plotly/matplotlib remain on light backgrounds; the light theme is intentionally
  retained for the data apps so plot contrast and exported figures stay correct.

---

## 6. How to use going forward

When adding UI, reference tokens instead of literals:

```css
.my-label   { font-size: var(--fs-2xs); font-weight: var(--fw-bold);
              letter-spacing: var(--ls-label); text-transform: uppercase; color: var(--muted); }
.my-input   { border: 1px solid var(--border-strong); border-radius: var(--radius); }
.my-status  { color: var(--ok-text); }      /* text */
.my-dot     { background: var(--ok); }      /* fill */
```

The shared tokens above are the single source of truth; each app's `templates/index.html` consumes them via its `:root` block.
