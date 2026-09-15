# Airbnb — motion

Friendly and soft: rounded, gentle lifts that feel physical without being showy.

- Hovers on cards: translateY(-2px) plus a slightly larger shadow, 200 ms (`var(--motion-base)`) on `var(--ease-standard)`.
- Buttons: background 150 ms (`var(--motion-fast)`); press scale 0.98.
- Page load: hero and search bar fade-up 400 ms, stagger 60 ms, ≤4 elements.
- Image carousels: translateX slides 300 ms, ease-out; dots fade.
- Modals/sheets: slide-up from bottom 300 ms with backdrop fade.
- Scroll-driven: none — content sections simply exist; a sticky filter bar is fine.
- Respect prefers-reduced-motion: reduce → disable transforms, keep opacity.
