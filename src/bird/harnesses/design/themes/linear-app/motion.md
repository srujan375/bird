# Linear — motion

Fast, quiet, precise: motion confirms input and otherwise stays out of the way.

- Hovers: background/border 150 ms (`var(--motion-fast)`) on `var(--ease-standard)`.
- Press: translateY(1px) for the press duration, back on release.
- Appear (menus, modals, toasts): opacity 0→1 plus translateY 4px→0 in 200 ms (`var(--motion-base)`).
- Page load: one fade-in of the hero block, 200 ms, no stagger beyond 3 elements.
- Keyboard focus: ring appears instantly (no transition on focus-visible).
- Nothing scroll-driven; no parallax; no sticky theatrics beyond a sticky nav.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
