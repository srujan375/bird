# Editorial

## Mood / Feel

A magazine spread, not a dashboard. Warm paper, serif display type, generous
whitespace, and hairline rules doing the work borders usually do. The page
should read like it was set by someone who cares about line length. Calm,
literate, unhurried — nothing shouts, nothing bounces.

## Color Roles

- `--bg`: `#faf7f2` (warm paper)
- `--surface`: `#fffdf9`
- `--text`: `#1a1712` (near-black, warm)
- `--text-secondary`: `#6b6257`
- `--accent`: `#8b2f1f` (deep oxblood red)
- `--accent-hover`: `#6f2418`
- `--border`: `#e3ddd2` (hairline)

## Typography

- Display: `'Iowan Old Style', 'Palatino Linotype', Georgia, serif` — weight
  600, `clamp(2.2rem, 5vw, 3.6rem)`, line-height 1.15, letter-spacing -0.01em.
- Body: `'Iowan Old Style', Georgia, serif` — 400, 1.06rem/1.65, max 68ch.
- Mono: `'Iowan Old Style', ui-monospace, monospace` is wrong for code — use
  `ui-monospace, 'SF Mono', Menlo, monospace` at 0.92rem.
- Drop caps on long-form openers are welcome (`::first-letter`), optional.

## Component Stylings

- Buttons: flat `--accent` background, paper-colored text, no radius beyond
  2px, no shadow, hover darkens to `--accent-hover`.
- Cards: `--surface` on `--bg`, 1px `--border`, no shadow, 24px padding.
- Inputs: 1px `--border`, `--surface` fill, focus ring 2px `--accent`.
- Rules between sections: 1px `--border`, full measure width.

## Layout Principles

- Max-width 72rem, content measure 68ch.
- Single column with an occasional asymmetric two-column (2:1) for asides.
- Section spacing: `--space-section` (5rem) between sections; let whitespace
  separate more than boxes do.

## Do's and Don'ts

- DO keep the accent rare: headlines' key phrase, one button, active nav.
- DON'T use Inter, Roboto, or any grotesque as the identity font.
- DON'T add shadows — this theme has none; separation is hairlines + space.
- DON'T use purple-gradient-on-white, emoji icons, or 3-col card spam.
- DON'T exceed the accent 3x per viewport.
- DO hold WCAG AA: `#1a1712` on `#faf7f2` and `#fffdf9` on `#8b2f1f` both pass.