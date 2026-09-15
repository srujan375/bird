# Wireframe

## Mood / Feel

A structural sketch, not a design. Greyscale only: boxes, rules, and labels.
Placeholder imagery is a grey box with a diagonal line, drawn in CSS. Nothing
here is a style decision — it exists so the group can argue about structure
before anyone falls in love with a color.

**This theme is for structure review only — apply a real theme before
finalize.**

## Color Roles

- `--bg`: `#ffffff`
- `--surface`: `#f5f5f5`
- `--text`: `#111111`
- `--text-secondary`: `#999999`
- `--accent`: NONE — this theme has no accent. Do not introduce one.
- `--accent-hover`: NONE
- `--border`: `#e5e5e5`

## Typography

- Display: `system-ui, sans-serif` — weight 700, `clamp(1.8rem, 4vw, 2.6rem)`,
  line-height 1.2. system-ui is allowed here and only here: wireframes are
  about structure, not type.
- Body: `system-ui, sans-serif` — 400, 1rem/1.5.
- Mono: `ui-monospace, 'SF Mono', Menlo, monospace` for labels and dims.

## Component Stylings

- Buttons: white fill, 1px `#111` border, radius 4px, no shadow.
- Cards: `--surface` fill, 1px `--border`, radius 4px, no shadow.
- Inputs: 1px `--border`, white fill, radius 4px.
- Placeholder imagery: a grey box with a diagonal line —
  `background: linear-gradient(135deg, transparent 49.6%, #999 49.6% 50.4%, transparent 50.4%)`
  over `#f5f5f5`, with a small `#999` label.

## Layout Principles

- Max-width 72rem; a plain stacked flow, no grid heroics.
- Section spacing: `--space-section` (3rem); blocks are separated by 1px
  rules, not whitespace alone.
- Every image slot gets the diagonal-line box; every button gets a real
  label, never lorem.

## Do's and Don'ts

- DO keep it greyscale: no accent, no color fills, no shadows, no gradients
  beyond the placeholder diagonal.
- DON'T style anything — if a choice is visible, it belongs to a real theme.
- DON'T finalize in this theme; it is for structure review only.
- DON'T use emoji icons or 3-col card spam here either.
- DO hold WCAG AA: `#111` on `#fff` passes; `#999` is for secondary labels.