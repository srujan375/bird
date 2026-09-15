# Notion — motion

Nearly still: the page is a document, and documents do not animate.

- Hovers: background tint 150 ms (`var(--motion-fast)`), `var(--ease-standard)`.
- Popovers/menus: opacity 0→1 in 200 ms (`var(--motion-base)`), no translate.
- Page load: none. Content is simply there.
- Toggles/disclosures: height/opacity 200 ms, one at a time.
- No scroll-driven motion, no parallax, no hero choreography.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
