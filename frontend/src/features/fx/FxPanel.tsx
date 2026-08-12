/** Compact FX panel, routed at `/fx` (App.tsx + AppShell.tsx's NAV both get
 * a one-line ALLOWED addition for this — see those files' own comments at
 * the edit site).
 *
 * Shapes mirror backend/src/app/schemas/fx.py / api/v1/fx.py exactly — see
 * ./api.ts's file header for the full rationale (the two-shapes-one-route
 * split, the `base`/`quote` query-param vs `base_currency`/`quote_currency`
 * body-field naming mismatch, why there's no `If-Match` here). Two
 * design decisions specific to this page:
 *
 *  - **Currency pickers are the same fixed six-option list already used by
 *    RfqsPage.tsx/QuotesSection.tsx** (`USD/EUR/GBP/JPY/CNY/MXN`), not a
 *    free-text ISO-3 input — `CurrencyCode` accepts any 3-letter code
 *    server-side, but every other currency control in this app is already
 *    a bounded select for the same reason RfqsPage.tsx's own `<select>`
 *    is: predictable, no typo-shaped 422s. Duplicated locally rather than
 *    imported cross-feature, matching how rfqs.css/boms.css/quotes.css
 *    each keep their own copy of shared visual language instead of
 *    reaching into a sibling feature directory.
 *  - **The overrides list reuses `components/DataTable`** (paginated via
 *    `page: {limit, offset, total}`, exactly like every other list page in
 *    this app) — `GET /exchange-rates` without all three of `base`/
 *    `quote`/`as_of` already returns that same list envelope, so this is
 *    the same "plain paginated resource list" shape as Suppliers/Parts/
 *    BOMs/RFQs, not a bespoke one.
 */

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../../auth/session";
import "../../components/badges.css";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { DataTable } from "../../components/DataTable";
import { FormField } from "../../components/FormField";
import { PaginationBar } from "../../components/PaginationBar";
import { PlusIcon } from "../../components/icons";
import "../../components/workspace.css";
import { formatDecimal, isDecimalString } from "../../lib/money";
import { isAnalystOrAbove } from "../../lib/roles";
import { zodResolver } from "../../lib/zodResolver";
import {
  type ExchangeRateResponse,
  type FxOverrideInput,
  useCreateFxOverride,
  useEffectiveRate,
  useExchangeRates,
} from "./api";
import "./fx.css";

const CURRENCY_OPTIONS = ["USD", "EUR", "GBP", "JPY", "CNY", "MXN"] as const;
const LIMIT = 50;

function todayIso(): string {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

/** `source` is `"synthetic_fixture"` or `"manual"` (see ./api.ts's file
 * header) — badge text/token switches on the exact string, with a neutral
 * fallback for any future third source rather than silently rendering
 * nothing. */
function SourceBadge({ source }: { source: string }) {
  if (source === "synthetic_fixture") {
    return <span className="badge fx-badge--synthetic">Synthetic</span>;
  }
  if (source === "manual") {
    return <span className="badge fx-badge--manual">Override</span>;
  }
  return <span className="badge fx-badge--other">{source}</span>;
}

// --- effective-rate lookup -----------------------------------------------

function EffectiveRateLookup() {
  const [base, setBase] = useState<string>("USD");
  const [quote, setQuote] = useState<string>("EUR");
  const [asOf, setAsOf] = useState<string>(todayIso());
  const effectiveQuery = useEffectiveRate(base, quote, asOf);

  return (
    <section className="detail-section">
      <h3 className="detail-section-title">Effective rate lookup</h3>
      <div className="fx-lookup-row">
        <FormField label="Base">
          <select value={base} onChange={(e) => setBase(e.target.value)} aria-label="Base currency">
            {CURRENCY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Quote">
          <select value={quote} onChange={(e) => setQuote(e.target.value)} aria-label="Quote currency">
            {CURRENCY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="As of">
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} aria-label="As of date" />
        </FormField>
      </div>
      {effectiveQuery.isLoading ? (
        <p className="detail-label">Loading…</p>
      ) : effectiveQuery.isError ? (
        <ApiErrorBanner error={effectiveQuery.error} />
      ) : effectiveQuery.data ? (
        <div className="fx-lookup-result">
          <span className="fx-lookup-rate mono">
            1 {effectiveQuery.data.base_currency} ={" "}
            <span title={effectiveQuery.data.rate}>{formatDecimal(effectiveQuery.data.rate)}</span>{" "}
            {effectiveQuery.data.quote_currency}
          </span>
          <SourceBadge source={effectiveQuery.data.source} />
          <span className="detail-label">
            effective {effectiveQuery.data.effective_date}
            {effectiveQuery.data.is_manual_override && effectiveQuery.data.override_reason
              ? ` · ${effectiveQuery.data.override_reason}`
              : ""}
          </span>
        </div>
      ) : null}
    </section>
  );
}

// --- add override form ---------------------------------------------------

const overrideFormSchema = z.object({
  base_currency: z
    .string()
    .trim()
    .refine((v) => (CURRENCY_OPTIONS as readonly string[]).includes(v), "Select a currency"),
  quote_currency: z
    .string()
    .trim()
    .refine((v) => (CURRENCY_OPTIONS as readonly string[]).includes(v), "Select a currency"),
  rate: z
    .string()
    .trim()
    .min(1, "Rate is required")
    .refine((v) => isDecimalString(v), "Must be a decimal number, e.g. 1.084000000000")
    .refine((v) => !v.startsWith("-"), "Must not be negative")
    .refine((v) => !/^0+(\.0+)?$/.test(v), "Must be greater than zero"),
  effective_date: z.string().trim().min(1, "Effective date is required"),
  // Required, non-blank — mirrors `ExchangeRateOverrideCreate.override_reason`
  // (`Field(min_length=1, ...)`), backed by a DB CHECK per fx.py's docstring.
  override_reason: z.string().trim().min(1, "Reason is required").max(1000, "Max 1000 characters"),
});

type OverrideFormValues = z.infer<typeof overrideFormSchema>;

const emptyOverrideValues: OverrideFormValues = {
  base_currency: "USD",
  quote_currency: "EUR",
  rate: "",
  effective_date: todayIso(),
  override_reason: "",
};

function AddOverrideForm({ onSaved, onCancel }: { onSaved: () => void; onCancel: () => void }) {
  const createMutation = useCreateFxOverride();
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<OverrideFormValues>({
    resolver: zodResolver(overrideFormSchema),
    defaultValues: emptyOverrideValues,
  });

  async function onValid(values: OverrideFormValues) {
    const data: FxOverrideInput = {
      base_currency: values.base_currency,
      quote_currency: values.quote_currency,
      rate: values.rate.trim(),
      effective_date: values.effective_date,
      override_reason: values.override_reason.trim(),
    };
    try {
      await createMutation.mutateAsync(data);
      onSaved();
    } catch {
      // surfaced via ApiErrorBanner reading createMutation.error
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
      <ApiErrorBanner error={createMutation.error} />
      <div className="form-grid">
        <FormField label="Base currency" error={errors.base_currency?.message}>
          <select {...register("base_currency")}>
            {CURRENCY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Quote currency" error={errors.quote_currency?.message}>
          <select {...register("quote_currency")}>
            {CURRENCY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Rate" hint="12dp" error={errors.rate?.message}>
          <input
            {...register("rate")}
            inputMode="decimal"
            placeholder="1.084000000000"
            aria-invalid={!!errors.rate}
          />
        </FormField>
        <FormField label="Effective date" error={errors.effective_date?.message}>
          <input type="date" {...register("effective_date")} aria-invalid={!!errors.effective_date} />
        </FormField>
        <FormField label="Reason" full error={errors.override_reason?.message}>
          <textarea
            {...register("override_reason")}
            rows={2}
            placeholder="Why override the fixture rate?"
            aria-invalid={!!errors.override_reason}
          />
        </FormField>
      </div>
      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onCancel} disabled={createMutation.isPending}>
          Cancel
        </button>
        <button type="submit" className="btn-primary-sm" disabled={createMutation.isPending}>
          {createMutation.isPending ? "Saving…" : "Save override"}
        </button>
      </div>
    </form>
  );
}

// --- override history list -----------------------------------------------

function OverridesList() {
  const [offset, setOffset] = useState(0);
  const listQuery = useExchangeRates({ limit: LIMIT, offset });
  const items = listQuery.data?.items ?? [];

  const columns = useMemo<ColumnDef<ExchangeRateResponse>[]>(
    () => [
      { id: "base_currency", accessorKey: "base_currency", header: "Base", meta: { mono: true } },
      { id: "quote_currency", accessorKey: "quote_currency", header: "Quote", meta: { mono: true } },
      {
        id: "rate",
        accessorKey: "rate",
        header: "Rate",
        meta: { align: "right", mono: true },
        // stored at 12dp; show it without the trailing-zero noise (full
        // precision stays on the cell's title)
        cell: ({ getValue }) => {
          const raw = String(getValue());
          return <span title={raw}>{formatDecimal(raw)}</span>;
        },
      },
      {
        id: "effective_date",
        accessorKey: "effective_date",
        header: "Effective",
      },
      {
        id: "source",
        header: "Source",
        enableSorting: false,
        cell: (ctx) => <SourceBadge source={ctx.row.original.source} />,
      },
      {
        id: "override_reason",
        accessorKey: "override_reason",
        header: "Reason",
        cell: (ctx) => ctx.getValue<string | null>() ?? "—",
      },
      {
        id: "created_at",
        accessorKey: "created_at",
        header: "Created",
        cell: (ctx) => new Date(ctx.getValue<string>()).toLocaleDateString(),
      },
    ],
    [],
  );

  return (
    <section className="detail-section">
      <h3 className="detail-section-title">Override history</h3>
      <DataTable
        ariaLabel="Exchange rate overrides"
        columns={columns}
        data={items}
        getRowId={(r) => r.id}
        isLoading={listQuery.isLoading}
        isError={listQuery.isError}
        errorMessage={listQuery.error instanceof Error ? listQuery.error.message : undefined}
        onRetry={() => void listQuery.refetch()}
        emptyTitle="No overrides yet"
        emptyDescription="Manual overrides you add will appear here."
      />
      <PaginationBar
        total={listQuery.data?.page.total ?? 0}
        limit={LIMIT}
        offset={offset}
        onOffsetChange={setOffset}
      />
    </section>
  );
}

// --- page -------------------------------------------------------------

export function FxPanel() {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);
  const [adding, setAdding] = useState(false);

  return (
    <section className="fx-page">
      <header className="page-toolbar">
        <div className="page-heading">
          <h1>FX rates</h1>
          <p>Effective-rate lookups and manual override history.</p>
        </div>
        {canWrite && (
          <div className="toolbar-controls">
            <button type="button" className="btn-primary-sm" onClick={() => setAdding((v) => !v)}>
              <PlusIcon /> {adding ? "Cancel" : "Add override"}
            </button>
          </div>
        )}
      </header>

      <EffectiveRateLookup />

      {adding && (
        <section className="detail-section">
          <h3 className="detail-section-title">Add override</h3>
          <AddOverrideForm onSaved={() => setAdding(false)} onCancel={() => setAdding(false)} />
        </section>
      )}

      <OverridesList />
    </section>
  );
}
