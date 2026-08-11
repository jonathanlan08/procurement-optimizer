/** Quote-document upload + extraction entry point, mounted inside the RFQ
 * detail drawer (RfqsPage.tsx's `RfqDetail`, per this task's ALLOWED
 * "minimal insertion" edit there — same mount pattern
 * features/quotes/QuotesSection.tsx already establishes).
 *
 * Design decisions:
 *  - **Self-contained role gating**, same as QuotesSection.tsx's own file
 *    header: calls `useAuth()` directly rather than threading `canWrite`
 *    down from RfqsPage.tsx, keeping the RfqsPage.tsx edit a single mount
 *    line.
 *  - **Upload is a small inline form, not a second stacked drawer.** Unlike
 *    quote entry (a multi-section form worth its own drawer), an upload is
 *    one file input plus an optional supplier — QuotesSection's own
 *    `AddRfqLineForm`-style "inline reveal" pattern (see rfqs/RfqsPage.tsx)
 *    fits better than a third drawer layer.
 *  - **"Extract" is only offered while `document.state === 'stored'`** —
 *    the only state `ExtractionService.start_run` accepts
 *    (`services/extraction_service.py`: "extraction can only start from
 *    'stored'"); every other state either hasn't reached that point yet or
 *    (for `quarantined`) never will, so the quarantine reason is shown
 *    instead of a doomed action button. `start_run` executes synchronously
 *    (see ../extraction/api.ts's file header) and returns the completed
 *    run, so on success this file jumps straight into
 *    `onReviewExtraction(run.id)` rather than leaving the analyst to find
 *    a new "Review extraction" link themselves.
 *  - **"Review extraction" opens the most recent run** (highest
 *    `run_number`) when one or more runs exist for a document — re-running
 *    extraction is supported (multiple runs per document), but this list
 *    view only surfaces the latest; ReviewPane.tsx itself is where a
 *    reviewer works one run in depth.
 *  - **Upload errors (413/415/409) need no special-case code** — see
 *    ./api.ts's file header: `ApiErrorBanner` already renders the shared
 *    error envelope's `message` + `details[]` generically for every status
 *    code this route can return.
 */

import { useMemo, useState } from "react";
import { useAuth } from "../../auth/session";
import "../../components/badges.css";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { FormField } from "../../components/FormField";
import { PlusIcon } from "../../components/icons";
import "../../components/workspace.css";
import { useDocumentExtractionRuns, useStartExtractionRun } from "../extraction/api";
import { isAnalystOrAbove } from "../../lib/roles";
import type { RfqResponse } from "../rfqs/api";
import { useRfqSuppliers } from "../rfqs/api";
import { useSupplier } from "../suppliers/api";
import {
  type DocumentResponse,
  documentTypeLabel,
  formatBytes,
  useRfqDocuments,
  useUploadDocument,
} from "./api";
import "./documents.css";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.csv,.xlsx";

function statusLabel(state: string): string {
  return state
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function DocumentStateBadge({ state }: { state: DocumentResponse["state"] }) {
  return <span className={`badge badge--doc-${state}`}>{statusLabel(state)}</span>;
}

function SupplierOption({ supplierId }: { supplierId: string }) {
  const q = useSupplier(supplierId);
  return <option value={supplierId}>{q.data ? `${q.data.code} — ${q.data.name}` : supplierId}</option>;
}

// --- upload form ---------------------------------------------------------

function UploadForm({
  rfq,
  invitedSupplierIds,
  onDone,
}: {
  rfq: RfqResponse;
  invitedSupplierIds: string[];
  onDone: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [supplierId, setSupplierId] = useState("");
  const uploadMutation = useUploadDocument();

  async function handleUpload() {
    if (!file) return;
    try {
      await uploadMutation.mutateAsync({ rfqId: rfq.id, file, supplierId: supplierId || null });
      setFile(null);
      onDone();
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <div className="document-upload-form">
      <ApiErrorBanner error={uploadMutation.error} />
      <div className="document-upload-row">
        <FormField label="Supplier" hint="optional — set once identified">
          <select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}>
            <option value="">— unassigned —</option>
            {invitedSupplierIds.map((id) => (
              <SupplierOption key={id} supplierId={id} />
            ))}
          </select>
        </FormField>
        <FormField label="File" hint="pdf, png, jpg, csv, or xlsx">
          <input
            type="file"
            accept={ACCEPT}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </FormField>
        <button
          type="button"
          className="btn-primary-sm"
          disabled={!file || uploadMutation.isPending}
          onClick={() => void handleUpload()}
        >
          {uploadMutation.isPending ? "Uploading…" : "Upload"}
        </button>
        <button type="button" className="btn-ghost-sm" onClick={onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// --- document row ----------------------------------------------------------

function DocumentRow({
  document,
  canWrite,
  onReviewExtraction,
}: {
  document: DocumentResponse;
  canWrite: boolean;
  onReviewExtraction: (runId: string) => void;
}) {
  const runsQuery = useDocumentExtractionRuns(document.id);
  const startMutation = useStartExtractionRun();
  const supplierQuery = useSupplier(document.supplier_id);

  const latestRun = useMemo(() => {
    const runs = runsQuery.data?.items ?? [];
    if (runs.length === 0) return null;
    return runs.reduce((best, r) => (r.run_number > best.run_number ? r : best));
  }, [runsQuery.data]);

  async function handleExtract() {
    try {
      const run = await startMutation.mutateAsync({ documentId: document.id });
      onReviewExtraction(run.id);
    } catch {
      // surfaced via the inline error line below
    }
  }

  const canExtract = canWrite && document.state === "stored";

  return (
    <li className={`document-row${document.is_archived ? " is-archived" : ""}`}>
      <div className="document-row-main">
        <span className="document-row-filename">{document.original_filename}</span>
        <span className="document-row-meta detail-label">
          <span className="badge badge--doc-type">{documentTypeLabel(document)}</span>
          <DocumentStateBadge state={document.state} />
          {formatBytes(document.size_bytes)} · uploaded{" "}
          {new Date(document.uploaded_at).toLocaleDateString()}
          {document.supplier_id
            ? ` · ${supplierQuery.data ? `${supplierQuery.data.code} — ${supplierQuery.data.name}` : document.supplier_id}`
            : ""}
        </span>
        {document.state === "quarantined" && document.quarantine_reason && (
          <span className="document-quarantine-note">{document.quarantine_reason}</span>
        )}
        {startMutation.isError && (
          <span className="document-quarantine-note">
            {startMutation.error instanceof Error
              ? startMutation.error.message
              : "Extraction failed to start."}
          </span>
        )}
      </div>
      <div className="document-row-actions">
        {canExtract && (
          <button
            type="button"
            className="btn-secondary-sm"
            disabled={startMutation.isPending}
            onClick={() => void handleExtract()}
          >
            {startMutation.isPending ? "Extracting…" : latestRun ? "Re-extract" : "Extract"}
          </button>
        )}
        {latestRun && (
          <button
            type="button"
            className="btn-ghost-sm"
            onClick={() => onReviewExtraction(latestRun.id)}
          >
            Review extraction
          </button>
        )}
      </div>
    </li>
  );
}

// --- section (mounted inside RfqDetail) ------------------------------------

export function DocumentsSection({
  rfq,
  onReviewExtraction,
}: {
  rfq: RfqResponse;
  onReviewExtraction: (runId: string) => void;
}) {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);
  const documentsQuery = useRfqDocuments(rfq.id);
  const suppliersQuery = useRfqSuppliers(rfq.id);
  const [uploading, setUploading] = useState(false);

  const items = documentsQuery.data?.items ?? [];
  const invitedSupplierIds = useMemo(
    () => (suppliersQuery.data?.items ?? []).filter((s) => s.excluded_at === null).map((s) => s.supplier_id),
    [suppliersQuery.data],
  );

  return (
    <section className="detail-section">
      <div className="detail-header-row">
        <h3 className="detail-section-title">Quote documents</h3>
        {canWrite && (
          <button type="button" className="btn-secondary-sm" onClick={() => setUploading((v) => !v)}>
            <PlusIcon /> {uploading ? "Cancel" : "Upload document"}
          </button>
        )}
      </div>

      {uploading && (
        <UploadForm
          rfq={rfq}
          invitedSupplierIds={invitedSupplierIds}
          onDone={() => setUploading(false)}
        />
      )}

      {documentsQuery.isLoading ? (
        <p className="detail-label">Loading…</p>
      ) : documentsQuery.isError ? (
        <ApiErrorBanner error={documentsQuery.error} />
      ) : items.length === 0 ? (
        <p className="detail-label">No documents uploaded yet.</p>
      ) : (
        <ul className="document-list">
          {items.map((d) => (
            <DocumentRow
              key={d.id}
              document={d}
              canWrite={canWrite}
              onReviewExtraction={onReviewExtraction}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
