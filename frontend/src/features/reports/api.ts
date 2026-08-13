/** Reports data layer - TanStack Query hooks against the FROZEN wire
 * contract given in the spec. The backend is being built
 * concurrently (unlike ../briefs/api.ts, whose backend already exists) -
 * this file codes to the contract exactly as specified, documenting the two
 * places the contract leaves a shape ambiguous:
 *  - **`scenario_id` is typed as a required, non-null `string`** on both the
 *    create request and `ReportResponse`. The contract's create body lists
 *    `{scenario_id, report_type, format, parameters?}` with `scenario_id`
 *    carrying no `?` (unlike `parameters`), and the task's own UI
 *    instructions ask for "a scenario picker" as the generate form's first
 *    control - both signal every report is generated against exactly one
 *    scenario, never org-wide, even for `report_type: "audit_history"`.
 *  - **`size_bytes`/`content_sha256`/`calculation_version`/`generated_at`/
 *    `error_message` are typed nullable** (`| null`), not present-always.
 *    The contract gives three `state` values - `pending`/`ready`/`failed` -
 *    and the UI spec itself only makes sense if these fields populate
 *    asynchronously as a report moves through that lifecycle (size/hash/
 *    calculation-version/generated-at once `ready`; `error_message` once
 *    `failed`; all five plausibly absent while `pending`). Same reasoning
 *    ../documents/api.ts's header applies to `DocumentResponse` fields that
 *    only populate once a later processing stage completes.
 *  - `parameters` is a `dict[str, Any]`-shaped JSONB parameter bag (the
 *    contract gives no fixed shape) - typed `Record<string, unknown>`, the
 *    same "genuinely untyped JSON, don't invent a shape" treatment
 *    ../comparison/api.ts's header documents for `ScenarioResponse`'s own
 *    snapshot fields. This UI's generate form sends none (no per-report-type
 *    parameter inputs are named in the task brief), so `useCreateReport`
 *    only ever omits the key.
 *  - `GET /reports/{id}/content` is a file download, not JSON - this file
 *    exposes only `reportContentUrl()`, a same-origin path ReportsPage.tsx
 *    hands to a plain `<a href download>` (the task brief's own suggested
 *    mechanism: "trigger via window.location or anchor download"). The
 *    session cookie rides automatically on that navigation the same way it
 *    does on every other same-origin request (api/client.ts's own header),
 *    so no hand-rolled fetch+blob dance is needed.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../../api/client";

export type ReportType =
  | "supplier_comparison"
  | "cfo_recommendation"
  | "negotiation_brief"
  | "scenario_summary"
  | "audit_history";

export type ReportFormat = "csv" | "xlsx" | "pdf";
export type ReportState = "pending" | "ready" | "failed";

export const REPORT_TYPES: { value: ReportType; label: string }[] = [
  { value: "supplier_comparison", label: "Supplier comparison" },
  { value: "cfo_recommendation", label: "CFO recommendation" },
  { value: "negotiation_brief", label: "Negotiation brief" },
  { value: "scenario_summary", label: "Scenario summary" },
  { value: "audit_history", label: "Audit history" },
];

export const REPORT_FORMATS: { value: ReportFormat; label: string }[] = [
  { value: "csv", label: "CSV" },
  { value: "xlsx", label: "XLSX" },
  { value: "pdf", label: "PDF" },
];

export interface ReportResponse {
  id: string;
  scenario_id: string;
  report_type: ReportType;
  format: ReportFormat;
  state: ReportState;
  size_bytes: number | null;
  content_sha256: string | null;
  parameters: Record<string, unknown>;
  calculation_version: string | null;
  generated_by_id: string;
  generated_at: string | null;
  expires_at: string | null;
  purged: boolean;
  error_message: string | null;
}

export interface ReportListResponse {
  items: ReportResponse[];
  page: { limit: number; offset: number; total: number };
}

export const reportKeys = {
  all: ["reports"] as const,
  lists: () => [...reportKeys.all, "list"] as const,
  list: (limit: number, offset: number) => [...reportKeys.lists(), limit, offset] as const,
  details: () => [...reportKeys.all, "detail"] as const,
  detail: (id: string) => [...reportKeys.details(), id] as const,
};

export function useReports(limit: number, offset: number) {
  return useQuery({
    queryKey: reportKeys.list(limit, offset),
    queryFn: () => get<ReportListResponse>(`/api/v1/reports?limit=${limit}&offset=${offset}`),
    placeholderData: (prev) => prev,
  });
}

export function useReport(id: string | null) {
  return useQuery({
    queryKey: reportKeys.detail(id ?? ""),
    queryFn: () => get<ReportResponse>(`/api/v1/reports/${id}`),
    enabled: id !== null,
  });
}

export interface CreateReportVars {
  scenario_id: string;
  report_type: ReportType;
  format: ReportFormat;
}

export function useCreateReport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: CreateReportVars) => post<ReportResponse>("/api/v1/reports", vars),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: reportKeys.lists() });
      queryClient.setQueryData(reportKeys.detail(created.id), created);
    },
  });
}

/** Same-origin download path - see this file's header for why no fetch/blob
 * hand-rolling is needed. */
export function reportContentUrl(id: string): string {
  return `/api/v1/reports/${id}/content`;
}
