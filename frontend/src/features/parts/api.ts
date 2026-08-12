/** Parts data layer — TanStack Query hooks over the live API.
 *
 * Shapes mirror backend/src/app/schemas/parts.py exactly:
 *  - list envelope is `{ items, page: { limit, offset, total } }`.
 *  - `target_price` is a NUMERIC(18,8) wire string (UNIT_PRICE_SCALE) and is
 *    always paired with `target_price_currency` — both null or both set
 *    (DB check `ck_parts_target_price_currency_paired`).
 *  - `unit_definition_id` is a bare UUID on the wire, with no accompanying
 *    label — but `GET /api/v1/units` (backend/src/app/api/v1/units.py) IS
 *    mounted and resolves it to `{code, name, dimension, is_global}`.
 *    PartsPage.tsx resolves this the same way RfqsPage.tsx/BomsPage.tsx
 *    already do, via `useUnits()` (../boms/api.ts) — the "no /units
 *    endpoint" note that used to live here was stale by the time this task
 *    landed (a units-catalog audit finding fixed elsewhere added the
 *    route); Parts was simply the one workspace that hadn't been wired to
 *    it yet.
 *  - `PATCH /parts/{id}` requires `If-Match: "<version>"`, same mechanics as
 *    suppliers (see features/suppliers/api.ts's file-level comment for why
 *    the CSRF token is threaded in from `session.csrf_token` instead of
 *    relying on client.ts's automatic header).
 *  - `DELETE /parts/{id}/alternatives/{aid}` returns `204 No Content`. The
 *    shared `api()` helper always does `return (await resp.json()) as T`,
 *    which throws on an empty body — a real footgun for any 204 response,
 *    but client.ts is read-only for this task. `deleteAlternative` below
 *    re-implements just enough of `api()`'s fetch/error-envelope handling
 *    (same CSRF/credentials rules) to stop short of parsing a body that
 *    was never sent.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, type ApiErrorBody, api, get, post } from "../../api/client";
import { useAuth } from "../../auth/session";

export interface PartResponse {
  id: string;
  internal_part_number: string;
  manufacturer_part_number: string | null;
  manufacturer: string | null;
  name: string;
  description: string | null;
  category: string | null;
  unit_definition_id: string;
  required_specifications: Record<string, string | number | boolean> | null;
  target_price: string | null;
  target_price_currency: string | null;
  is_active: boolean;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface PageInfo {
  limit: number;
  offset: number;
  total: number;
}

export interface PartListResponse {
  items: PartResponse[];
  page: PageInfo;
}

export interface PartListParams {
  // `| undefined` (not just `?`) so callers building this from a ternary
  // (e.g. `q: search || undefined`) satisfy `exactOptionalPropertyTypes`.
  q?: string | undefined;
  category?: string | undefined;
  is_active?: boolean | undefined;
  include_archived?: boolean | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
}

export interface PartWriteInput {
  internal_part_number: string;
  manufacturer_part_number: string | null;
  manufacturer: string | null;
  name: string;
  description: string | null;
  category: string | null;
  unit_definition_id: string;
  target_price: string | null;
  target_price_currency: string | null;
  is_active: boolean;
}

function buildListQuery(params: PartListParams): string {
  const sp = new URLSearchParams();
  if (params.q) sp.set("q", params.q);
  if (params.category) sp.set("category", params.category);
  if (params.is_active !== undefined) sp.set("is_active", String(params.is_active));
  if (params.include_archived !== undefined) {
    sp.set("include_archived", String(params.include_archived));
  }
  sp.set("limit", String(params.limit ?? 50));
  sp.set("offset", String(params.offset ?? 0));
  return sp.toString();
}

export const partKeys = {
  all: ["parts"] as const,
  lists: () => [...partKeys.all, "list"] as const,
  list: (params: PartListParams) => [...partKeys.lists(), params] as const,
  details: () => [...partKeys.all, "detail"] as const,
  detail: (id: string) => [...partKeys.details(), id] as const,
  alternatives: (id: string) => [...partKeys.detail(id), "alternatives"] as const,
};

export function useParts(params: PartListParams) {
  return useQuery({
    queryKey: partKeys.list(params),
    queryFn: () => get<PartListResponse>(`/api/v1/parts?${buildListQuery(params)}`),
    placeholderData: (prev) => prev,
  });
}

export function usePart(id: string | null) {
  return useQuery({
    queryKey: partKeys.detail(id ?? ""),
    queryFn: () => get<PartResponse>(`/api/v1/parts/${id}`),
    enabled: id !== null,
  });
}

export function useCreatePart() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: PartWriteInput) => post<PartResponse>("/api/v1/parts", data),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: partKeys.lists() });
      queryClient.setQueryData(partKeys.detail(created.id), created);
    },
  });
}

export interface UpdatePartVars {
  id: string;
  version: number;
  data: PartWriteInput;
}

export function useUpdatePart() {
  const { session } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, data }: UpdatePartVars) =>
      api<PartResponse>("PATCH", `/api/v1/parts/${id}`, data, {
        headers: {
          "Content-Type": "application/json",
          "If-Match": `"${version}"`,
          ...(session?.csrf_token ? { "X-CSRF-Token": session.csrf_token } : {}),
        },
      }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: partKeys.lists() });
      queryClient.setQueryData(partKeys.detail(updated.id), updated);
    },
  });
}

export interface ArchivePartVars {
  id: string;
  reason: string;
}

export function useArchivePart() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: ArchivePartVars) =>
      post<PartResponse>(`/api/v1/parts/${id}/archive`, { reason }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: partKeys.lists() });
      queryClient.setQueryData(partKeys.detail(updated.id), updated);
    },
  });
}

export function useUnarchivePart() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: ArchivePartVars) =>
      post<PartResponse>(`/api/v1/parts/${id}/unarchive`, { reason }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: partKeys.lists() });
      queryClient.setQueryData(partKeys.detail(updated.id), updated);
    },
  });
}

// --- Alternatives -----------------------------------------------------

export type ApprovalStatus = "approved" | "conditional" | "rejected";

export interface PartAlternativeResponse {
  id: string;
  part_id: string;
  alternative_part_id: string | null;
  alternative_mpn: string | null;
  approval_status: ApprovalStatus;
  rationale: string | null;
  approved_by_id: string | null;
  approved_at: string | null;
}

export interface PartAlternativeListResponse {
  items: PartAlternativeResponse[];
  page: PageInfo;
}

export function usePartAlternatives(partId: string | null) {
  return useQuery({
    queryKey: partKeys.alternatives(partId ?? ""),
    queryFn: () =>
      get<PartAlternativeListResponse>(`/api/v1/parts/${partId}/alternatives?limit=200&offset=0`),
    enabled: partId !== null,
  });
}

export interface AddAlternativeInput {
  alternative_part_id?: string;
  alternative_mpn?: string;
  approval_status: ApprovalStatus;
  rationale?: string;
}

export function useAddPartAlternative(partId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: AddAlternativeInput) =>
      post<PartAlternativeResponse>(`/api/v1/parts/${partId}/alternatives`, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: partKeys.alternatives(partId) });
    },
  });
}

async function deleteAlternative(
  partId: string,
  alternativeId: string,
  csrfToken: string | null | undefined,
): Promise<void> {
  const headers: Record<string, string> = {};
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const resp = await fetch(`/api/v1/parts/${partId}/alternatives/${alternativeId}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers,
  });
  if (resp.ok) return; // 204 No Content — nothing to parse
  let parsed: { error?: ApiErrorBody } | undefined;
  try {
    parsed = (await resp.json()) as { error?: ApiErrorBody };
  } catch {
    /* non-JSON error */
  }
  if (parsed?.error) throw new ApiError(parsed.error);
  throw new ApiError({
    code: "internal_error",
    message: `Request failed (${resp.status}).`,
    status: resp.status,
    details: [],
    request_id: resp.headers.get("X-Request-ID") ?? "",
    timestamp: new Date().toISOString(),
  });
}

export function useRemovePartAlternative(partId: string) {
  const { session } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (alternativeId: string) =>
      deleteAlternative(partId, alternativeId, session?.csrf_token),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: partKeys.alternatives(partId) });
    },
  });
}
