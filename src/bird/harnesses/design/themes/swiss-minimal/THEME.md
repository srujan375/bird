# Swiss Minimal

## Mood / Feel

The grid is the design. White ground, black grotesque type, flush-left
ragged-right, and one accent used so rarely it lands like a shout. Nothing is
decorated; everything is positioned. If a rule or a box is not carrying
information, it comes off.

## Color Roles

- `--bg`: `#ffffff`
- `--surface`: `#ffffff`
- `--text`: `#000000`
- `--text-secondary`: `#555555`
- `--accent`: `#e30613` (signal red)
- `--accent-hover`: `#b80510`
- `--border`: `#000000` (hairline, 1px)

## Typography

- Display: `'Helvetica Neue', Helvetica, Arial, sans-serif` — weight 700,
  `clamp(2.4rem, 6vw, 4rem)`, line-height 1.1, letter-spacing -0.02em.
- Body: same stack — 400, 1rem/1.5, flush-left ragged-right, never justified.
- Mono: `ui-monospace, 'SF Mono', Menlo, monospace` for labels and figures.
- Uppercase + letterspaced micro-labels (11px, +0.08em) are the wayfinding.

## Component Stylings

- Buttons: flat `--accent` fill, white text, radius 0, no shadow; secondary
  buttons are 1px black outline on white.
- Cards: white, 1px black border, radius 0, no shadow, square corners.
- Inputs: 1px black bottom-border only, no fill, focus = 2px black underline.
- Rules: 1px black; a 4px black bar is the heaviest permitted stroke.

## Layout Principles

- Max-width 80rem on a strict 12-col grid; margins align to it exactly.
- Flush-left, ragged-right. Never center body text, never justify.
- Section spacing: `--space-section` (6rem); whitespace is structural, not
  leftover.

## Do's and Don'ts

- DO use the accent at most 3x per viewport — a marker, not a palette entry.
- DON'T use Inter or Roboto as the identity font; the grotesque is Helvetica
  Neue or nothing.
- DON'T add shadows, gradients, or rounded corners — radius is 0 everywhere.
- DON'T use purple-gradient-on-white, emoji icons, or 3-col card spam.
- DO hold WCAG AA: black on white and white on `#e30613` both pass.