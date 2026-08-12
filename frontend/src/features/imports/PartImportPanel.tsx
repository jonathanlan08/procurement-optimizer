/** Part-import workflow — mounted as a self-contained "Import CSV/XLSX"
 * button inside the Parts workspace toolbar (PartsPage.tsx's one-line
 * insertion; P0 audit finding: "the part-import backend workflow has no
 * UI"). Follows the reports/briefs feature-module convention (api.ts +
 * this panel + imports.css + imports.test.tsx).
 *
 * Design decisions:
 *  - **Self-contained role gating**, same convention
 *    ../documents/DocumentsSection.tsx's own file header documents: calls
 *    `useAuth()` directly rather than threading `canWrite` down from
 *    PartsPage.tsx, so the PartsPage.tsx edit stays a single mount line.
 *    `POST /part-imports`/`.../commit`/`.../cancel` all require
 *    `Role.ANALYST` server-side (api/v1/part_imports.py) — this mirrors
 *    that exactly, the same "hide, don't just disable" pattern the "New
 *    part" button already uses.
 *  - **A four-step state machine** (`pick` -> `preview` -> `done` |
 *    `cancelled`) inside one `Drawer`, not a route or a second stacked
 *    drawer — an import is a short, linear wizard the same way
 *    ../rfqs/RfqsPage.tsx's `RfqForm` is one drawer with an internal mode
 *    toggle, not several drawers chained together.
 *  - **Commit is disabled while the batch has any error row**
 *    (`canCommitPartImport`, ./api.ts) — the exact client-side mirror of
 *    `PartImportService.commit`'s own `409 conflict_state` refusal rule.
 *    The server re-validates regardless; this only saves a doomed round
 *    trip and tells the analyst why up front.
 *  - **The preview table renders `sample_rows` (up to 20), not the full
 *    cursor-paginated row list** — see ./api.ts's file header for the
 *    scope call. When `rows_total` exceeds the sample size, a caveat line
 *    says so explicitly ("Showing the first N of TOTAL rows") rather than
 *    implying the table is complete.
 *  - **Errors surface two ways**: the shared `ApiErrorBanner` for
 *    request-level failures (upload/commit/cancel — 413/415/422/409 all
 *    share the one envelope, same as every other mutation in this app), and
 *    a per-row "Errors" column for row-level validation verdicts, which are
 *    a `200`/`201` response field, not a failure.
 */

import { useState, type ReactNode } from "react";
import { useAuth } from "../../auth/session";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import "../../components/badges.css";
import { Drawer } from "../../components/Drawer";
import { PlusIcon } from "../../components/icons";
import "../../components/workspace.css";
import { isAnalystOrAbove } from "../../lib/roles";
import {
  canCommitPartImport,
  useCancelPartImport,
  useCommitPartImport,
  useUploadPartImport,
  type PartImportCommitResponse,
  type PartImportPreviewResponse,
  type PartImportRowDisposition,
  type PartImportRowError,
  type PartImportRowResponse,
} from "./api";
import "./imports.css";

const ACCEPT = ".csv,.xlsx";

const DISPOSITION_LABELS: Record<PartImportRowDisposition, string> = {
  create: "Will create",
  skip_duplicate: "Skip — duplicate",
  error: "Invalid",
};

function DispositionBadge({ disposition }: { disposition: PartImportRowDisposition }) {
  return <span className={`badge badge--import-${disposition}`}>{DISPOSITION_LABELS[disposition]}</span>;
}

/** Prefers the normalized (validated/parsed) value; falls back to the raw
 * uploaded cell — both are genuinely untyped JSONB (./api.ts's file
 * header), narrowed defensively here at the one render site. */
function rowField(row: PartImportRowResponse, key: string): string {
  const fromNormalized = row.normalized_values?.[key];
  const v = fromNormalized !== undefined && fromNormalized !== null ? fromNormalized : row.raw_values[key];
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

function ErrorsCell({ errors }: { errors: PartImportRowError[] }): ReactNode {
  if (errors.length === 0) return "—";
  return (
    <ul className="import-row-errors">
      {errors.map((e, i) => (
        <li key={`${e.field ?? "row"}-${i}`}>{e.field ? `${e.field}: ${e.issue}` : e.issue}</li>
      ))}
    </ul>
  );
}

function PreviewTable({ preview }: { preview: PartImportPreviewResponse }) {
  return (
    <>
      <div className="import-summary-row">
        <span className="import-summary-stat">
          <strong>{preview.rows_total}</strong> row(s) total
        </span>
        <span className="import-summary-stat import-summary-stat--valid">
          <strong>{preview.rows_valid}</strong> will create
        </span>
        <span className="import-summary-stat import-summary-stat--duplicate">
          <strong>{preview.rows_duplicate}</strong> duplicate
        </span>
        <span className="import-summary-stat import-summary-stat--error">
          <strong>{preview.rows_invalid}</strong> invalid
        </span>
      </div>

      {preview.rows_total > preview.sample_rows.length && (
        <p className="detail-label import-sample-caveat">
          Showing the first {preview.sample_rows.length} of {preview.rows_total} rows.
        </p>
      )}

      <div className="data-table-wrap import-table-wrap">
        <table className="import-rows-table">
          <thead>
            <tr>
              <th>Row</th>
              <th>Status</th>
              <th>Internal part #</th>
              <th>Name</th>
              <th>Unit code</th>
              <th data-align="right">Target price</th>
              <th>Errors</th>
            </tr>
          </thead>
          <tbody>
            {preview.sample_rows.map((row) => (
              <tr key={row.row_number} className={row.disposition === "error" ? "is-error" : undefined}>
                <td data-align="right" className="mono">
                  {row.row_number}
                </td>
                <td>
                  <DispositionBadge disposition={row.disposition} />
                </td>
                <td className="mono">{rowField(row, "internal_part_number")}</td>
                <td>{rowField(row, "name")}</td>
                <td className="mono">{rowField(row, "unit_code")}</td>
                <td data-align="right" className="mono">
                  {rowField(row, "target_price")}
                </td>
                <td>
                  <ErrorsCell errors={row.errors} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

type Step =
  | { kind: "pick" }
  | { kind: "preview"; batchId: string; filename: string; preview: PartImportPreviewResponse }
  | { kind: "done"; filename: string; result: PartImportCommitResponse }
  | { kind: "cancelled"; filename: string };

function ImportDrawerBody({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState<Step>({ kind: "pick" });
  const [file, setFile] = useState<File | null>(null);
  const uploadMutation = useUploadPartImport();
  const commitMutation = useCommitPartImport();
  const cancelMutation = useCancelPartImport();

  async function handleUpload() {
    if (!file) return;
    try {
      const preview = await uploadMutation.mutateAsync(file);
      setStep({ kind: "preview", batchId: preview.batch_id, filename: file.name, preview });
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  async function handleCommit() {
    if (step.kind !== "preview") return;
    try {
      const result = await commitMutation.mutateAsync(step.batchId);
      setStep({ kind: "done", filename: step.filename, result });
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  async function handleCancelImport() {
    if (step.kind !== "preview") return;
    try {
      await cancelMutation.mutateAsync(step.batchId);
      setStep({ kind: "cancelled", filename: step.filename });
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  function resetToPick() {
    setFile(null);
    setStep({ kind: "pick" });
  }

  if (step.kind === "pick") {
    return (
      <div className="import-step">
        <p className="detail-label">
          Upload a CSV or XLSX file of parts. Required columns: <code>internal_part_number</code>,{" "}
          <code>name</code>. Optional: <code>manufacturer_part_number</code>, <code>description</code>,{" "}
          <code>category</code>, <code>unit_code</code>, <code>target_price</code>,{" "}
          <code>target_price_currency</code>.
        </p>
        <ApiErrorBanner error={uploadMutation.error} />
        <div className="import-pick-row">
          <input
            type="file"
            accept={ACCEPT}
            aria-label="Part import file"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <button
            type="button"
            className="btn-primary-sm"
            disabled={!file || uploadMutation.isPending}
            onClick={() => void handleUpload()}
          >
            {uploadMutation.isPending ? "Uploading…" : "Upload"}
          </button>
        </div>
        <div className="form-actions">
          <button type="button" className="btn-ghost-sm" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    );
  }

  if (step.kind === "preview") {
    const canCommit = canCommitPartImport(step.preview) && !commitMutation.isPending;
    return (
      <div className="import-step">
        <p className="detail-label">
          Previewing <span className="detail-value mono">{step.filename}</span>
        </p>
        <PreviewTable preview={step.preview} />
        <ApiErrorBanner error={commitMutation.error ?? cancelMutation.error} />
        {!canCommitPartImport(step.preview) && (
          <p className="import-commit-hint" role="alert">
            {step.preview.rows_invalid} row(s) have errors. Fix the source file and re-upload, or cancel
            this import — a batch with any invalid row cannot be committed.
          </p>
        )}
        <div className="form-actions">
          <button
            type="button"
            className="btn-danger-sm"
            onClick={() => void handleCancelImport()}
            disabled={cancelMutation.isPending || commitMutation.isPending}
          >
            {cancelMutation.isPending ? "Cancelling…" : "Cancel import"}
          </button>
          <button
            type="button"
            className="btn-primary-sm"
            onClick={() => void handleCommit()}
            disabled={!canCommit}
          >
            {commitMutation.isPending ? "Committing…" : "Commit import"}
          </button>
        </div>
      </div>
    );
  }

  if (step.kind === "done") {
    return (
      <div className="import-step">
        <div className="import-success-banner" role="status">
          Import committed for <span className="mono">{step.filename}</span>.
        </div>
        <div className="import-summary-row">
          <span className="import-summary-stat import-summary-stat--valid">
            <strong>{step.result.created}</strong> created
          </span>
          <span className="import-summary-stat import-summary-stat--duplicate">
            <strong>{step.result.skipped}</strong> skipped
          </span>
          <span className="import-summary-stat">
            <strong>{step.result.updated}</strong> updated
          </span>
        </div>
        <div className="form-actions">
          <button type="button" className="btn-ghost-sm" onClick={resetToPick}>
            Import another file
          </button>
          <button type="button" className="btn-primary-sm" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="import-step">
      <p className="detail-label">
        Import of <span className="detail-value mono">{step.filename}</span> was cancelled — no parts
        were created.
      </p>
      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={resetToPick}>
          Import another file
        </button>
        <button type="button" className="btn-primary-sm" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

export function PartImportPanel() {
  const { session } = useAuth();
  const canImport = isAnalystOrAbove(session?.role);
  const [open, setOpen] = useState(false);
  // Bumped on every close so ImportDrawerBody remounts fresh next open,
  // rather than resuming mid-wizard on a stale batch.
  const [instanceKey, setInstanceKey] = useState(0);

  if (!canImport) return null;

  function close() {
    setOpen(false);
    setInstanceKey((k) => k + 1);
  }

  return (
    <>
      <button type="button" className="btn-secondary-sm" onClick={() => setOpen(true)}>
        <PlusIcon /> Import CSV/XLSX
      </button>
      <Drawer open={open} onClose={close} title="Import parts">
        <ImportDrawerBody key={instanceKey} onClose={close} />
      </Drawer>
    </>
  );
}
