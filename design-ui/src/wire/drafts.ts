import { useSyncExternalStore } from "react";
import { slug, ToolArgs } from "./partial";
import type { WireArtboard } from "./types";

/**
 * The artboards being written right now.
 *
 * A draft is a `design_create` call whose arguments are still arriving. The
 * page draws it as it comes, so the board shows the designer's work while it
 * is happening rather than a spinner until it is over. When the call runs,
 * the harness pushes the real artboard and the draft retires in its favour.
 *
 * The text itself stays out of React state: a document arrives in thousands
 * of pieces, and re-rendering the board for each would be the slowest way to
 * append a character. Pieces go straight to whoever is drawing the draft
 * (`subscribe`); React only hears when a draft starts, learns its name, is
 * finished being written, or goes away.
 */

export interface Draft {
  /** unique for the life of the page: `${message}:${index}` */
  key: string;
  /** the artboard card's name, once the arguments have said it */
  title: string | null;
  /** the artboard being rebuilt, or null for a new one */
  artboard: string | null;
  /** the document has been fully written; the call has not yet run */
  closed: boolean;
  /** the version the target artboard was at when the rebuild started */
  fromVersion: string | null;
}

interface Stream {
  key: string;
  name: string;
  args: ToolArgs;
  draft: Draft | null;
}

let drafts: Draft[] = [];
/** decoded html per draft, for a drawer that arrives late (or mounts twice) */
const html = new Map<string, string>();
const listeners = new Set<() => void>();
const drawers = new Map<string, Set<(piece: string) => void>>();
const emit = () => { for (const l of listeners) l(); };

/** Drafts whose artboard has just arrived, by that artboard's id. The board
 *  keeps such a draft mounted underneath the new frame until the frame has
 *  measured itself, so the handover is a fade rather than a white card that
 *  jumps size: the draft measured the document as it was written, and the
 *  frame's first size will be the same number. */
let posters: Record<string, Draft> = {};
const posterListeners = new Set<() => void>();
const emitPosters = () => { for (const l of posterListeners) l(); };
export const getPosters = () => posters;
export function usePosters(): Record<string, Draft> {
  return useSyncExternalStore(
    (cb) => { posterListeners.add(cb); return () => posterListeners.delete(cb); },
    getPosters,
    getPosters,
  );
}
/** The frame has painted: the poster under it is done. */
export function clearPoster(artboardId: string): void {
  if (!(artboardId in posters)) return;
  const { [artboardId]: _gone, ...rest } = posters;
  posters = rest;
  emitPosters();
}

/** streams of the message being written, by tool-call index */
let streams = new Map<number, Stream>();
/** which message: tool indices restart at 0 on every assistant message */
let message = 0;

export const getDrafts = () => drafts;
export function useDrafts(): Draft[] {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    getDrafts,
    getDrafts,
  );
}

export const draftHtml = (key: string) => html.get(key) ?? "";

/** Draw this draft: called with what is already written, then with each new
 *  piece. Returns the unsubscribe. */
export function subscribe(key: string, draw: (piece: string) => void): () => void {
  let set = drawers.get(key);
  if (!set) { set = new Set(); drawers.set(key, set); }
  set.add(draw);
  return () => { set!.delete(draw); if (!set!.size) drawers.delete(key); };
}

const patch = (key: string, p: Partial<Draft>) => {
  drafts = drafts.map((d) => (d.key === key ? { ...d, ...p } : d));
  emit();
};

/** A piece of a tool call's arguments arrived. `current` is the board as the
 *  harness last pushed it, so a rebuild can remember where it started. */
export function feed(index: number, name: string, piece: string, current: WireArtboard[]): void {
  let s = streams.get(index);
  if (!s) {
    s = { key: `${message}:${index}`, name, args: new ToolArgs(), draft: null };
    streams.set(index, s);
  }
  if (name && !s.name) s.name = name;
  if (s.name !== "design_create") return;
  const added = s.args.feed(piece);
  if (!s.draft) {
    s.draft = { key: s.key, title: null, artboard: null, closed: false, fromVersion: null };
    html.set(s.key, "");
    drafts = [...drafts, s.draft];
    emit();
  }
  const d = s.draft;
  if (added) {
    html.set(d.key, html.get(d.key) + added);
    const set = drawers.get(d.key);
    if (set) for (const draw of set) draw(added);
  }
  const p: Partial<Draft> = {};
  if (s.args.title !== d.title) p.title = s.args.title;
  if (s.args.artboard !== d.artboard) {
    p.artboard = s.args.artboard;
    p.fromVersion = current.find((a) => a.id === s.args.artboard)?.current ?? null;
  }
  if (s.args.htmlClosed && !d.closed) p.closed = true;
  if (Object.keys(p).length) { s.draft = { ...d, ...p }; patch(d.key, p); }
}

/** The message is over: the next tool call's index 0 is a new call. The
 *  drafts stay until the calls run. */
export function messageDone(): void {
  message++;
  streams = new Map();
}

function drop(keys: string[]): void {
  if (!keys.length) return;
  const gone = new Set(keys);
  drafts = drafts.filter((d) => !gone.has(d.key));
  for (const k of keys) { html.delete(k); drawers.delete(k); }
  emit();
}

/** The oldest draft whose call has now run, by the result's order. */
export function retireOldest(): void {
  const first = drafts[0];
  if (first) drop([first.key]);
}

/** The harness pushed the board: any draft whose artboard has now arrived
 *  retires — a new one by the id its title slugs to, a rebuild by its target
 *  moving to a new version. A new artboard nobody's title accounts for (the
 *  harness suffixes a colliding slug) takes the oldest unclaimed new draft. */
export function retireArrived(before: WireArtboard[], after: WireArtboard[]): void {
  const was = new Map(before.map((a) => [a.id, a.current]));
  const gone: string[] = [];
  const handed: Record<string, Draft> = {};
  const fresh = after.filter((a) => !was.has(a.id)).map((a) => a.id);
  const claimed = new Set<string>();
  for (const d of drafts) {
    if (d.artboard) {
      const now = after.find((a) => a.id === d.artboard);
      if (now && now.current !== (d.fromVersion ?? was.get(d.artboard))) { gone.push(d.key); handed[d.artboard] = d; }
      continue;
    }
    if (d.title) {
      const id = fresh.find((f) => f === slug(d.title!) && !claimed.has(f));
      if (id) { claimed.add(id); gone.push(d.key); handed[id] = d; }
    }
  }
  const unclaimed = fresh.filter((f) => !claimed.has(f));
  for (const d of drafts) {
    if (!unclaimed.length) break;
    if (d.artboard || gone.includes(d.key)) continue;
    handed[unclaimed.shift()!] = d;
    gone.push(d.key);
  }
  if (Object.keys(handed).length) { posters = { ...posters, ...handed }; emitPosters(); }
  drop(gone);
}

/** The turn is over. Whatever is still being drawn was never made — an
 *  interrupted or failed call — and stays on the board only as a lie. */
export function abandon(): void {
  streams = new Map();
  drop(drafts.map((d) => d.key));
}

/** Tests and fixtures: back to nothing. */
export function resetDrafts(): void {
  abandon();
  message = 0;
  if (Object.keys(posters).length) { posters = {}; emitPosters(); }
}
