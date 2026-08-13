/** Quote-document data layer - TanStack Query hooks over the live API.
 *
 * Shapes mirror backend/src/app/schemas/documents.py exactly
 * (backend/src/app/api/v1/documents.py):
 *  - `GET /rfqs/{rfq_id}/quote-documents` returns `{ items }` - no `page`
 *    envelope (`DocumentListResponse`, unlike `RfqListResponse`/
 *    `SupplierListResponse`), same "whole list in one call" shape
 *    ../quotes/api.ts's own file header documents for `QuoteListResponse`.
 *  - `DocumentResponse` never carries a storage URL or download link inline
 *    (documents.py's own module docstring: "never a storage URL") - this
 *    file therefore has no hook for `GET /quote-documents/{id}/content`;
 *    DocumentsSection.tsx does not offer a download action, a deliberate
 *    scope call matching the task brief's own shorter list (list, upload,
 *    per-document Extract, Review-extraction link only).
 *  - **Upload is `multipart/form-data`, not JSON** - `POST
 *    /rfqs/{rfq_id}/quote-documents` takes a `file` part plus an optional
 *    `supplier_id` form field (api/v1/documents.py's `upload_document`).
 *    `api()`/`post()` (../../api/client) unconditionally
 *    `JSON.stringify()` any provided body and always set `Content-Type:
 *    application/json`, so neither can be reused here - `uploadDocument`
 *    below is a small hand-rolled `fetch()` that mirrors `api()`'s own
 *    same-origin-credentials + CSRF-header + single-error-envelope
 *    handling line for line, but passes a `FormData` body untouched (the
 *    browser sets the multipart boundary itself; setting `Content-Type`
 *    by hand here would omit that boundary and break the upload).
 *  - **413/415/409 upload errors need no special handling.** All three map
 *    to the same `ApiError` envelope (`message` + `details[]`) every other
 *    mutation in this app already produces - 413 payload_too_large / 415
 *    unsupported_media_type (api/v1/documents.py's own
 *    `_map_file_validation_error`) / 409 conflict_duplicate (sha256 dedupe,
 *    `services/document_service.py`'s module docstring point 3, surfaced as
 *    `ErrorDetail(field="file", issue="duplicate_of", value_hint=<existing
 *    document id>)`) - `ApiErrorBanner` already renders `message` plus every
 *    `details[]` entry generically, so DocumentsSection.tsx needs no
 *    per-status-code branching to satisfy "shows 413/415/409 errors inline."
 *  - `size_bytes` is a plain Pydantic `int` (`DocumentResponse.size_bytes`),
 *    not a `NUMERIC`-backed decimal wire string - an ordinary JSON number,
 *    safe to use with normal arithmetic (`formatBytes` below), unlike the
 *    quantity/price/rate strings lib/money.ts's rule is about.
 *  - `content_sha256` is a lowercase hex string or `null` (still nullable
 *    pre-Stage-2 - documents.py's own module docstring point/`from_model`),
 *    not surfaced in the UI here (no dedupe/debug affordance is in scope).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, type ApiErrorBody, get } from "../../api/client";
import { useAuth } from "../../auth/session";

export type DocumentState =
  | "uploaded"
  | "validated"
  | "stored"
  | "processing"
  | "extracted"
  | "in_review"
  | "confirmed"
  | "quarantined"
  | "archived";

export interface DocumentResponse {
  id: string;
  rfq_id: string;
  supplier_id: string | null;
  original_filename: string;
  detected_mime: string | null;
  declared_mime: string;
  size_bytes: number;
  content_sha256: string | null;
  state: DocumentState;
  quarantine_reason: string | null;
  page_count: number | null;
  uploaded_by_id: string;
  uploaded_at: string;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
}

export interface DocumentListResponse {
  items: DocumentResponse[];
}

export interface DocumentListParams {
  state?: DocumentState | undefined;
  supplier_id?: string | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
}

function buildListQuery(params: DocumentListParams): string {
  const sp = new URLSearchParams();
  if (params.state) sp.set("state", params.state);
  if (params.supplier_id) sp.set("supplier_id", params.supplier_id);
  sp.set("limit", String(params.limit ?? 50));
  sp.set("offset", String(params.offset ?? 0));
  return sp.toString();
}

export const documentKeys = {
  all: ["documents"] as const,
  rfqLists: () => [...documentKeys.all, "rfq-list"] as const,
  rfqList: (rfqId: string) => [...documentKeys.rfqLists(), rfqId] as const,
  details: () => [...documentKeys.all, "detail"] as const,
  detail: (id: string) => [...documentKeys.details(), id] as const,
};

export function useRfqDocuments(rfqId: string | null, params: DocumentListParams = {}) {
  return useQuery({
    queryKey: [...documentKeys.rfqList(rfqId ?? ""), params],
    queryFn: () =>
      get<DocumentListResponse>(
        `/api/v1/rfqs/${rfqId}/quote-documents?${buildListQuery(params)}`,
      ),
    enabled: rfqId !== null,
  });
}

export function useDocument(id: string | null) {
  return useQuery({
    queryKey: documentKeys.detail(id ?? ""),
    queryFn: () => get<DocumentResponse>(`/api/v1/quote-documents/${id}`),
    enabled: id !== null,
  });
}

/** Hand-rolled multipart upload - see this file's header for why `api()`/
 * `post()` can't be reused. Mirrors client.ts's `api()` line for line:
 * same-origin credentials, CSRF header on the mutating request, and the
 * identical single-envelope error parse (falls back to a generic
 * `ApiError` when the body isn't JSON or carries no `error` key). */
async function uploadDocument(
  rfqId: string,
  file: File,
  supplierId: string | null,
  csrfToken: string | null,
): Promise<DocumentResponse> {
  const form = new FormData();
  form.append("file", file);
  if (supplierId) form.append("supplier_id", supplierId);

  const headers: Record<string, string> = {};
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;

  const resp = await fetch(`/api/v1/rfqs/${rfqId}/quote-documents`, {
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
  return (await resp.json()) as DocumentResponse;
}

export interface UploadDocumentVars {
  rfqId: string;
  file: File;
  supplierId: string | null;
}

export function useUploadDocument() {
  const { session } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rfqId, file, supplierId }: UploadDocumentVars) =>
      uploadDocument(rfqId, file, supplierId, session?.csrf_token ?? null),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: documentKeys.rfqList(created.rfq_id) });
    },
  });
}

/** Bytes -> a human size string. `size_bytes` is a plain JSON number (see
 * this file's header) - ordinary arithmetic is safe here. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

/** Type badge label from `detected_mime` (preferred, sniffed and verified)
 * falling back to `declared_mime` (untrusted, client-supplied) when
 * detection hasn't run yet - matches the same trust ordering
 * `services/document_service.py`'s module docstring point 5 documents for
 * which mime this codebase treats as authoritative. */
export function documentTypeLabel(document: DocumentResponse): string {
  const mime = document.detected_mime ?? document.declared_mime;
  switch (mime) {
    case "application/pdf":
      return "PDF";
    case "image/png":
      return "PNG";
    case "image/jpeg":
      return "JPEG";
    case "text/csv":
      return "CSV";
    case "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
      return "XLSX";
    default:
      return mime;
  }
}
