# Figma — motion

Snappy and playful in the details, still at the page level.

- Hovers: 150 ms (`var(--motion-fast)`) on `var(--ease-standard)`; tool buttons scale 1→1.05 on hover.
- Appear: opacity plus translateY 4px→0 in 200 ms (`var(--motion-base)`).
- Page load: hero words or shapes stagger in 60 ms apart, 500 ms total, ≤6 elements; one accent shape may rotate 0→8deg over the same window.
- Cursors/avatars: translate 200 ms ease-out as they move.
- Scroll-driven: one product-shot scale 0.98→1 via `animation-timeline: view()`; nothing else.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
