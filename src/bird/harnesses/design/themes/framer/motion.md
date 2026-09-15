# Framer — motion

Confident and kinetic, but disciplined: a few large moves, not many small ones.

- Hovers: 150 ms (`var(--motion-fast)`) on `var(--ease-standard)`; cards lift translateY(-4px).
- Page load: hero headline and CTA fade-up 600 ms, translateY 24px→0, stagger 80 ms, ≤6 elements.
- Section reveal: one per section, opacity 0→1 and translateY 32px→0 via `animation-timeline: view(); animation-range: entry 0% entry 35%`.
- Marquees: linear translateX loop at ≥20 s per cycle, pause on hover.
- Sticky feature panels are allowed; parallax ≤ 6%.
- No bounce; ease-out only.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
