/** BOMs data layer — TanStack Query hooks over the live API.
 *
 * Shapes mirror backend/src/app/schemas/boms.py exactly:
 *  - list / version-chain envelopes are `{ items, page: {...} }` /
 *    `{ items }` respectively (`BomListResponse` / `BomVersionChainResponse`).
 *  - `quantity_per_assembly` is a NUMERIC(18,6) wire string (QTY_SCALE) —
 *    never run through Number()/parseFloat, matching lib/money.ts's rule.
 *  - BOMs carry NO `ETag`/`If-Match`: backend/src/app/api/v1/boms.py's file
 *    header states "§1.7 explicitly enumerates which resources carry
 *    optimistic-concurrency headers ... and BOMs are not among them" — the
 *    copy-on-write version chain is BOMs' own concurrency control. So unlike
 *    features/parts/api.ts / features/suppliers/api.ts, no mutation here
 *    threads a custom `If-Match` header or reads `session.csrf_token` — a
 *    plain `post()` already attaches the CSRF token automatically for every
 *    mutating call (api/client.ts's `api()`).
 *  - `status` is a real wire enum (`draft|active|superseded|archived`,
 *    backend/src/app/models/boms.py `BomStatus`), not a derived boolean —
 *    the frontend renders it directly rather than reconstructing it from
 *    `is_active`/`is_archived` the way the Parts/Suppliers `StatusBadge`
 *    does (see services/bom_service.py's docstring: `archive()` sets
 *    `status = archived` itself, keeping both in sync).
 *  - `GET /boms` accepts only `all_versions` / `include_archived` / `limit` /
 *    `offset` (api/v1/boms.py `list_boms`) — there is no `q` search
 *    parameter, unlike `GET /parts` / `GET /suppliers`. BomsPage.tsx's
 *    search box therefore filters client-side over the currently-loaded
 *    page (flagged there too) rather than inventing a server contract that
 *    doesn't exist — the same judgement call DataTable.tsx documents for
 *    client-side sort on Parts/Suppliers.
 *  - `GET /boms/{id}/versions` accepts *any* version id in a chain (not only
 *    the root's) and resolves it server-side (`BomService.version_chain`,
 *    see boms.py's file header point 3); `useBomVersionChain` just forwards
 *    whatever id the caller has on hand.
 *  - Part numbers/names and unit codes are not embedded in `BomLineResponse`
 *    (it only carries `part_id` / `unit_definition_id` UUIDs) — BomsPage.tsx
 *    resolves them per-row via `usePart` (imported from `../parts/api`,
 *    mirroring how PartsPage itself queries parts) and via `useUnits` below.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../../api/client";

export type BomStatus = "draft" | "active" | "superseded" | "archived";

export interface BomLineResponse {
  id: string;
  line_number: number;
  part_id: string;
  quantity_per_assembly: string;
  unit_definition_id: string;
  is_optional: boolean;
  substitute_part_id: string | null;
  notes: string | null;
}

export interface BomResponse {
  id: string;
  root_bom_id: string;
  version_number: number;
  previous_version_id: string | null;
  name: string;
  product_name: string;
  status: BomStatus;
  notes: string | null;
  created_by_id: string;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
  created_at: string;
  updated_at: string;
  lines: BomLineResponse[];
}

/** `GET /boms` and `GET /boms/{id}/versions` items — no `lines`, kept out of
 * list payloads (see `BomResponse` vs `BomSummaryResponse` in boms.py). */
export type BomSummaryResponse = Omit<BomResponse, "lines">;

export interface PageInfo {
  limit: number;
  offset: number;
  total: number;
}

export interface BomListResponse {
  items: BomSummaryResponse[];
  page: PageInfo;
}

export interface BomVersionChainResponse {
  items: BomSummaryResponse[];
}

export interface BomListParams {
  // `| undefined` (not just `?`) so callers building this from a ternary
  // satisfy `exactOptionalPropertyTypes`, matching parts/suppliers api.ts.
  all_versions?: boolean | undefined;
  include_archived?: boolean | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
}

export interface BomLineInput {
  part_id: string;
  quantity_per_assembly: string;
  unit_definition_id: string | null;
  is_optional: boolean;
  substitute_part_id: string | null;
  notes: string | null;
}

export interface BomCreateInput {
  name: string;
  product_name: string;
  notes: string | null;
  lines: BomLineInput[];
}

export interface BomVersionCreateInput {
  name: string | null;
  product_name: string | null;
  notes: string | null;
  lines: BomLineInput[];
}

function buildListQuery(params: BomListParams): string {
  const sp = new URLSearchParams();
  if (params.all_versions !== undefined) sp.set("all_versions", String(params.all_versions));
  if (params.include_archived !== undefined) {
    sp.set("include_archived", String(params.include_archived));
  }
  sp.set("limit", String(params.limit ?? 50));
  sp.set("offset", String(params.offset ?? 0));
  return sp.toString();
}

export const bomKeys = {
  all: ["boms"] as const,
  lists: () => [...bomKeys.all, "list"] as const,
  list: (params: BomListParams) => [...bomKeys.lists(), params] as const,
  details: () => [...bomKeys.all, "detail"] as const,
  detail: (id: string) => [...bomKeys.details(), id] as const,
  chains: () => [...bomKeys.all, "chain"] as const,
  chain: (id: string) => [...bomKeys.chains(), id] as const,
};

export function useBoms(params: BomListParams) {
  return useQuery({
    queryKey: bomKeys.list(params),
    queryFn: () => get<BomListResponse>(`/api/v1/boms?${buildListQuery(params)}`),
    placeholderData: (prev) => prev,
  });
}

export function useBom(id: string | null) {
  return useQuery({
    queryKey: bomKeys.detail(id ?? ""),
    queryFn: () => get<BomResponse>(`/api/v1/boms/${id}`),
    enabled: id !== null,
  });
}

export function useBomVersionChain(id: string | null) {
  return useQuery({
    queryKey: bomKeys.chain(id ?? ""),
    queryFn: () => get<BomVersionChainResponse>(`/api/v1/boms/${id}/versions`),
    enabled: id !== null,
  });
}

export function useCreateBom() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: BomCreateInput) => post<BomResponse>("/api/v1/boms", data),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: bomKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: bomKeys.chains() });
      queryClient.setQueryData(bomKeys.detail(created.id), created);
    },
  });
}

export interface NewBomVersionVars {
  id: string;
  data: BomVersionCreateInput;
}

export function useNewBomVersion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: NewBomVersionVars) =>
      post<BomResponse>(`/api/v1/boms/${id}/versions`, data),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: bomKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: bomKeys.chains() });
      queryClient.setQueryData(bomKeys.detail(created.id), created);
    },
  });
}

export function useActivateBom() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => post<BomResponse>(`/api/v1/boms/${id}/activate`),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: bomKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: bomKeys.chains() });
      queryClient.setQueryData(bomKeys.detail(updated.id), updated);
      // Activation may also flip the predecessor to `superseded` server-side
      // (services/bom_service.py `activate()`) — invalidate its cached
      // detail too, in case a caller still has it open.
      if (updated.previous_version_id) {
        void queryClient.invalidateQueries({
          queryKey: bomKeys.detail(updated.previous_version_id),
        });
      }
    },
  });
}

export interface ArchiveBomVars {
  id: string;
  reason: string;
}

export function useArchiveBom() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: ArchiveBomVars) =>
      post<BomResponse>(`/api/v1/boms/${id}/archive`, { reason }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: bomKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: bomKeys.chains() });
      queryClient.setQueryData(bomKeys.detail(updated.id), updated);
    },
  });
}

// --- Units (for resolving unit_definition_id -> code/name) --------------
// backend/src/app/api/v1/units.py: global catalogue + this org's own units,
// read-only. Field is `name` on the wire (mapped from the model's
// `display_name` server-side).

export interface UnitDefinitionResponse {
  id: string;
  code: string;
  name: string;
  dimension: string;
  is_global: boolean;
}

export interface UnitListResponse {
  items: UnitDefinitionResponse[];
}

export const unitKeys = {
  all: ["units"] as const,
  list: () => [...unitKeys.all, "list"] as const,
};

/** Units change rarely — cache for 5 minutes so every line row / picker
 * resolving a unit doesn't refetch independently. */
export function useUnits() {
  return useQuery({
    queryKey: unitKeys.list(),
    queryFn: () => get<UnitListResponse>("/api/v1/units"),
    staleTime: 5 * 60 * 1000,
  });
}
