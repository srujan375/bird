import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output lands directly in the harness's STATIC_DIR, so no Python changes
// and `ox arch` keeps working for anyone without Node installed.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../src/ox/harnesses/arch/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    // `npm run dev` against a live session: ox arch prints its port, set OX_URL
    proxy: {
      "/events": { target: process.env.OX_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/input": { target: process.env.OX_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/permission": { target: process.env.OX_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/interrupt": { target: process.env.OX_URL || "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
