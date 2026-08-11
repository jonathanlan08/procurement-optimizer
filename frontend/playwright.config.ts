/** Playwright E2E configuration — runs the full product workflow against a
 * REAL backend (real PostgreSQL, real FastAPI routes) with the seeded
 * synthetic Meridian demo dataset. No paid providers anywhere: the backend's
 * mock extraction provider and synthetic FX carry every "AI"/rate-lookup
 * surface these specs touch — see backend/scripts/seed_demo.py.
 *
 * Process lifecycle (DB boot -> migrate -> seed -> backend -> frontend) is
 * owned by scripts/e2e_local.sh, not by Playwright's `webServer` option —
 * the backend needs an ephemeral, freshly-seeded PostgreSQL per run (via
 * `pgserver`) so the suite is safe to run twice in a row and prove there is
 * no order-dependence, which a single long-lived `webServer` can't express.
 * `E2E_BASE_URL` is exported by that script once both processes are healthy.
 *
 * chromium only, single worker (serial): the seeded dataset and the
 * backend's in-memory rate limiter are shared, mutable state across every
 * spec in this suite (extraction runs move through review states, scenarios
 * accumulate briefs/reports) — parallel workers would race each other.
 */

import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env["E2E_BASE_URL"] ?? "http://localhost:5173";

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: 1,
  timeout: 90_000,
  expect: {
    timeout: 10_000,
  },
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],
  outputDir: "test-results",
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
