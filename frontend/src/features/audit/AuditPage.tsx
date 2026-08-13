/** Audit log workspace - routed at `/audit` (replaces the `PlaceholderPage`,
 * per the allowed App.tsx edit): a dense, filterable, cursor-paged
 * table of every recorded audit event, with a row drawer showing the
 * explanation plus a before/after state diff.
 *
 * Design decisions:
 *  - **Pages are keyed by the cursor that produced them, not concatenated
 *    blindly.** `useAuditEvents` is a plain `useQuery` keyed on the full
 *    params object (including `cursor`) - a background refetch of an
 *    already-loaded page (e.g. on window refocus) must *replace* that
 *    page's slot, not duplicate it, so accumulated rows are stored as
 *    `Record<cursorKey, items>` plus an ordered list of cursor keys, and
 *    flattened for the table on every render.
 *  - **Changing a filter resets pages during render, not in an Effect.**
 *    This is the officially-documented "adjust state when a prop changes"
 *    pattern (comparing a derived `filterKey` against a ref-like previous
 *    value and calling `setState` conditionally mid-render) rather than an
 *    Effect that would cost an extra render pass - appending a *new* page
 *    once fresh data arrives is a genuine side effect (synchronizing with
 *    the query result) and stays in a `useEffect` below.
 *  - **`entity_type` is a free-text filter, not a `<select>`.** The frozen
 *    contract gives no fixed enum of entity types (unlike e.g.
 *    `RfqStatus`/`QuoteStatus` elsewhere in this codebase) - a text input
 *    avoids fabricating a shape the contract doesn't define, the same
 *    "don't fake a shape that doesn't exist" call this codebase makes
 *    repeatedly (e.g. ../comparison/ScenarioHistory.tsx's file header on
 *    not faking a missing list-row field).
 *  - **`event_type` accepts a comma-separated list**, split into the
 *    contract's repeatable `event_type` query parameter - the task brief
 *    asks for "event_type text filter" (singular text input) while the
 *    contract itself says the parameter repeats; comma-splitting a single
 *    input reconciles both without adding a multi-select UI the brief
 *    never asks for.
 */

import type { ColumnDef } from "@tanstack/react-table";
import { useEffect, useMemo, useState } from "react";
import { DataTable } from "../../components/DataTable";
import { DetailRow } from "../../components/DetailRow";
import { Drawer } from "../../components/Drawer";
import { FormField } from "../../components/FormField";
import { PageEyebrow } from "../../components/PageEyebrow";
import "../../components/workspace.css";
import { useDebouncedValue } from "../../lib/useDebouncedValue";
import { type AuditEventListParams, type AuditEventResponse, useAuditEvents } from "./api";
import "./audit.css";

const FIRST_PAGE_KEY = "__first__";

/** "supplier.created" -> "Supplier created" - a display courtesy only; the
 * raw key remains the filter vocabulary and the detail panel's value. */
function humanizeEventType(raw: string): string {
  const words = raw.replace(/[._]/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : raw;
}

/** UUIDs read as noise in a scanning column - show the first 8 chars with the
 * full identifier one hover (and, always, in the detail panel). */
function ShortId({ id }: { id: string | null | undefined }) {
  if (!id) return <>-</>;
  return <span title={id}>{id.length > 12 ? `${id.slice(0, 8)}…` : id}</span>;
}

interface FilterInputs {
  eventType: string;
  entityType: string;
  entityId: string;
  actorUserId: string;
  from: string;
  to: string;
}

const emptyFilters: FilterInputs = {
  eventType: "",
  entityType: "",
  entityId: "",
  actorUserId: "",
  from: "",
  to: "",
};

function AuditDrawer({
  event,
  open,
  onClose,
}: {
  event: AuditEventResponse | null;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="Audit event"
      panelClassName="hue-panel-top hue-panel-top--audit"
    >
      {event && (
        <div>
          <div className="detail-grid">
            <DetailRow label="Event type" value={event.event_type} />
            <DetailRow label="Entity type" value={event.entity_type} />
            <DetailRow label="Entity ID" value={event.entity_id ?? "-"} mono full />
            <DetailRow label="Actor user ID" value={event.actor_user_id ?? "system"} mono />
            <DetailRow label="Occurred at" value={new Date(event.occurred_at).toLocaleString()} />
            {event.request_id && <DetailRow label="Request ID" value={event.request_id} mono full />}
          </div>

          <section className="detail-section">
            <h3 className="detail-section-title">Explanation</h3>
            <p className="detail-value">{event.explanation ?? "No explanation recorded."}</p>
          </section>

          <section className="detail-section">
            <h3 className="detail-section-title">State change</h3>
            <div className="audit-diff-grid">
              <div className="audit-diff-col">
                <span className="detail-label">Before</span>
                <pre className="audit-diff-pre">
                  {event.before_state ? JSON.stringify(event.before_state, null, 2) : "null"}
                </pre>
              </div>
              <div className="audit-diff-col">
                <span className="detail-label">After</span>
                <pre className="audit-diff-pre">
                  {event.after_state ? JSON.stringify(event.after_state, null, 2) : "null"}
                </pre>
              </div>
            </div>
          </section>
        </div>
      )}
    </Drawer>
  );
}

export function AuditPage() {
  const [inputs, setInputs] = useState<FilterInputs>(emptyFilters);
  const debounced = useDebouncedValue(inputs, 300);
  const filterKey = JSON.stringify(debounced);

  const [prevFilterKey, setPrevFilterKey] = useState(filterKey);
  const [cursor, setCursor] = useState<string | null>(null);
  const [pages, setPages] = useState<Record<string, AuditEventResponse[]>>({});
  const [pageOrder, setPageOrder] = useState<string[]>([]);

  // Adjust state when the (debounced) filters change - see this file's
  // header on why this runs during render rather than in an Effect.
  if (filterKey !== prevFilterKey) {
    setPrevFilterKey(filterKey);
    setCursor(null);
    setPages({});
    setPageOrder([]);
  }

  const params: AuditEventListParams = useMemo(() => {
    const eventTypes = debounced.eventType
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    return {
      event_type: eventTypes.length > 0 ? eventTypes : undefined,
      entity_type: debounced.entityType.trim() || undefined,
      entity_id: debounced.entityId.trim() || undefined,
      actor_user_id: debounced.actorUserId.trim() || undefined,
      from: debounced.from || undefined,
      to: debounced.to || undefined,
      limit: 50,
      cursor: cursor ?? undefined,
    };
  }, [debounced, cursor]);

  const query = useAuditEvents(params);

  useEffect(() => {
    if (!query.data) return;
    const key = cursor ?? FIRST_PAGE_KEY;
    setPages((prev) => ({ ...prev, [key]: query.data.items }));
    setPageOrder((prev) => (prev.includes(key) ? prev : [...prev, key]));
  }, [query.data, cursor]);

  const items = pageOrder.flatMap((k) => pages[k] ?? []);
  const nextCursor = query.data?.next_cursor ?? null;

  const [selectedEvent, setSelectedEvent] = useState<AuditEventResponse | null>(null);

  const columns = useMemo<ColumnDef<AuditEventResponse>[]>(
    () => [
      {
        id: "occurred_at",
        accessorKey: "occurred_at",
        header: "Occurred",
        enableSorting: false,
        cell: (ctx) => new Date(ctx.getValue<string>()).toLocaleString(),
      },
      {
        id: "event_type",
        accessorKey: "event_type",
        header: "Event",
        enableSorting: false,
        // "supplier.created" -> "Supplier created"; the raw key (the filter
        // vocabulary) stays on the tooltip and in the detail panel
        cell: (ctx) => {
          const raw = ctx.getValue<string>();
          return <span title={raw}>{humanizeEventType(raw)}</span>;
        },
      },
      {
        id: "entity_type",
        accessorKey: "entity_type",
        header: "Entity type",
        enableSorting: false,
      },
      {
        id: "entity_id",
        accessorKey: "entity_id",
        header: "Entity ID",
        enableSorting: false,
        meta: { mono: true },
        cell: (ctx) => <ShortId id={ctx.getValue<string>()} />,
      },
      {
        id: "actor_user_id",
        accessorFn: (r) => r.actor_user_id ?? "",
        header: "Actor",
        enableSorting: false,
        meta: { mono: true },
        cell: (ctx) => {
          const v = ctx.getValue<string>();
          return v ? <ShortId id={v} /> : "system";
        },
      },
      {
        id: "explanation",
        accessorFn: (r) => r.explanation ?? "",
        header: "Explanation",
        enableSorting: false,
        cell: (ctx) => {
          const v = ctx.getValue<string>();
          return <span className="audit-explanation-cell">{v || "-"}</span>;
        },
      },
    ],
    [],
  );

  return (
    <section className="audit-page">
      <header className="page-toolbar">
        <div className="page-heading">
          <PageEyebrow hue="audit">Audit</PageEyebrow>
          <h1>Audit log</h1>
          <p>Every recorded change across the workspace, with before/after state.</p>
        </div>
      </header>

      <div className="audit-filters">
        <FormField label="Event type" hint="comma-separated, optional">
          <input
            type="text"
            value={inputs.eventType}
            onChange={(e) => setInputs((prev) => ({ ...prev, eventType: e.target.value }))}
            placeholder="e.g. scenario.created"
          />
        </FormField>
        <FormField label="Entity type" hint="optional">
          <input
            type="text"
            value={inputs.entityType}
            onChange={(e) => setInputs((prev) => ({ ...prev, entityType: e.target.value }))}
            placeholder="e.g. rfq"
          />
        </FormField>
        <FormField label="Entity ID" hint="optional">
          <input
            type="text"
            value={inputs.entityId}
            onChange={(e) => setInputs((prev) => ({ ...prev, entityId: e.target.value }))}
            placeholder="UUID"
          />
        </FormField>
        <FormField label="Actor user ID" hint="optional">
          <input
            type="text"
            value={inputs.actorUserId}
            onChange={(e) => setInputs((prev) => ({ ...prev, actorUserId: e.target.value }))}
            placeholder="UUID"
          />
        </FormField>
        <FormField label="From">
          <input
            type="date"
            value={inputs.from}
            onChange={(e) => setInputs((prev) => ({ ...prev, from: e.target.value }))}
          />
        </FormField>
        <FormField label="To">
          <input
            type="date"
            value={inputs.to}
            onChange={(e) => setInputs((prev) => ({ ...prev, to: e.target.value }))}
          />
        </FormField>
      </div>

      <DataTable
        ariaLabel="Audit events"
        columns={columns}
        data={items}
        getRowId={(r) => r.id}
        isLoading={query.isLoading && items.length === 0}
        isError={query.isError}
        errorMessage={query.error instanceof Error ? query.error.message : undefined}
        onRetry={() => void query.refetch()}
        emptyTitle="No audit events found"
        emptyDescription="Adjust the filters above, or check back after activity occurs."
        onRowClick={(r) => setSelectedEvent(r)}
      />

      <div className="audit-load-more-row">
        {nextCursor && (
          <button
            type="button"
            className="btn-secondary-sm"
            onClick={() => setCursor(nextCursor)}
            disabled={query.isFetching}
          >
            {query.isFetching ? "Loading…" : "Load more"}
          </button>
        )}
      </div>

      <AuditDrawer
        event={selectedEvent}
        open={selectedEvent !== null}
        onClose={() => setSelectedEvent(null)}
      />
    </section>
  );
}
