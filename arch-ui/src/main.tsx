import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { useSession } from "./store/session";
import type { WireEvent } from "./types";
import "./theme.css";

/**
 * One SSE connection for the life of the page. The server replays late joiners
 * (ready, a bounded transcript buffer, the latest arch_state, any pending
 * gate), so a refresh mid-session rebuilds everything without special-casing.
 */
function connect(): void {
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    let ev: WireEvent;
    try {
      ev = JSON.parse(e.data) as WireEvent;
    } catch {
      return; // a malformed frame must never take the page down
    }
    useSession.getState().apply(ev);
  };
  es.onerror = () => {
    es.close();
    useSession.getState().disconnect();
  };
}

connect();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
