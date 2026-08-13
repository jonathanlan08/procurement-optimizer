/** The core product journey, against the real backend and the seeded
 * "Enclosure Pilot Build" RFQ (RFQ-2026-ENC-PILOT - see
 * backend/src/app/seed/demo_dataset.py): scenario comparison workspace ->
 * a feasible (optimal) allocation's honest status banner + split table ->
 * an engineered-infeasible allocation's explainer with a relaxation
 * callout -> generating a negotiation brief for a scored supplier ->
 * provenance badges + the "never sends emails" callout -> marking it
 * reviewed and watching the state chip update.
 */

import { expect, test } from "@playwright/test";
import { ANALYST_STORAGE } from "./helpers/roles";
import { selectByLabel, selectOptionContaining } from "./helpers/select";

test.use({ storageState: ANALYST_STORAGE });

test("comparison workspace: feasible allocation, infeasible explainer, and a reviewed negotiation brief", async ({
  page,
}) => {
  await page.goto("/scenarios");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  await selectOptionContaining(selectByLabel(page, "RFQ"), "RFQ-2026-ENC-PILOT");
  await expect(page.getByRole("heading", { name: "Scenario history" })).toBeVisible();

  const scenarioRow = (nameSubstring: string) =>
    page.getByRole("button", { name: nameSubstring });

  // -- feasible scenario: honest "optimal" status + an allocation table
  // that splits the order across the RFQ's suppliers -----------------
  await scenarioRow("Enclosure Pilot - Lowest Landed Cost").click();

  // deterministic solve (fixed seed, canonical ordering, single search
  // worker - docs/OPTIMIZATION.md) over the seeded quotes: this scenario
  // reliably reports a PROVEN-optimal allocation, never a bare "feasible"
  // one that only "found something" - the honest status banner text below
  // is the backend's own composed prose, asserted verbatim.
  const statusBanner = page.getByRole("status").filter({ hasText: "Optimal" });
  await expect(statusBanner).toBeVisible();
  await expect(statusBanner).toContainText(
    "A provably optimal allocation was found within the deterministic search budget.",
  );

  await expect(page.getByRole("columnheader", { name: "Supplier" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Quantity" })).toBeVisible();
  const allocationRowCount = await page.locator(".allocation-table tbody tr").count();
  expect(allocationRowCount).toBeGreaterThan(0);

  // -- infeasible scenario (engineered: a 500 USD budget ceiling cannot
  // cover the pilot's required quantities at any quoted price) -------
  await scenarioRow("Enclosure Pilot - Budget Ceiling (Infeasible Demo)").click();

  const infeasibleBanner = page.getByRole("status").filter({ hasText: /infeasible/i });
  await expect(infeasibleBanner).toBeVisible();
  await expect(page.getByText(/is below the cheapest possible allocation/)).toBeVisible();
  await expect(page.getByText("Suggested relaxation")).toBeVisible();
  await expect(page.getByText(/restores feasibility/)).toBeVisible();
  // the conflicting-constraint-group chip
  await expect(page.getByText("Budget", { exact: true })).toBeVisible();
  // infeasible has no allocation table to show
  await expect(page.locator(".allocation-table")).toHaveCount(0);

  // -- back to the feasible scenario to generate a negotiation brief for
  // its top-ranked, scored supplier -----------------------------------
  await scenarioRow("Enclosure Pilot - Lowest Landed Cost").click();
  await expect(statusBanner).toBeVisible();

  await expect(page.getByRole("heading", { name: "Negotiation briefs" })).toBeVisible();
  // this button toggles the generate form open; it is the only element
  // with this accessible name until the form (with its own submit button
  // of the same name) is revealed just below.
  await page.getByRole("button", { name: "Generate brief" }).click();
  const briefForm = page.locator(".brief-generate-form");
  await selectOptionContaining(selectByLabel(briefForm, "Supplier"), "Cascade Precision");
  await briefForm.getByLabel("Objective").fill("Reduce unit price while holding the quoted lead time.");
  await briefForm.getByRole("button", { name: "Generate brief" }).click();

  // the brief drawer opens automatically once the brief is created
  const briefDrawer = page.getByRole("dialog", { name: "Negotiation brief" });
  await expect(briefDrawer).toBeVisible();

  // per-section provenance badges: at minimum a supplier-provided figure,
  // a calculated figure, and an AI-narrative section should each appear
  // (several sections share each provenance label, so `.first()` - this
  // is only asserting presence of the badge kind, not counting them)
  await expect(briefDrawer.getByText("Supplier-provided").first()).toBeVisible();
  await expect(briefDrawer.getByText("Calculated").first()).toBeVisible();
  await expect(briefDrawer.getByText("AI narrative").first()).toBeVisible();

  // the mandated "this system never sends emails" callout
  await expect(briefDrawer.getByText(/this system never sends emails/i)).toBeVisible();
  await expect(briefDrawer.getByText(/Template narrative/i)).toBeVisible();

  // draft state, requires review
  await expect(briefDrawer.getByText("Draft", { exact: true })).toBeVisible();
  await expect(briefDrawer.getByText("Draft - requires human review")).toBeVisible();

  // -- mark reviewed: the state chip updates in place ------------------
  await briefDrawer.getByRole("button", { name: "Mark reviewed" }).click();
  await expect(briefDrawer.getByText("Human reviewed")).toBeVisible();
  await expect(briefDrawer.getByText(/^Reviewed /)).toBeVisible();
  // the review form is gone now that the brief is no longer a draft
  await expect(briefDrawer.getByRole("button", { name: "Mark reviewed" })).toHaveCount(0);

  await briefDrawer.getByRole("button", { name: "Close panel" }).click();
  await expect(briefDrawer).toBeHidden();
  // the list row behind the drawer also reflects the new state
  await expect(page.getByText("Human reviewed").first()).toBeVisible();
});
