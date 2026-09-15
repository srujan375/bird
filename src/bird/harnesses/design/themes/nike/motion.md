# Nike — motion

Bold and athletic: big, fast moves on imagery and type; nothing timid.

- Hovers: 150 ms (`var(--motion-fast)`) on `var(--ease-standard)`; product images scale 1→1.06 over 200 ms (`var(--motion-base)`).
- Page load: hero type slides in from the left, translateX -40px→0 and opacity, 500 ms, stagger 60 ms, ≤4 lines.
- Scroll-driven hero: image scale 1.1→1 across the first viewport via `animation-timeline: scroll()`.
- Full-bleed bands: sticky image with copy scrolling over it.
- Buttons: press scale 0.97, no color fade longer than 150 ms.
- No soft fades on everything; motion is either decisive or absent.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
