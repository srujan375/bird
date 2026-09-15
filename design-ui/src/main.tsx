import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { startFrameBus } from "./board/frameBus";
import { connect } from "./wire/session";
import "@arch/styles/board.css";
import "@arch/styles/live.css";
import "./styles/design.css";

/* A scripted session with no harness behind it: `?fixture=<name>` in dev only.
   The branch is static so the loader is dropped from the production bundle. */
const fixture = import.meta.env.DEV
  ? new URLSearchParams(location.search).get("fixture")
  : null;
/* the frames' replies — captures, forwarded keys — are heard for the life of
   the page, whether or not the board is mounted to hear them */
startFrameBus();
if (fixture) void import("./dev/fixture").then((m) => m.loadFixture(fixture));
else connect();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
