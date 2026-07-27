/**
 * Light or dark, chosen — never inherited from the OS.
 *
 * The canvas is a diagram before it is an app surface, and a diagram is read,
 * screenshotted and pasted into documents far more often than it is stared at.
 * Light is the mode the tokens were designed in, so light is the default; the
 * choice sticks across sessions because it is a preference, not session state.
 */
import { create } from "zustand";

export type ThemeName = "light" | "dark";

const KEY = "ox_arch_theme";

function initial(): ThemeName {
  try {
    return localStorage.getItem(KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

function apply(theme: ThemeName): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* private mode — the page still works, it just won't remember */
  }
}

interface ThemeState {
  theme: ThemeName;
  setTheme: (t: ThemeName) => void;
  toggle: () => void;
}

export const useTheme = create<ThemeState>((set, get) => ({
  theme: initial(),
  setTheme: (theme) => {
    apply(theme); // before the re-render, so palette() reads the new values
    set({ theme });
  },
  toggle: () => get().setTheme(get().theme === "dark" ? "light" : "dark"),
}));

apply(useTheme.getState().theme);

export interface Palette {
  edge: string;
  edgeBack: string;
  changed: string;
  sketchLine: string;
}

/**
 * Concrete colours for the SVG bits that cannot take a `var()`.
 *
 * An arrowhead is a `<marker>` whose colour arrives as a presentation
 * attribute, and those do not resolve custom properties — an arrow painted
 * `var(--edge)` is an arrow painted nothing at all.
 */
export function palette(): Palette {
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    edge: v("--edge", "#6b7280"),
    edgeBack: v("--edge-back", "#7c5cd6"),
    changed: v("--changed", "#c98a2e"),
    sketchLine: v("--sketch-line", "#9aa0aa"),
  };
}
