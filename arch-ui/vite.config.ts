import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output lands directly in the harness's STATIC_DIR, so no Python changes
// and `mha arch` keeps working for anyone without Node installed.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../src/mha/harnesses/arch/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    // `npm run dev` against a live session: mha arch prints its port, set MHA_URL
    proxy: {
      "/events": { target: process.env.MHA_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/input": { target: process.env.MHA_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/permission": { target: process.env.MHA_URL || "http://127.0.0.1:8000", changeOrigin: true },
      "/interrupt": { target: process.env.MHA_URL || "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
