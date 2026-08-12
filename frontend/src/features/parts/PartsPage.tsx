/** Parts workspace: dense list + search/category filters + pagination, with
 * a right-side drawer for create/view/edit/archive and an alternatives list.
 *
 * Design decisions mirror SuppliersPage.tsx (see its file-level comment for
 * the drawer-vs-route and client-side-sort rationale) plus:
 *  - **Unit resolution** (P1 audit finding: "creating and editing require a
 *    raw unit-definition UUID... list rows display UUID fragments, and
 *    units ha[ve] no frontend workflow"). `GET /api/v1/units` IS mounted
 *    (backend/src/app/api/v1/units.py) and already has a typed hook —
 *    `useUnits()` in ../boms/api.ts, the same one RfqsPage.tsx/BomsPage.tsx
 *    use to resolve their own `unit_definition_id` columns/pickers. The
 *    create/edit form's "Unit" field is a `<select>` of `"code — name"`
 *    options (mirrors RfqLineFormRow's line-unit `<select>`); the list's
 *    "Unit" column and the detail drawer's "Unit" row resolve the id
 *    through a `unitsById` map the same way BomLineViewRow/RfqLineRow do,
 *    falling back to the raw id (mono) only if a part somehow references a
 *    unit this org's catalogue no longer returns.
 *  - Alternatives (internal-by-search / external-by-MPN) live in their own
 *    section of the detail view, using the same `useParts` search hook the
 *    table itself uses for the internal picker.
 */

import type { ColumnDef } from "@tanstack/react-table";
import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../../auth/session";
import { ApiErrorBanner } from "../../components/ApiErrorBanner";
import { DataTable } from "../../components/DataTable";
import { DetailRow } from "../../components/DetailRow";
import { Drawer } from "../../components/Drawer";
import { FormField } from "../../components/FormField";
import { PaginationBar } from "../../components/PaginationBar";
import { PlusIcon, SearchIcon, XIcon } from "../../components/icons";
import { StatusBadge } from "../../components/StatusBadge";
import "../../components/workspace.css";
import { compareDecimalStrings } from "../../lib/decimalSort";
import { formatMoney, isDecimalString } from "../../lib/money";
import { isAdministratorOrAbove, isAnalystOrAbove } from "../../lib/roles";
import { useDebouncedValue } from "../../lib/useDebouncedValue";
import { zodResolver } from "../../lib/zodResolver";
import { useUnits } from "../boms/api";
import { PartImportPanel } from "../imports/PartImportPanel"; // ALLOWED insertion: part-import workflow
import {
  type ApprovalStatus,
  type PartResponse,
  type PartWriteInput,
  useAddPartAlternative,
  useArchivePart,
  useCreatePart,
  usePart,
  usePartAlternatives,
  useParts,
  useRemovePartAlternative,
  useUnarchivePart,
  useUpdatePart,
} from "./api";

const LIMIT = 50;

const decimalField = z
  .string()
  .trim()
  .refine((v) => v === "" || isDecimalString(v), "Must be a decimal number, e.g. 10.50")
  .refine((v) => v === "" || !v.startsWith("-"), "Must not be negative");

const partFormSchema = z
  .object({
    internal_part_number: z
      .string()
      .trim()
      .min(1, "Internal part number is required")
      .max(100, "Max 100 characters"),
    manufacturer_part_number: z.string().trim().max(100, "Max 100 characters"),
    manufacturer: z.string().trim().max(200, "Max 200 characters"),
    name: z.string().trim().min(1, "Name is required").max(200, "Max 200 characters"),
    description: z.string().trim().max(4000, "Max 4000 characters"),
    category: z.string().trim().max(100, "Max 100 characters"),
    unit_definition_id: z.string().trim().min(1, "Select a unit"),
    target_price: decimalField,
    target_price_currency: z
      .string()
      .trim()
      .toUpperCase()
      .refine((v) => v === "" || /^[A-Z]{3}$/.test(v), "3-letter ISO currency code, e.g. USD"),
    is_active: z.boolean(),
  })
  .superRefine((values, ctx) => {
    const hasPrice = values.target_price.trim() !== "";
    const hasCurrency = values.target_price_currency.trim() !== "";
    if (hasPrice === hasCurrency) return;
    const message = "Target price and currency must both be set, or both left blank.";
    if (!hasCurrency) ctx.addIssue({ code: z.ZodIssueCode.custom, message, path: ["target_price_currency"] });
    if (!hasPrice) ctx.addIssue({ code: z.ZodIssueCode.custom, message, path: ["target_price"] });
  });

type PartFormValues = z.infer<typeof partFormSchema>;

const emptyFormValues: PartFormValues = {
  internal_part_number: "",
  manufacturer_part_number: "",
  manufacturer: "",
  name: "",
  description: "",
  category: "",
  unit_definition_id: "",
  target_price: "",
  target_price_currency: "",
  is_active: true,
};

function toFormValues(p: PartResponse): PartFormValues {
  return {
    internal_part_number: p.internal_part_number,
    manufacturer_part_number: p.manufacturer_part_number ?? "",
    manufacturer: p.manufacturer ?? "",
    name: p.name,
    description: p.description ?? "",
    category: p.category ?? "",
    unit_definition_id: p.unit_definition_id,
    target_price: p.target_price ?? "",
    target_price_currency: p.target_price_currency ?? "",
    is_active: p.is_active,
  };
}

function toWriteInput(v: PartFormValues): PartWriteInput {
  const hasPrice = v.target_price.trim() !== "";
  return {
    internal_part_number: v.internal_part_number.trim(),
    manufacturer_part_number: v.manufacturer_part_number.trim() || null,
    manufacturer: v.manufacturer.trim() || null,
    name: v.name.trim(),
    description: v.description.trim() || null,
    category: v.category.trim() || null,
    unit_definition_id: v.unit_definition_id.trim(),
    target_price: hasPrice ? v.target_price.trim() : null,
    target_price_currency: hasPrice ? v.target_price_currency.trim().toUpperCase() : null,
    is_active: v.is_active,
  };
}

function PartForm({
  mode,
  initial,
  onSaved,
  onCancel,
}: {
  mode: "create" | "edit";
  initial?: PartResponse;
  onSaved: (p: PartResponse) => void;
  onCancel: () => void;
}) {
  const createMutation = useCreatePart();
  const updateMutation = useUpdatePart();
  const unitsQuery = useUnits();
  const units = unitsQuery.data?.items ?? [];
  const busy = createMutation.isPending || updateMutation.isPending;
  const mutationError = createMutation.error ?? updateMutation.error;

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<PartFormValues>({
    resolver: zodResolver(partFormSchema),
    defaultValues: initial ? toFormValues(initial) : emptyFormValues,
  });

  async function onValid(values: PartFormValues) {
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
        <FormField label="Internal part number" error={errors.internal_part_number?.message}>
          <input {...register("internal_part_number")} aria-invalid={!!errors.internal_part_number} autoComplete="off" />
        </FormField>
        <FormField label="Name" error={errors.name?.message}>
          <input {...register("name")} aria-invalid={!!errors.name} autoComplete="off" />
        </FormField>
        <FormField label="Manufacturer part number" hint="optional" error={errors.manufacturer_part_number?.message}>
          <input {...register("manufacturer_part_number")} aria-invalid={!!errors.manufacturer_part_number} />
        </FormField>
        <FormField label="Manufacturer" hint="optional" error={errors.manufacturer?.message}>
          <input {...register("manufacturer")} aria-invalid={!!errors.manufacturer} />
        </FormField>
        <FormField label="Category" hint="optional" error={errors.category?.message}>
          <input {...register("category")} aria-invalid={!!errors.category} />
        </FormField>
        <FormField label="Unit" error={errors.unit_definition_id?.message}>
          <select {...register("unit_definition_id")} aria-invalid={!!errors.unit_definition_id}>
            <option value="">Select unit…</option>
            {units.map((u) => (
              <option key={u.id} value={u.id}>
                {u.code} — {u.name}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Target price" hint="optional, decimal" error={errors.target_price?.message}>
          <input {...register("target_price")} aria-invalid={!!errors.target_price} inputMode="decimal" />
        </FormField>
        <FormField
          label="Target price currency"
          hint="required with target price"
          error={errors.target_price_currency?.message}
        >
          <input {...register("target_price_currency")} aria-invalid={!!errors.target_price_currency} maxLength={3} />
        </FormField>
        <FormField label="Description" hint="optional" full error={errors.description?.message}>
          <textarea {...register("description")} aria-invalid={!!errors.description} rows={3} />
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
          {busy ? "Saving…" : mode === "create" ? "Create part" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

function PartAlternativesSection({ partId, canWrite }: { partId: string; canWrite: boolean }) {
  const { data, isLoading, isError, error } = usePartAlternatives(partId);
  const addMutation = useAddPartAlternative(partId);
  const removeMutation = useRemovePartAlternative(partId);

  const [kind, setKind] = useState<"internal" | "external">("internal");
  const [internalQuery, setInternalQuery] = useState("");
  const debouncedInternalQuery = useDebouncedValue(internalQuery, 300);
  const [selectedPart, setSelectedPart] = useState<{ id: string; label: string } | null>(null);
  const [externalMpn, setExternalMpn] = useState("");
  const [approvalStatus, setApprovalStatus] = useState<ApprovalStatus>("conditional");
  const [rationale, setRationale] = useState("");
  const [pendingRemoveId, setPendingRemoveId] = useState<string | null>(null);

  const searchQuery = useParts({ q: debouncedInternalQuery || undefined, limit: 8, offset: 0 });
  const searchResults = (searchQuery.data?.items ?? []).filter((p) => p.id !== partId);

  async function handleAdd() {
    try {
      if (kind === "internal" && selectedPart) {
        await addMutation.mutateAsync({
          alternative_part_id: selectedPart.id,
          approval_status: approvalStatus,
          ...(rationale.trim() ? { rationale: rationale.trim() } : {}),
        });
      } else if (kind === "external" && externalMpn.trim() !== "") {
        await addMutation.mutateAsync({
          alternative_mpn: externalMpn.trim(),
          approval_status: approvalStatus,
          ...(rationale.trim() ? { rationale: rationale.trim() } : {}),
        });
      } else {
        return;
      }
      setSelectedPart(null);
      setInternalQuery("");
      setExternalMpn("");
      setRationale("");
    } catch {
      // surfaced via ApiErrorBanner reading addMutation.error
    }
  }

  async function handleRemove(id: string) {
    try {
      await removeMutation.mutateAsync(id);
      setPendingRemoveId(null);
    } catch {
      // surfaced via ApiErrorBanner reading removeMutation.error
    }
  }

  const canSubmitAdd =
    !addMutation.isPending && (kind === "internal" ? selectedPart !== null : externalMpn.trim() !== "");

  return (
    <section className="detail-section">
      <h3 className="detail-section-title">Alternatives</h3>
      <ApiErrorBanner error={addMutation.error ?? removeMutation.error} />

      {isLoading ? (
        <p className="detail-label">Loading…</p>
      ) : isError ? (
        <ApiErrorBanner error={error} />
      ) : (data?.items.length ?? 0) === 0 ? (
        <p className="detail-label">No alternatives recorded.</p>
      ) : (
        <ul className="alt-list">
          {(data?.items ?? []).map((alt) => (
            <li key={alt.id} className="alt-row">
              <div className="alt-row-main">
                <span>
                  {alt.alternative_part_id
                    ? `Internal · ${alt.alternative_part_id}`
                    : `External MPN · ${alt.alternative_mpn ?? "—"}`}
                </span>
                <span className="detail-label">
                  {alt.approval_status}
                  {alt.rationale ? ` — ${alt.rationale}` : ""}
                </span>
              </div>
              {canWrite &&
                (pendingRemoveId === alt.id ? (
                  <span className="detail-actions">
                    <button
                      type="button"
                      className="btn-danger-sm"
                      onClick={() => void handleRemove(alt.id)}
                      disabled={removeMutation.isPending}
                    >
                      {removeMutation.isPending ? "Removing…" : "Confirm"}
                    </button>
                    <button type="button" className="btn-ghost-sm" onClick={() => setPendingRemoveId(null)}>
                      Cancel
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="alt-row-remove"
                    aria-label={`Remove alternative ${alt.alternative_mpn ?? alt.alternative_part_id ?? ""}`}
                    onClick={() => setPendingRemoveId(alt.id)}
                  >
                    <XIcon size={12} />
                  </button>
                ))}
            </li>
          ))}
        </ul>
      )}

      {canWrite && (
        <div className="alt-add-row">
          <div className="field field--full">
            <span className="field-label">Add alternative</span>
            <div className="detail-actions">
              <button
                type="button"
                className={kind === "internal" ? "btn-secondary-sm" : "btn-ghost-sm"}
                onClick={() => setKind("internal")}
              >
                Internal part
              </button>
              <button
                type="button"
                className={kind === "external" ? "btn-secondary-sm" : "btn-ghost-sm"}
                onClick={() => setKind("external")}
              >
                External MPN
              </button>
            </div>
          </div>

          {kind === "internal" ? (
            <div className="field field--full">
              <span className="field-label">Search parts by name or number</span>
              <input
                type="text"
                value={internalQuery}
                onChange={(e) => {
                  setInternalQuery(e.target.value);
                  setSelectedPart(null);
                }}
                placeholder="Start typing…"
              />
              {selectedPart ? (
                <span className="detail-label">
                  Selected: {selectedPart.label}{" "}
                  <button type="button" className="btn-ghost-sm" onClick={() => setSelectedPart(null)}>
                    Change
                  </button>
                </span>
              ) : debouncedInternalQuery && searchResults.length > 0 ? (
                <ul className="alt-list">
                  {searchResults.map((p) => (
                    <li key={p.id}>
                      <button
                        type="button"
                        className="btn-ghost-sm"
                        onClick={() =>
                          setSelectedPart({ id: p.id, label: `${p.internal_part_number} — ${p.name}` })
                        }
                      >
                        {p.internal_part_number} — {p.name}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : debouncedInternalQuery ? (
                <span className="detail-label">No matches.</span>
              ) : null}
            </div>
          ) : (
            <FormField label="Manufacturer part number">
              <input type="text" value={externalMpn} onChange={(e) => setExternalMpn(e.target.value)} />
            </FormField>
          )}

          <FormField label="Approval status">
            <select value={approvalStatus} onChange={(e) => setApprovalStatus(e.target.value as ApprovalStatus)}>
              <option value="approved">Approved</option>
              <option value="conditional">Conditional</option>
              <option value="rejected">Rejected</option>
            </select>
          </FormField>
          <FormField label="Rationale" hint="optional">
            <input type="text" value={rationale} onChange={(e) => setRationale(e.target.value)} />
          </FormField>

          <div className="form-actions">
            <button
              type="button"
              className="btn-primary-sm"
              onClick={() => void handleAdd()}
              disabled={!canSubmitAdd}
            >
              {addMutation.isPending ? "Adding…" : "Add alternative"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function PartDetail({
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
  const { data: part, isLoading, isError, error, refetch } = usePart(id);
  const unitsQuery = useUnits();
  const unitsById = useMemo(
    () => new Map((unitsQuery.data?.items ?? []).map((u) => [u.id, u] as const)),
    [unitsQuery.data],
  );
  const [editing, setEditing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [archiveReason, setArchiveReason] = useState("");
  const archiveMutation = useArchivePart();
  const unarchiveMutation = useUnarchivePart();

  if (isLoading) {
    return <p className="detail-label">Loading…</p>;
  }
  if (isError || !part) {
    return (
      <div>
        {error ? (
          <ApiErrorBanner error={error} />
        ) : (
          <div className="banner-error" role="alert">
            Part not found.
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
      <PartForm mode="edit" initial={part} onSaved={() => setEditing(false)} onCancel={() => setEditing(false)} />
    );
  }

  async function handleArchiveConfirm() {
    if (!part || archiveReason.trim() === "") return;
    try {
      await archiveMutation.mutateAsync({ id: part.id, reason: archiveReason.trim() });
      setArchiving(false);
      setArchiveReason("");
    } catch {
      // surfaced via ApiErrorBanner reading archiveMutation.error
    }
  }

  async function handleUnarchive() {
    if (!part) return;
    try {
      await unarchiveMutation.mutateAsync({ id: part.id, reason: "Restored from archive" });
    } catch {
      // surfaced via ApiErrorBanner reading unarchiveMutation.error
    }
  }

  const unit = unitsById.get(part.unit_definition_id);

  return (
    <div>
      <div className="detail-header-row">
        <StatusBadge isActive={part.is_active} isArchived={part.is_archived} />
        <div className="detail-actions">
          {canWrite && !part.is_archived && (
            <button type="button" className="btn-secondary-sm" onClick={() => setEditing(true)}>
              Edit
            </button>
          )}
          {canArchive && !part.is_archived && (
            <button type="button" className="btn-danger-sm" onClick={() => setArchiving(true)}>
              Archive
            </button>
          )}
          {canArchive && part.is_archived && (
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
          <DetailRow label="Internal part number" value={part.internal_part_number} mono />
          <DetailRow label="Manufacturer part number" value={part.manufacturer_part_number ?? "—"} mono />
          <DetailRow label="Name" value={part.name} />
          <DetailRow label="Manufacturer" value={part.manufacturer ?? "—"} />
          <DetailRow label="Category" value={part.category ?? "—"} />
          <DetailRow
            label="Unit"
            value={unit ? `${unit.code} — ${unit.name}` : part.unit_definition_id}
            mono={!unit}
          />
          <DetailRow
            label="Target price"
            value={
              part.target_price !== null && part.target_price_currency !== null
                ? formatMoney(part.target_price, { currency: part.target_price_currency })
                : "—"
            }
            num
          />
          <DetailRow label="Description" value={part.description ?? "—"} full />
        </div>
      </section>

      {part.is_archived && (
        <section className="detail-section">
          <h3 className="detail-section-title">Archive record</h3>
          <div className="detail-grid">
            <DetailRow
              label="Archived at"
              value={part.archived_at ? new Date(part.archived_at).toLocaleString() : "—"}
            />
            <DetailRow label="Reason" value={part.archive_reason ?? "—"} full />
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
              placeholder="Why is this part being archived?"
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

      <PartAlternativesSection partId={part.id} canWrite={canWrite} />

      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onClosed}>
          Close
        </button>
      </div>
    </div>
  );
}

type DrawerState = { mode: "create" } | { mode: "view"; id: string } | null;

export function PartsPage() {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);
  const canArchive = isAdministratorOrAbove(session?.role);

  const [searchInput, setSearchInput] = useState("");
  const debouncedSearch = useDebouncedValue(searchInput, 300);
  const [categoryInput, setCategoryInput] = useState("");
  const debouncedCategory = useDebouncedValue(categoryInput, 300);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [offset, setOffset] = useState(0);
  const [drawerState, setDrawerState] = useState<DrawerState>(null);

  useEffect(() => {
    setOffset(0);
  }, [debouncedSearch, debouncedCategory, includeArchived]);

  const listParams = useMemo(
    () => ({
      q: debouncedSearch.trim() || undefined,
      category: debouncedCategory.trim() || undefined,
      include_archived: includeArchived,
      limit: LIMIT,
      offset,
    }),
    [debouncedSearch, debouncedCategory, includeArchived, offset],
  );

  const partsQuery = useParts(listParams);
  const items = partsQuery.data?.items ?? [];

  const unitsQuery = useUnits();
  const unitsById = useMemo(
    () => new Map((unitsQuery.data?.items ?? []).map((u) => [u.id, u] as const)),
    [unitsQuery.data],
  );

  const columns = useMemo<ColumnDef<PartResponse>[]>(
    () => [
      {
        id: "internal_part_number",
        accessorKey: "internal_part_number",
        header: "Part number",
        meta: { mono: true },
      },
      {
        id: "mpn",
        accessorKey: "manufacturer_part_number",
        header: "MPN",
        meta: { mono: true },
        cell: (ctx) => ctx.getValue<string | null>() ?? "—",
      },
      {
        id: "name",
        accessorKey: "name",
        header: "Name",
      },
      {
        id: "category",
        accessorKey: "category",
        header: "Category",
        cell: (ctx) => ctx.getValue<string | null>() ?? "—",
      },
      {
        id: "unit",
        accessorKey: "unit_definition_id",
        header: "Unit",
        enableSorting: false,
        meta: { mono: true },
        cell: (ctx) => {
          const v = ctx.getValue<string>();
          const unit = unitsById.get(v);
          return unit ? <span title={unit.name}>{unit.code}</span> : <span title={v}>{v.slice(0, 8)}…</span>;
        },
      },
      {
        id: "target_price",
        accessorFn: (row) => row.target_price,
        header: "Target price",
        meta: { align: "right" },
        sortingFn: (a, b) => compareDecimalStrings(a.original.target_price, b.original.target_price),
        cell: (ctx) => {
          const row = ctx.row.original;
          return row.target_price !== null && row.target_price_currency !== null
            ? formatMoney(row.target_price, { currency: row.target_price_currency })
            : "—";
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
    [unitsById],
  );

  function closeDrawer() {
    setDrawerState(null);
  }

  const drawerTitle =
    drawerState?.mode === "create" ? "New part" : drawerState?.mode === "view" ? "Part detail" : "";

  return (
    <section className="parts-page">
      <header className="page-toolbar">
        <div className="page-heading">
          <h1>Parts</h1>
          <p>Catalogue master data: identifiers, target pricing, and approved alternatives.</p>
        </div>
        <div className="toolbar-controls">
          <label className="search-field">
            <SearchIcon />
            <input
              type="text"
              placeholder="Search parts…"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              aria-label="Search parts"
            />
          </label>
          <input
            type="text"
            className="category-field"
            placeholder="Filter by category…"
            value={categoryInput}
            onChange={(e) => setCategoryInput(e.target.value)}
            aria-label="Filter by category"
          />
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
            />
            Include archived
          </label>
          <PartImportPanel /> {/* ALLOWED insertion: part-import workflow, self-gated */}
          {canWrite && (
            <button
              type="button"
              className="btn-primary-sm"
              onClick={() => setDrawerState({ mode: "create" })}
            >
              <PlusIcon /> New part
            </button>
          )}
        </div>
      </header>

      <DataTable
        ariaLabel="Parts"
        columns={columns}
        data={items}
        getRowId={(p) => p.id}
        isLoading={partsQuery.isLoading}
        isError={partsQuery.isError}
        errorMessage={partsQuery.error instanceof Error ? partsQuery.error.message : undefined}
        onRetry={() => void partsQuery.refetch()}
        emptyTitle="No parts found"
        emptyDescription={
          debouncedSearch || debouncedCategory
            ? "No parts match the current filters."
            : "Create your first part to get started."
        }
        onRowClick={(p) => setDrawerState({ mode: "view", id: p.id })}
        rowClassName={(p) => (p.is_archived ? "is-muted" : undefined)}
      />

      <PaginationBar
        total={partsQuery.data?.page.total ?? 0}
        limit={LIMIT}
        offset={offset}
        onOffsetChange={setOffset}
      />

      <Drawer open={drawerState !== null} onClose={closeDrawer} title={drawerTitle}>
        {drawerState?.mode === "create" && (
          <PartForm
            mode="create"
            onSaved={(created) => setDrawerState({ mode: "view", id: created.id })}
            onCancel={closeDrawer}
          />
        )}
        {drawerState?.mode === "view" && (
          <PartDetail
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
