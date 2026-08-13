/** BOMs workspace: dense version-aware list + search/toggle toolbar + a
 * right-side drawer for create/view/new-version/activate/archive, following
 * the same drawer-vs-route + client-side-sort idioms as SuppliersPage.tsx /
 * PartsPage.tsx (see those files' headers for the rationale).
 *
 * BOM-specific design decisions:
 *  - **Search is client-side.** `GET /boms` has no `q` parameter (see
 *    features/boms/api.ts's file header) - the toolbar's search box filters
 *    `name`/`product_name` over whatever page is currently loaded, same
 *    caveat DataTable.tsx documents for client-side sort.
 *  - **`include_archived` is always `true`, with no toolbar toggle.** The
 *    task brief's toolbar list is exactly "search, all-versions toggle, New
 *    BOM" - no archived-visibility control. Superseded/archived rows must
 *    still be reachable to see their muted styling, so rather than adding
 *    an unrequested fourth toolbar control, archived versions are always
 *    included and rendered muted (`rowClassName`) alongside active/draft
 *    ones.
 *  - **Version chain drives "is this the head?".** `POST /boms/{id}/versions`
 *    and the "New version" action are only valid on the current head of a
 *    chain while it's `draft`/`active` (services/bom_service.py
 *    `new_version()`); the frontend computes `isHead` from the already-
 *    fetched version-chain query (last item, ascending by version_number)
 *    rather than duplicating that rule against a second, unrelated API
 *    shape.
 *  - **Status badges are drawn from `status` directly** (draft/active/
 *    superseded/archived, four distinct styles per boms.css), not
 *    reconstructed from `is_active`/`is_archived` - see api.ts's header for
 *    why that's different from the shared `StatusBadge` component Parts/
 *    Suppliers use.
 *  - **Part numbers/units are resolved per-row.** `BomLineResponse` only
 *    carries `part_id`/`unit_definition_id` UUIDs. Each line row resolves
 *    its own part via `usePart` (imported from `../parts/api` - mirrors how
 *    PartsPage itself queries parts, per this task's brief - rather than
 *    duplicating that fetch/cache logic here) and units via `useUnits()`.
 *  - **Line-array validation errors use a bracket-key lookup**, not
 *    `errors.lines?.[i]?.field`. lib/zodResolver.ts's minimal zod<->RHF
 *    bridge stores every issue under a flat dotted-path property name
 *    (`"lines.0.quantity_per_assembly"`) rather than react-hook-form's
 *    usual nested tree - see that file's header. It never had to support
 *    array fields before this form; `BomLineFormRow`'s `err()` helper reads
 *    errors the way the resolver actually writes them.
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
import { isAdministratorOrAbove, isAnalystOrAbove } from "../../lib/roles";
import { useDebouncedValue } from "../../lib/useDebouncedValue";
import { zodResolver } from "../../lib/zodResolver";
import { type PartResponse, usePart, useParts } from "../parts/api";
import {
  type BomLineInput,
  type BomLineResponse,
  type BomResponse,
  type BomStatus,
  type BomSummaryResponse,
  type UnitDefinitionResponse,
  useActivateBom,
  useArchiveBom,
  useBom,
  useBoms,
  useBomVersionChain,
  useCreateBom,
  useNewBomVersion,
  useUnits,
} from "./api";
import "./boms.css";

const LIMIT = 50;

// --- form schema ----------------------------------------------------------

const decimalQtyField = z
  .string()
  .trim()
  .min(1, "Quantity is required")
  .refine((v) => isDecimalString(v), "Must be a decimal number, e.g. 10.5")
  .refine((v) => !v.startsWith("-"), "Must not be negative")
  .refine((v) => !/^0+(\.0+)?$/.test(v), "Must be greater than zero");

const lineFormSchema = z.object({
  part_id: z.string().trim().min(1, "Select a part"),
  // Display-only - never sent to the API (see `toLineInput`). Kept inside
  // the RHF field array (rather than separate component state) so
  // useFieldArray's append/remove keeps it in sync by index automatically.
  part_label: z.string(),
  quantity_per_assembly: decimalQtyField,
  unit_definition_id: z.string().trim(),
  is_optional: z.boolean(),
  substitute_part_id: z.string().trim(),
  substitute_label: z.string(),
  notes: z.string().trim().max(4000, "Max 4000 characters"),
});

const bomFormSchema = z.object({
  name: z.string().trim().min(1, "Name is required").max(200, "Max 200 characters"),
  product_name: z.string().trim().min(1, "Product is required").max(200, "Max 200 characters"),
  notes: z.string().trim().max(4000, "Max 4000 characters"),
  lines: z.array(lineFormSchema).min(1, "At least one line is required"),
});

type LineFormValues = z.infer<typeof lineFormSchema>;
type BomFormValues = z.infer<typeof bomFormSchema>;

const emptyLine: LineFormValues = {
  part_id: "",
  part_label: "",
  quantity_per_assembly: "",
  unit_definition_id: "",
  is_optional: false,
  substitute_part_id: "",
  substitute_label: "",
  notes: "",
};

const emptyFormValues: BomFormValues = {
  name: "",
  product_name: "",
  notes: "",
  lines: [emptyLine],
};

function toFormValues(b: BomResponse): BomFormValues {
  return {
    name: b.name,
    product_name: b.product_name,
    notes: b.notes ?? "",
    lines:
      b.lines.length > 0
        ? b.lines.map((l) => ({
            part_id: l.part_id,
            part_label: "",
            quantity_per_assembly: l.quantity_per_assembly,
            unit_definition_id: l.unit_definition_id,
            is_optional: l.is_optional,
            substitute_part_id: l.substitute_part_id ?? "",
            substitute_label: "",
            notes: l.notes ?? "",
          }))
        : [emptyLine],
  };
}

function toLineInput(l: LineFormValues): BomLineInput {
  return {
    part_id: l.part_id.trim(),
    quantity_per_assembly: l.quantity_per_assembly.trim(),
    unit_definition_id: l.unit_definition_id.trim() || null,
    is_optional: l.is_optional,
    substitute_part_id: l.substitute_part_id.trim() || null,
    notes: l.notes.trim() || null,
  };
}

/** Trims trailing fractional zeros for display without ever parsing the
 * decimal wire string through Number()/parseFloat (lib/money.ts's rule). */
function formatQuantity(raw: string): string {
  if (!raw.includes(".")) return raw;
  const trimmed = raw.replace(/0+$/, "").replace(/\.$/, "");
  return trimmed === "" ? "0" : trimmed;
}

function statusLabel(status: BomStatus): string {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function BomStatusBadge({ status }: { status: BomStatus }) {
  const modifier =
    status === "active"
      ? "bom-badge--active"
      : status === "draft"
        ? "bom-badge--draft"
        : status === "superseded"
          ? "bom-badge--superseded"
          : "bom-badge--archived";
  return <span className={`badge ${modifier}`}>{statusLabel(status)}</span>;
}

// --- part picker (search-as-you-type, mirrors PartAlternativesSection) ----

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
    <div className="bom-part-picker">
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
                onClick={() => onSelect(p.id, `${p.internal_part_number} - ${p.name}`)}
              >
                {p.internal_part_number} - {p.name}
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

// --- line editor row --------------------------------------------------

function BomLineFormRow({
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
  control: Control<BomFormValues>;
  register: UseFormRegister<BomFormValues>;
  setValue: UseFormSetValue<BomFormValues>;
  errors: FieldErrors<BomFormValues>;
  units: UnitDefinitionResponse[];
  onRemove: () => void;
  canRemove: boolean;
}) {
  const partId = useWatch({ control, name: `lines.${index}.part_id` });
  const partLabel = useWatch({ control, name: `lines.${index}.part_label` });
  const subId = useWatch({ control, name: `lines.${index}.substitute_part_id` });
  const subLabel = useWatch({ control, name: `lines.${index}.substitute_label` });

  // See this file's header: the resolver stores errors under flat dotted
  // keys, not a nested `errors.lines[i].field` tree.
  const flatErrors = errors as unknown as Record<string, { message?: string } | undefined>;
  const err = (field: string) => flatErrors[`lines.${index}.${field}`]?.message;

  const resolvedPart = usePart(partLabel ? null : partId || null);
  const partDisplay =
    partLabel || (resolvedPart.data ? `${resolvedPart.data.internal_part_number} - ${resolvedPart.data.name}` : partId);

  const resolvedSub = usePart(subLabel ? null : subId || null);
  const subDisplay =
    subLabel || (resolvedSub.data ? `${resolvedSub.data.internal_part_number} - ${resolvedSub.data.name}` : subId);

  return (
    <div className="bom-line-row">
      <div className="bom-line-row-header">
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
        <FormField label="Quantity" error={err("quantity_per_assembly")}>
          <input
            {...register(`lines.${index}.quantity_per_assembly`)}
            aria-invalid={!!err("quantity_per_assembly")}
            inputMode="decimal"
          />
        </FormField>
        <FormField label="Unit" hint="optional - defaults to the part's own unit">
          <select {...register(`lines.${index}.unit_definition_id`)}>
            <option value="">(same as part)</option>
            {units.map((u) => (
              <option key={u.id} value={u.id}>
                {u.code} - {u.name}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Substitute part" hint="optional" full>
          {subId ? (
            <span className="detail-label">
              {subDisplay}{" "}
              <button
                type="button"
                className="btn-ghost-sm"
                onClick={() => {
                  setValue(`lines.${index}.substitute_part_id`, "");
                  setValue(`lines.${index}.substitute_label`, "");
                }}
              >
                Change
              </button>
            </span>
          ) : (
            <PartPicker
              placeholder="Search substitute part…"
              onSelect={(id, label) => {
                setValue(`lines.${index}.substitute_part_id`, id, { shouldValidate: true });
                setValue(`lines.${index}.substitute_label`, label);
              }}
            />
          )}
        </FormField>
        <FormField label="Optional line" checkbox>
          <input type="checkbox" {...register(`lines.${index}.is_optional`)} />
        </FormField>
        <FormField label="Notes" hint="optional" full error={err("notes")}>
          <input {...register(`lines.${index}.notes`)} />
        </FormField>
      </div>
    </div>
  );
}

// --- create / new-version form -----------------------------------------

function BomForm({
  mode,
  base,
  onSaved,
  onCancel,
}: {
  mode: "create" | "version";
  base?: BomResponse;
  onSaved: (b: BomResponse) => void;
  onCancel: () => void;
}) {
  const createMutation = useCreateBom();
  const versionMutation = useNewBomVersion();
  const unitsQuery = useUnits();
  const busy = createMutation.isPending || versionMutation.isPending;
  const mutationError = createMutation.error ?? versionMutation.error;

  const {
    register,
    handleSubmit,
    control,
    setValue,
    formState: { errors },
  } = useForm<BomFormValues>({
    resolver: zodResolver(bomFormSchema),
    defaultValues: base ? toFormValues(base) : emptyFormValues,
  });
  const { fields, append, remove } = useFieldArray({ control, name: "lines" });

  async function onValid(values: BomFormValues) {
    const data = {
      name: values.name.trim(),
      product_name: values.product_name.trim(),
      notes: values.notes.trim() || null,
      lines: values.lines.map(toLineInput),
    };
    try {
      if (mode === "create") {
        const created = await createMutation.mutateAsync(data);
        onSaved(created);
      } else if (base) {
        const created = await versionMutation.mutateAsync({ id: base.id, data });
        onSaved(created);
      }
    } catch {
      // surfaced via ApiErrorBanner reading createMutation.error / versionMutation.error
    }
  }

  const units = unitsQuery.data?.items ?? [];
  const linesError = (errors as unknown as Record<string, { message?: string } | undefined>)["lines"]?.message;

  return (
    <form onSubmit={(e) => void handleSubmit(onValid)(e)} noValidate>
      <ApiErrorBanner error={mutationError} />
      <div className="form-grid">
        <FormField label="Name" error={errors.name?.message}>
          <input {...register("name")} aria-invalid={!!errors.name} autoComplete="off" />
        </FormField>
        <FormField label="Product" error={errors.product_name?.message}>
          <input {...register("product_name")} aria-invalid={!!errors.product_name} autoComplete="off" />
        </FormField>
        <FormField label="Notes" hint="optional" full error={errors.notes?.message}>
          <textarea {...register("notes")} rows={3} />
        </FormField>
      </div>

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
        <div className="bom-line-list">
          {fields.map((field, index) => (
            <BomLineFormRow
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

      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="submit" className="btn-primary-sm" disabled={busy}>
          {busy ? "Saving…" : mode === "create" ? "Create BOM" : "Create new version"}
        </button>
      </div>
    </form>
  );
}

// --- read-only lines table ----------------------------------------------

function BomLineViewRow({
  line,
  unitsById,
}: {
  line: BomLineResponse;
  unitsById: Map<string, UnitDefinitionResponse>;
}) {
  const partQuery = usePart(line.part_id);
  const subQuery = usePart(line.substitute_part_id);
  const unit = unitsById.get(line.unit_definition_id);

  return (
    <tr>
      <td className="mono">
        {partQuery.data ? `${partQuery.data.internal_part_number} - ${partQuery.data.name}` : line.part_id}
      </td>
      <td data-align="right" className="mono">
        {formatQuantity(line.quantity_per_assembly)}
      </td>
      <td className="mono">{unit ? unit.code : line.unit_definition_id}</td>
      <td>
        {line.substitute_part_id
          ? subQuery.data
            ? `${subQuery.data.internal_part_number} - ${subQuery.data.name}`
            : line.substitute_part_id
          : "-"}
        {line.is_optional && <span className="detail-label"> · optional</span>}
      </td>
      <td>{line.notes ?? "-"}</td>
    </tr>
  );
}

// --- version chain --------------------------------------------------------

function BomVersionChainList({
  isLoading,
  items,
  currentId,
  onSelect,
}: {
  isLoading: boolean;
  items: BomSummaryResponse[];
  currentId: string;
  onSelect: (id: string) => void;
}) {
  if (isLoading) {
    return <p className="detail-label">Loading…</p>;
  }
  if (items.length === 0) {
    return <p className="detail-label">No version history.</p>;
  }
  return (
    <ul className="bom-version-chain">
      {items.map((v) => {
        const isCurrent = v.id === currentId;
        return (
          <li key={v.id}>
            <button
              type="button"
              className={`bom-version-chain-item${isCurrent ? " is-current" : ""}`}
              onClick={() => onSelect(v.id)}
              aria-current={isCurrent ? "true" : undefined}
            >
              <span className="mono">v{v.version_number}</span>
              <BomStatusBadge status={v.status} />
            </button>
          </li>
        );
      })}
    </ul>
  );
}

// --- detail drawer ---------------------------------------------------------

function BomDetail({
  id,
  canWrite,
  canArchive,
  onClosed,
  onSwitchVersion,
  onNewVersion,
}: {
  id: string;
  canWrite: boolean;
  canArchive: boolean;
  onClosed: () => void;
  onSwitchVersion: (id: string) => void;
  onNewVersion: (bom: BomResponse) => void;
}) {
  const { data: bom, isLoading, isError, error, refetch } = useBom(id);
  const chainQuery = useBomVersionChain(id);
  const unitsQuery = useUnits();
  const [archiving, setArchiving] = useState(false);
  const [archiveReason, setArchiveReason] = useState("");
  const activateMutation = useActivateBom();
  const archiveMutation = useArchiveBom();

  const unitsById = useMemo(
    () => new Map((unitsQuery.data?.items ?? []).map((u) => [u.id, u] as const)),
    [unitsQuery.data],
  );

  if (isLoading) {
    return <p className="detail-label">Loading…</p>;
  }
  if (isError || !bom) {
    return (
      <div>
        {error ? (
          <ApiErrorBanner error={error} />
        ) : (
          <div className="banner-error" role="alert">
            BOM not found.
          </div>
        )}
        <button type="button" className="btn-ghost-sm" onClick={() => void refetch()}>
          Retry
        </button>
      </div>
    );
  }

  const chainItems = chainQuery.data?.items ?? [];
  const isHead = chainItems.length > 0 && chainItems[chainItems.length - 1]?.id === bom.id;
  const canActivate = canWrite && bom.status === "draft" && !bom.is_archived;
  const canStartNewVersion =
    canWrite &&
    !chainQuery.isLoading &&
    !bom.is_archived &&
    (bom.status === "draft" || bom.status === "active") &&
    isHead;

  async function handleActivate() {
    if (!bom) return;
    try {
      await activateMutation.mutateAsync(bom.id);
    } catch {
      // surfaced via ApiErrorBanner reading activateMutation.error
    }
  }

  async function handleArchiveConfirm() {
    if (!bom || archiveReason.trim() === "") return;
    try {
      await archiveMutation.mutateAsync({ id: bom.id, reason: archiveReason.trim() });
      setArchiving(false);
      setArchiveReason("");
    } catch {
      // surfaced via ApiErrorBanner reading archiveMutation.error
    }
  }

  return (
    <div>
      <div className="detail-header-row">
        <BomStatusBadge status={bom.status} />
        <div className="detail-actions">
          {canStartNewVersion && (
            <button type="button" className="btn-secondary-sm" onClick={() => onNewVersion(bom)}>
              New version
            </button>
          )}
          {canActivate && (
            <button
              type="button"
              className="btn-secondary-sm"
              onClick={() => void handleActivate()}
              disabled={activateMutation.isPending}
            >
              {activateMutation.isPending ? "Activating…" : "Activate"}
            </button>
          )}
          {canArchive && !bom.is_archived && (
            <button type="button" className="btn-danger-sm" onClick={() => setArchiving(true)}>
              Archive
            </button>
          )}
        </div>
      </div>

      <ApiErrorBanner error={activateMutation.error ?? archiveMutation.error} />

      <section className="detail-section">
        <h3 className="detail-section-title">Overview</h3>
        <div className="detail-grid">
          <DetailRow label="Name" value={bom.name} />
          <DetailRow label="Product" value={bom.product_name} />
          <DetailRow label="Version" value={`v${bom.version_number}`} mono />
          <DetailRow label="Status" value={statusLabel(bom.status)} />
          <DetailRow label="Updated" value={new Date(bom.updated_at).toLocaleString()} />
          <DetailRow label="Notes" value={bom.notes ?? "-"} full />
        </div>
      </section>

      {bom.is_archived && (
        <section className="detail-section">
          <h3 className="detail-section-title">Archive record</h3>
          <div className="detail-grid">
            <DetailRow
              label="Archived at"
              value={bom.archived_at ? new Date(bom.archived_at).toLocaleString() : "-"}
            />
            <DetailRow label="Reason" value={bom.archive_reason ?? "-"} full />
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
              placeholder="Why is this BOM version being archived?"
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

      <section className="detail-section">
        <h3 className="detail-section-title">Version history</h3>
        <BomVersionChainList
          isLoading={chainQuery.isLoading}
          items={chainItems}
          currentId={bom.id}
          onSelect={onSwitchVersion}
        />
      </section>

      <section className="detail-section">
        <h3 className="detail-section-title">Lines</h3>
        <div className="data-table-wrap">
          <table className="bom-lines-table">
            <thead>
              <tr>
                <th>Part</th>
                <th data-align="right">Quantity</th>
                <th>Unit</th>
                <th>Substitute</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {bom.lines.map((l) => (
                <BomLineViewRow key={l.id} line={l} unitsById={unitsById} />
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="form-actions">
        <button type="button" className="btn-ghost-sm" onClick={onClosed}>
          Close
        </button>
      </div>
    </div>
  );
}

// --- page -------------------------------------------------------------

type DrawerState =
  | { mode: "create" }
  | { mode: "version"; base: BomResponse }
  | { mode: "view"; id: string }
  | null;

export function BomsPage() {
  const { session } = useAuth();
  const canWrite = isAnalystOrAbove(session?.role);
  const canArchive = isAdministratorOrAbove(session?.role);

  const [searchInput, setSearchInput] = useState("");
  const debouncedSearch = useDebouncedValue(searchInput, 300);
  const [allVersions, setAllVersions] = useState(false);
  const [offset, setOffset] = useState(0);
  const [drawerState, setDrawerState] = useState<DrawerState>(null);

  useEffect(() => {
    setOffset(0);
  }, [allVersions, debouncedSearch]);

  const listParams = useMemo(
    () => ({
      // Server-side search (2026-08 external review P2: the old client-side
      // filter only scanned the loaded page while the pagination footer kept
      // reporting the unfiltered server total).
      q: debouncedSearch.trim() || undefined,
      all_versions: allVersions,
      include_archived: true,
      limit: LIMIT,
      offset,
    }),
    [allVersions, offset, debouncedSearch],
  );

  const bomsQuery = useBoms(listParams);
  // The server filters and counts - items and the pagination total always agree.
  const items = bomsQuery.data?.items ?? [];

  const columns = useMemo<ColumnDef<BomSummaryResponse>[]>(
    () => [
      {
        id: "name",
        accessorKey: "name",
        header: "Name",
      },
      {
        id: "product",
        accessorKey: "product_name",
        header: "Product",
      },
      {
        id: "version",
        accessorKey: "version_number",
        header: "Version",
        meta: { align: "right", mono: true },
        cell: (ctx) => `v${ctx.getValue<number>()}`,
      },
      {
        id: "status",
        header: "Status",
        enableSorting: false,
        cell: (ctx) => <BomStatusBadge status={ctx.row.original.status} />,
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
    drawerState?.mode === "create"
      ? "New BOM"
      : drawerState?.mode === "version"
        ? `New version - ${drawerState.base.name}`
        : drawerState?.mode === "view"
          ? "BOM detail"
          : "";

  return (
    <section className="boms-page">
      <header className="page-toolbar">
        <div className="page-heading">
          <h1>Bills of materials</h1>
          <p>Versioned product structures: components, quantities, and approved substitutes.</p>
        </div>
        <div className="toolbar-controls">
          <label className="search-field">
            <SearchIcon />
            <input
              type="text"
              placeholder="Search BOMs…"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              aria-label="Search BOMs"
            />
          </label>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={allVersions}
              onChange={(e) => setAllVersions(e.target.checked)}
            />
            All versions
          </label>
          {canWrite && (
            <button
              type="button"
              className="btn-primary-sm"
              onClick={() => setDrawerState({ mode: "create" })}
            >
              <PlusIcon /> New BOM
            </button>
          )}
        </div>
      </header>

      <DataTable
        ariaLabel="Bills of materials"
        columns={columns}
        data={items}
        getRowId={(b) => b.id}
        isLoading={bomsQuery.isLoading}
        isError={bomsQuery.isError}
        errorMessage={bomsQuery.error instanceof Error ? bomsQuery.error.message : undefined}
        onRetry={() => void bomsQuery.refetch()}
        emptyTitle="No BOMs found"
        emptyDescription={
          debouncedSearch
            ? `No BOMs match "${debouncedSearch}".`
            : "Create your first bill of materials to get started."
        }
        onRowClick={(b) => setDrawerState({ mode: "view", id: b.id })}
        rowClassName={(b) => (b.is_archived || b.status === "superseded" ? "is-muted" : undefined)}
      />

      <PaginationBar
        total={bomsQuery.data?.page.total ?? 0}
        limit={LIMIT}
        offset={offset}
        onOffsetChange={setOffset}
      />

      <Drawer open={drawerState !== null} onClose={closeDrawer} title={drawerTitle}>
        {drawerState?.mode === "create" && (
          <BomForm
            mode="create"
            onSaved={(created) => setDrawerState({ mode: "view", id: created.id })}
            onCancel={closeDrawer}
          />
        )}
        {drawerState?.mode === "version" && (
          <BomForm
            mode="version"
            base={drawerState.base}
            onSaved={(created) => setDrawerState({ mode: "view", id: created.id })}
            onCancel={closeDrawer}
          />
        )}
        {drawerState?.mode === "view" && (
          <BomDetail
            key={drawerState.id}
            id={drawerState.id}
            canWrite={canWrite}
            canArchive={canArchive}
            onClosed={closeDrawer}
            onSwitchVersion={(id) => setDrawerState({ mode: "view", id })}
            onNewVersion={(bom) => setDrawerState({ mode: "version", base: bom })}
          />
        )}
      </Drawer>
    </section>
  );
}
