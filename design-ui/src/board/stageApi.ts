/** The stage's camera, reachable from the rest of the app.
 *
 *  A "show me" line in the chat has to move the viewport onto the artboard it
 *  is talking about, and putting the rail away has to slide the world so you
 *  keep your place. The Stage owns pan and zoom — 60fps imperative work, kept
 *  out of React state on purpose — so it registers the verbs everything else
 *  is allowed to call. */

export interface StageApi {
  /** bring these artboards on screen — or the whole board, when null */
  reveal: (ids: string[] | null) => void;
  /** slide the world sideways, for when the rail takes or gives back width */
  nudgeX: (dx: number, ms?: number) => void;
}

let api: StageApi | null = null;
export const setStageApi = (next: StageApi | null) => { api = next; };
export const reveal: StageApi["reveal"] = (ids) => api?.reveal(ids);
export const nudgeX: StageApi["nudgeX"] = (dx, ms) => api?.nudgeX(dx, ms);
