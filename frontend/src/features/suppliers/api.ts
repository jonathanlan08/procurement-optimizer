/** Suppliers data layer - TanStack Query hooks over the live API.
 *
 * Shapes mirror backend/src/app/schemas/suppliers.py exactly:
 *  - list envelope is `{ items, page: { limit, offset, total } }`.
 *  - `capacity_units_per_month` / `default_moq` are NUMERIC(18,6) wire
 *    strings (QTY_SCALE) - never run through Number()/parseFloat.
 *  - `PATCH /suppliers/{id}` requires an `If-Match: "<version>"` header
 *    (backend/src/app/api/v1/suppliers.py `_parse_if_match`); the current
 *    version travels in the resource body (`SupplierResponse.version`), so
 *    callers pass it explicitly rather than re-parsing a response header.
 *
 * `api()` (frontend/src/api/client.ts) replaces its whole internal `headers`
 * object whenever `init.headers` is supplied (`{ ...built, ...init }` is a
 * shallow spread, so a caller-supplied `headers` key wins outright) - so the
 * one call site that needs a custom header (`If-Match`) must also re-supply
 * `Content-Type` and the CSRF token itself, or they're silently dropped.
 * The CSRF token isn't otherwise readable outside client.ts (no getter is
 * exported, only `setCsrfToken`), so it's threaded in from `session.csrf_token`
 * (already public on `SessionInfo`) rather than touching that forbidden file.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, get, post } from "../../api/client";
import { useAuth } from "../../auth/session";

export interface SupplierResponse {
  id: string;
  code: string;
  name: string;
  country_code: string;
  supported_currencies: string[];
  standard_payment_terms: string | null;
  standard_incoterm: string | null;
  typical_lead_time_days: number | null;
  capacity_units_per_month: string | null;
  default_moq: string | null;
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

export interface SupplierListResponse {
  items: SupplierResponse[];
  page: PageInfo;
}

export interface SupplierListParams {
  // `| undefined` (not just `?`) so callers building this from a ternary
  // (e.g. `q: search || undefined`) satisfy `exactOptionalPropertyTypes`.
  q?: string | undefined;
  country?: string | undefined;
  is_active?: boolean | undefined;
  include_archived?: boolean | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
}

export interface SupplierWriteInput {
  code: string;
  name: string;
  country_code: string;
  supported_currencies: string[];
  standard_payment_terms: string | null;
  standard_incoterm: string | null;
  typical_lead_time_days: number | null;
  capacity_units_per_month: string | null;
  default_moq: string | null;
  is_active: boolean;
}

function buildListQuery(params: SupplierListParams): string {
  const sp = new URLSearchParams();
  if (params.q) sp.set("q", params.q);
  if (params.country) sp.set("country", params.country);
  if (params.is_active !== undefined) sp.set("is_active", String(params.is_active));
  if (params.include_archived !== undefined) {
    sp.set("include_archived", String(params.include_archived));
  }
  sp.set("limit", String(params.limit ?? 50));
  sp.set("offset", String(params.offset ?? 0));
  return sp.toString();
}

export const supplierKeys = {
  all: ["suppliers"] as const,
  lists: () => [...supplierKeys.all, "list"] as const,
  list: (params: SupplierListParams) => [...supplierKeys.lists(), params] as const,
  details: () => [...supplierKeys.all, "detail"] as const,
  detail: (id: string) => [...supplierKeys.details(), id] as const,
};

export function useSuppliers(params: SupplierListParams) {
  return useQuery({
    queryKey: supplierKeys.list(params),
    queryFn: () => get<SupplierListResponse>(`/api/v1/suppliers?${buildListQuery(params)}`),
    placeholderData: (prev) => prev,
  });
}

export function useSupplier(id: string | null) {
  return useQuery({
    queryKey: supplierKeys.detail(id ?? ""),
    queryFn: () => get<SupplierResponse>(`/api/v1/suppliers/${id}`),
    enabled: id !== null,
  });
}

export function useCreateSupplier() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: SupplierWriteInput) =>
      post<SupplierResponse>("/api/v1/suppliers", data),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: supplierKeys.lists() });
      queryClient.setQueryData(supplierKeys.detail(created.id), created);
    },
  });
}

export interface UpdateSupplierVars {
  id: string;
  version: number;
  data: SupplierWriteInput;
}

export function useUpdateSupplier() {
  const { session } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, data }: UpdateSupplierVars) =>
      api<SupplierResponse>("PATCH", `/api/v1/suppliers/${id}`, data, {
        headers: {
          "Content-Type": "application/json",
          "If-Match": `"${version}"`,
          ...(session?.csrf_token ? { "X-CSRF-Token": session.csrf_token } : {}),
        },
      }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: supplierKeys.lists() });
      queryClient.setQueryData(supplierKeys.detail(updated.id), updated);
    },
  });
}

export interface ArchiveSupplierVars {
  id: string;
  reason: string;
}

export function useArchiveSupplier() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: ArchiveSupplierVars) =>
      post<SupplierResponse>(`/api/v1/suppliers/${id}/archive`, { reason }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: supplierKeys.lists() });
      queryClient.setQueryData(supplierKeys.detail(updated.id), updated);
    },
  });
}

export function useUnarchiveSupplier() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: ArchiveSupplierVars) =>
      post<SupplierResponse>(`/api/v1/suppliers/${id}/unarchive`, { reason }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: supplierKeys.lists() });
      queryClient.setQueryData(supplierKeys.detail(updated.id), updated);
    },
  });
}
