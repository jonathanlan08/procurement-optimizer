/** Negotiation-briefs data layer — TanStack Query hooks over the live API.
 *
 * The backend for this surface already exists (committed ahead of this
 * task, per this task's own brief — unlike ../reports/api.ts and
 * ../audit/api.ts, which code against a frozen contract for a backend still
 * landing concurrently). Shapes mirror the wire contract given in this
 * task's brief field-for-field:
 *  - `GET .../negotiation-briefs` returns lean summaries only (no
 *    `sections`/targets/draft email) — the full brief requires the separate
 *    `GET /negotiation-briefs/{id}` this file also exposes.
 *    BriefsPanel.tsx's list therefore only fetches the full detail once a
 *    row is opened in the drawer, the same "don't N+1 the list, fetch full
 *    detail on demand" convention ../comparison/ScenarioHistory.tsx
 *    documents for its own summary-vs-detail split.
 *  - **`sections` is a `Record<section_key, ...>`, not an array.** The task
 *    brief's own fixed 18-key display order (`SECTION_ORDER` below) is
 *    applied client-side in BriefsPanel.tsx. A brief whose `sections` map
 *    is missing a given key simply renders no row for that section — never
 *    fabricated as `provenance: "missing"`, which is a real value the
 *    backend writes for a section it chose to include with nothing to say
 *    (a materially different thing from a key the backend never wrote at
 *    all).
 *  - `price_target`/`stretch_target`/`walk_away_threshold` are decimal wire
 *    strings (never run through `Number()`/`parseFloat`, lib/money.ts's
 *    file-level rule) with no accompanying currency field anywhere in this
 *    contract (unlike e.g. `AllocationResultResponse.objective_total_cost`
 *    in ../comparison/api.ts) — BriefsPanel.tsx formats them with
 *    `formatMoney()` and no `currency` option rather than inventing one.
 *  - The create request's `objective`/`targets` (and each field within
 *    `targets`) are independently optional — omitted entirely when blank,
 *    never sent as `""`/`"0"`, the same "missing stays missing" convention
 *    ../quotes/api.ts's file header documents for quote-line cost fields.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../../api/client";

export type BriefState = "draft" | "human_reviewed";

export type BriefProvenance =
  | "supplier_provided"
  | "user_assumption"
  | "calculated"
  | "ai_narrative"
  | "missing";

export interface BriefSectionResponse {
  provenance: BriefProvenance;
  text: string;
  data: Record<string, unknown> | null;
}

/** Fixed display order from this task's brief — the 18 section keys a full
 * brief's `sections` map may carry. */
export type BriefSectionKey =
  | "procurement_objective"
  | "supplier_position"
  | "quoted_unit_price"
  | "effective_unit_cost"
  | "landed_cost_comparison"
  | "price_target"
  | "stretch_target"
  | "walk_away_threshold"
  | "volume_leverage"
  | "payment_terms_opportunity"
  | "lead_time_concerns"
  | "quality_concerns"
  | "spec_concerns"
  | "alternative_suppliers"
  | "batna"
  | "recommended_concessions"
  | "questions_for_clarification"
  | "draft_supplier_email";

export const SECTION_ORDER: BriefSectionKey[] = [
  "procurement_objective",
  "supplier_position",
  "quoted_unit_price",
  "effective_unit_cost",
  "landed_cost_comparison",
  "price_target",
  "stretch_target",
  "walk_away_threshold",
  "volume_leverage",
  "payment_terms_opportunity",
  "lead_time_concerns",
  "quality_concerns",
  "spec_concerns",
  "alternative_suppliers",
  "batna",
  "recommended_concessions",
  "questions_for_clarification",
  "draft_supplier_email",
];

export interface BriefSummaryResponse {
  id: string;
  scenario_id: string;
  supplier_id: string;
  state: BriefState;
  requires_review: boolean;
  narrative_provider: string;
  simulated: boolean;
  created_by_id: string;
  created_at: string;
  is_archived: boolean;
}

export interface BriefListResponse {
  items: BriefSummaryResponse[];
  page: { limit: number; offset: number; total: number };
}

export interface BriefResponse {
  id: string;
  scenario_id: string;
  supplier_id: string;
  sections: Partial<Record<BriefSectionKey, BriefSectionResponse>>;
  price_target: string | null;
  stretch_target: string | null;
  walk_away_threshold: string | null;
  draft_email_subject: string | null;
  draft_email_body: string | null;
  narrative_provider: string;
  simulated: boolean;
  narrative_is_generated: boolean;
  state: BriefState;
  requires_review: boolean;
  reviewed_by_id: string | null;
  // org-scoped display name; null when the reviewer can't be resolved, in
  // which case the UI falls back to the (truncated) id rather than inventing one
  reviewed_by_full_name?: string | null;
  reviewed_at: string | null;
  created_by_id: string;
  created_at: string;
  updated_at: string;
  version: number;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
}

export const briefKeys = {
  all: ["negotiation-briefs"] as const,
  scenarioLists: () => [...briefKeys.all, "scenario-list"] as const,
  scenarioList: (scenarioId: string) => [...briefKeys.scenarioLists(), scenarioId] as const,
  details: () => [...briefKeys.all, "detail"] as const,
  detail: (id: string) => [...briefKeys.details(), id] as const,
};

export function useScenarioBriefs(scenarioId: string | null) {
  return useQuery({
    queryKey: briefKeys.scenarioList(scenarioId ?? ""),
    queryFn: () =>
      get<BriefListResponse>(`/api/v1/comparison-scenarios/${scenarioId}/negotiation-briefs`),
    enabled: scenarioId !== null,
  });
}

export function useBrief(id: string | null) {
  return useQuery({
    queryKey: briefKeys.detail(id ?? ""),
    queryFn: () => get<BriefResponse>(`/api/v1/negotiation-briefs/${id}`),
    enabled: id !== null,
  });
}

export interface BriefTargetsInput {
  price_target?: string;
  stretch_target?: string;
  walk_away_threshold?: string;
}

export interface CreateBriefVars {
  scenarioId: string;
  supplierId: string;
  /** `null` omits `objective` from the request body entirely — see this
   * file's header on "missing stays missing". */
  objective: string | null;
  /** `null` omits `targets` from the request body entirely. */
  targets: BriefTargetsInput | null;
}

export function useCreateBrief() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ scenarioId, supplierId, objective, targets }: CreateBriefVars) => {
      const body: Record<string, unknown> = { supplier_id: supplierId };
      if (objective !== null) body["objective"] = objective;
      if (targets !== null) body["targets"] = targets;
      return post<BriefResponse>(
        `/api/v1/comparison-scenarios/${scenarioId}/negotiation-briefs`,
        body,
      );
    },
    onSuccess: (created, vars) => {
      void queryClient.invalidateQueries({ queryKey: briefKeys.scenarioList(vars.scenarioId) });
      queryClient.setQueryData(briefKeys.detail(created.id), created);
    },
  });
}

export interface ReviewBriefVars {
  id: string;
  scenarioId: string;
  approved: boolean;
  reviewerNotes: string | null;
}

export function useReviewBrief() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, approved, reviewerNotes }: ReviewBriefVars) => {
      const body: Record<string, unknown> = { approved };
      if (reviewerNotes !== null) body["reviewer_notes"] = reviewerNotes;
      return post<BriefResponse>(`/api/v1/negotiation-briefs/${id}/review`, body);
    },
    onSuccess: (updated, vars) => {
      queryClient.setQueryData(briefKeys.detail(updated.id), updated);
      void queryClient.invalidateQueries({ queryKey: briefKeys.scenarioList(vars.scenarioId) });
    },
  });
}
