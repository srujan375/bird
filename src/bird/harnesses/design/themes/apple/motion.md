# Apple — motion

Cinematic and weighted: everything decelerates hard and settles, never bounces or springs past its target.

- Page load: hero fade-up (opacity 0→1, translateY 16px→0) 600 ms on `var(--ease-standard)` (cubic-bezier(0.28, 0, 0.22, 1)), stagger 80 ms across ≤6 elements.
- Hover/focus tints: `var(--motion-fast)` (150 ms); layout-affecting states: `var(--motion-base)` (220 ms).
- Product sections: `position: sticky; top: 0` panels that pin while copy scrolls past.
- Scroll-driven reveal: `animation-timeline: view(); animation-range: entry 0% entry 40%` on opacity 0→1 and scale 0.96→1 — one reveal per section, never per element.
- Parallax: background media translateY ≤ 8% over the section's scroll range, via `animation-timeline: scroll()`.
- Image/device zooms: scale 1→1.04 over a full viewport of scroll, no faster.
- No bounce, no overshoot, no spring easing anywhere.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
