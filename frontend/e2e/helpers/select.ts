/** `<select>` locating + option-picking helpers.
 *
 * `selectByLabel` exists because this app's `FormField.tsx` wraps every
 * control in an IMPLICIT `<label>` — for a `<select>`, every `<option>`'s
 * own text becomes part of the `<label>` element's plain DOM `textContent`.
 * Playwright's `getByLabel(name, { exact: true })` matches against that raw
 * label text, not the browser's real accessible name (which correctly
 * excludes an embedded control's own content per the accname spec) — so an
 * exact-match `getByLabel` on any `<select>` here spuriously finds zero
 * elements (verified empirically against this app's own RFQ picker:
 * `getByLabel("RFQ", {exact:true})` -> 0 matches, while
 * `getByRole("combobox", {name:"RFQ"})` -> 1, the correct element).
 * `getByRole("combobox", ...)` uses the real accessibility tree and has no
 * such issue, so every `<select>` in these specs is targeted through this
 * helper rather than `getByLabel`.
 *
 * Matching stays substring (not `exact`): a `FormField` with a `hint` (e.g.
 * ReviewPane.tsx's Supplier picker, hint "invited suppliers of this RFQ
 * only") folds that hint text into the same accessible name, so an exact
 * match on the bare label would itself fail. Each call site picks a label
 * substring that is unambiguous at the point in the page it's called (e.g.
 * "RFQ" is always selected before the conditionally-rendered "RFQ line"
 * picker exists).
 *
 * `selectOptionContaining` then picks an `<option>` by a text SUBSTRING
 * rather than an exact label — several of this app's option labels are
 * built with an em dash (e.g. `"RFQ-2026-ENC-PILOT — Enclosure Pilot Build
 * (Under Review)"`, ../../src/features/rfqs/RfqsPage.tsx) or embed a value
 * that varies per seed run (dates, ids) — matching a stable substring (the
 * internal reference code, a supplier code, a scenario name) instead of the
 * full label keeps these specs resilient to formatting/whitespace details
 * that aren't the point of the test.
 */

import type { Locator, Page } from "@playwright/test";

export function selectByLabel(scope: Locator | Page, label: string): Locator {
  return scope.getByRole("combobox", { name: label });
}

export async function selectOptionContaining(select: Locator, substring: string): Promise<void> {
  const option = select.locator("option", { hasText: substring });
  const value = await option.getAttribute("value");
  if (value === null) {
    throw new Error(`selectOptionContaining: no <option> containing "${substring}" was found`);
  }
  await select.selectOption(value);
}
