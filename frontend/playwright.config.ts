/** Playwright E2E configuration - runs the full product workflow against a
 * REAL backend (real PostgreSQL, real FastAPI routes) with the seeded
 * synthetic Meridian demo dataset. No paid providers anywhere: the backend's
 * mock extraction provider and synthetic FX carry every "AI"/rate-lookup
 * surface these specs touch - see backend/scripts/seed_demo.py.
 *
 * Process lifecycle (DB boot -> migrate -> seed -> backend -> frontend) is
 * owned by scripts/e2e_local.sh, not by Playwright's `webServer` option -
 * the backend needs an ephemeral, freshly-seeded PostgreSQL per run (via
 * `pgserver`) so the suite is safe to run twice in a row and prove there is
 * no order-dependence, which a single long-lived `webServer` can't express.
 * `E2E_BASE_URL` is exported by that script once both processes are healthy.
 *
 * Single worker (serial) across EVERY project: the seeded dataset and the
 * backend's in-memory rate limiter are shared, mutable state across every
 * spec in this suite (extraction runs move through review states, scenarios
 * accumulate briefs/reports) - parallel workers would race each other, and
 * that holds just as much between two projects' copies of the same spec as
 * between two different specs in one project.
 *
 * Browser matrix (2026-08 audit remediation, item 5 - "Add WebKit, Firefox,
 * mobile emulation, accessibility automation"):
 *  - `chromium` runs the FULL suite (every `*.spec.ts` under `./e2e`,
 *    including this file's own `mobile.spec.ts`/`a11y.spec.ts`) - the
 *    primary, fully-covered target.
 *  - `firefox`/`webkit` run a SMOKE SUBSET ONLY, via `testMatch`: just
 *    auth.spec.ts (login/logout, the role-gated read-only workspace) and
 *    workflow.spec.ts (the core comparison-workspace -> brief journey).
 *    Running the full ~9-spec suite three times over would triple CI wall
 *    time for marginal extra signal - these two files already exercise
 *    login, routing, forms, drawers, and API-driven rendering, which is
 *    where a real cross-engine rendering/JS difference would show up; the
 *    narrower flows (uploads, reports downloads, audit cursor pagination)
 *    are DOM/fetch mechanics Chromium coverage already exercises identically
 *    across engines. Each additional project still boots the seeded dataset
 *    once per project (scripts/e2e_local.sh re-seeds only between full runs,
 *    not between projects), so these two specs' own idempotency (proven by
 *    "safe to run twice in a row" above) is what makes running them a
 *    second and third time, against the same seed, safe here too.
 */

import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env["E2E_BASE_URL"] ?? "http://localhost:5173";

const SMOKE_SUBSET = [/auth\.spec\.ts$/, /workflow\.spec\.ts$/];

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
    {
      name: "firefox",
      use: { ...devices["Desktop Firefox"] },
      testMatch: SMOKE_SUBSET,
    },
    {
      name: "webkit",
      use: { ...devices["Desktop Safari"] },
      testMatch: SMOKE_SUBSET,
    },
  ],
});
