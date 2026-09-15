# Swiss minimal — motion

Almost none: the grid does the work; state changes are instant or nearly so.

- Hovers: 120 ms opacity or color change, linear or ease-out; no transforms.
- Page load: none, or a single 300 ms opacity fade of the whole page.
- Navigation states: swap instantly.
- No scroll-driven motion, no parallax, no staggered entrances.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
