/** Typed API client — PRINCIPAL-OWNED.
 *
 * - session cookie rides automatically (same-origin via Vite proxy)
 * - CSRF token from login is attached to every mutating request
 * - every non-2xx is parsed into the single error envelope
 */

export interface ApiErrorDetail {
  field?: string;
  issue: string;
  value_hint?: string;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  status: number;
  details: ApiErrorDetail[];
  request_id: string;
  timestamp: string;
}

export class ApiError extends Error {
  readonly body: ApiErrorBody;

  constructor(body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.body = body;
  }

  get code(): string {
    return this.body.code;
  }
}

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export async function api<T>(
  method: string,
  path: string,
  body?: unknown,
  init?: RequestInit,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (MUTATING.has(method) && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  const request: RequestInit = { method, credentials: "same-origin", headers, ...init };
  if (body !== undefined) request.body = JSON.stringify(body);
  const resp = await fetch(path, request);

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
  if (resp.status === 204 || resp.headers.get("content-length") === "0") {
    return undefined as T;
  }
  return (await resp.json()) as T;
}

export const get = <T>(path: string) => api<T>("GET", path);
export const post = <T>(path: string, body?: unknown) => api<T>("POST", path, body);
