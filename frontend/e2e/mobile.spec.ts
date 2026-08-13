/** Mobile viewport smoke test (2026-08 audit remediation, item 5 - "mobile
 * emulation"), against the real backend + seeded dataset like every other
 * spec in this suite. Chromium only (playwright.config.ts's own header):
 * this exercises the responsive layout itself (AppShell.tsx's hamburger ->
 * slide-in nav drawer, replacing the old <=768px "sidebar just vanishes"
 * bug - see that file's own header comment) rather than a cross-engine
 * rendering concern, so it belongs in the full-coverage project, not
 * duplicated into the firefox/webkit smoke subset.
 *
 * 390x844 is an iPhone 12/13-class viewport, comfortably under the 768px
 * breakpoint app-shell.css switches on.
 */

import { expect, test } from "@playwright/test";
import { ANALYST_STORAGE } from "./helpers/roles";

test.use({ storageState: ANALYST_STORAGE, viewport: { width: 390, height: 844 } });

test("mobile nav drawer: hamburger -> drawer -> navigate to Parts, with no page-level horizontal overflow", async ({
  page,
}) => {
  await page.goto("/");
  // NOT `getByRole("navigation", { name: "Primary" })` here: at <=768px
  // app-shell.css sets `aside.shell-sidebar { display: none }` on the
  // static desktop nav (its own "Primary" landmark goes fully out of the
  // accessibility tree, not just visually hidden) and the drawer's own copy
  // of that landmark is `aria-hidden="true"` until opened (AppShell.tsx) -
  // so there is genuinely no accessible "Primary" nav at all until the
  // hamburger is clicked. The page heading is this viewport's real
  // "loaded" signal instead.
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();

  // -- hamburger visible, drawer closed to start ---------------------------
  const menuBtn = page.getByRole("button", { name: "Open navigation" });
  await expect(menuBtn).toBeVisible();
  await expect(menuBtn).toHaveAttribute("aria-expanded", "false");

  // -- opens the drawer (aria-expanded flips, panel becomes visible) ------
  await menuBtn.click();
  await expect(menuBtn).toHaveAttribute("aria-expanded", "true");
  const drawer = page.getByRole("dialog", { name: "Primary navigation" });
  await expect(drawer).toBeVisible();

  // -- navigate to Parts via the drawer's own nav link ---------------------
  await drawer.getByRole("link", { name: "Parts" }).click();
  await expect(page).toHaveURL(/\/parts$/);

  // -- a route change closes the drawer (AppShell.tsx's own effect) -------
  await expect(menuBtn).toHaveAttribute("aria-expanded", "false");
  await expect(drawer).toBeHidden();

  // -- the page body itself never scrolls horizontally at this width ------
  // (the whole point of the hamburger/drawer fix: a narrow viewport must
  // not force the page itself wider than the device, even though the dense
  // data table it contains legitimately needs its OWN horizontal scroll -
  // asserted separately below).
  const bodyOverflowsPage = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(bodyOverflowsPage).toBe(false);

  // -- the Parts table itself scrolls horizontally inside its own
  // container (DataTable.css's `.data-table-wrap { overflow: auto }`) -
  // six-plus columns (Part number/MPN/Name/Category/Unit/Target price/
  // Status) legitimately don't fit in 390px, so this container, not the
  // page, is where that overflow must land.
  await expect(page.getByRole("table")).toBeVisible();
  const tableWrap = page.locator(".data-table-wrap");
  await expect(tableWrap).toBeVisible();
  await expect
    .poll(() =>
      tableWrap.evaluate((el) => el.scrollWidth > el.clientWidth),
    )
    .toBe(true);
});
