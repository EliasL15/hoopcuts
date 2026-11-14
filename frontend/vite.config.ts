import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8001",
      "/config": "http://127.0.0.1:8001",
      "/health": "http://127.0.0.1:8001",
      "/internal": "http://127.0.0.1:8001",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    manifest: true,
  },
});
