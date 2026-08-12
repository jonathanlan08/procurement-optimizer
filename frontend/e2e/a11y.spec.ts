/** Accessibility automation (2026-08 audit remediation, item 5), against the
 * real backend + seeded dataset like every other spec in this suite: an
 * axe-core scan of the same seven surfaces a human accessibility reviewer
 * would check first, as the analyst role at desktop size, asserting zero
 * `wcag2a`/`wcag2aa` violations on each. Chromium only (playwright.config.ts
 * — axe-core's rule engine is the thing under test here, not a rendering
 * difference between browser engines, so this stays out of the
 * firefox/webkit smoke subset).
 *
 * Each page is driven into a settled, representative state first (same
 * navigation idioms as workflow.spec.ts/reports_audit.spec.ts) rather than
 * scanned mid-loading-skeleton or mid opacity-transition: a query still
 * `isFetching` (Reports' own "Refresh" button, disabled+50%-opacity while
 * fetching per workspace.css's `:disabled { opacity: 0.5 }`) or a
 * `disabled`-until-data-loads control (Compare's "Calculate" button,
 * disabled while `columns.length === 0`) genuinely fails color-contrast for
 * that instant — a real static analysis should read the settled UI a user
 * actually acts on, not a sub-150ms transition frame, so every test below
 * waits for its page's own real "loaded" signal (a table, or a specific
 * enabled control) before scanning. `reducedMotion: "reduce"` on top
 * collapses that same opacity transition to near-zero (tokens.css's own
 * `prefers-reduced-motion` block, `transition-duration: 0.01ms`) as a second
 * layer of protection against being caught mid-transition.
 */

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { ANALYST_STORAGE } from "./helpers/roles";
import { selectByLabel, selectOptionContaining } from "./helpers/select";

test.use({
  storageState: ANALYST_STORAGE,
  viewport: { width: 1280, height: 800 },
  reducedMotion: "reduce",
});

async function expectNoViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
}

/** `reducedMotion: "reduce"` above (tokens.css's own `prefers-reduced-motion`
 * block) collapses workspace.css's `:disabled -> enabled` opacity transition
 * to 0.01ms, but a freshly-`toBeEnabled()` button can still be observed
 * mid-transition for a few ms after that assertion resolves (proven
 * empirically: axe once caught the "Calculate" button at computed opacity
 * ~0.85, producing a real but entirely transient sub-4.5:1 reading — not a
 * static markup defect). Polling the element's own computed `opacity` to
 * `"1"` waits for the actual rendered, settled state deterministically,
 * instead of guessing at a fixed delay. */
async function waitForOpaque(locator: Locator): Promise<void> {
  await expect
    .poll(() => locator.evaluate((el) => getComputedStyle(el).opacity))
    .toBe("1");
}

test.describe("accessibility (wcag2a/wcag2aa, zero violations)", () => {
  test("Overview", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
    await expectNoViolations(page);
  });

  test("Suppliers", async ({ page }) => {
    await page.goto("/suppliers");
    await expect(page.getByRole("table")).toBeVisible();
    await expectNoViolations(page);
  });

  test("Parts", async ({ page }) => {
    await page.goto("/parts");
    await expect(page.getByRole("table")).toBeVisible();
    await expectNoViolations(page);
  });

  test("RFQs", async ({ page }) => {
    await page.goto("/rfqs");
    await expect(page.getByRole("table")).toBeVisible();
    await expectNoViolations(page);
  });

  test("Compare (RFQ selected)", async ({ page }) => {
    await page.goto("/scenarios");
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
    await selectOptionContaining(selectByLabel(page, "RFQ"), "RFQ-2026-ENC-PILOT");
    await expect(page.getByRole("heading", { name: "Scenario history" })).toBeVisible();
    // the comparison table only renders once `columns` (matched quotes) has
    // loaded — the same signal that flips the "Calculate" button's own
    // `disabled={columns.length === 0}` off, so waiting for it here means
    // the button is scanned in its real, enabled resting state.
    await expect(page.getByRole("table")).toBeVisible();
    const calculateButton = page.getByRole("button", { name: "Calculate" });
    await expect(calculateButton).toBeEnabled();
    await waitForOpaque(calculateButton);
    await expectNoViolations(page);
  });

  test("Reports", async ({ page }) => {
    await page.goto("/reports");
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
    await expect(page.getByRole("table")).toBeVisible();
    // settled past the initial `reportsQuery.isFetching` (ReportsPage.tsx's
    // own "Refresh"/"Refreshing…" label swap) rather than mid-fetch.
    const refreshButton = page.getByRole("button", { name: "Refresh", exact: true });
    await expect(refreshButton).toBeEnabled();
    await waitForOpaque(refreshButton);
    await expectNoViolations(page);
  });

  test("Audit", async ({ page }) => {
    await page.goto("/audit");
    await expect(page.getByRole("table")).toBeVisible();
    await expectNoViolations(page);
  });
});
