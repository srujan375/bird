# OpenAI — motion

Smooth and understated: ease-out-quint settles, generous but brief.

- Hovers: 150 ms (`var(--motion-fast)`); appear: 220 ms (`var(--motion-base)`) on `var(--ease-standard)` (cubic-bezier(0.16, 1, 0.3, 1)).
- Page load: hero fade-up 600 ms, translateY 12px→0, stagger 80 ms, ≤5 elements.
- Media blocks: scroll-driven opacity 0→1 via `animation-timeline: view(); animation-range: entry 0% entry 30%`, one per section.
- Cards: no lift on hover; border/background shift only.
- No parallax, no bounce.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
