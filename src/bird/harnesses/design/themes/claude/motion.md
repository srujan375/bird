# Claude — motion

Warm and calm: soft fades, no snap, nothing that draws attention to itself.

- Hovers: color/background 150 ms (`var(--motion-fast)`) on `var(--ease-standard)`.
- Appear: opacity 0→1 plus translateY 6px→0 in 200 ms (`var(--motion-base)`).
- Page load: one fade-up over the hero copy, 500 ms, stagger 80 ms, ≤4 elements.
- Streaming/typing affordances: opacity pulses 1→0.4→1 at 1.2 s, ease-in-out, never scale.
- No scroll-driven motion; at most one sticky nav.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
