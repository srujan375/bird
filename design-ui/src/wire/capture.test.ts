import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { markFrameLoading, markFrameReady, registerFrame, resetFrames } from "../board/frames";
import { captured, onCaptureRequest, resetCapture, watchCapture, type CapturePhase } from "./capture";

/* The capture round trip's handshake. The harness asks for the screenshot on
 * the same push that hands the page the artboard's document, and a message
 * posted into a frame that is still loading is lost without a trace — which
 * is how half of every session's automatic critiques used to time out at
 * exactly the page deadline. The request now waits for the frame's load. */

const frame = () => {
  const postMessage = vi.fn();
  return { el: { contentWindow: { postMessage } } as unknown as HTMLIFrameElement, postMessage };
};
const posted = () => (fetch as unknown as Mock).mock.calls.map(([, init]) => JSON.parse((init as RequestInit).body as string));

beforeEach(() => {
  resetFrames();
  resetCapture();
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true })));
});
afterEach(() => {
  watchCapture(null);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the capture handshake", () => {
  it("holds a request for a loading frame and posts it exactly once on load", () => {
    const f = frame();
    registerFrame("hero", f.el);
    markFrameLoading("hero");
    onCaptureRequest({ id: "cap-1", artboard: "hero", deadline: 10 });
    expect(f.postMessage).not.toHaveBeenCalled();
    markFrameReady("hero");
    expect(f.postMessage).toHaveBeenCalledTimes(1);
    expect(f.postMessage).toHaveBeenCalledWith({ type: "capture", id: "cap-1" }, "*");
    markFrameReady("hero");
    expect(f.postMessage).toHaveBeenCalledTimes(1);
  });

  it("holds a request for a frame that is not even mounted yet", () => {
    onCaptureRequest({ id: "cap-1", artboard: "hero", deadline: 10 });
    const f = frame();
    registerFrame("hero", f.el);
    markFrameReady("hero");
    expect(f.postMessage).toHaveBeenCalledWith({ type: "capture", id: "cap-1" }, "*");
  });

  it("never posts a request that was superseded while it waited", () => {
    const f = frame();
    registerFrame("hero", f.el);
    markFrameLoading("hero");
    onCaptureRequest({ id: "cap-1", artboard: "hero", deadline: 10 });
    onCaptureRequest({ id: "cap-2", artboard: "hero", deadline: 10 });
    expect(posted()).toEqual([{ id: "cap-1", error: "superseded" }]);
    markFrameReady("hero");
    expect(f.postMessage).toHaveBeenCalledTimes(1);
    expect(f.postMessage).toHaveBeenCalledWith({ type: "capture", id: "cap-2" }, "*");
  });

  it("posts straight away into a frame that has loaded", () => {
    const f = frame();
    registerFrame("hero", f.el);
    markFrameReady("hero");
    onCaptureRequest({ id: "cap-1", artboard: "hero", deadline: 10 });
    expect(f.postMessage).toHaveBeenCalledWith({ type: "capture", id: "cap-1" }, "*");
  });

  it("a new document makes the frame not-ready again until it loads", () => {
    const f = frame();
    registerFrame("hero", f.el);
    markFrameReady("hero");
    markFrameLoading("hero"); // a rebuild's srcdoc is being set
    onCaptureRequest({ id: "cap-3", artboard: "hero", deadline: 10 });
    expect(f.postMessage).not.toHaveBeenCalled();
    markFrameReady("hero");
    expect(f.postMessage).toHaveBeenCalledWith({ type: "capture", id: "cap-3" }, "*");
  });

  it("the deadline still answers for a frame that never loads, and a late load posts nothing", () => {
    const f = frame();
    registerFrame("hero", f.el);
    markFrameLoading("hero");
    onCaptureRequest({ id: "cap-1", artboard: "hero", deadline: 1 });
    vi.advanceTimersByTime(1000);
    expect(posted()).toEqual([{ id: "cap-1", error: "the page did not raster the artboard in time" }]);
    markFrameReady("hero");
    expect(f.postMessage).not.toHaveBeenCalled();
  });

  it("the bridge's png settles the request and the watcher hears each phase", () => {
    const phases: CapturePhase[] = [];
    watchCapture((phase) => { phases.push(phase); });
    const f = frame();
    registerFrame("hero", f.el);
    markFrameReady("hero");
    onCaptureRequest({ id: "cap-1", artboard: "hero", deadline: 10 });
    captured("hero", { id: "cap-1", png: "data:image/png;base64,xx" });
    expect(posted()).toEqual([{ id: "cap-1", png: "data:image/png;base64,xx" }]);
    expect(phases).toEqual(["requested", "captured"]);
  });
});
