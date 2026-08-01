import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output lands directly in the harness's STATIC_DIR, so no Python changes
// and `bird arch` keeps working for anyone without Node installed.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../src/bird/harnesses/arch/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    // `npm run dev` against a live session: bird arch prints its port, set BIRD_URL
    proxy: {
      "/events": { target: process.env.BIRD_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/input": { target: process.env.BIRD_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/permission": { target: process.env.BIRD_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/interrupt": { target: process.env.BIRD_URL || "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
