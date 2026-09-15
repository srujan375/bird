name: design-craft
description: Use when generating or editing a design artboard in a design session — the house style that keeps work out of AI-slop territory. Load before your first design_create or design_edit.

# Design Craft

The anti-slop rules for artboards. They apply to every `design_create` and
`design_edit`, in any theme. Precedence: the active theme's tokens and
Do-nots win over anything here — `var(--accent)` is right even when the
theme's accent is purple. The bans below bind when you choose a value
yourself: no theme, or styling outside the injected tokens.

Announce: "I'm using the design-craft skill."

## Never (reads as AI-slop)

- Inter, Roboto, Arial or system-ui as the identity font. (system-ui is
  acceptable ONLY in the wireframe theme, where type is not the point.)
- A purple-to-blue gradient on white.
- Gradient buttons.
- A box-shadow on every element.
- 3-column card grids repeated across sections.
- Every section = heading + description + 3 cards.
- Emoji as section icons.
- Scroll-reveal opacity animations on everything.
- "Learn more" CTAs.
- Uniform section padding rhythm — every section the same height and spacing.
- Center-aligning everything.
- 5+ competing colors.

### The second generation

The defaults AI output reached for after the list above stopped catching it.
They read as slop now, however tasteful each one looks in isolation — and the
critic judges against this same list, so a render wearing any of them gets
flagged:

- Cream background + serif display + terracotta accent — the current
  "tasteful editorial" default. If the brief did not name a mood, this
  combination is a tell, not a choice.
- Near-black background + acid-green accent — the "terminal luxury" default.
- Tailwind's default indigo/violet scale as the accent (#6366f1, #8b5cf6,
  #7c3aed) — the most common AI-generated accent, because training data is
  saturated with Tailwind/shadcn defaults.
- Generic SaaS blue as the primary (#3B82F6 / Tailwind blue-500) — the
  "safe" pick that reads as a template, not a decision.
- Broadsheet hairline rules as the only structure — a page held together by
  1px lines and nothing else.
- A tracked-caps kicker over an oversized serif hero — the same opening move
  on every page.
- Drop caps or justified text as "editorial" dressing — typography borrowed,
  not composed.

## Always

- **Composition**: vary the skeleton between sections — split hero, offset
  grid, full-bleed band, asymmetric two-column. Two sections with the same
  skeleton: change one.
- **Copy**: concrete verbs and specific CTAs — "Calculate your budget",
  "Start the 14-day trial". Max 2 CTAs per section.
- **Icons**: inline SVG shapes, never emoji.
- **Touch**: 44px minimum touch targets.
- **Restraint**: one base + one accent; flat buttons; borders over shadows.
- **Edits**: batch related ops in one `design_edit` call — one `set_style`
  with five props, not five calls.

## Only when no theme is active

A theme's tokens carry fonts, scale and color: use `--font-display`,
`--text-4xl`, `--tracking-display` (vendored themes) or the h1 styling in
tokens.css (hand-written themes). The fallbacks below are for unthemed work:

- **Type**: the artboard cannot load web fonts, so pick stacks that resolve
  locally. Editorial serif `'Iowan Old Style', 'Palatino Linotype', Georgia,
  serif`; humanist sans `'Avenir Next', 'Helvetica Neue', sans-serif`;
  geometric `'Futura', 'Gill Sans', sans-serif`; reading `'Charter', Georgia,
  serif`; mono `'SF Mono', Menlo, monospace`. Never the banned list.
- **Scale**: dramatic, not polite — hero `clamp(2.5rem, 6vw, 4rem)`, display
  tracking `-0.02em`, heading line-height 1.1–1.3, body 1.6–1.8.
- **Color**: one specific, justified hex per role — never a framework
  default. If a brief calls for purple or blue, adopt the theme that owns
  that hue (`linear-app` for indigo) and style strictly with its tokens.

## Motion

One page-load choreography at most: ≤600 ms total, ≤6 staggered elements
(`animation-delay` steps of 60–80 ms). Transitions 150–250 ms on the theme's
`--ease-standard`. Scroll-driven motion only when the theme's recipe or the
brief asks for it, in CSS (`animation-timeline: view()`) — never opacity
reveals on everything. Wrap it all in
`@media (prefers-reduced-motion: no-preference)`; reduced motion keeps
opacity changes and drops transforms.

## Wireframe exception

With the wireframe theme, ignore color and type craft entirely — structure
only: grey boxes, real content hierarchy, placeholder blocks with real
labels, system-ui is fine. Do not dress a wireframe; every style decision you
add is one the user must argue out before a real theme can land.
