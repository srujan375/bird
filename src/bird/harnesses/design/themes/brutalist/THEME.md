# Brutalist

## Mood / Feel

The machine showing its bolts. Raw white or off-black ground, monospace and
heavy grotesque display, thick black borders, hard offset shadows, and no
attempt to soften anything. Structure is exposed: the grid, the tags, the
numbers. It should feel printed, not rendered.

## Color Roles

- `--bg`: `#ffffff` (raw white; invert to `#111111` bg / `#f2f2f2` text for a
  dark variant — keep the borders black either way)
- `--surface`: `#f2f2f2`
- `--text`: `#111111`
- `--text-secondary`: `#444444`
- `--accent`: `#0000ff` (pure blue; yellow `#ffd400` works as a highlighter)
- `--accent-hover`: `#0000cc`
- `--border`: `#000000` (2–3px)

## Typography

- Display: `'Archivo Black', 'Arial Black', 'Helvetica Neue', sans-serif` —
  weight 900, `clamp(2.6rem, 7vw, 4.5rem)`, line-height 1.05, letter-spacing
  -0.02em. Monospace display (`ui-monospace, 'Courier New', monospace`,
  weight 700) is the alternate voice.
- Body: `ui-monospace, 'Courier New', monospace` — 400, 0.95rem/1.55.
- Mono: same stack; it is the identity, not a fallback.

## Component Stylings

- Buttons: flat `--accent` or white, 2px black border, radius 0, hard shadow
  `4px 4px 0 #000`; hover shifts the shadow to `2px 2px 0 #000` (pressed).
- Cards: `--surface`, 2px black border, radius 0, shadow `4px 4px 0 #000`.
- Inputs: 2px black border, white fill, radius 0, focus = 3px black border.
- Borders over shadows where only one is possible; both is allowed here.

## Layout Principles

- Max-width 72rem; a visible, uneven grid is fine — exposed structure beats
  tidy alignment.
- Section spacing: `--space-section` (4rem); density is part of the look.
- Let elements overlap the grid lines; label sections like form fields.

## Do's and Don'ts

- DO keep contrast brutal: `#111` on `#fff`, no greys below AA.
- DON'T use border-radius anywhere — 0px, always.
- DON'T use soft shadows; the only shadow is the hard offset `4px 4px 0 #000`.
- DON'T use Inter or Roboto as the identity font, purple-gradient-on-white,
  emoji icons, or 3-col card spam.
- DO hold WCAG AA: `#111` on `#fff`, `#fff` on `#0000ff` both pass.