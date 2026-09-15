# GitHub — motion

Utilitarian and fast: state changes confirm, they never perform.

- Hovers: 80 ms (`var(--motion-fast)`), `var(--ease-standard)` (ease-out).
- Dropdowns/popovers: opacity 0→1 in 200 ms (`var(--motion-base)`), no translate.
- Page load: none.
- Progress/loading: linear indeterminate bar or a 1 s rotating spinner, nothing else.
- Sticky: header only.
- No scroll-driven motion, no parallax, no hero choreography.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
