/** Generates a small, uniquely-content CSV quote document for
 * upload.spec.ts, written to a fresh temp directory (never committed).
 *
 * Why generated rather than reusing one of the four committed golden
 * fixtures at backend/tests/fixtures/documents/: `DocumentService.upload`
 * dedupes by sha256 PER ORGANIZATION (services/document_service.py's own
 * module docstring, deviation 3) and the demo seed already uploads all four
 * committed fixtures — including this same CSV shape — to the seeded org, so
 * re-uploading identical bytes through the UI would deterministically hit a
 * 409 `conflict_duplicate` instead of exercising the happy path. Embedding a
 * fresh random id in the content guarantees a new sha256 every run (and
 * across the "run twice" verification), the same way
 * backend/scripts/generate_fixtures.py's own four fixtures are generated
 * (this mirrors that script's documented header/table CSV shape, minus the
 * prompt-injection line — that trust-boundary case is already covered
 * end-to-end by documents.spec.ts against the seeded Nordic Fastener CSV).
 *
 * The table row is shaped so the backend's extraction MOCK PROVIDER's
 * regex heuristic (app/providers/extraction/mock.py) can find at least one
 * line item even though this generated file has no committed "golden"
 * fixture keyed by its sha256 — see that module's own header: any
 * unrecognized document hash still gets a low-confidence heuristic pass
 * rather than failing outright, which is exactly the "no paid providers"
 * demo behavior this suite must exercise.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export interface UploadFixture {
  path: string;
  uniqueId: string;
}

export function generateUploadCsvFixture(): UploadFixture {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "po-e2e-upload-"));
  const uniqueId = crypto.randomUUID();
  const shortId = uniqueId.slice(0, 8).toUpperCase();
  const filePath = path.join(dir, `e2e-upload-${shortId}.csv`);
  const lines = [
    `quote_number,QN-E2E-${shortId}`,
    "supplier,Playwright E2E Fixture Supplier",
    "quote_date,2026-08-01",
    "currency,USD",
    "payment_terms,Net 30",
    "",
    "part_number,description,quantity,unit_of_measure,unit_price,notes",
    `MF-E2E-${shortId},Playwright upload fixture ${uniqueId},25,each,12.50,`,
  ];
  fs.writeFileSync(filePath, lines.join("\n") + "\n", "utf8");
  return { path: filePath, uniqueId };
}
