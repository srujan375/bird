import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { connect } from "./wire/session";
import "./styles/board.css";
import "./styles/live.css";

/* A saved board with no harness behind it: `?fixture=<name>` in dev only.
   The branch is static so the loader is dropped from the production bundle. */
const fixture = import.meta.env.DEV
  ? new URLSearchParams(location.search).get("fixture")
  : null;
if (fixture) void import("./dev/fixture").then((m) => m.loadFixture(fixture));
else connect();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
