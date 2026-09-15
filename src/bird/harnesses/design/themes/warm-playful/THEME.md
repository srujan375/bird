# Warm Playful

## Mood / Feel

A friendly product, not a toy. Cream ground, rounded humanist type, pill
buttons, and soft shadows that lift rather than float. Microcopy is allowed a
smile ("Nice — saved."). The overall effect is a warm welcome, not a
confetti cannon: playful lives in the corners and the copy, the layout stays
calm.

## Color Roles

- `--bg`: `#fff8f0` (cream)
- `--surface`: `#ffffff`
- `--text`: `#3d2c29` (warm dark brown)
- `--text-secondary`: `#8a7268`
- `--accent`: `#f26b4e` (coral)
- `--accent-hover`: `#d95538`
- `--border`: `#f0e2d8`

## Typography

- Display: `'Nunito', 'Quicksand', 'Trebuchet MS', sans-serif` — weight 800,
  `clamp(2rem, 4.5vw, 3rem)`, line-height 1.2, letter-spacing -0.01em.
- Body: `'Nunito', 'Quicksand', 'Trebuchet MS', sans-serif` — 400,
  1.02rem/1.6.
- Mono: `ui-monospace, 'SF Mono', Menlo, monospace` at 0.9rem.
- Sentence case everywhere; ALL CAPS only for tiny badges.

## Component Stylings

- Buttons: pill (radius 999px), flat `--accent` bg, white text, soft shadow
  `0 2px 8px rgba(61, 44, 41, 0.15)`; hover darkens to `--accent-hover`.
- Cards: `--surface`, 1px `--border`, radius 16px, soft shadow
  `0 4px 14px rgba(61, 44, 41, 0.08)`.
- Inputs: 1px `--border`, radius 12px, `--surface` fill; focus ring 2px
  `--accent` with a warm offset.
- Chips/badges: pill, `--accent` at 12% opacity fill, `--accent` text.

## Layout Principles

- Max-width 68rem; content breathes — 24px gutters minimum.
- Section spacing: `--space-section` (4.5rem); group related things in soft
  cards rather than rules.
- Rounded corners everywhere are the point; a square corner here reads as a
  bug.

## Do's and Don'ts

- DO keep the coral to buttons, links, and one highlight per viewport.
- DON'T use Inter or Roboto as the identity font — humanist or nothing.
- DON'T use emoji as icons; the friendliness is in the copy and the curves.
- DON'T use purple-gradient-on-white or 3-col card spam.
- DON'T exceed the accent 3x per viewport; shadows stay soft, never dark.
- DO hold WCAG AA: `#3d2c29` on `#fff8f0` and `#ffffff` on `#f26b4e` pass.