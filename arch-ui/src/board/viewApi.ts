/** The board's camera, reachable from the rest of the app.
 *
 *  A "show me" line in the chat has to move the viewport onto the boxes it is
 *  talking about, and putting the rail away has to slide the world so you keep
 *  your place. The Board owns pan and zoom — it is 60fps imperative work, kept
 *  out of React state on purpose — so it registers the verbs everything else
 *  is allowed to call. */

export interface ViewApi {
  /** Move the camera so `ids` — or the whole board when null — is on screen. */
  frame: (ids: string[] | null, pad?: number, maxK?: number) => void;
  /** Slide the world sideways, for when the rail takes or gives back width. */
  nudgeX: (dx: number, ms?: number) => void;
  /** Snap to the opening view without animating. */
  fitNow: (pad?: number) => void;
}

let api: ViewApi | null = null;

export const setViewApi = (next: ViewApi | null) => { api = next; };

export const frame: ViewApi["frame"] = (ids, pad, maxK) => api?.frame(ids, pad, maxK);
export const nudgeX: ViewApi["nudgeX"] = (dx, ms) => api?.nudgeX(dx, ms);
export const fitNow: ViewApi["fitNow"] = (pad) => api?.fitNow(pad);
