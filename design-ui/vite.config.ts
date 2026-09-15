import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The chat rail — thread, picker, composer — is arch-ui's, imported as it is
// through the `@arch` alias rather than copied. `dedupe` keeps one React
// between the two trees; `fs.allow` lets the dev server read across.
const archSrc = fileURLToPath(new URL("../arch-ui/src", import.meta.url));
const repoRoot = fileURLToPath(new URL("..", import.meta.url));

// `npm run dev` against a live session: bird design prints its port, set BIRD_URL
const harness = process.env.BIRD_URL || "http://127.0.0.1:8000";
const proxy = Object.fromEntries(
  ["/events", "/input", "/mutate", "/answer", "/board", "/permission", "/interrupt"]
    .map((path) => [path, { target: harness, changeOrigin: true }]),
);

// Build output lands directly in the harness's STATIC_DIR, so no Python
// changes and `bird design` keeps working for anyone without Node installed.
export default defineConfig({
  plugins: [react()],
  base: "./",
  resolve: {
    alias: { "@arch": archSrc },
    dedupe: ["react", "react-dom"],
  },
  server: {
    fs: { allow: [repoRoot] },
    proxy,
  },
  build: {
    outDir: "../src/bird/harnesses/design/static",
    emptyOutDir: true,
    sourcemap: false,
  },
});
