/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { configDefaults } from "vitest/config";

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
    // e2e/** holds Playwright specs (frontend/playwright.config.ts), which
    // import `test`/`expect` from @playwright/test rather than vitest —
    // without this exclusion vitest's default include glob (**/*.spec.ts)
    // also picks them up and fails them, since @playwright/test's `test`
    // registry only runs under the Playwright test runner.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
