/** RFQs data layer - TanStack Query hooks over the live API.
 *
 * Shapes mirror backend/src/app/schemas/rfqs.py exactly:
 *  - list envelope is `{ items, page: { limit, offset, total } }`
 *    (`RfqListResponse`); `RfqSummaryResponse` (list rows) carries no
 *    `lines`/`invited_supplier_count` - those still only exist on
 *    `RfqResponse` (single-resource GET), to keep the list query from
 *    turning into an N+1 (rfqs.py schema module docstring, `RfqSummaryResponse`).
 *  - **`RfqSummaryResponse.line_count` is a widened-contract exception to
 *    that N+1 guard.** A backend agent added a cheap aggregate count column
 *    to the list query concurrently with this task (P2 audit finding: the
 *    list "Lines" column previously always rendered "-"). It's typed
 *    optional (`line_count?: number`) rather than required, so a stale
 *    cached response or an older server build that hasn't shipped the field
 *    yet degrades to RfqsPage.tsx's previous "-" rendering instead of
 *    `undefined` leaking into the table - see that file's own header.
 *  - `required_quantity` is a NUMERIC(18,6) wire string (QTY_SCALE) - never
 *    run through Number()/parseFloat, same rule as `quantity_per_assembly`
 *    in ../boms/api.ts.
 *  - `GET /rfqs` DOES accept `q` (unlike `GET /boms`) plus `status[]`
 *    (repeated query param, FastAPI's `Query(list[...])` convention),
 *    `due_before`/`due_after`, and `include_archived` - so search/status
 *    filtering here is server-side, not the client-side page-filter BomsPage
 *    uses (its own file header explains why `GET /boms` is different).
 *  - RFQs carry `version` + `ETag`/`If-Match` (api/v1/rfqs.py file header:
 *    "RFQs are explicitly listed among the resources carrying ETag/If-Match"),
 *    same mechanics as Parts/Suppliers - only `PATCH /rfqs/{id}` requires
 *    `If-Match`; status-change and line/supplier sub-resource routes mutate
 *    and return a fresh `ETag`/`version` without demanding the header as
 *    input. `useUpdateRfq` therefore needs the same custom-header dance
 *    `useUpdateSupplier`/`useUpdatePart` use (see those files' headers for
 *    why the CSRF token has to be re-supplied by hand whenever a caller sets
 *    its own `headers`).
 *  - `ALLOWED_RFQ_TRANSITIONS` below is a client-side mirror of
 *    backend/src/app/models/rfqs.py's dict of the same name - the backend
 *    dict is the single source of truth (re-validated server-side on every
 *    `POST /rfqs/{id}/status`); this copy only decides which transition
 *    buttons RfqsPage.tsx renders.
 *  - Every mutation here except `useUpdateRfq` relies on `api()`'s own
 *    automatic CSRF attachment (client.ts: any POST/PATCH/DELETE picks up
 *    the module-level csrf token) - no custom `headers` are passed, so
 *    nothing needs to be re-supplied by hand for those calls, including the
 *    two DELETE routes that carry a JSON body (`excludeSupplier`,
 *    `removeRfqLine` with an override reason): `api()` already returns
 *    `undefined` for a `204`/empty body (client.ts), so - unlike
 *    ../parts/api.ts's `deleteAlternative`, written against an older
 *    client.ts - no hand-rolled fetch is needed here.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, get, post } from "../../api/client";
import { useAuth } from "../../auth/session";

export type RfqStatus = "draft" | "open" | "under_review" | "awarded" | "closed" | "archived";

export const RFQ_STATUSES: RfqStatus[] = [
  "draft",
  "open",
  "under_review",
  "awarded",
  "closed",
  "archived",
];

/** Client-side mirror of backend/src/app/models/rfqs.py `ALLOWED_RFQ_TRANSITIONS`
 * - see this file's header. Every status is a key (including the terminal
 * `archived`, mapped to `[]`) so lookups never need a fallback default. */
export const ALLOWED_RFQ_TRANSITIONS: Record<RfqStatus, RfqStatus[]> = {
  draft: ["open", "archived"],
  open: ["under_review"],
  under_review: ["awarded", "open"],
  awarded: ["closed"],
  closed: ["archived"],
  archived: [],
};

export interface RfqLineResponse {
  id: string;
  line_number: number;
  part_id: string;
  required_quantity: string;
  unit_definition_id: string;
  required_specifications: Record<string, unknown> | null;
  notes: string | null;
}

export interface RfqResponse {
  id: string;
  name: string;
  internal_reference: string;
  status: RfqStatus;
  base_currency: string;
  requested_payment_terms: string | null;
  requested_incoterm: string | null;
  due_date: string;
  requested_delivery_date: string | null;
  notes: string | null;
  source_bom_id: string | null;
  created_by_id: string;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  line_count: number;
  invited_supplier_count: number;
  lines: RfqLineResponse[];
}

/** `GET /rfqs` items - no `lines`/`invited_supplier_count`; `line_count` is
 * present but optional, see this file's header. */
export interface RfqSummaryResponse {
  id: string;
  name: string;
  internal_reference: string;
  status: RfqStatus;
  base_currency: string;
  due_date: string;
  requested_delivery_date: string | null;
  source_bom_id: string | null;
  is_archived: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  line_count?: number;
}

export interface RfqSupplierResponse {
  id: string;
  rfq_id: string;
  supplier_id: string;
  invited_at: string;
  excluded_at: string | null;
  exclusion_reason: string | null;
  notes: string | null;
}

export interface RfqStatusHistoryResponse {
  id: string;
  rfq_id: string;
  from_status: RfqStatus;
  to_status: RfqStatus;
  actor_user_id: string;
  actor_full_name?: string | null;
  note: string | null;
  occurred_at: string;
}

export interface PageInfo {
  limit: number;
  offset: number;
  total: number;
}

export interface RfqListResponse {
  items: RfqSummaryResponse[];
  page: PageInfo;
}

export interface RfqLineListResponse {
  items: RfqLineResponse[];
}

export interface RfqSupplierListResponse {
  items: RfqSupplierResponse[];
}

export interface RfqStatusHistoryListResponse {
  items: RfqStatusHistoryResponse[];
}

export interface RfqListParams {
  // `| undefined` (not just `?`) so callers building this from a ternary
  // satisfy `exactOptionalPropertyTypes`, matching boms/parts/suppliers api.ts.
  status?: RfqStatus[] | undefined;
  q?: string | undefined;
  due_before?: string | undefined;
  due_after?: string | undefined;
  include_archived?: boolean | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
}

export interface RfqLineInput {
  part_id: string;
  required_quantity: string;
  unit_definition_id: string | null;
  required_specifications: Record<string, unknown> | null;
  notes: string | null;
}

/** `POST /rfqs` body. Matches `RfqCreate`'s "lines XOR source_bom_id"
 * validator exactly (app/schemas/rfqs.py): callers must set either `lines`
 * (non-empty, `source_bom_id`/`assembly_quantity` both `null`) or
 * `source_bom_id` (`lines` empty). RfqsPage.tsx's create form's two toggle
 * modes build one or the other, never a mix. */
export interface RfqCreateInput {
  name: string;
  internal_reference: string;
  base_currency: string;
  due_date: string;
  requested_delivery_date: string | null;
  requested_payment_terms: string | null;
  requested_incoterm: string | null;
  notes: string | null;
  lines: RfqLineInput[];
  source_bom_id: string | null;
  assembly_quantity: string | null;
}

/** `PATCH /rfqs/{id}` body. No `override_reason`: RfqsPage.tsx only exposes
 * editing while `status === "draft"` (the brief's edit-gating rule), so the
 * administrator-override path documented in rfqs.py's schema module
 * docstring is intentionally not surfaced in this UI. */
export interface RfqUpdateInput {
  name: string;
  internal_reference: string;
  base_currency: string;
  due_date: string;
  requested_delivery_date: string | null;
  requested_payment_terms: string | null;
  requested_incoterm: string | null;
  notes: string | null;
}

function buildListQuery(params: RfqListParams): string {
  const sp = new URLSearchParams();
  if (params.q) sp.set("q", params.q);
  for (const s of params.status ?? []) sp.append("status", s);
  if (params.due_before) sp.set("due_before", params.due_before);
  if (params.due_after) sp.set("due_after", params.due_after);
  if (params.include_archived !== undefined) {
    sp.set("include_archived", String(params.include_archived));
  }
  sp.set("limit", String(params.limit ?? 50));
  sp.set("offset", String(params.offset ?? 0));
  return sp.toString();
}

export const rfqKeys = {
  all: ["rfqs"] as const,
  lists: () => [...rfqKeys.all, "list"] as const,
  list: (params: RfqListParams) => [...rfqKeys.lists(), params] as const,
  details: () => [...rfqKeys.all, "detail"] as const,
  detail: (id: string) => [...rfqKeys.details(), id] as const,
  suppliers: (id: string) => [...rfqKeys.detail(id), "suppliers"] as const,
  statusHistory: (id: string) => [...rfqKeys.detail(id), "status-history"] as const,
};

export function useRfqs(params: RfqListParams) {
  return useQuery({
    queryKey: rfqKeys.list(params),
    queryFn: () => get<RfqListResponse>(`/api/v1/rfqs?${buildListQuery(params)}`),
    placeholderData: (prev) => prev,
  });
}

export function useRfq(id: string | null) {
  return useQuery({
    queryKey: rfqKeys.detail(id ?? ""),
    queryFn: () => get<RfqResponse>(`/api/v1/rfqs/${id}`),
    enabled: id !== null,
  });
}

export function useCreateRfq() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: RfqCreateInput) => post<RfqResponse>("/api/v1/rfqs", data),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.lists() });
      queryClient.setQueryData(rfqKeys.detail(created.id), created);
    },
  });
}

export interface UpdateRfqVars {
  id: string;
  version: number;
  data: RfqUpdateInput;
}

export function useUpdateRfq() {
  const { session } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, data }: UpdateRfqVars) =>
      api<RfqResponse>("PATCH", `/api/v1/rfqs/${id}`, data, {
        headers: {
          "Content-Type": "application/json",
          "If-Match": `"${version}"`,
          ...(session?.csrf_token ? { "X-CSRF-Token": session.csrf_token } : {}),
        },
      }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.lists() });
      queryClient.setQueryData(rfqKeys.detail(updated.id), updated);
    },
  });
}

export interface ChangeRfqStatusVars {
  id: string;
  to_status: RfqStatus;
  reason?: string | undefined;
}

export function useChangeRfqStatus() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, to_status, reason }: ChangeRfqStatusVars) =>
      post<RfqResponse>(`/api/v1/rfqs/${id}/status`, { to_status, reason: reason ?? null }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.lists() });
      queryClient.setQueryData(rfqKeys.detail(updated.id), updated);
      void queryClient.invalidateQueries({ queryKey: rfqKeys.statusHistory(updated.id) });
    },
  });
}

export function useRfqStatusHistory(id: string | null) {
  return useQuery({
    queryKey: rfqKeys.statusHistory(id ?? ""),
    queryFn: () => get<RfqStatusHistoryListResponse>(`/api/v1/rfqs/${id}/status-history`),
    enabled: id !== null,
  });
}

// --- lines ----------------------------------------------------------------

export interface AddRfqLinesVars {
  id: string;
  lines: RfqLineInput[];
  override_reason?: string | undefined;
}

export function useAddRfqLines() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, lines, override_reason }: AddRfqLinesVars) =>
      post<RfqLineListResponse>(`/api/v1/rfqs/${id}/lines`, {
        lines,
        override_reason: override_reason ?? null,
      }),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.detail(vars.id) });
    },
  });
}

export interface UpdateRfqLineInput {
  required_quantity?: string;
  unit_definition_id?: string | null;
  notes?: string | null;
}

export interface UpdateRfqLineVars {
  rfqId: string;
  lineId: string;
  data: UpdateRfqLineInput;
}

export function useUpdateRfqLine() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rfqId, lineId, data }: UpdateRfqLineVars) =>
      api<RfqLineResponse>("PATCH", `/api/v1/rfqs/${rfqId}/lines/${lineId}`, data),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.detail(vars.rfqId) });
    },
  });
}

export interface RemoveRfqLineVars {
  rfqId: string;
  lineId: string;
  overrideReason?: string | undefined;
}

export function useRemoveRfqLine() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rfqId, lineId, overrideReason }: RemoveRfqLineVars) =>
      api<void>(
        "DELETE",
        `/api/v1/rfqs/${rfqId}/lines/${lineId}`,
        overrideReason ? { override_reason: overrideReason } : undefined,
      ),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.detail(vars.rfqId) });
    },
  });
}

// --- suppliers --------------------------------------------------------

export function useRfqSuppliers(id: string | null) {
  return useQuery({
    queryKey: rfqKeys.suppliers(id ?? ""),
    queryFn: () => get<RfqSupplierListResponse>(`/api/v1/rfqs/${id}/suppliers`),
    enabled: id !== null,
  });
}

export interface InviteSuppliersVars {
  id: string;
  supplierIds: string[];
}

export function useInviteSuppliers() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, supplierIds }: InviteSuppliersVars) =>
      post<RfqSupplierListResponse>(`/api/v1/rfqs/${id}/suppliers`, {
        supplier_ids: supplierIds,
      }),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.suppliers(vars.id) });
      void queryClient.invalidateQueries({ queryKey: rfqKeys.detail(vars.id) });
    },
  });
}

export interface ExcludeSupplierVars {
  rfqId: string;
  rfqSupplierId: string;
  reason: string;
}

export function useExcludeSupplier() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rfqId, rfqSupplierId, reason }: ExcludeSupplierVars) =>
      api<RfqSupplierResponse>("DELETE", `/api/v1/rfqs/${rfqId}/suppliers/${rfqSupplierId}`, {
        exclusion_reason: reason,
      }),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.suppliers(vars.rfqId) });
      void queryClient.invalidateQueries({ queryKey: rfqKeys.detail(vars.rfqId) });
    },
  });
}

export interface ReinstateSupplierVars {
  rfqId: string;
  rfqSupplierId: string;
}

export function useReinstateSupplier() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rfqId, rfqSupplierId }: ReinstateSupplierVars) =>
      post<RfqSupplierResponse>(`/api/v1/rfqs/${rfqId}/suppliers/${rfqSupplierId}/reinstate`),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.suppliers(vars.rfqId) });
      void queryClient.invalidateQueries({ queryKey: rfqKeys.detail(vars.rfqId) });
    },
  });
}
