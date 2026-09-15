# Brutalist — motion

Hard cuts: states change instantly; if something moves, it moves bluntly.

- Hovers: instant inversion (background/foreground swap), 0 ms; or a 1px hard outline appearing.
- Press: translate(2px, 2px) with the box-shadow removed, instant.
- Page load: none.
- Marquee text is allowed: linear translateX, ≥12 s per loop, no easing.
- No fades, no ease curves, no scroll reveals, no parallax.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
