# Warm playful — motion

Friendly with a little bounce: small overshoots on interaction, calm at rest.

- Hovers: scale 1→1.03 or translateY(-2px) in 200 ms on cubic-bezier(0.34, 1.56, 0.64, 1) (slight overshoot).
- Press: scale 0.96, 120 ms ease-out.
- Page load: hero elements pop in (scale 0.94→1 plus opacity) 500 ms, stagger 70 ms, ≤6 elements.
- Icons/illustrations: one may wiggle (rotate ±3deg, 300 ms) on hover, never on a loop.
- Scroll-driven: none; sections arrive without reveals.
- Keep overshoot ≤ 4% and never on layout-affecting properties.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
