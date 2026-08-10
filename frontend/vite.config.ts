/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Backend port: 8000 by default; override with BACKEND_PORT when something else
// occupies it (export BACKEND_PORT=8001 before `npm run dev`).
const backendPort = process.env["BACKEND_PORT"] ?? "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: `http://localhost:${backendPort}`,
        changeOrigin: false,
      },
    },
  },
  build: {
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["src/test/setup.ts"],
  },
});
