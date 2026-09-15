# Wireframe — motion

None. A wireframe is structure; motion is a decision that has not been made yet.

- No page-load animation, no hovers beyond a cursor change, no scroll effects.
- If the brief specifies a motion behaviour, annotate it in a placeholder label (e.g. "[sticky panel — pins while copy scrolls]") instead of implementing it.
- Interactive states (open/closed) may swap instantly with no transition.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
