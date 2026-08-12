/** Suppliers workspace: dense list + search/filter toolbar + pagination,
 * with a right-side drawer for create/view/edit/archive.
 *
 * Design decisions:
 *  - Row click opens the shared `Drawer` (see components/Drawer.tsx's
 *    file-level comment for why a drawer was chosen over a `/suppliers/:id`
 *    route).
 *  - Column sort is client-side over the current page — see DataTable.tsx's
 *    file-level comment: `GET /suppliers` has no `sort` query parameter.
 *  - Archive uses an inline reason panel inside the drawer (textarea +
 *    Confirm/Cancel) rather than a native `window.prompt()`, to stay inside
 *    the design system's token/motion rules and keep the destructive action
 *    reviewable before it fires.
 */

import type { ColumnDef } from "@tanstack/react-table";
import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { DataTable } from "../../components/DataTable";
import { DetailRow } from "../../components/DetailRow";
import { Drawer } from "../../components/Drawer";
import { FormField } from "../../components/FormField";
import { PageEyebrow } from "../../components/PageEyebrow";
import { PaginationBar } from "../../components/PaginationBar";
import { PlusIcon, SearchIcon } from "../../components/icons";
import { StatusBadge } from "../../components/StatusBadge";
import "../../components/workspace.css";
import { useAuth } from "../../auth/session";
import { compareDecimalStrings } from "../../lib/decimalSort";
import { formatMoney, isDecimalString } from "../../lib/money";
import { isAdministratorOrAbove, isAnalystOrAbove } from "../../lib/roles";
import { useDebouncedValue } from "../../lib/useDebouncedValue";
import { zodResolver } from "../../lib/zodResolver";
import {
  type SupplierResponse,
  type SupplierWriteInput,
  useArchiveSupplier,
  useCreateSupplier,
  useSupplier,
  useSuppliers,
  useUnarchiveSupplier,
  useUpdateSupplier,
} from "./api";

const LIMIT = 50;

const decimalField = z
  .string()
  .trim()
  .refine((v) => v === "" || isDecimalString(v), "Must be a decimal number, e.g. 12.5")
  .refine((v) => v === "" || !v.startsWith("-"), "Must not be negative");

const supplierFormSchema = z.object({
  code: z.string().trim().min(1, "Code is required").max(64, "Max 64 characters"),
  name: z.string().trim().min(1, "Name is required").max(200, "Max 200 characters"),
  country_code: z
    .string()
    .trim()
    .toUpperCase()
    .regex(/^[A-Z]{2}$/, "2-letter ISO country code, e.g. US"),
  supported_currencies: z
    .string()
    .trim()
    .refine(
      (v) => v === "" || v.split(",").every((c) => /^[A-Za-z]{3}$/.test(c.trim())),
      "Comma-separated 3-letter currency codes, e.g. USD, EUR",
    ),
  standard_payment_terms: z.string().trim().max(200, "Max 200 characters"),
  standard_incoterm: z.string().trim().max(100, "Max 100 characters"),
  typical_lead_time_days: z
    .string()
    .trim()
    .refine((v) => v === "" || /^\d+$/.test(v), "Whole number of days, 0 or more"),
  capacity_units_per_month: decimalField,
  default_moq: decimalField,
  is_active: z.boolean(),
});

type SupplierFormValues = z.infer<typeof supplierFormSchema>;

const emptyFormValues: SupplierFormValues = {
  code: "",
  name: "",
  country_code: "",
  supported_currencies: "",
  standard_payment_terms: "",
  standard_incoterm: "",
  typical_lead_time_days: "",
  capacity_units_per_month: "",
  default_moq: "",
  is_active: true,
};

function toFormValues(s: SupplierResponse): SupplierFormValues {
  return {
    code: s.code,
    name: s.name,
    country_code: s.country_code,
    supported_currencies: s.supported_currencies.join(", "),
    standard_payment_terms: s.standard_payment_terms ?? "",
    standard_incoterm: s.standard_incoterm ?? "",
    typical_lead_time_days: s.typical_lead_time_days !== null ? String(s.typical_lead_time_days) : "",
    capacity_units_per_month: s.capacity_units_per_month ?? "",
    default_moq: s.default_moq ?? "",
    is_active: s.is_active,
  };
}

function toWriteInput(v: SupplierFormValues): SupplierWriteInput {
  return {
    code: v.code.trim(),
    name: v.name.trim(),
    country_code: v.country_code.trim().toUpperCase(),
    supported_currencies: v.supported_currencies
      .split(",")
      .map((c) => c.trim().toUpperCase())
      .filter(Boolean),
    standard_payment_terms: v.standard_payment_terms.trim() || null,
    standard_incoterm: v.standard_incoterm.trim() || null,
    typical_lead_time_days:
      v.typical_lead_time_days.trim() === "" ? null : Number(v.typical_lead_time_days),
    capacity_units_per_month:
      v.capacity_units_per_month.trim() === "" ? null : v.capacity_units_per_month.trim(),
    default_moq: v.default_moq.trim() === "" ? null : v.default_moq.trim(),
    is_active: v.is_active,
  };
}

function SupplierForm({
  mode,
  initial,
  onSaved,
  onCancel,
}: {
  mode: "create" | "edit";
  initial?: SupplierResponse;
  onSaved: (s: SupplierResponse) => void;
  onCancel: () => void;
}) {
  const createMutation = useCreateSupplier();
  const updateMutation = useUpdateSupplier();
  const busy = createMutation.isPending || updateMutation.isPending;
  const mutationError = createMutation.error ?? updateMutation.error;

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<SupplierFormValues>({
    resolver: zodResolver(supplierFormSchema),
    defaultValues: initial ? toFormValues(initial) : emptyFormValues,
  });

  async function onValid(values: SupplierFormValues) {
    const data = toWriteInput(values);
    try {
      if (mode === "create") {
        const created = await createMutation.mutateAsync(data);
        onSaved(created);
      } else if (initial) {
        const updated = await updateMutation.mutateAsync({
          id: initial.id,
          version: initial.version,
          data,
        });
        onSaved(updated);
      }
    } catch {
      // surfaced via ApiErrorBanner reading createMutation.error / updateMutation.error
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
      <ApiErrorBanner error={mutationError} />
      <div className="form-grid">
        <FormField label="Code" error={errors.code?.message}>
          <input {...register("code")} aria-invalid={!!errors.code} autoComplete="off" />
        </FormField>
        <FormField label="Name" error={errors.name?.message}>
          <input {...register("name")} aria-invalid={!!errors.name} autoComplete="off" />
        </FormField>
        <FormField label="Country code" hint="ISO 3166-1 alpha-2" error={errors.country_code?.message}>
          <input {...register("country_code")} aria-invalid={!!errors.country_code} maxLength={2} />
        </FormField>
        <FormField
          label="Currencies"
          hint="comma-separated, e.g. USD, EUR"
          error={errors.supported_currencies?.message}
        >
          <input {...register("supported_currencies")} aria-invalid={!!errors.supported_currencies} />
        </FormField>
        <FormField label="Lead time (days)" hint="optional" error={errors.typical_lead_time_days?.message}>
          <input
            {...register("typical_lead_time_days")}
            aria-invalid={!!errors.typical_lead_time_days}
            inputMode="numeric"
          />
        </FormField>
        <FormField label="MOQ" hint="optional, decimal" error={errors.default_moq?.message}>
          <input {...register("default_moq")} aria-invalid={!!errors.default_moq} inputMode="decimal" />
        </FormField>
        <FormField
          label="Monthly capacity"
          hint="optional, decimal units"
          error={errors.capacity_units_per_month?.message}
        >
          <input
            {...register("capacity_units_per_month")}
            aria-invalid={!!errors.capacity_units_per_month}
            inputMode="decimal"
          />
        </FormField>
        <FormField label="Payment terms" hint="optional" error={errors.standard_payment_terms?.message}>
          <input {...register("standard_payment_terms")} aria-invalid={!!errors.standard_payment_terms} />
        </FormField>
        <FormField label="Incoterm" hint="optional" error={errors.standard_incoterm?.message}>
          <input {...register("standard_incoterm")} aria-invalid={!!errors.standard_incoterm} />
        </FormField>
        <FormField label="Active" checkbox>
          <input type="checkbox" {...register("is_active")} />
        </FormField>
      </div>
      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="submit" className="btn-primary-sm" disabled={busy}>
          {busy ? "Saving…" : mode === "create" ? "Create supplier" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

function SupplierDetail({
  id,
  canWrite,
  canArchive,
  onClosed,
}: {
  id: string;
  canWrite: boolean;
  canArchive: boolean;
  onClosed: () => void;
}) {
  const { data: supplier, isLoading, isError, error, refetch } = useSupplier(id);
  const [editing, setEditing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [archiveReason, setArchiveReason] = useState("");
  const archiveMutation = useArchiveSupplier();
  const unarchiveMutation = useUnarchiveSupplier();

  if (isLoading) {
    return <p className="detail-label">Loading…</p>;
  }
  if (isError || !supplier) {
    return (
      <div>
        {error ? (
          <ApiErrorBanner error={error} />
        ) : (
          <div className="banner-error" role="alert">
            Supplier not found.
          </div>
        )}
        <button type="button" className="btn-ghost-sm" onClick={() => void refetch()}>
          Retry
        </button>
      </div>
    );
  }

  if (editing) {
    return (
      <SupplierForm
        mode="edit"
        initial={supplier}
        onSaved={() => setEditing(false)}
        onCancel={() => setEditing(false)}
      />
    );
  }

  async function handleArchiveConfirm() {
    if (!supplier || archiveReason.trim() === "") return;
    try {
      await archiveMutation.mutateAsync({ id: supplier.id, reason: archiveReason.trim() });
      setArchiving(false);
      setArchiveReason("");
    } catch {
      // surfaced via ApiErrorBanner reading archiveMutation.error
    }
  }

  async function handleUnarchive() {
    if (!supplier) return;
    try {
      await unarchiveMutation.mutateAsync({ id: supplier.id, reason: "Restored from archive" });
    } catch {
      // surfaced via ApiErrorBanner reading unarchiveMutation.error
    }
  }

  return (
    <div>
      <div className="detail-header-row">
        <StatusBadge isActive={supplier.is_active} isArchived={supplier.is_archived} />
        <div className="detail-actions">
          {canWrite && !supplier.is_archived && (
            <button type="button" className="btn-secondary-sm" onClick={() => setEditing(true)}>
              Edit
            </button>
          )}
          {canArchive && !supplier.is_archived && (
            <button type="button" className="btn-danger-sm" onClick={() => setArchiving(true)}>
              Archive
            </button>
          )}
          {canArchive && supplier.is_archived && (
            <button
              type="button"
              className="btn-secondary-sm"
              onClick={() => void handleUnarchive()}
              disabled={unarchiveMutation.isPending}
            >
              {unarchiveMutation.isPending ? "Restoring…" : "Restore"}
            </button>
          )}
        </div>
      </div>

      <ApiErrorBanner error={archiveMutation.error ?? unarchiveMutation.error} />

      <section className="detail-section">
        <h3 className="detail-section-title">Overview</h3>
        <div className="detail-grid">
          <DetailRow label="Code" value={supplier.code} mono />
          <DetailRow label="Name" value={supplier.name} />
          <DetailRow label="Country" value={supplier.country_code} />
          <DetailRow label="Currencies" value={supplier.supported_currencies.join(", ") || "—"} />
          <DetailRow
            label="Lead time"
            value={supplier.typical_lead_time_days !== null ? `${supplier.typical_lead_time_days} days` : "—"}
            num
          />
          <DetailRow
            label="MOQ"
            value={supplier.default_moq !== null ? formatMoney(supplier.default_moq) : "—"}
            num
          />
          <DetailRow
            label="Monthly capacity"
            value={
              supplier.capacity_units_per_month !== null
                ? formatMoney(supplier.capacity_units_per_month)
                : "—"
            }
            num
          />
          <DetailRow label="Payment terms" value={supplier.standard_payment_terms ?? "—"} />
          <DetailRow label="Incoterm" value={supplier.standard_incoterm ?? "—"} />
        </div>
      </section>

      {supplier.is_archived && (
        <section className="detail-section">
          <h3 className="detail-section-title">Archive record</h3>
          <div className="detail-grid">
            <DetailRow
              label="Archived at"
              value={supplier.archived_at ? new Date(supplier.archived_at).toLocaleString() : "—"}
            />
            <DetailRow label="Reason" value={supplier.archive_reason ?? "—"} full />
          </div>
        </section>
      )}

      {archiving && (
        <div className="archive-panel">
          <label className="field">
            <span className="field-label">Archive reason</span>
            <textarea
              value={archiveReason}
              onChange={(e) => setArchiveReason(e.target.value)}
              placeholder="Why is this supplier being archived?"
              autoFocus
            />
          </label>
          <div className="form-actions">
            <button type="button" className="btn-ghost-sm" onClick={() => setArchiving(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-danger-sm"
              onClick={() => void handleArchiveConfirm()}
              disabled={archiveReason.trim() === "" || archiveMutation.isPending}
            >
              {archiveMutation.isPending ? "Archiving…" : "Confirm archive"}
            </button>
          </div>
        </div>
      )}

      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onClosed}>
          Close
        </button>
      </div>
    </div>
  );
}

type DrawerState = { mode: "create" } | { mode: "view"; id: string } | null;

export function SuppliersPage() {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);
  const canArchive = isAdministratorOrAbove(session?.role);

  const [searchInput, setSearchInput] = useState("");
  const debouncedSearch = useDebouncedValue(searchInput, 300);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [offset, setOffset] = useState(0);
  const [drawerState, setDrawerState] = useState<DrawerState>(null);

  useEffect(() => {
    setOffset(0);
  }, [debouncedSearch, includeArchived]);

  const listParams = useMemo(
    () => ({
      q: debouncedSearch.trim() || undefined,
      include_archived: includeArchived,
      limit: LIMIT,
      offset,
    }),
    [debouncedSearch, includeArchived, offset],
  );

  const suppliersQuery = useSuppliers(listParams);
  const items = suppliersQuery.data?.items ?? [];

  const columns = useMemo<ColumnDef<SupplierResponse>[]>(
    () => [
      {
        id: "code",
        accessorKey: "code",
        header: "Code",
        meta: { mono: true },
      },
      {
        id: "name",
        accessorKey: "name",
        header: "Name",
      },
      {
        id: "country",
        accessorKey: "country_code",
        header: "Country",
      },
      {
        id: "currencies",
        accessorFn: (row) => row.supported_currencies.join(", "),
        header: "Currencies",
        enableSorting: false,
        cell: (ctx) => ctx.getValue<string>() || "—",
      },
      {
        id: "lead_time",
        accessorKey: "typical_lead_time_days",
        header: "Lead time (d)",
        meta: { align: "right" },
        cell: (ctx) => {
          const v = ctx.getValue<number | null>();
          return v === null ? "—" : v;
        },
      },
      {
        id: "moq",
        accessorFn: (row) => row.default_moq,
        header: "MOQ",
        meta: { align: "right" },
        sortingFn: (a, b) => compareDecimalStrings(a.original.default_moq, b.original.default_moq),
        cell: (ctx) => {
          const v = ctx.getValue<string | null>();
          return v === null ? "—" : formatMoney(v);
        },
      },
      {
        id: "status",
        header: "Status",
        enableSorting: false,
        cell: (ctx) => (
          <StatusBadge isActive={ctx.row.original.is_active} isArchived={ctx.row.original.is_archived} />
        ),
      },
    ],
    [],
  );

  function closeDrawer() {
    setDrawerState(null);
  }

  const drawerTitle =
    drawerState?.mode === "create"
      ? "New supplier"
      : drawerState?.mode === "view"
        ? "Supplier detail"
        : "";

  return (
    <section className="suppliers-page">
      <header className="page-toolbar">
        <div className="page-heading">
          <PageEyebrow hue="suppliers">Suppliers</PageEyebrow>
          <h1>Suppliers</h1>
          <p>Vendor master data: currencies, lead times, and order minimums.</p>
        </div>
        <div className="toolbar-controls">
          <label className="search-field">
            <SearchIcon />
            <input
              type="text"
              placeholder="Search suppliers…"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              aria-label="Search suppliers"
            />
          </label>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
            />
            Include archived
          </label>
          {canWrite && (
            <button
              type="button"
              className="btn-primary-sm"
              onClick={() => setDrawerState({ mode: "create" })}
            >
              <PlusIcon /> New supplier
            </button>
          )}
        </div>
      </header>

      <DataTable
        ariaLabel="Suppliers"
        columns={columns}
        data={items}
        getRowId={(s) => s.id}
        isLoading={suppliersQuery.isLoading}
        isError={suppliersQuery.isError}
        errorMessage={
          suppliersQuery.error instanceof Error ? suppliersQuery.error.message : undefined
        }
        onRetry={() => void suppliersQuery.refetch()}
        emptyTitle="No suppliers found"
        emptyDescription={
          debouncedSearch
            ? `No suppliers match "${debouncedSearch}".`
            : "Create your first supplier to get started."
        }
        onRowClick={(s) => setDrawerState({ mode: "view", id: s.id })}
        rowClassName={(s) => (s.is_archived ? "is-muted" : undefined)}
      />

      <PaginationBar
        total={suppliersQuery.data?.page.total ?? 0}
        limit={LIMIT}
        offset={offset}
        onOffsetChange={setOffset}
      />

      <Drawer
        open={drawerState !== null}
        onClose={closeDrawer}
        title={drawerTitle}
        panelClassName="hue-panel-top hue-panel-top--suppliers"
      >
        {drawerState?.mode === "create" && (
          <SupplierForm
            mode="create"
            onSaved={(created) => setDrawerState({ mode: "view", id: created.id })}
            onCancel={closeDrawer}
          />
        )}
        {drawerState?.mode === "view" && (
          <SupplierDetail
            key={drawerState.id}
            id={drawerState.id}
            canWrite={canWrite}
            canArchive={canArchive}
            onClosed={closeDrawer}
          />
        )}
      </Drawer>
    </section>
  );
}
