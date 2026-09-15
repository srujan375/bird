/**
 * Keys, from wherever they were pressed.
 *
 * Clicking into an artboard to select an element puts keyboard focus inside
 * that sandboxed iframe, and from then on the page's own keydown listener
 * hears nothing: the very action that makes a selection disabled the key
 * that clears it. The bridge forwards a small allowlist as `key` messages,
 * and the app registers one handler that both the window listener and those
 * messages go through.
 */

export interface KeyPress {
  key: string;
  meta: boolean;
  ctrl: boolean;
  alt: boolean;
  shift: boolean;
  /** the press happened in a text field — shortcuts must stand down */
  typing: boolean;
  preventDefault: () => void;
}

let handler: ((k: KeyPress) => void) | null = null;

export const setKeyHandler = (fn: ((k: KeyPress) => void) | null) => { handler = fn; };

/** A press forwarded by a frame's bridge. Nothing to prevent: the frame's
 *  own default has already run or been stopped over there. */
export function dispatchFrameKey(msg: { key?: unknown; meta?: unknown; ctrl?: unknown; shift?: unknown }): void {
  handler?.({
    key: String(msg.key ?? ""),
    meta: Boolean(msg.meta),
    ctrl: Boolean(msg.ctrl),
    alt: false,
    shift: Boolean(msg.shift),
    typing: false,
    preventDefault: () => { /* the frame's event is over */ },
  });
}

export function dispatchWindowKey(e: KeyboardEvent): void {
  const t = e.target as HTMLElement | null;
  handler?.({
    key: e.key,
    meta: e.metaKey,
    ctrl: e.ctrlKey,
    alt: e.altKey,
    shift: e.shiftKey,
    typing: Boolean(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)),
    preventDefault: () => e.preventDefault(),
  });
}
