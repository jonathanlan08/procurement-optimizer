/** RFQ workspace: dense list + search/status-filter toolbar + pagination,
 * with a right-side drawer for create/view/edit/status-change/lines/suppliers.
 *
 * Design decisions (see features/boms/BomsPage.tsx / features/suppliers/
 * SuppliersPage.tsx for the shared drawer-vs-route + dense-table rationale
 * this file follows too):
 *
 *  - **Search and status filter are server-side.** Unlike `GET /boms` (no
 *    `q` param — BomsPage.tsx filters client-side), `GET /rfqs` accepts both
 *    `q` and repeated `status` query params (backend/src/app/api/v1/rfqs.py
 *    `list_rfqs`), so both are passed straight through as query params
 *    rather than filtering the loaded page in JS.
 *  - **Status filter is a row of toggle chips**, not a native
 *    `<select multiple>` — six options are easy to scan and toggle as chips,
 *    and it reuses the same "distinct badge per status" visual language
 *    (rfqs.css) the list/drawer already use elsewhere.
 *  - **Selecting "Archived" implies `include_archived=true`.** The backend
 *    repository ANDs `status IN (...)` with `archived_at IS NULL` unless
 *    `include_archived` is passed (backend/src/app/repositories/
 *    rfq_repository.py `_filters`) — an archived RFQ always has
 *    `archived_at` set, so without this the "Archived" chip would silently
 *    return zero rows. There is no separate "include archived" toolbar
 *    control (the brief's toolbar list is search + status filter + New RFQ
 *    only), so this derives the flag from the chip selection instead of
 *    adding an unrequested fourth control.
 *  - **List column "Lines" renders `RfqSummaryResponse.line_count` when
 *    present, "—" otherwise.** The field was added to the list route
 *    concurrently by a backend agent (P2 audit finding — this column used
 *    to always render "—"; see ./api.ts's file header for why it's typed
 *    optional rather than required). "—" is kept as the fallback for
 *    backward compatibility with any stale cached row shape, not removed
 *    outright — the same defensive posture PartsPage.tsx/BomsPage.tsx apply
 *    to their own genuine API-shape gaps.
 *  - **Edit gating mirrors the server rule exactly**: `RfqService.
 *    _ensure_draft_or_override` (backend/src/app/services/rfq_service.py)
 *    blocks metadata *and* line edits once status isn't `draft`, with an
 *    administrator-override escape hatch. This UI only implements the
 *    common path — editable while `draft`, a quiet lock hint otherwise — and
 *    does not surface the admin-override `{override_reason}` flow, which is
 *    a deliberate scope call for this workspace, not an oversight.
 *  - **Status-change actions are driven entirely by `ALLOWED_RFQ_TRANSITIONS`**
 *    (./api.ts, a client-side mirror of backend/src/app/models/rfqs.py's
 *    dict of the same name) — only legal next statuses ever render a button,
 *    and the server re-validates every transition regardless.
 *  - **No user-name resolution for status-history actors / audit actors.**
 *    backend/src/app/main.py mounts no `/users` (or similar) list endpoint,
 *    so `RfqStatusHistoryResponse.actor_user_id` is rendered as a raw
 *    (mono) UUID — the same kind of gap PartsPage.tsx already flags for
 *    `unit_definition_id` before ../boms/api.ts's `useUnits` existed.
 */

import type { ColumnDef } from "@tanstack/react-table";
import { useEffect, useMemo, useState } from "react";
import {
  useFieldArray,
  useForm,
  useWatch,
  type Control,
  type FieldErrors,
  type UseFormRegister,
  type UseFormSetValue,
} from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../../auth/session";
import "../../components/badges.css";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { DataTable } from "../../components/DataTable";
import { DetailRow } from "../../components/DetailRow";
import { Drawer } from "../../components/Drawer";
import { FormField } from "../../components/FormField";
import { PaginationBar } from "../../components/PaginationBar";
import { PlusIcon, SearchIcon, XIcon } from "../../components/icons";
import "../../components/workspace.css";
import { isDecimalString } from "../../lib/money";
import { isAnalystOrAbove } from "../../lib/roles";
import { useDebouncedValue } from "../../lib/useDebouncedValue";
import { zodResolver } from "../../lib/zodResolver";
import { DocumentsSection } from "../documents/DocumentsSection"; // ALLOWED insertion: quote documents, mounted below
import { ReviewPane } from "../extraction/ReviewPane"; // ALLOWED insertion: full-screen extraction review
import { type PartResponse, usePart, useParts } from "../parts/api";
import { QuotesSection } from "../quotes/QuotesSection"; // ALLOWED insertion: quote entry, mounted below
import { type SupplierResponse, useSupplier, useSuppliers } from "../suppliers/api";
import { type UnitDefinitionResponse, useBom, useBoms, useUnits } from "../boms/api";
import {
  ALLOWED_RFQ_TRANSITIONS,
  RFQ_STATUSES,
  type RfqCreateInput,
  type RfqLineInput,
  type RfqLineResponse,
  type RfqResponse,
  type RfqStatus,
  type RfqSummaryResponse,
  type RfqSupplierResponse,
  type RfqUpdateInput,
  useAddRfqLines,
  useChangeRfqStatus,
  useCreateRfq,
  useExcludeSupplier,
  useInviteSuppliers,
  useReinstateSupplier,
  useRemoveRfqLine,
  useRfq,
  useRfqs,
  useRfqStatusHistory,
  useRfqSuppliers,
  useUpdateRfq,
  useUpdateRfqLine,
} from "./api";
import "./rfqs.css";

const LIMIT = 50;
const CURRENCY_OPTIONS = ["USD", "EUR", "GBP", "JPY", "CNY", "MXN"] as const;

function isPositiveDecimal(v: string): boolean {
  return isDecimalString(v) && !v.startsWith("-") && !/^0+(\.0+)?$/.test(v);
}

function statusLabel(status: RfqStatus): string {
  return status
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** `due_date`/`requested_delivery_date` are plain calendar dates (`DATE`
 * columns, no timezone — backend/src/app/models/rfqs.py's file header cites
 * 00-decisions.md §4 #25's "organization-local business date" ruling).
 * Parsing via `new Date(y, m-1, d)` (local-timezone constructor) rather than
 * `new Date(raw)` (which parses "YYYY-MM-DD" as UTC midnight) avoids an
 * off-by-one day shift in negative-UTC-offset timezones. */
function formatDate(raw: string | null): string {
  if (!raw) return "—";
  const [y, m, d] = raw.split("-").map(Number);
  if (!y || !m || !d) return raw;
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Trims trailing fractional zeros for display without ever parsing the
 * decimal wire string through Number()/parseFloat (lib/money.ts's rule). */
function formatQuantity(raw: string): string {
  if (!raw.includes(".")) return raw;
  const trimmed = raw.replace(/0+$/, "").replace(/\.$/, "");
  return trimmed === "" ? "0" : trimmed;
}

/** Renders the line's spec-key count, or the app's standard missing-data
 * string when there is nothing recorded (`null` or an empty object) — a
 * fabricated `0` there would read as "confirmed zero specifications
 * required," which is not what an absent/empty value means (P2 audit
 * finding). A non-empty object's real count still renders as a plain
 * number: that IS supplied data. */
function formatSpecsCell(specs: Record<string, unknown> | null): string {
  const count = specs ? Object.keys(specs).length : 0;
  return count > 0 ? String(count) : "— not stated";
}

function RfqStatusBadge({ status }: { status: RfqStatus }) {
  return <span className={`badge rfq-badge--${status}`}>{statusLabel(status)}</span>;
}

// --- part / BOM / supplier pickers (search-as-you-type) --------------------

function PartPicker({
  onSelect,
  placeholder,
}: {
  onSelect: (id: string, label: string) => void;
  placeholder?: string;
}) {
  const [query, setQuery] = useState("");
  const debounced = useDebouncedValue(query, 300);
  const searchQuery = useParts({ q: debounced || undefined, limit: 8, offset: 0 });
  const results = searchQuery.data?.items ?? [];

  return (
    <div className="rfq-part-picker">
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={placeholder ?? "Search parts by number or name…"}
      />
      {debounced && results.length > 0 && (
        <ul className="alt-list">
          {results.map((p: PartResponse) => (
            <li key={p.id}>
              <button
                type="button"
                className="btn-ghost-sm"
                onClick={() => onSelect(p.id, `${p.internal_part_number} — ${p.name}`)}
              >
                {p.internal_part_number} — {p.name}
              </button>
            </li>
          ))}
        </ul>
      )}
      {debounced && results.length === 0 && !searchQuery.isLoading && (
        <span className="detail-label">No matches.</span>
      )}
    </div>
  );
}

/** Active BOMs only — `RfqService.create` rejects a non-`active` source BOM
 * (backend/src/app/services/rfq_service.py: "only an active BOM version can
 * be exploded into an RFQ"). `GET /boms` has no `q` param (../boms/api.ts's
 * file header), so this filters the loaded page client-side, same caveat
 * BomsPage.tsx's own search box documents. */
function BomPicker({ onSelect }: { onSelect: (id: string, label: string) => void }) {
  const [query, setQuery] = useState("");
  const debounced = useDebouncedValue(query, 300);
  const bomsQuery = useBoms({ include_archived: false, all_versions: false, limit: 100, offset: 0 });
  const activeBoms = (bomsQuery.data?.items ?? []).filter((b) => b.status === "active");
  const q = debounced.trim().toLowerCase();
  const results = q
    ? activeBoms.filter(
        (b) => b.name.toLowerCase().includes(q) || b.product_name.toLowerCase().includes(q),
      )
    : activeBoms;

  return (
    <div className="rfq-part-picker">
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search active BOMs by name or product…"
      />
      {bomsQuery.isLoading ? (
        <span className="detail-label">Loading…</span>
      ) : results.length > 0 ? (
        <ul className="alt-list">
          {results.slice(0, 8).map((b) => (
            <li key={b.id}>
              <button
                type="button"
                className="btn-ghost-sm"
                onClick={() => onSelect(b.id, `${b.name} — ${b.product_name} (v${b.version_number})`)}
              >
                {b.name} — {b.product_name} (v{b.version_number})
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <span className="detail-label">No active BOMs found.</span>
      )}
    </div>
  );
}

function SupplierInvitePicker({
  rfqId,
  alreadyInvitedIds,
}: {
  rfqId: string;
  alreadyInvitedIds: Set<string>;
}) {
  const [query, setQuery] = useState("");
  const debounced = useDebouncedValue(query, 300);
  const searchQuery = useSuppliers({ q: debounced || undefined, limit: 8, offset: 0 });
  const inviteMutation = useInviteSuppliers();
  const results = (searchQuery.data?.items ?? []).filter((s) => !alreadyInvitedIds.has(s.id));

  async function handleInvite(supplierId: string) {
    try {
      await inviteMutation.mutateAsync({ id: rfqId, supplierIds: [supplierId] });
      setQuery("");
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <div className="rfq-part-picker">
      <ApiErrorBanner error={inviteMutation.error} />
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search suppliers to invite…"
      />
      {debounced && results.length > 0 && (
        <ul className="alt-list">
          {results.map((s: SupplierResponse) => (
            <li key={s.id}>
              <button
                type="button"
                className="btn-ghost-sm"
                disabled={inviteMutation.isPending}
                onClick={() => void handleInvite(s.id)}
              >
                {s.code} — {s.name}
              </button>
            </li>
          ))}
        </ul>
      )}
      {debounced && results.length === 0 && !searchQuery.isLoading && (
        <span className="detail-label">No matches.</span>
      )}
    </div>
  );
}

// --- create form: line editor row -----------------------------------------

const rfqLineFormSchema = z.object({
  part_id: z.string(),
  // Display-only — never sent to the API. Kept inside the RHF field array
  // (not separate component state) so useFieldArray's append/remove keeps
  // it in sync by index automatically — same pattern as BomLineFormRow.
  part_label: z.string(),
  required_quantity: z.string(),
  unit_definition_id: z.string(),
  notes: z.string(),
});

type RfqLineFormValues = z.infer<typeof rfqLineFormSchema>;

const CREATE_MODES = ["inline", "from_bom"] as const;
type CreateMode = (typeof CREATE_MODES)[number];

// No per-field zod refinements on `rfqLineFormSchema` itself: `z.array(...)`
// would validate every element unconditionally, including the placeholder
// line still sitting in `lines` while `mode === "from_bom"`. Line validation
// therefore runs only inside `superRefine`, only for the branch that's
// actually active — the same "flat dotted-path" issue keys BomLineFormRow's
// `err()` helper reads (lib/zodResolver.ts's file header) still apply, since
// `ctx.addIssue({ path: ["lines", i, field] })` joins to `"lines.i.field"`.
const rfqFormSchema = z
  .object({
    name: z.string().trim().min(1, "Name is required").max(200, "Max 200 characters"),
    internal_reference: z
      .string()
      .trim()
      .min(1, "Internal reference is required")
      .max(100, "Max 100 characters"),
    base_currency: z
      .string()
      .trim()
      .refine((v) => (CURRENCY_OPTIONS as readonly string[]).includes(v), "Select a currency"),
    due_date: z.string().trim().min(1, "Due date is required"),
    requested_delivery_date: z.string().trim(),
    requested_payment_terms: z.string().trim().max(200, "Max 200 characters"),
    requested_incoterm: z.string().trim().max(100, "Max 100 characters"),
    notes: z.string().trim().max(4000, "Max 4000 characters"),
    mode: z.enum(CREATE_MODES),
    lines: z.array(rfqLineFormSchema),
    source_bom_id: z.string().trim(),
    source_bom_label: z.string(),
    assembly_quantity: z.string().trim(),
  })
  .superRefine((val, ctx) => {
    if (val.mode === "inline") {
      if (val.lines.length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "At least one line is required",
          path: ["lines"],
        });
      }
      val.lines.forEach((line, i) => {
        if (!line.part_id.trim()) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Select a part",
            path: ["lines", i, "part_id"],
          });
        }
        const qty = line.required_quantity.trim();
        if (qty === "") {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Quantity is required",
            path: ["lines", i, "required_quantity"],
          });
        } else if (!isDecimalString(qty)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Must be a decimal number, e.g. 10.5",
            path: ["lines", i, "required_quantity"],
          });
        } else if (qty.startsWith("-")) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Must not be negative",
            path: ["lines", i, "required_quantity"],
          });
        } else if (/^0+(\.0+)?$/.test(qty)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Must be greater than zero",
            path: ["lines", i, "required_quantity"],
          });
        }
        if (line.notes.length > 4000) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Max 4000 characters",
            path: ["lines", i, "notes"],
          });
        }
      });
    } else {
      if (!val.source_bom_id.trim()) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Select a BOM",
          path: ["source_bom_id"],
        });
      }
      const aq = val.assembly_quantity.trim();
      if (aq !== "" && !isPositiveDecimal(aq)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Must be a positive decimal number, e.g. 1.5",
          path: ["assembly_quantity"],
        });
      }
    }
  });

type RfqFormValues = z.infer<typeof rfqFormSchema>;

const emptyLine: RfqLineFormValues = {
  part_id: "",
  part_label: "",
  required_quantity: "",
  unit_definition_id: "",
  notes: "",
};

const emptyFormValues: RfqFormValues = {
  name: "",
  internal_reference: "",
  base_currency: "USD",
  due_date: "",
  requested_delivery_date: "",
  requested_payment_terms: "",
  requested_incoterm: "",
  notes: "",
  mode: "inline",
  lines: [emptyLine],
  source_bom_id: "",
  source_bom_label: "",
  assembly_quantity: "",
};

function toLineInput(l: RfqLineFormValues): RfqLineInput {
  return {
    part_id: l.part_id.trim(),
    required_quantity: l.required_quantity.trim(),
    unit_definition_id: l.unit_definition_id.trim() || null,
    required_specifications: null,
    notes: l.notes.trim() || null,
  };
}

function toCreatePayload(v: RfqFormValues): RfqCreateInput {
  const base = {
    name: v.name.trim(),
    internal_reference: v.internal_reference.trim(),
    base_currency: v.base_currency,
    due_date: v.due_date,
    requested_delivery_date: v.requested_delivery_date.trim() || null,
    requested_payment_terms: v.requested_payment_terms.trim() || null,
    requested_incoterm: v.requested_incoterm.trim() || null,
    notes: v.notes.trim() || null,
  };
  if (v.mode === "inline") {
    return { ...base, lines: v.lines.map(toLineInput), source_bom_id: null, assembly_quantity: null };
  }
  return {
    ...base,
    lines: [],
    source_bom_id: v.source_bom_id.trim(),
    assembly_quantity: v.assembly_quantity.trim() || null,
  };
}

function RfqLineFormRow({
  index,
  control,
  register,
  setValue,
  errors,
  units,
  onRemove,
  canRemove,
}: {
  index: number;
  control: Control<RfqFormValues>;
  register: UseFormRegister<RfqFormValues>;
  setValue: UseFormSetValue<RfqFormValues>;
  errors: FieldErrors<RfqFormValues>;
  units: UnitDefinitionResponse[];
  onRemove: () => void;
  canRemove: boolean;
}) {
  const partId = useWatch({ control, name: `lines.${index}.part_id` });
  const partLabel = useWatch({ control, name: `lines.${index}.part_label` });

  // See this file's header on `rfqFormSchema`: issues on array fields are
  // stored under flat dotted-path keys ("lines.0.part_id"), not react-hook-
  // form's usual nested `errors.lines[i].field` tree.
  const flatErrors = errors as unknown as Record<string, { message?: string } | undefined>;
  const err = (field: string) => flatErrors[`lines.${index}.${field}`]?.message;

  const resolvedPart = usePart(partLabel ? null : partId || null);
  const partDisplay =
    partLabel ||
    (resolvedPart.data ? `${resolvedPart.data.internal_part_number} — ${resolvedPart.data.name}` : partId);

  return (
    <div className="rfq-line-row">
      <div className="rfq-line-row-header">
        <span className="detail-label">Line {index + 1}</span>
        {canRemove && (
          <button
            type="button"
            className="alt-row-remove"
            aria-label={`Remove line ${index + 1}`}
            onClick={onRemove}
          >
            <XIcon size={12} />
          </button>
        )}
      </div>
      <div className="form-grid">
        <FormField label="Part" error={err("part_id")} full>
          {partId ? (
            <span className="detail-label">
              {partDisplay}{" "}
              <button
                type="button"
                className="btn-ghost-sm"
                onClick={() => {
                  setValue(`lines.${index}.part_id`, "", { shouldValidate: true });
                  setValue(`lines.${index}.part_label`, "");
                }}
              >
                Change
              </button>
            </span>
          ) : (
            <PartPicker
              onSelect={(id, label) => {
                setValue(`lines.${index}.part_id`, id, { shouldValidate: true });
                setValue(`lines.${index}.part_label`, label);
              }}
            />
          )}
        </FormField>
        <FormField label="Quantity" error={err("required_quantity")}>
          <input
            {...register(`lines.${index}.required_quantity`)}
            aria-invalid={!!err("required_quantity")}
            inputMode="decimal"
          />
        </FormField>
        <FormField label="Unit" hint="optional — defaults to the part's own unit">
          <select {...register(`lines.${index}.unit_definition_id`)}>
            <option value="">(same as part)</option>
            {units.map((u) => (
              <option key={u.id} value={u.id}>
                {u.code} — {u.name}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Notes" hint="optional" full error={err("notes")}>
          <input {...register(`lines.${index}.notes`)} />
        </FormField>
      </div>
    </div>
  );
}

// --- create form ------------------------------------------------------

function RfqForm({
  onSaved,
  onCancel,
}: {
  onSaved: (r: RfqResponse) => void;
  onCancel: () => void;
}) {
  const createMutation = useCreateRfq();
  const unitsQuery = useUnits();
  const busy = createMutation.isPending;

  const {
    register,
    handleSubmit,
    control,
    setValue,
    watch,
    formState: { errors },
  } = useForm<RfqFormValues>({
    resolver: zodResolver(rfqFormSchema),
    defaultValues: emptyFormValues,
  });
  const { fields, append, remove } = useFieldArray({ control, name: "lines" });
  const mode = watch("mode");
  const sourceBomId = watch("source_bom_id");
  const sourceBomLabel = watch("source_bom_label");

  async function onValid(values: RfqFormValues) {
    try {
      const created = await createMutation.mutateAsync(toCreatePayload(values));
      onSaved(created);
    } catch {
      // surfaced via ApiErrorBanner reading createMutation.error
    }
  }

  const units = unitsQuery.data?.items ?? [];
  const flatErrors = errors as unknown as Record<string, { message?: string } | undefined>;
  const linesError = flatErrors["lines"]?.message;

  function setMode(next: CreateMode) {
    setValue("mode", next, { shouldValidate: true });
  }

  return (
    <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
      <ApiErrorBanner error={createMutation.error} />
      <div className="form-grid">
        <FormField label="Name" error={errors.name?.message}>
          <input {...register("name")} aria-invalid={!!errors.name} autoComplete="off" />
        </FormField>
        <FormField label="Internal reference" error={errors.internal_reference?.message}>
          <input
            {...register("internal_reference")}
            aria-invalid={!!errors.internal_reference}
            autoComplete="off"
          />
        </FormField>
        <FormField label="Base currency" error={errors.base_currency?.message}>
          <select {...register("base_currency")}>
            {CURRENCY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Due date" error={errors.due_date?.message}>
          <input type="date" {...register("due_date")} aria-invalid={!!errors.due_date} />
        </FormField>
        <FormField label="Requested delivery date" hint="optional">
          <input type="date" {...register("requested_delivery_date")} />
        </FormField>
        <FormField label="Payment terms" hint="optional" error={errors.requested_payment_terms?.message}>
          <input {...register("requested_payment_terms")} />
        </FormField>
        <FormField label="Incoterm" hint="optional" error={errors.requested_incoterm?.message}>
          <input {...register("requested_incoterm")} />
        </FormField>
        <FormField label="Notes" hint="optional" full error={errors.notes?.message}>
          <textarea {...register("notes")} rows={3} />
        </FormField>
      </div>

      <section className="detail-section">
        <span className="field-label">Line source</span>
        <div className="mode-toggle" role="group" aria-label="Line source">
          <button
            type="button"
            className="mode-toggle-btn"
            aria-pressed={mode === "inline"}
            onClick={() => setMode("inline")}
          >
            Inline lines
          </button>
          <button
            type="button"
            className="mode-toggle-btn"
            aria-pressed={mode === "from_bom"}
            onClick={() => setMode("from_bom")}
          >
            From BOM
          </button>
        </div>
      </section>

      {mode === "inline" ? (
        <section className="detail-section">
          <div className="detail-header-row">
            <h3 className="detail-section-title">Lines</h3>
            <button type="button" className="btn-secondary-sm" onClick={() => append(emptyLine)}>
              <PlusIcon /> Add line
            </button>
          </div>
          {linesError && (
            <div className="field-error" role="alert">
              {linesError}
            </div>
          )}
          <div className="rfq-line-list">
            {fields.map((field, index) => (
              <RfqLineFormRow
                key={field.id}
                index={index}
                control={control}
                register={register}
                setValue={setValue}
                errors={errors}
                units={units}
                onRemove={() => remove(index)}
                canRemove={fields.length > 1}
              />
            ))}
          </div>
        </section>
      ) : (
        <section className="detail-section">
          <h3 className="detail-section-title">Source BOM</h3>
          <div className="form-grid">
            <FormField label="BOM" error={errors.source_bom_id?.message} full>
              {sourceBomId ? (
                <span className="detail-label">
                  {sourceBomLabel || sourceBomId}{" "}
                  <button
                    type="button"
                    className="btn-ghost-sm"
                    onClick={() => {
                      setValue("source_bom_id", "", { shouldValidate: true });
                      setValue("source_bom_label", "");
                    }}
                  >
                    Change
                  </button>
                </span>
              ) : (
                <BomPicker
                  onSelect={(id, label) => {
                    setValue("source_bom_id", id, { shouldValidate: true });
                    setValue("source_bom_label", label);
                  }}
                />
              )}
            </FormField>
            <FormField
              label="Assembly quantity"
              hint="optional — defaults to 1"
              error={errors.assembly_quantity?.message}
            >
              <input {...register("assembly_quantity")} inputMode="decimal" placeholder="1" />
            </FormField>
          </div>
        </section>
      )}

      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="submit" className="btn-primary-sm" disabled={busy}>
          {busy ? "Creating…" : "Create RFQ"}
        </button>
      </div>
    </form>
  );
}

// --- header edit form (draft-only) -----------------------------------------

const rfqHeaderFormSchema = z.object({
  name: z.string().trim().min(1, "Name is required").max(200, "Max 200 characters"),
  internal_reference: z
    .string()
    .trim()
    .min(1, "Internal reference is required")
    .max(100, "Max 100 characters"),
  base_currency: z
    .string()
    .trim()
    .refine((v) => (CURRENCY_OPTIONS as readonly string[]).includes(v), "Select a currency"),
  due_date: z.string().trim().min(1, "Due date is required"),
  requested_delivery_date: z.string().trim(),
  requested_payment_terms: z.string().trim().max(200, "Max 200 characters"),
  requested_incoterm: z.string().trim().max(100, "Max 100 characters"),
  notes: z.string().trim().max(4000, "Max 4000 characters"),
});

type RfqHeaderFormValues = z.infer<typeof rfqHeaderFormSchema>;

function toHeaderFormValues(r: RfqResponse): RfqHeaderFormValues {
  return {
    name: r.name,
    internal_reference: r.internal_reference,
    base_currency: r.base_currency,
    due_date: r.due_date,
    requested_delivery_date: r.requested_delivery_date ?? "",
    requested_payment_terms: r.requested_payment_terms ?? "",
    requested_incoterm: r.requested_incoterm ?? "",
    notes: r.notes ?? "",
  };
}

function toUpdateInput(v: RfqHeaderFormValues): RfqUpdateInput {
  return {
    name: v.name.trim(),
    internal_reference: v.internal_reference.trim(),
    base_currency: v.base_currency,
    due_date: v.due_date,
    requested_delivery_date: v.requested_delivery_date.trim() || null,
    requested_payment_terms: v.requested_payment_terms.trim() || null,
    requested_incoterm: v.requested_incoterm.trim() || null,
    notes: v.notes.trim() || null,
  };
}

function RfqHeaderForm({
  rfq,
  onSaved,
  onCancel,
}: {
  rfq: RfqResponse;
  onSaved: (r: RfqResponse) => void;
  onCancel: () => void;
}) {
  const updateMutation = useUpdateRfq();
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<RfqHeaderFormValues>({
    resolver: zodResolver(rfqHeaderFormSchema),
    defaultValues: toHeaderFormValues(rfq),
  });

  async function onValid(values: RfqHeaderFormValues) {
    try {
      const updated = await updateMutation.mutateAsync({
        id: rfq.id,
        version: rfq.version,
        data: toUpdateInput(values),
      });
      onSaved(updated);
    } catch {
      // surfaced via ApiErrorBanner reading updateMutation.error
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
      <ApiErrorBanner error={updateMutation.error} />
      <div className="form-grid">
        <FormField label="Name" error={errors.name?.message}>
          <input {...register("name")} aria-invalid={!!errors.name} autoComplete="off" />
        </FormField>
        <FormField label="Internal reference" error={errors.internal_reference?.message}>
          <input
            {...register("internal_reference")}
            aria-invalid={!!errors.internal_reference}
            autoComplete="off"
          />
        </FormField>
        <FormField label="Base currency" error={errors.base_currency?.message}>
          <select {...register("base_currency")}>
            {CURRENCY_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Due date" error={errors.due_date?.message}>
          <input type="date" {...register("due_date")} aria-invalid={!!errors.due_date} />
        </FormField>
        <FormField label="Requested delivery date" hint="optional">
          <input type="date" {...register("requested_delivery_date")} />
        </FormField>
        <FormField label="Payment terms" hint="optional" error={errors.requested_payment_terms?.message}>
          <input {...register("requested_payment_terms")} />
        </FormField>
        <FormField label="Incoterm" hint="optional" error={errors.requested_incoterm?.message}>
          <input {...register("requested_incoterm")} />
        </FormField>
        <FormField label="Notes" hint="optional" full error={errors.notes?.message}>
          <textarea {...register("notes")} rows={3} />
        </FormField>
      </div>
      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onCancel} disabled={updateMutation.isPending}>
          Cancel
        </button>
        <button type="submit" className="btn-primary-sm" disabled={updateMutation.isPending}>
          {updateMutation.isPending ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

// --- status timeline ---------------------------------------------------

function StatusTimeline({ id }: { id: string }) {
  const historyQuery = useRfqStatusHistory(id);
  const items = useMemo(() => {
    const list = historyQuery.data?.items ?? [];
    // Chronological read order (oldest first) — the route's own ordering
    // isn't documented, so this sorts client-side to guarantee it rather
    // than assume server insertion order.
    return [...list].sort((a, b) => a.occurred_at.localeCompare(b.occurred_at));
  }, [historyQuery.data]);

  if (historyQuery.isLoading) {
    return <p className="detail-label">Loading…</p>;
  }
  if (items.length === 0) {
    return <p className="detail-label">No status changes yet.</p>;
  }
  return (
    <ul className="status-timeline">
      {items.map((h) => (
        <li key={h.id} className="status-timeline-item">
          <span className="status-timeline-transition">
            {statusLabel(h.from_status)} → {statusLabel(h.to_status)}
          </span>
          <span className="status-timeline-meta">
            {new Date(h.occurred_at).toLocaleString()} · <span className="mono">{h.actor_user_id}</span>
          </span>
          {h.note && <span className="status-timeline-note">{h.note}</span>}
        </li>
      ))}
    </ul>
  );
}

// --- status-change actions --------------------------------------------

function StatusChangeActions({ rfq, canWrite }: { rfq: RfqResponse; canWrite: boolean }) {
  const [pending, setPending] = useState<RfqStatus | null>(null);
  const [note, setNote] = useState("");
  const changeMutation = useChangeRfqStatus();
  const allowed = ALLOWED_RFQ_TRANSITIONS[rfq.status];

  if (!canWrite || allowed.length === 0) return null;

  async function handleConfirm() {
    if (!pending) return;
    try {
      await changeMutation.mutateAsync({ id: rfq.id, to_status: pending, reason: note.trim() || undefined });
      setPending(null);
      setNote("");
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <section className="detail-section">
      <div className="detail-actions">
        {allowed.map((s) => (
          <button
            key={s}
            type="button"
            className={s === "archived" ? "btn-danger-sm" : "btn-secondary-sm"}
            onClick={() => {
              setPending(s);
              setNote("");
            }}
          >
            Move to {statusLabel(s)}
          </button>
        ))}
      </div>
      <ApiErrorBanner error={changeMutation.error} />
      {pending && (
        <div className="status-change-panel">
          <label className="field">
            <span className="field-label">Note (optional)</span>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={`Why is this RFQ moving to ${statusLabel(pending)}?`}
              autoFocus
            />
          </label>
          <div className="form-actions">
            <button type="button" className="btn-ghost-sm" onClick={() => setPending(null)}>
              Cancel
            </button>
            <button
              type="button"
              className={pending === "archived" ? "btn-danger-sm" : "btn-primary-sm"}
              onClick={() => void handleConfirm()}
              disabled={changeMutation.isPending}
            >
              {changeMutation.isPending ? "Saving…" : `Confirm: move to ${statusLabel(pending)}`}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

// --- lines section (drawer detail) -------------------------------------

function AddRfqLineForm({
  rfqId,
  units,
  onDone,
}: {
  rfqId: string;
  units: UnitDefinitionResponse[];
  onDone: () => void;
}) {
  const addMutation = useAddRfqLines();
  const [partId, setPartId] = useState("");
  const [partLabel, setPartLabel] = useState("");
  const [qty, setQty] = useState("");
  const [unitId, setUnitId] = useState("");
  const [notes, setNotes] = useState("");
  const [qtyError, setQtyError] = useState<string | undefined>();

  async function handleAdd() {
    if (!partId) return;
    if (!isPositiveDecimal(qty)) {
      setQtyError("Must be a positive decimal number, e.g. 10.5");
      return;
    }
    setQtyError(undefined);
    try {
      const line: RfqLineInput = {
        part_id: partId,
        required_quantity: qty.trim(),
        unit_definition_id: unitId || null,
        required_specifications: null,
        notes: notes.trim() || null,
      };
      await addMutation.mutateAsync({ id: rfqId, lines: [line] });
      onDone();
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <div className="rfq-add-line-wrap">
      <div className="rfq-line-edit-row">
        <FormField label="Part">
          {partId ? (
            <span className="detail-label">
              {partLabel || partId}{" "}
              <button
                type="button"
                className="btn-ghost-sm"
                onClick={() => {
                  setPartId("");
                  setPartLabel("");
                }}
              >
                Change
              </button>
            </span>
          ) : (
            <PartPicker
              onSelect={(id, label) => {
                setPartId(id);
                setPartLabel(label);
              }}
            />
          )}
        </FormField>
        <FormField label="Quantity" error={qtyError}>
          <input value={qty} onChange={(e) => setQty(e.target.value)} inputMode="decimal" />
        </FormField>
        <FormField label="Unit" hint="optional">
          <select value={unitId} onChange={(e) => setUnitId(e.target.value)}>
            <option value="">(same as part)</option>
            {units.map((u) => (
              <option key={u.id} value={u.id}>
                {u.code} — {u.name}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Notes" hint="optional">
          <input value={notes} onChange={(e) => setNotes(e.target.value)} />
        </FormField>
        <button
          type="button"
          className="btn-primary-sm"
          disabled={!partId || addMutation.isPending}
          onClick={() => void handleAdd()}
        >
          {addMutation.isPending ? "Adding…" : "Add"}
        </button>
        <button type="button" className="btn-ghost-sm" onClick={onDone}>
          Cancel
        </button>
      </div>
      <ApiErrorBanner error={addMutation.error} />
    </div>
  );
}

function RfqLineRow({
  rfqId,
  line,
  unitsById,
  units,
  editable,
}: {
  rfqId: string;
  line: RfqLineResponse;
  unitsById: Map<string, UnitDefinitionResponse>;
  units: UnitDefinitionResponse[];
  editable: boolean;
}) {
  const partQuery = usePart(line.part_id);
  const unit = unitsById.get(line.unit_definition_id);
  const [editing, setEditing] = useState(false);
  const [qty, setQty] = useState(line.required_quantity);
  const [unitId, setUnitId] = useState(line.unit_definition_id);
  const [notes, setNotes] = useState(line.notes ?? "");
  const [qtyError, setQtyError] = useState<string | undefined>();
  const updateMutation = useUpdateRfqLine();
  const removeMutation = useRemoveRfqLine();

  function startEdit() {
    setQty(line.required_quantity);
    setUnitId(line.unit_definition_id);
    setNotes(line.notes ?? "");
    setQtyError(undefined);
    setEditing(true);
  }

  async function handleSave() {
    if (!isPositiveDecimal(qty)) {
      setQtyError("Must be a positive decimal number, e.g. 10.5");
      return;
    }
    setQtyError(undefined);
    try {
      await updateMutation.mutateAsync({
        rfqId,
        lineId: line.id,
        data: { required_quantity: qty.trim(), unit_definition_id: unitId || null, notes: notes.trim() || null },
      });
      setEditing(false);
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  async function handleRemove() {
    try {
      await removeMutation.mutateAsync({ rfqId, lineId: line.id });
    } catch {
      // surfaced via inline field-error below
    }
  }

  if (editing) {
    return (
      <tr>
        <td colSpan={editable ? 6 : 5}>
          <div className="rfq-line-edit-row">
            <FormField label="Quantity" error={qtyError}>
              <input value={qty} onChange={(e) => setQty(e.target.value)} inputMode="decimal" />
            </FormField>
            <FormField label="Unit" hint="optional">
              <select value={unitId} onChange={(e) => setUnitId(e.target.value)}>
                <option value="">(same as part)</option>
                {units.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.code} — {u.name}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="Notes" hint="optional">
              <input value={notes} onChange={(e) => setNotes(e.target.value)} />
            </FormField>
            <button
              type="button"
              className="btn-primary-sm"
              disabled={updateMutation.isPending}
              onClick={() => void handleSave()}
            >
              {updateMutation.isPending ? "Saving…" : "Save"}
            </button>
            <button type="button" className="btn-ghost-sm" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
          <ApiErrorBanner error={updateMutation.error} />
        </td>
      </tr>
    );
  }

  return (
    <tr>
      <td className="mono">
        {partQuery.data ? `${partQuery.data.internal_part_number} — ${partQuery.data.name}` : line.part_id}
      </td>
      <td data-align="right" className="mono">
        {formatQuantity(line.required_quantity)}
      </td>
      <td className="mono">{unit ? unit.code : line.unit_definition_id}</td>
      <td data-align="right">{formatSpecsCell(line.required_specifications)}</td>
      <td>{line.notes ?? "—"}</td>
      {editable && (
        <td>
          <div className="detail-actions">
            <button type="button" className="btn-ghost-sm" onClick={startEdit}>
              Edit
            </button>
            <button
              type="button"
              className="alt-row-remove"
              aria-label={`Remove line ${line.line_number}`}
              disabled={removeMutation.isPending}
              onClick={() => void handleRemove()}
            >
              <XIcon size={12} />
            </button>
          </div>
          {removeMutation.isError && (
            <div className="field-error">
              {removeMutation.error instanceof Error ? removeMutation.error.message : "Failed to remove line."}
            </div>
          )}
        </td>
      )}
    </tr>
  );
}

function RfqLinesSection({
  rfq,
  unitsById,
  units,
  canEdit,
}: {
  rfq: RfqResponse;
  unitsById: Map<string, UnitDefinitionResponse>;
  units: UnitDefinitionResponse[];
  canEdit: boolean;
}) {
  const [adding, setAdding] = useState(false);

  return (
    <section className="detail-section">
      <div className="detail-header-row">
        <h3 className="detail-section-title">Lines</h3>
        {canEdit && !adding && (
          <button type="button" className="btn-secondary-sm" onClick={() => setAdding(true)}>
            <PlusIcon /> Add line
          </button>
        )}
      </div>
      <div className="data-table-wrap">
        <table className="rfq-lines-table">
          <thead>
            <tr>
              <th>Part</th>
              <th data-align="right">Quantity</th>
              <th>Unit</th>
              <th data-align="right">Specs</th>
              <th>Notes</th>
              {canEdit && <th>Actions</th>}
            </tr>
          </thead>
          <tbody>
            {rfq.lines.length === 0 ? (
              <tr>
                <td colSpan={canEdit ? 6 : 5} className="detail-label">
                  No lines yet.
                </td>
              </tr>
            ) : (
              rfq.lines.map((l) => (
                <RfqLineRow
                  key={l.id}
                  rfqId={rfq.id}
                  line={l}
                  unitsById={unitsById}
                  units={units}
                  editable={canEdit}
                />
              ))
            )}
          </tbody>
        </table>
      </div>
      {adding && <AddRfqLineForm rfqId={rfq.id} units={units} onDone={() => setAdding(false)} />}
    </section>
  );
}

// --- suppliers section (drawer detail) ----------------------------------

function RfqSupplierRow({
  rfqId,
  rs,
  canWrite,
}: {
  rfqId: string;
  rs: RfqSupplierResponse;
  canWrite: boolean;
}) {
  const supplierQuery = useSupplier(rs.supplier_id);
  const [excluding, setExcluding] = useState(false);
  const [reason, setReason] = useState("");
  const excludeMutation = useExcludeSupplier();
  const reinstateMutation = useReinstateSupplier();
  const isExcluded = rs.excluded_at !== null;

  async function handleExcludeConfirm() {
    if (reason.trim() === "") return;
    try {
      await excludeMutation.mutateAsync({ rfqId, rfqSupplierId: rs.id, reason: reason.trim() });
      setExcluding(false);
      setReason("");
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  async function handleReinstate() {
    try {
      await reinstateMutation.mutateAsync({ rfqId, rfqSupplierId: rs.id });
    } catch {
      // surfaced via ApiErrorBanner below
    }
  }

  return (
    <li className={`rfq-supplier-row${isExcluded ? " is-excluded" : ""}`}>
      <div className="rfq-supplier-row-top">
        <div className="rfq-supplier-row-main">
          <span>{supplierQuery.data ? `${supplierQuery.data.code} — ${supplierQuery.data.name}` : rs.supplier_id}</span>
          <span className="detail-label">
            Invited {new Date(rs.invited_at).toLocaleDateString()}
            {isExcluded && rs.excluded_at
              ? ` · Excluded ${new Date(rs.excluded_at).toLocaleDateString()}: ${rs.exclusion_reason ?? ""}`
              : ""}
          </span>
        </div>
        {canWrite && (
          <div className="rfq-supplier-row-actions">
            {!isExcluded && !excluding && (
              <button type="button" className="btn-danger-sm" onClick={() => setExcluding(true)}>
                Exclude
              </button>
            )}
            {isExcluded && (
              <button
                type="button"
                className="btn-secondary-sm"
                disabled={reinstateMutation.isPending}
                onClick={() => void handleReinstate()}
              >
                {reinstateMutation.isPending ? "Reinstating…" : "Reinstate"}
              </button>
            )}
          </div>
        )}
      </div>
      {excluding && (
        <div className="status-change-panel">
          <label className="field">
            <span className="field-label">Exclusion reason</span>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Why is this supplier being excluded?"
              autoFocus
            />
          </label>
          <div className="form-actions">
            <button type="button" className="btn-ghost-sm" onClick={() => setExcluding(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-danger-sm"
              disabled={reason.trim() === "" || excludeMutation.isPending}
              onClick={() => void handleExcludeConfirm()}
            >
              {excludeMutation.isPending ? "Excluding…" : "Confirm exclude"}
            </button>
          </div>
        </div>
      )}
      <ApiErrorBanner error={excludeMutation.error ?? reinstateMutation.error} />
    </li>
  );
}

function RfqSupplierSection({ rfq, canWrite }: { rfq: RfqResponse; canWrite: boolean }) {
  const suppliersQuery = useRfqSuppliers(rfq.id);
  const [inviting, setInviting] = useState(false);
  const items = suppliersQuery.data?.items ?? [];
  const alreadyInvitedIds = useMemo(() => new Set(items.map((s) => s.supplier_id)), [items]);
  // Invite is only valid while draft/open (RfqService.invite_suppliers).
  const canInvite = canWrite && (rfq.status === "draft" || rfq.status === "open");

  return (
    <section className="detail-section">
      <div className="detail-header-row">
        <h3 className="detail-section-title">Invited suppliers</h3>
        {canInvite && (
          <button type="button" className="btn-secondary-sm" onClick={() => setInviting((v) => !v)}>
            <PlusIcon /> {inviting ? "Done" : "Invite suppliers"}
          </button>
        )}
      </div>
      {inviting && <SupplierInvitePicker rfqId={rfq.id} alreadyInvitedIds={alreadyInvitedIds} />}
      {suppliersQuery.isLoading ? (
        <p className="detail-label">Loading…</p>
      ) : items.length === 0 ? (
        <p className="detail-label">No suppliers invited yet.</p>
      ) : (
        <ul className="rfq-supplier-list">
          {items.map((rs) => (
            <RfqSupplierRow key={rs.id} rfqId={rfq.id} rs={rs} canWrite={canWrite} />
          ))}
        </ul>
      )}
    </section>
  );
}

// --- detail drawer -------------------------------------------------------

function RfqDetail({
  id,
  canWrite,
  onClosed,
  onReviewExtraction,
}: {
  id: string;
  canWrite: boolean;
  onClosed: () => void;
  onReviewExtraction: (runId: string) => void;
}) {
  const { data: rfq, isLoading, isError, error, refetch } = useRfq(id);
  const unitsQuery = useUnits();
  const sourceBomQuery = useBom(rfq?.source_bom_id ?? null);
  const [editingHeader, setEditingHeader] = useState(false);

  const unitsById = useMemo(
    () => new Map((unitsQuery.data?.items ?? []).map((u) => [u.id, u] as const)),
    [unitsQuery.data],
  );
  const units = unitsQuery.data?.items ?? [];

  if (isLoading) {
    return <p className="detail-label">Loading…</p>;
  }
  if (isError || !rfq) {
    return (
      <div>
        {error ? (
          <ApiErrorBanner error={error} />
        ) : (
          <div className="banner-error" role="alert">
            RFQ not found.
          </div>
        )}
        <button type="button" className="btn-ghost-sm" onClick={() => void refetch()}>
          Retry
        </button>
      </div>
    );
  }

  if (editingHeader) {
    return (
      <RfqHeaderForm
        rfq={rfq}
        onSaved={() => setEditingHeader(false)}
        onCancel={() => setEditingHeader(false)}
      />
    );
  }

  // Mirrors RfqService._ensure_draft_or_override's common path: editable
  // only while draft. The administrator-override escape hatch is
  // deliberately not surfaced here — see this file's header.
  const canEditNow = canWrite && rfq.status === "draft" && !rfq.is_archived;

  return (
    <div>
      <div className="detail-header-row">
        <RfqStatusBadge status={rfq.status} />
        {canEditNow && (
          <button type="button" className="btn-secondary-sm" onClick={() => setEditingHeader(true)}>
            Edit
          </button>
        )}
      </div>
      {canWrite && !canEditNow && (
        <p className="lock-hint">Header fields and lines are editable only while status is draft.</p>
      )}

      <StatusChangeActions rfq={rfq} canWrite={canWrite} />

      <section className="detail-section">
        <h3 className="detail-section-title">Overview</h3>
        <div className="detail-grid">
          <DetailRow label="Name" value={rfq.name} />
          <DetailRow label="Internal reference" value={rfq.internal_reference} mono />
          <DetailRow label="Base currency" value={rfq.base_currency} />
          <DetailRow label="Due date" value={formatDate(rfq.due_date)} />
          <DetailRow label="Requested delivery date" value={formatDate(rfq.requested_delivery_date)} />
          <DetailRow label="Payment terms" value={rfq.requested_payment_terms ?? "—"} />
          <DetailRow label="Incoterm" value={rfq.requested_incoterm ?? "—"} />
          <DetailRow label="Version" value={String(rfq.version)} mono />
          <DetailRow label="Updated" value={new Date(rfq.updated_at).toLocaleString()} />
          {rfq.source_bom_id && (
            <DetailRow
              label="Exploded from BOM"
              value={
                sourceBomQuery.data
                  ? `${sourceBomQuery.data.name} v${sourceBomQuery.data.version_number}`
                  : rfq.source_bom_id
              }
              mono={!sourceBomQuery.data}
            />
          )}
          <DetailRow label="Notes" value={rfq.notes ?? "—"} full />
        </div>
      </section>

      {rfq.is_archived && (
        <section className="detail-section">
          <h3 className="detail-section-title">Archive record</h3>
          <div className="detail-grid">
            <DetailRow
              label="Archived at"
              value={rfq.archived_at ? new Date(rfq.archived_at).toLocaleString() : "—"}
            />
            <DetailRow label="Reason" value={rfq.archive_reason ?? "—"} full />
          </div>
        </section>
      )}

      <section className="detail-section">
        <h3 className="detail-section-title">Status timeline</h3>
        <StatusTimeline id={rfq.id} />
      </section>

      <RfqLinesSection rfq={rfq} unitsById={unitsById} units={units} canEdit={canEditNow} />

      <RfqSupplierSection rfq={rfq} canWrite={canWrite} />

      {/* ALLOWED insertion: quote documents + extraction entry point */}
      <DocumentsSection rfq={rfq} onReviewExtraction={onReviewExtraction} />

      <QuotesSection rfq={rfq} /> {/* ALLOWED insertion: quote entry section */}

      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onClosed}>
          Close
        </button>
      </div>
    </div>
  );
}

// --- page -------------------------------------------------------------

type DrawerState = { mode: "create" } | { mode: "view"; id: string } | null;

export function RfqsPage() {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);

  const [searchInput, setSearchInput] = useState("");
  const debouncedSearch = useDebouncedValue(searchInput, 300);
  const [statusFilter, setStatusFilter] = useState<Set<RfqStatus>>(new Set());
  const [offset, setOffset] = useState(0);
  const [drawerState, setDrawerState] = useState<DrawerState>(null);
  // ALLOWED insertion: "Review extraction" wiring — a full-screen surface
  // (../extraction/ReviewPane.tsx), rendered as a page-level sibling of the
  // RFQ drawer rather than nested inside it (see that file's own header for
  // why), so it needs its own state here rather than living inside
  // RfqDetail's local state.
  const [reviewingRunId, setReviewingRunId] = useState<string | null>(null);

  useEffect(() => {
    setOffset(0);
  }, [debouncedSearch, statusFilter]);

  const listParams = useMemo(
    () => ({
      q: debouncedSearch.trim() || undefined,
      status: statusFilter.size > 0 ? Array.from(statusFilter) : undefined,
      // See this file's header: selecting the "Archived" chip must also
      // widen the archived-row filter, or the repository's default
      // `archived_at IS NULL` clause hides every row that status filter
      // would otherwise match.
      include_archived: statusFilter.has("archived"),
      limit: LIMIT,
      offset,
    }),
    [debouncedSearch, statusFilter, offset],
  );

  const rfqsQuery = useRfqs(listParams);
  const items = rfqsQuery.data?.items ?? [];

  function toggleStatus(s: RfqStatus) {
    setStatusFilter((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  }

  const columns = useMemo<ColumnDef<RfqSummaryResponse>[]>(
    () => [
      {
        id: "internal_reference",
        accessorKey: "internal_reference",
        header: "Reference",
        meta: { mono: true },
      },
      {
        id: "name",
        accessorKey: "name",
        header: "Name",
      },
      {
        id: "status",
        header: "Status",
        enableSorting: false,
        cell: (ctx) => <RfqStatusBadge status={ctx.row.original.status} />,
      },
      {
        id: "due_date",
        accessorKey: "due_date",
        header: "Due",
        cell: (ctx) => formatDate(ctx.getValue<string>()),
      },
      {
        id: "delivery_date",
        accessorKey: "requested_delivery_date",
        header: "Delivery",
        cell: (ctx) => formatDate(ctx.getValue<string | null>()),
      },
      {
        id: "line_count",
        header: "Lines",
        enableSorting: false,
        meta: { align: "right" },
        // Present once the backend's widened list contract lands for a given
        // row; "—" for backward compat otherwise — see this file's header.
        cell: (ctx) => {
          const v = ctx.row.original.line_count;
          return typeof v === "number" ? v : "—";
        },
      },
      {
        id: "updated",
        accessorKey: "updated_at",
        header: "Updated",
        cell: (ctx) => new Date(ctx.getValue<string>()).toLocaleDateString(),
      },
    ],
    [],
  );

  function closeDrawer() {
    setDrawerState(null);
  }

  const drawerTitle =
    drawerState?.mode === "create" ? "New RFQ" : drawerState?.mode === "view" ? "RFQ detail" : "";

  return (
    <section className="rfqs-page">
      <header className="page-toolbar">
        <div className="page-heading">
          <h1>Requests for quotation</h1>
          <p>Track supplier outreach from draft through award.</p>
        </div>
        <div className="toolbar-controls">
          <label className="search-field">
            <SearchIcon />
            <input
              type="text"
              placeholder="Search RFQs…"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              aria-label="Search RFQs"
            />
          </label>
          <div className="status-filter" role="group" aria-label="Filter by status">
            {RFQ_STATUSES.map((s) => (
              <button
                key={s}
                type="button"
                className="status-filter-chip"
                aria-pressed={statusFilter.has(s)}
                onClick={() => toggleStatus(s)}
              >
                {statusLabel(s)}
              </button>
            ))}
          </div>
          {canWrite && (
            <button
              type="button"
              className="btn-primary-sm"
              onClick={() => setDrawerState({ mode: "create" })}
            >
              <PlusIcon /> New RFQ
            </button>
          )}
        </div>
      </header>

      <DataTable
        ariaLabel="Requests for quotation"
        columns={columns}
        data={items}
        getRowId={(r) => r.id}
        isLoading={rfqsQuery.isLoading}
        isError={rfqsQuery.isError}
        errorMessage={rfqsQuery.error instanceof Error ? rfqsQuery.error.message : undefined}
        onRetry={() => void rfqsQuery.refetch()}
        emptyTitle="No RFQs found"
        emptyDescription={
          debouncedSearch
            ? `No RFQs match "${debouncedSearch}".`
            : "Create your first RFQ to get started."
        }
        onRowClick={(r) => setDrawerState({ mode: "view", id: r.id })}
        rowClassName={(r) => (r.is_archived ? "is-muted" : undefined)}
      />

      <PaginationBar
        total={rfqsQuery.data?.page.total ?? 0}
        limit={LIMIT}
        offset={offset}
        onOffsetChange={setOffset}
      />

      <Drawer open={drawerState !== null} onClose={closeDrawer} title={drawerTitle}>
        {drawerState?.mode === "create" && (
          <RfqForm
            onSaved={(created) => setDrawerState({ mode: "view", id: created.id })}
            onCancel={closeDrawer}
          />
        )}
        {drawerState?.mode === "view" && (
          <RfqDetail
            key={drawerState.id}
            id={drawerState.id}
            canWrite={canWrite}
            onClosed={closeDrawer}
            onReviewExtraction={setReviewingRunId}
          />
        )}
      </Drawer>

      {/* ALLOWED insertion: "Review extraction" wiring line */}
      {reviewingRunId && (
        <ReviewPane runId={reviewingRunId} onClose={() => setReviewingRunId(null)} />
      )}
    </section>
  );
}
