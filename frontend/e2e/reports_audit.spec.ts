/** Reports + audit workspaces, against the real backend and the seeded
 * "Enclosure Pilot Build" RFQ's feasible scenario: generate a supplier-
 * comparison CSV, watch it become ready, download it and check the
 * response actually carries a file; then the audit log: filter by
 * `event_type=report.generated` and confirm the event this test just
 * created appears, and exercise cursor load-more when more than one page
 * of events exists (the seeded dataset alone produces >50 audit rows, so
 * this is expected to run for real rather than skip).
 */

import fs from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { ANALYST_STORAGE } from "./helpers/roles";
import { selectByLabel, selectOptionContaining } from "./helpers/select";

test.use({ storageState: ANALYST_STORAGE });

test("generate a supplier-comparison report and download it", async ({ page }) => {
  await page.goto("/reports");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  await selectOptionContaining(selectByLabel(page, "RFQ"), "RFQ-2026-ENC-PILOT");
  const scenarioSelect = selectByLabel(page, "Scenario");
  await expect(scenarioSelect).toBeEnabled();
  await selectOptionContaining(scenarioSelect, "Enclosure Pilot - Lowest Landed Cost");

  await selectByLabel(page, "Report type").selectOption({ label: "Supplier comparison" });
  await selectByLabel(page, "Format").selectOption({ label: "CSV" });
  await page.getByRole("button", { name: "Generate report" }).click();

  const row = page.getByRole("row").filter({ hasText: "Supplier comparison" }).first();
  // 60s, not 20s: report generation runs on the thread job runner and the
  // FIRST one in a process also pays for reportlab/openpyxl import warm-up.
  // That fits comfortably on a dev machine but exceeded 20s on a cold, shared
  // CI runner (observed as a flake on the first GitHub Actions run) - the
  // work is genuinely slow there, not stuck, so the wait is what should give.
  await expect(row).toContainText("Ready", { timeout: 60_000 });

  const downloadLink = row.getByRole("link", { name: "Download" });
  const downloadUrl = await downloadLink.getAttribute("href");
  expect(downloadUrl).toBeTruthy();

  // the download itself, through the real UI control (a plain `<a href
  // download>`, per ReportsPage.tsx's own header - no JS blob dance)
  const downloadPromise = page.waitForEvent("download");
  await downloadLink.click();
  const download = await downloadPromise;

  const downloadPath = await download.path();
  expect(downloadPath).toBeTruthy();
  const stats = await fs.stat(downloadPath as string);
  expect(stats.size).toBeGreaterThan(0);

  // the underlying HTTP response itself: same authenticated browser
  // context (the session cookie rides along), asserted directly rather
  // than racing `page.waitForResponse` against a `download`-attribute
  // click, which Chromium can route through its download manager without
  // ever surfacing a matching page-level "response" event.
  const contentResponse = await page.request.get(downloadUrl as string);
  expect(contentResponse.status()).toBe(200);
  expect(contentResponse.headers()["content-disposition"] ?? "").toContain("attachment");
  const body = await contentResponse.body();
  expect(body.byteLength).toBeGreaterThan(0);
});

test("audit log: filter by event_type=report.generated finds the event", async ({ page }) => {
  await page.goto("/audit");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  // The filter vocabulary is still the raw event key…
  await page.getByLabel("Event type").fill("report.generated");
  // …but the table now shows the humanized label ("Report generated"); the raw
  // key lives on the cell's title attribute.
  const row = page.getByRole("row").filter({ hasText: "Report generated" }).first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(row).toContainText("generated_report");
  await expect(row.getByTitle("report.generated")).toBeVisible();

  await row.click();
  const drawer = page.getByRole("dialog", { name: "Audit event" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText("report.generated")).toBeVisible();
  await expect(drawer.getByRole("heading", { name: "State change" })).toBeVisible();
});

test("audit log: cursor load-more reveals additional rows when more than one page exists", async ({
  page,
}) => {
  await page.goto("/audit");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  // unfiltered: the seed alone (org/users/suppliers/parts/BOMs/RFQs/quotes/
  // scenarios/documents/extraction/corrections) produces many more than one
  // 50-row page, but the exact count isn't this test's contract - skip
  // gracefully if a future seed ever shrinks below one page.
  await expect(page.getByRole("table")).toBeVisible();
  // wait for the first real page to actually land (skeleton-loading rows
  // are `aria-hidden` and excluded from the accessibility tree, so a
  // premature row count here would read as "0 rows, no Load more" every
  // time rather than reflecting the real, settled data).
  await expect.poll(() => page.getByRole("row").count(), { timeout: 10_000 }).toBeGreaterThan(1);
  const initialRows = await page.getByRole("row").count();
  const loadMoreButton = page.getByRole("button", { name: "Load more" });
  const hasMore = (await loadMoreButton.count()) > 0;
  test.skip(!hasMore, "fewer than one page of audit events currently exist");

  await loadMoreButton.click();
  await expect.poll(() => page.getByRole("row").count(), { timeout: 10_000 }).toBeGreaterThan(initialRows);
});
