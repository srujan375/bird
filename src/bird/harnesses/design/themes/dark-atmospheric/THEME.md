# Dark Atmospheric

## Mood / Feel

A room with the lights down. Near-black ground, depth built from layered
subtle gradients rather than borders, light text, and one luminous accent
that glows when it matters. Glass panels are a seasoning, not a meal. The
page should feel like a dimmed control room at night — quiet, deep, focused.

## Color Roles

- `--bg`: `#0d0f14` (near-black, blue-cold)
- `--surface`: `#151922`
- `--text`: `#e8eaf2`
- `--text-secondary`: `#9aa1b5`
- `--accent`: `#6c8cff` (luminous periwinkle)
- `--accent-hover`: `#8aa3ff`
- `--border`: `#262c3a`

## Typography

- Display: `'Sora', 'Space Grotesk', 'Segoe UI', sans-serif` — weight 600,
  `clamp(2.2rem, 5vw, 3.4rem)`, line-height 1.15, letter-spacing -0.02em.
- Body: `'Inter', 'Segoe UI', system-ui, sans-serif` — 400, 1rem/1.65. Inter
  is a body fallback here, never the display voice.
- Mono: `ui-monospace, 'SF Mono', Menlo, monospace` at 0.9rem for data.

## Component Stylings

- Buttons: flat `--accent` bg, `#0d0f14` text, radius 10px, glow on focus
  `0 0 0 3px rgba(108, 140, 255, 0.35)`; hover lifts to `--accent-hover`.
- Cards: `--surface` at 85% opacity with `backdrop-filter: blur(8px)` — glass,
  used sparingly (one or two per viewport); 1px `--border`.
- Inputs: `--surface` fill, 1px `--border`, radius 8px; focus = glowing ring
  `0 0 0 3px rgba(108, 140, 255, 0.35)`.
- Depth: layered radial gradients on the bg, never hard-edged blocks.

## Layout Principles

- Max-width 76rem; content floats on the gradient, not in boxes.
- Section spacing: `--space-section` (5rem); let the background carry the
  separation instead of rules.
- One hero gradient per page; the rest of the depth is whisper-quiet.

## Do's and Don'ts

- DO keep large text ≥ 4.5:1 and small text ≥ 4.5:1 against `#0d0f14` —
  `#e8eaf2` passes; `#9aa1b5` is for secondary only, never body copy.
- DON'T use pure black `#000` or pure white `#fff` — the palette is tuned.
- DON'T use Inter or Roboto as the identity/display font.
- DON'T use purple-gradient-on-white (this theme's gradients live on dark),
  emoji icons, or 3-col card spam.
- DON'T exceed the accent 3x per viewport; glass panels max two per viewport.