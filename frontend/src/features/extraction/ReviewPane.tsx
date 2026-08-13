/** Full-screen extraction-review surface (the task brief's own words),
 * opened from features/documents/DocumentsSection.tsx's "Review extraction"
 * / "Extract" actions via a callback that bubbles up to RfqsPage.tsx (the
 * ALLOWED "Review extraction wiring line" edit there) and renders this
 * component as a page-level sibling of the RFQ drawer, not nested inside
 * it - a review surface this dense needs the full viewport, not a
 * fixed-width side panel.
 *
 * Design decisions:
 *  - **Not built on components/Drawer.tsx** (FORBIDDEN to edit, and it is a
 *    fixed-width *side* panel - there is no "full screen" variant to opt
 *    into without editing it). This file implements its own overlay/panel
 *    markup (extraction.css) plus a small self-contained focus-trap +
 *    Escape-to-close effect, deliberately simpler than Drawer.tsx's own
 *    (no restore-focus-to-trigger, since this panel can be opened from
 *    several different trigger buttons across DocumentsSection's list, and
 *    no participation in Drawer.tsx's private `drawerStack` module - that
 *    symbol isn't exported). **Known quirk, same class already documented
 *    in features/quotes/QuotesSection.tsx's own file header for stacking
 *    two real `Drawer` instances**: because this panel's Escape handling is
 *    entirely independent of Drawer.tsx's stack, pressing Escape while this
 *    panel is open over the RFQ drawer may also close that RFQ drawer
 *    underneath (the RFQ drawer's own listener has no idea a non-Drawer
 *    overlay is stacked above it). Not fixed here for the same reason
 *    QuotesSection's own quirk isn't: doing so requires editing Drawer.tsx.
 *  - **Fields are grouped exactly per the API's own `target_entity`**
 *    (`"quote"` -> header fields, `"quote_terms"` -> terms, `"quote_line"` +
 *    `target_line_index` -> one group per line), not re-derived from
 *    `field_path` string parsing.
 *  - **Field "status" is a 4-way UI simplification of two independent API
 *    booleans** (`is_confirmed`, `requires_confirmation`) plus a validity
 *    signal (`normalized_value === null` while `raw_value !== null` means
 *    the extracted text failed type/business validation -
 *    `services/extraction_service.py`'s `_validate_value`/`mark_invalid`):
 *    `invalid` (validation failed) > `confirmed` (`is_confirmed`) >
 *    `needs_review` (`requires_confirmation` and not yet confirmed) >
 *    `accepted` (a HIGH-band field nobody has touched - auto-accepted per
 *    `ConfidenceBand`'s own docstring, "accepted automatically, still
 *    correctable", not one of the three states the brief names but a
 *    useful fourth label rather than mislabeling it "needs review").
 *  - **Match-candidate confidence has no pre-computed `band`** (unlike
 *    `ExtractionFieldResponse.band` - `MatchCandidateResponse` only carries
 *    the raw `confidence` decimal string), so this file derives one with
 *    `lib/decimalSort.ts`'s existing `compareDecimalStrings` against the
 *    same 0.95/0.60 thresholds MASTER.md documents - a client-side mirror
 *    of `app.domain.confidence.band()`, the same kind of "advisory,
 *    non-authoritative" duplication features/quotes/QuotesSection.tsx's own
 *    header already documents for its price-break structural hints.
 *  - **Per-candidate "Confirm"/"Reject" is a judgment call over the real
 *    line-addressed API** - see ./api.ts's file header for the full
 *    rationale; "Confirm" calls `useConfirmQuoteLineMatch` with that
 *    candidate's own `rfq_line_id`, "Reject" (rendered only on the line's
 *    currently-confirmed candidate) calls `useUnmatchQuoteLineMatch`.
 *  - **Materialize renders a quote reference, not a live link** - this app
 *    has no standalone quote detail route (quotes only render inside the
 *    RFQ drawer's own QuotesSection); on success this pane shows the new
 *    quote's number/id and points the reviewer at "this RFQ's Quotes
 *    section," which the materialize mutation's own cross-feature
 *    `quoteKeys` invalidation (./api.ts) keeps fresh for when they get
 *    there.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "../../auth/session";
import "../../components/badges.css";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { FormField } from "../../components/FormField";
import { XIcon } from "../../components/icons";
import { compareDecimalStrings } from "../../lib/decimalSort";
import { isAnalystOrAbove } from "../../lib/roles";
import { useDocument } from "../documents/api";
import { usePart } from "../parts/api";
import type { QuoteResponse } from "../quotes/api";
import { useRfqSuppliers } from "../rfqs/api";
import { useSupplier } from "../suppliers/api";
import {
  type ConfidenceBand,
  type ExtractionFieldResponse,
  type ExtractionRunResponse,
  type ExtractionRunState,
  type MatchCandidateResponse,
  type MatchStrategy,
  useConfirmAllExtractionFields,
  useConfirmQuoteLineMatch,
  useExtractionFields,
  useExtractionRun,
  useGenerateQuoteMatches,
  useMaterializeExtractionRun,
  usePatchExtractionField,
  useQuoteMatches,
  useUnmatchQuoteLineMatch,
} from "./api";
import "./extraction.css";

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function statusLabel(raw: string): string {
  return raw
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** Pure string decimal-point shift (never Number()/parseFloat, lib/money.ts's
 * rule): "0.987000" -> "98.7%". Used for confidence/rate display. */
function toPercentDisplay(raw: string): string {
  const negative = raw.startsWith("-");
  const body = negative ? raw.slice(1) : raw;
  const [intPart, fracPart = ""] = body.split(".");
  const padded = fracPart.padEnd(2, "0");
  const moved = padded.slice(0, 2);
  const remaining = padded.slice(2).replace(/0+$/, "");
  let newInt = (intPart + moved).replace(/^0+(?=\d)/, "");
  if (newInt === "") newInt = "0";
  const result = remaining ? `${newInt}.${remaining}` : newInt;
  return `${negative ? "-" : ""}${result}%`;
}

function bandFromDecimalString(raw: string): ConfidenceBand {
  if (compareDecimalStrings(raw, "0.95") >= 0) return "high";
  if (compareDecimalStrings(raw, "0.60") >= 0) return "medium";
  return "low";
}

// --- focus trap / escape -------------------------------------------------

function useFullScreenPanel(onClose: () => void) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const panel = panelRef.current;
    const focusables = panel
      ? Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      : [];
    (focusables[0] ?? panel)?.focus();

    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) {
        e.preventDefault();
        return;
      }
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = originalOverflow;
    };
  }, [onClose]);

  return panelRef;
}

// --- badges ---------------------------------------------------------------

function RunStateBadge({ state }: { state: ExtractionRunState }) {
  return <span className={`badge badge--run-${state}`}>{statusLabel(state)}</span>;
}

/** "lines[0].country_of_origin" -> "Line 1 · Country of origin" - raw
 * machine paths overwhelmed reviewers (2026-08 external review P1/P2). The
 * machine path stays available on the element's title attribute. */
function humanizeFieldPath(path: string): string {
  const lineMatch = /^lines\[(\d+)\]\.(.+)$/.exec(path);
  const humanize = (raw: string): string => {
    const words = raw.replace(/_/g, " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
  };
  if (lineMatch?.[1] !== undefined && lineMatch[2] !== undefined) {
    return `Line ${Number(lineMatch[1]) + 1} · ${humanize(lineMatch[2])}`;
  }
  return humanize(path);
}

function ConfidenceBadge({ band }: { band: ConfidenceBand }) {
  const labels: Record<ConfidenceBand, string> = { high: "High", medium: "Medium", low: "Low" };
  return <span className={`badge badge--conf-${band}`}>{labels[band]}</span>;
}

type FieldStatus = "confirmed" | "needs_review" | "invalid" | "accepted";

function fieldStatus(f: ExtractionFieldResponse): FieldStatus {
  if (f.normalized_value === null && f.raw_value !== null) return "invalid";
  if (f.is_confirmed) return "confirmed";
  if (f.requires_confirmation) return "needs_review";
  return "accepted";
}

function FieldStatusBadge({ status }: { status: FieldStatus }) {
  const labels: Record<FieldStatus, string> = {
    confirmed: "Confirmed",
    needs_review: "Needs review",
    invalid: "Invalid",
    accepted: "Auto-accepted",
  };
  return <span className={`badge badge--field-${status}`}>{labels[status]}</span>;
}

// --- run header ------------------------------------------------------------

function RunHeader({ run }: { run: ExtractionRunResponse }) {
  return (
    <section className="detail-section">
      <div className="run-header-row">
        <RunStateBadge state={run.state} />
        {run.simulated && <span className="badge badge--simulated">Simulated</span>}
        <span className="detail-label">
          {run.provider}
          {run.provider_model ? ` (${run.provider_model})` : ""} · run #{run.run_number}
        </span>
        {run.overall_confidence !== null && (
          <span className="run-confidence">
            {toPercentDisplay(run.overall_confidence)} overall confidence
          </span>
        )}
        {run.fields_requiring_review !== null && run.fields_requiring_review > 0 && (
          <span className="detail-label">{run.fields_requiring_review} field(s) need review</span>
        )}
      </div>
      {run.injection_suspected && (
        <div className="injection-banner" role="alert">
          <strong>Possible prompt injection detected</strong>
          <span>
            This document contained instruction-like text embedded in its content. It was ignored
            during extraction and flagged - review every value on this run carefully before
            confirming fields or creating a quote from it.
          </span>
        </div>
      )}
      {run.error && (
        <div className="banner-error" role="alert">
          {run.error.message}
        </div>
      )}
    </section>
  );
}

// --- fields ----------------------------------------------------------------

interface FieldGroup {
  key: string;
  title: string;
  fields: ExtractionFieldResponse[];
}

function groupFields(fields: ExtractionFieldResponse[]): FieldGroup[] {
  const groups: FieldGroup[] = [];
  const header = fields.filter((f) => f.target_entity === "quote");
  if (header.length > 0) groups.push({ key: "header", title: "Header fields", fields: header });

  const lineIndices = Array.from(
    new Set(
      fields
        .filter((f) => f.target_entity === "quote_line" && f.target_line_index !== null)
        .map((f) => f.target_line_index as number),
    ),
  ).sort((a, b) => a - b);
  for (const idx of lineIndices) {
    groups.push({
      key: `line-${idx}`,
      title: `Line ${idx + 1}`,
      fields: fields.filter((f) => f.target_entity === "quote_line" && f.target_line_index === idx),
    });
  }

  const terms = fields.filter((f) => f.target_entity === "quote_terms");
  if (terms.length > 0) groups.push({ key: "terms", title: "Terms", fields: terms });

  return groups;
}

function FieldRow({
  runId,
  field,
  canAct,
}: {
  runId: string;
  field: ExtractionFieldResponse;
  canAct: boolean;
}) {
  const patchMutation = usePatchExtractionField();
  const [correcting, setCorrecting] = useState(false);
  const [value, setValue] = useState(field.normalized_value ?? field.raw_value ?? "");
  const [note, setNote] = useState("");

  async function handleConfirm() {
    try {
      await patchMutation.mutateAsync({ runId, fieldId: field.id, data: { is_confirmed: true } });
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  async function handleSaveCorrection() {
    try {
      await patchMutation.mutateAsync({
        runId,
        fieldId: field.id,
        data: { normalized_value: value.trim(), is_confirmed: true, reason: note.trim() || null },
      });
      setCorrecting(false);
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  const status = fieldStatus(field);

  return (
    <tr>
      <td className="field-path">
        {field.field_name}
        <br />
        <span className="field-path" title={field.field_path}>
          {humanizeFieldPath(field.field_path)}
        </span>
      </td>
      <td>
        <span className={`field-value${field.normalized_value === null ? " is-missing" : ""}`}>
          {field.normalized_value ?? field.raw_value ?? "not extracted"}
        </span>
      </td>
      <td>
        <ConfidenceBadge band={field.band} />{" "}
        <span className="detail-label">{toPercentDisplay(field.confidence)}</span>
      </td>
      <td>
        <FieldStatusBadge status={status} />
      </td>
      {canAct && (
        <td>
          <div className="field-actions">
            {!field.is_confirmed && (
              <button
                type="button"
                className="btn-ghost-sm"
                disabled={patchMutation.isPending}
                onClick={() => void handleConfirm()}
              >
                Confirm
              </button>
            )}
            <button type="button" className="btn-ghost-sm" onClick={() => setCorrecting((v) => !v)}>
              {correcting ? "Cancel" : "Correct"}
            </button>
          </div>
          {correcting && (
            <div className="field-correct-form">
              <input
                value={value}
                onChange={(e) => setValue(e.target.value)}
                aria-label={`Corrected value for ${field.field_path}`}
              />
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Reason (optional)"
                rows={2}
                aria-label={`Correction reason for ${field.field_path}`}
              />
              <div className="detail-actions">
                <button
                  type="button"
                  className="btn-primary-sm"
                  disabled={patchMutation.isPending}
                  onClick={() => void handleSaveCorrection()}
                >
                  {patchMutation.isPending ? "Saving…" : "Save correction"}
                </button>
              </div>
            </div>
          )}
          <ApiErrorBanner error={patchMutation.error} />
        </td>
      )}
    </tr>
  );
}

function FieldsSection({
  runId,
  fields,
  canAct,
}: {
  runId: string;
  fields: ExtractionFieldResponse[];
  canAct: boolean;
}) {
  const groups = useMemo(() => groupFields(fields), [fields]);
  if (fields.length === 0) return <p className="detail-label">No fields extracted.</p>;

  return (
    <>
      {groups.map((g) => (
        <div className="field-group" key={g.key}>
          <div className="field-group-title">{g.title}</div>
          <div className="field-table-wrap">
            <table className="field-table">
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Extracted value</th>
                  <th>Confidence</th>
                  <th>Status</th>
                  {canAct && <th>Actions</th>}
                </tr>
              </thead>
              <tbody>
                {g.fields.map((f) => (
                  <FieldRow key={f.id} runId={runId} field={f} canAct={canAct} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </>
  );
}

/** Bulk counterpart to each `FieldRow`'s own per-field Confirm button
 * (2026-08 audit remediation, wave B - "46 field-level actions create a
 * slow exception-review workflow"). Rendered below the field tables so
 * "the values above" in its caution line literally means the table rows a
 * reviewer just scrolled past. Gated on the same `canAct` the per-field
 * actions use (viewer role + non-terminal run state) and on there being
 * anything left to confirm - `./api.ts`'s `useConfirmAllExtractionFields`
 * both seeds the run cache with the returned (now `ready`, or unchanged)
 * run and invalidates the fields list, so this bar disappears on its own
 * once the count drops to zero. */
function ConfirmAllBar({ runId, remainingCount }: { runId: string; remainingCount: number }) {
  const confirmAllMutation = useConfirmAllExtractionFields();
  const [armed, setArmed] = useState(false);

  async function handleConfirmAll() {
    try {
      await confirmAllMutation.mutateAsync(runId);
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  if (remainingCount === 0) return null;

  // Two-step arm/confirm (2026-08 external review P1/P2: the one-click bulk
  // action encouraged blind confirmation of dozens of fields).
  return (
    <div className="confirm-all-bar">
      {!armed ? (
        <button
          type="button"
          className="btn-secondary-sm"
          disabled={confirmAllMutation.isPending}
          onClick={() => setArmed(true)}
        >
          {`Confirm all remaining (${remainingCount})…`}
        </button>
      ) : (
        <>
          <p className="detail-label confirm-all-caution">
            This marks all {remainingCount} remaining flagged fields as human-confirmed -
            including values the document never stated. Only proceed if you have reviewed
            them above.
          </p>
          <button
            type="button"
            className="btn-danger-sm"
            disabled={confirmAllMutation.isPending}
            onClick={() => void handleConfirmAll()}
          >
            {confirmAllMutation.isPending
              ? "Confirming…"
              : `Yes, confirm ${remainingCount} fields`}
          </button>
          <button
            type="button"
            className="btn-ghost-sm"
            disabled={confirmAllMutation.isPending}
            onClick={() => setArmed(false)}
          >
            Cancel
          </button>
        </>
      )}
      {!armed && (
        <p className="detail-label confirm-all-caution">
          Confirms every remaining flagged field at once - review the values above first.
        </p>
      )}
      <ApiErrorBanner error={confirmAllMutation.error} />
    </div>
  );
}

// --- materialize -------------------------------------------------------

function SupplierOption({ supplierId }: { supplierId: string }) {
  const q = useSupplier(supplierId);
  return (
    <option value={supplierId}>{q.data ? `${q.data.code} - ${q.data.name}` : supplierId}</option>
  );
}

function MaterializeSection({
  runId,
  rfqId,
  onMaterialized,
}: {
  runId: string;
  rfqId: string;
  onMaterialized: (q: QuoteResponse) => void;
}) {
  const suppliersQuery = useRfqSuppliers(rfqId);
  const [supplierId, setSupplierId] = useState("");
  const materializeMutation = useMaterializeExtractionRun();

  const invitedSupplierIds = useMemo(
    () =>
      (suppliersQuery.data?.items ?? [])
        .filter((s) => s.excluded_at === null)
        .map((s) => s.supplier_id),
    [suppliersQuery.data],
  );

  async function handleCreate() {
    if (!supplierId) return;
    try {
      const quote = await materializeMutation.mutateAsync({
        runId,
        data: { supplier_id: supplierId },
      });
      onMaterialized(quote);
    } catch {
      // surfaced via ApiErrorBanner below - this is where a 409
      // "unconfirmed low-confidence fields" callout renders, one list item
      // per blocking field_path (see ./api.ts's file header).
    }
  }

  return (
    <section className="detail-section">
      <h3 className="detail-section-title">Create quote</h3>
      <ApiErrorBanner error={materializeMutation.error} />
      <div className="materialize-panel">
        <FormField label="Supplier" hint="invited suppliers of this RFQ only">
          <select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}>
            <option value="">Select supplier…</option>
            {invitedSupplierIds.map((id) => (
              <SupplierOption key={id} supplierId={id} />
            ))}
          </select>
        </FormField>
        <button
          type="button"
          className="btn-primary-sm"
          disabled={!supplierId || materializeMutation.isPending}
          onClick={() => void handleCreate()}
        >
          {materializeMutation.isPending ? "Creating…" : "Create quote"}
        </button>
      </div>
    </section>
  );
}

// --- part matches ------------------------------------------------------

function MatchCandidatePartLabel({ partId }: { partId: string }) {
  const q = usePart(partId);
  return (
    <span className="mono">
      {q.data ? `${q.data.internal_part_number} - ${q.data.name}` : partId}
    </span>
  );
}

const STRATEGY_LABELS: Record<MatchStrategy, string> = {
  internal_pn: "Internal part number",
  mpn: "Manufacturer part number",
  normalized_text: "Normalized text",
  alternative: "Approved alternative",
  fuzzy: "Fuzzy match",
};

function MatchCandidateRow({
  quoteId,
  quoteLineId,
  candidate,
  canWrite,
}: {
  quoteId: string;
  quoteLineId: string;
  candidate: MatchCandidateResponse;
  canWrite: boolean;
}) {
  const confirmMutation = useConfirmQuoteLineMatch();
  const unmatchMutation = useUnmatchQuoteLineMatch();

  async function handleConfirm() {
    try {
      await confirmMutation.mutateAsync({ quoteId, quoteLineId, rfqLineId: candidate.rfq_line_id });
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  async function handleReject() {
    try {
      await unmatchMutation.mutateAsync({ quoteId, quoteLineId });
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <div className="match-candidate-row">
      <div className="match-candidate-main">
        <span>
          <MatchCandidatePartLabel partId={candidate.part_id} /> · {STRATEGY_LABELS[candidate.strategy]}
        </span>
        {candidate.explanation && (
          <span className="match-candidate-explanation">{candidate.explanation}</span>
        )}
        <ApiErrorBanner error={confirmMutation.error ?? unmatchMutation.error} />
      </div>
      <div className="field-actions">
        <ConfidenceBadge band={bandFromDecimalString(candidate.confidence)} />
        {canWrite &&
          (candidate.human_confirmed ? (
            <button
              type="button"
              className="btn-danger-sm"
              disabled={unmatchMutation.isPending}
              onClick={() => void handleReject()}
            >
              Reject
            </button>
          ) : (
            <button
              type="button"
              className="btn-secondary-sm"
              disabled={confirmMutation.isPending}
              onClick={() => void handleConfirm()}
            >
              Confirm
            </button>
          ))}
      </div>
    </div>
  );
}

function MatchSection({ quote, canWrite }: { quote: QuoteResponse; canWrite: boolean }) {
  const matchesQuery = useQuoteMatches(quote.id);
  const generateMutation = useGenerateQuoteMatches();
  const items = matchesQuery.data?.items ?? [];
  const hasAnyCandidates = items.some((l) => l.candidates.length > 0);

  async function handleGenerate() {
    try {
      await generateMutation.mutateAsync(quote.id);
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <section className="detail-section">
      <div className="detail-header-row">
        <h3 className="detail-section-title">Part matches</h3>
        {canWrite && (
          <button
            type="button"
            className="btn-secondary-sm"
            disabled={generateMutation.isPending}
            onClick={() => void handleGenerate()}
          >
            {generateMutation.isPending ? "Generating…" : "Generate matches"}
          </button>
        )}
      </div>
      <p className="detail-label">
        Quote {quote.quote_number ?? quote.id} created - open it from this RFQ&apos;s Quotes
        section.
      </p>
      <ApiErrorBanner error={generateMutation.error ?? matchesQuery.error} />
      {matchesQuery.isLoading ? (
        <p className="detail-label">Loading…</p>
      ) : items.length === 0 ? (
        <p className="detail-label">No lines to match.</p>
      ) : !hasAnyCandidates ? (
        <p className="detail-label">No match candidates yet - click &quot;Generate matches.&quot;</p>
      ) : (
        items.map((line) => (
          <div className="match-line" key={line.quote_line_id}>
            <div className="detail-header-row">
              <span className="detail-label">Line</span>
              {line.match_status === "auto" ? (
                <span className="badge badge--auto-matched">Auto-matched</span>
              ) : (
                <span className="detail-label">{statusLabel(line.match_status)}</span>
              )}
            </div>
            {line.candidates.length === 0 ? (
              <p className="detail-label">No candidates found.</p>
            ) : (
              line.candidates
                .slice()
                .sort((a, b) => a.rank - b.rank)
                .map((c) => (
                  <MatchCandidateRow
                    key={c.id}
                    quoteId={quote.id}
                    quoteLineId={line.quote_line_id}
                    candidate={c}
                    canWrite={canWrite && line.match_status !== "auto"}
                  />
                ))
            )}
          </div>
        ))
      )}
    </section>
  );
}

// --- panel ---------------------------------------------------------------

export function ReviewPane({ runId, onClose }: { runId: string; onClose: () => void }) {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);
  const panelRef = useFullScreenPanel(onClose);

  const runQuery = useExtractionRun(runId);
  const fieldsQuery = useExtractionFields(runId);
  const documentQuery = useDocument(runQuery.data?.document_id ?? null);
  const rfqId = documentQuery.data?.rfq_id ?? null;

  const [materializedQuote, setMaterializedQuote] = useState<QuoteResponse | null>(null);

  const run = runQuery.data;
  const canActOnFields = canWrite && run !== undefined && run.state !== "materialized" && run.state !== "superseded";
  const remainingConfirmCount = (fieldsQuery.data?.items ?? []).filter(
    (f) => f.requires_confirmation && !f.is_confirmed,
  ).length;

  return (
    <>
      <div className="review-overlay" onClick={onClose} aria-hidden="true" />
      <div
        className="review-panel"
        data-state="open"
        role="dialog"
        aria-modal="true"
        aria-label="Extraction review"
        ref={panelRef}
      >
        <div className="review-header">
          <h2 className="review-title">Extraction review</h2>
          <button type="button" className="review-close-btn" onClick={onClose} aria-label="Close">
            <XIcon size={16} />
          </button>
        </div>
        <div className="review-body">
          {runQuery.isLoading ? (
            <p className="detail-label">Loading…</p>
          ) : runQuery.isError || !run ? (
            <ApiErrorBanner error={runQuery.error} />
          ) : (
            <>
              <RunHeader run={run} />

              <section className="detail-section">
                <h3 className="detail-section-title">Fields</h3>
                {fieldsQuery.isLoading ? (
                  <p className="detail-label">Loading…</p>
                ) : fieldsQuery.isError ? (
                  <ApiErrorBanner error={fieldsQuery.error} />
                ) : (
                  <>
                    <FieldsSection
                      runId={runId}
                      fields={fieldsQuery.data?.items ?? []}
                      canAct={canActOnFields}
                    />
                    {canActOnFields && (
                      <ConfirmAllBar runId={runId} remainingCount={remainingConfirmCount} />
                    )}
                  </>
                )}
              </section>

              {run.state === "ready" && canWrite && rfqId && !materializedQuote && (
                <MaterializeSection runId={runId} rfqId={rfqId} onMaterialized={setMaterializedQuote} />
              )}

              {materializedQuote && <MatchSection quote={materializedQuote} canWrite={canWrite} />}

              {!materializedQuote && run.state === "materialized" && (
                <p className="detail-label">
                  This run has already been materialized into a quote - open it from this RFQ&apos;s
                  Quotes section (the extraction API does not expose which quote a past run
                  produced).
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}
