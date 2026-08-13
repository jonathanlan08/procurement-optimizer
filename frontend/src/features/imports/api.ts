/** Part-import data layer - TanStack Query hooks over the live API.
 *
 * Shapes mirror backend/src/app/schemas/part_imports.py exactly
 * (backend/src/app/api/v1/part_imports.py - mounted in app/main.py as
 * `part_imports_router`, prefix `/part-imports`):
 *  - `POST /part-imports` is `multipart/form-data` (one `file` part), not
 *    JSON - same reason ../documents/api.ts's `uploadDocument` can't reuse
 *    `api()`/`post()` (../../api/client): those unconditionally
 *    `JSON.stringify()` the body and set `Content-Type: application/json`.
 *    `uploadPartImport` below is the same hand-rolled fetch, line for line
 *    (same-origin credentials, CSRF header, single-envelope error parse),
 *    just posting a `FormData` instead.
 *  - `POST /part-imports`'s `201` response (`PartImportPreviewResponse`) is
 *    a different, narrower shape than `GET /part-imports/{id}`
 *    (`PartImportBatchDetailResponse`) - the contract's own explicit
 *    example, not this file's invention (part_imports.py schema module
 *    docstring: "intentionally does not match GET's shape"). It carries
 *    `sample_rows` (up to 20) plus a flattened `errors[]` (up to 100) - not
 *    the full row set.
 *  - **This UI shows `sample_rows` as the preview table, not the full
 *    cursor-paginated row list `GET /part-imports/{id}` exposes.** A
 *    deliberate scope call: the task brief asks for "a preview table
 *    showing the backend's per-row validation verdicts," which `sample_rows`
 *    already satisfies (every disposition value can appear in it, and
 *    `rows_invalid`/`rows_valid`/`rows_duplicate` on the same response give
 *    the true totals) - building a second "load more rows" cursor-paging UI
 *    on top would add a second query surface for marginal value over the
 *    honest "showing the first N of `rows_total` rows" caveat
 *    PartImportPanel.tsx renders when the file has more rows than the
 *    sample. `usePartImportBatch` (cursor-paginated) is still exported here
 *    for completeness against the contract, but is unused by this task's UI.
 *  - **`disposition` is typed as the 3-value domain the service actually
 *    emits** (`create`/`skip_duplicate`/`error` - app/models/part_imports.py's
 *    own module docstring: `update` is "documented but not emitted by any
 *    code path"), not the ERD's wider 4-value one.
 *  - `raw_values`/`normalized_values` are genuinely untyped JSONB
 *    (`dict[str, Any]` server-side, one key per canonical import column) -
 *    typed `Record<string, unknown>` and narrowed defensively at the one
 *    render site, the same "don't invent a shape" treatment
 *    ../comparison/api.ts's header documents for scenario snapshot fields.
 *  - **Commit's gate mirrors the backend's own refusal rule exactly**:
 *    `PartImportService.commit` (services/part_import_service.py) refuses
 *    the whole batch - `409 conflict_state` - if it contains any `error`
 *    disposition row. `canCommit()` below is that same rule
 *    (`rows_invalid === 0`) applied client-side purely to disable the
 *    button pre-emptively; the server re-validates regardless, so a stale
 *    preview can never bypass it.
 *  - **Commit invalidates the Parts list** (`partKeys.lists()`, imported
 *    from ../parts/api - within this task's own ownership) so a successful
 *    import's newly created parts show up in PartsPage.tsx's table without
 *    a manual refresh, the same "mutation invalidates the list it affects"
 *    convention every other feature's api.ts already follows.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, type ApiErrorBody, get, post } from "../../api/client";
import { useAuth } from "../../auth/session";
import { partKeys } from "../parts/api";

export type PartImportFormat = "csv" | "xlsx";
export type PartImportState = "previewing" | "committed" | "rolled_back" | "failed";
export type PartImportRowDisposition = "create" | "skip_duplicate" | "error";

export interface PartImportRowError {
  field: string | null;
  issue: string;
}

export interface PartImportRowResponse {
  row_number: number;
  raw_values: Record<string, unknown>;
  normalized_values: Record<string, unknown> | null;
  disposition: PartImportRowDisposition;
  errors: PartImportRowError[];
  resulting_part_id: string | null;
}

export interface PartImportRowErrorResponse {
  row_number: number;
  field: string | null;
  issue: string;
}

/** `201` response of `POST /part-imports` - the contract's own explicit
 * shape (see this file's header), distinct from `PartImportBatchDetailResponse`. */
export interface PartImportPreviewResponse {
  batch_id: string;
  rows_total: number;
  rows_valid: number;
  rows_invalid: number;
  rows_duplicate: number;
  sample_rows: PartImportRowResponse[];
  errors: PartImportRowErrorResponse[];
}

export interface PartImportBatchSummaryResponse {
  id: string;
  state: PartImportState;
  source_filename: string;
  format: PartImportFormat;
  rows_total: number;
  rows_valid: number;
  rows_invalid: number;
  rows_duplicate: number;
  created_by_id: string;
  created_at: string;
  committed_at: string | null;
}

export interface PartImportPageInfo {
  limit: number;
  next_cursor: string | null;
  prev_cursor: string | null;
  has_more: boolean;
}

export interface PartImportBatchDetailResponse extends PartImportBatchSummaryResponse {
  items: PartImportRowResponse[];
  page: PartImportPageInfo;
}

/** `200` response of `POST /part-imports/{id}/commit`. `updated` is always
 * `0` - this service never updates an existing part from an import row,
 * only creates or skips (part_imports.py schema module docstring on
 * `PartImportCommitResponse`). */
export interface PartImportCommitResponse {
  created: number;
  updated: number;
  skipped: number;
}

/** Mirrors `PartImportService.commit`'s own refusal rule exactly - see this
 * file's header. */
export function canCommitPartImport(preview: {
  rows_invalid: number;
}): boolean {
  return preview.rows_invalid === 0;
}

/** Hand-rolled multipart upload - see this file's header for why `api()`/
 * `post()` can't be reused. Mirrors ../documents/api.ts's `uploadDocument`
 * line for line. */
async function uploadPartImport(
  file: File,
  csrfToken: string | null,
): Promise<PartImportPreviewResponse> {
  const form = new FormData();
  form.append("file", file);

  const headers: Record<string, string> = {};
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;

  const resp = await fetch("/api/v1/part-imports", {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: form,
  });

  if (!resp.ok) {
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
  return (await resp.json()) as PartImportPreviewResponse;
}

export function useUploadPartImport() {
  const { session } = useAuth();
  return useMutation({
    mutationFn: (file: File) => uploadPartImport(file, session?.csrf_token ?? null),
  });
}

export function useCommitPartImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) =>
      post<PartImportCommitResponse>(`/api/v1/part-imports/${batchId}/commit`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: partKeys.lists() });
    },
  });
}

export function useCancelPartImport() {
  return useMutation({
    mutationFn: (batchId: string) =>
      post<PartImportBatchSummaryResponse>(`/api/v1/part-imports/${batchId}/cancel`),
  });
}

/** `GET /part-imports/{id}` - cursor-paginated full row list. Exported
 * for contract completeness; not used by this task's preview-table UI
 * (see this file's header for the scope call). */
export function usePartImportBatch(batchId: string | null, cursor: string | null, limit = 50) {
  return useQuery({
    queryKey: ["part-imports", "detail", batchId, cursor, limit] as const,
    queryFn: () => {
      const sp = new URLSearchParams();
      sp.set("limit", String(limit));
      if (cursor) sp.set("cursor", cursor);
      return get<PartImportBatchDetailResponse>(`/api/v1/part-imports/${batchId}?${sp.toString()}`);
    },
    enabled: batchId !== null,
  });
}
