# Dark atmospheric — motion

Slow and ambient: depth and glow, long durations, nothing abrupt.

- Page load: hero fade-in 800 ms, ease-out, with a glow/blur element scaling 0.9→1 over the same window; stagger 100 ms, ≤4 elements.
- Hovers: glow intensity or border color 250 ms, ease-out; no lifts.
- Ambient: one background element may drift (translate ≤ 2%, 20–30 s loop, ease-in-out), never more than one.
- Scroll-driven depth: background layer translateY ≤ 10% via `animation-timeline: scroll()`; foreground stays put.
- Section reveal: opacity 0→1 only, 600 ms, via `animation-timeline: view()`, one per section.
- No fast snaps, no bounce.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
