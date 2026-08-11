/** Real file upload through the UI: a freshly generated CSV quote document
 * (see helpers/fixtures.ts for why it's generated per run rather than
 * reusing one of the four committed golden fixtures) attached to the
 * seeded draft RFQ "Legacy Gasket Buy" (RFQ-2026-LEGACY-GASKET — has no
 * documents of its own in the seed, so this spec owns a clean slate), then
 * extraction started on it — no golden fixture exists for this file's
 * sha256, so the mock provider's regex heuristic handles it
 * (app/providers/extraction/mock.py), which is itself part of what "no paid
 * providers anywhere" means in practice.
 */

import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { generateUploadCsvFixture } from "./helpers/fixtures";
import { ANALYST_STORAGE } from "./helpers/roles";

test.use({ storageState: ANALYST_STORAGE });

test("upload a real CSV quote document through the UI and start extraction", async ({ page }) => {
  const fixture = generateUploadCsvFixture();

  await page.goto("/rfqs");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  await page.getByRole("row", { name: /RFQ-2026-LEGACY-GASKET/ }).click();
  const rfqDrawer = page.getByRole("dialog", { name: "RFQ detail" });
  await expect(rfqDrawer).toBeVisible();

  const documentsSection = rfqDrawer.locator('section.detail-section:has(h3:text-is("Quote documents"))');
  await expect(documentsSection.getByText("No documents uploaded yet.")).toBeVisible();

  await documentsSection.getByRole("button", { name: "Upload document" }).click();
  await documentsSection.getByLabel("File").setInputFiles(fixture.path);
  await documentsSection.getByRole("button", { name: "Upload", exact: true }).click();

  const fileName = path.basename(fixture.path);
  const uploadedRow = documentsSection.locator("li.document-row", { hasText: fileName });
  await expect(uploadedRow).toBeVisible();
  await expect(uploadedRow).toContainText("CSV");
  await expect(uploadedRow).toContainText("Stored");

  await uploadedRow.getByRole("button", { name: "Extract" }).click();

  // `POST /quote-documents/{id}/extraction-runs` runs synchronously (no job
  // queue in this codebase — extraction_service.py's own module docstring),
  // so this opens the review panel directly once the run completes.
  const reviewDialog = page.getByRole("dialog", { name: "Extraction review" });
  await expect(reviewDialog).toBeVisible();

  await expect(reviewDialog.getByText("mock", { exact: false })).toBeVisible();
  await expect(reviewDialog.getByText("Simulated", { exact: true })).toBeVisible();
  // never "failed" — an unrecognized document still gets a (low-confidence)
  // heuristic pass rather than erroring outright
  await expect(reviewDialog.getByText("Failed", { exact: true })).toHaveCount(0);

  await reviewDialog.getByRole("button", { name: "Close" }).click();
  await expect(reviewDialog).toHaveCount(0);

  // back on the RFQ drawer, this document now offers "Review extraction"
  // instead of only "Extract"/"Re-extract"
  await expect(uploadedRow.getByRole("button", { name: "Review extraction" })).toBeVisible();

  fs.rmSync(path.dirname(fixture.path), { recursive: true, force: true });
});
