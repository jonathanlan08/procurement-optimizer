/** Comparison workspace data layer — TanStack Query hooks over the live
 * API: landed-cost calculation + scoring-configuration CRUD are real, wired
 * routes; ranked supplier scoring is a documented gap (see the bottom
 * section of this header).
 *
 * Shapes mirror backend/src/app/schemas/analysis.py exactly
 * (backend/src/app/api/v1/analysis.py):
 *  - **Every landed-cost assumption is an optional, UNSCALED decimal
 *    string** (`LandedCostAssumptionsRequest`'s own module note: these are
 *    recorded in `landed_cost_results.inputs_snapshot`/`.assumptions`
 *    JSONB, not a fixed-scale `NUMERIC` column, so there is no boundary
 *    scale to enforce — unlike `QuantityString`/`UnitPriceString`
 *    elsewhere). Omitted means "not supplied" and the corresponding
 *    component becomes `is_missing`, never a silent zero
 *    (`app.domain.values.Quantified`) — this file's `emptyToNull`-style
 *    helper in ComparisonPage.tsx must never default a blank assumption
 *    field to `"0"`, same rule ../quotes/QuotesSection.tsx's own file
 *    header states for quote-line cost fields.
 *  - **The task brief names six assumption controls** (quality risk %,
 *    delay cost/day, annual rate %, baseline terms days, tariff %,
 *    assume-missing-zero) but the real request body carries **eight**
 *    decimal fields — `duty_rate` (symmetric with `tariff_rate`) and
 *    `promised_lead_time_days`/`required_lead_time_days` (both required
 *    for the `DELAY_RISK` component to ever be non-missing;
 *    `services/landed_cost_service.py`'s own module docstring: "no table
 *    anywhere in this schema has a 'required lead time' column... added as
 *    an assumption override"). All eight are exposed here (the schema
 *    wins over the brief's shorter prose, the same resolution this
 *    codebase's own backend docstrings apply repeatedly, e.g.
 *    `app/models/part_imports.py`'s "ERD is the more authoritative
 *    source"), the extra two grouped under an "Advanced" disclosure in
 *    ComparisonPage.tsx so the six the brief names stay the primary
 *    surface.
 *  - **`GET /rfqs/{rfq_id}/landed-costs` returns the latest result PER
 *    QUOTE LINE across every quote against that RFQ** (`services/
 *    landed_cost_service.py`'s `latest_per_line_for_rfq`, joined
 *    `quote_line -> quote -> rfq_id`), not one row per supplier —
 *    ComparisonPage.tsx groups these by `quote_line_id` itself to build
 *    per-supplier comparison columns for one selected RFQ line.
 *  - **`POST /rfqs/{rfq_id}/landed-costs` persists exactly one quote
 *    line's result per call** (`quote_line_id` in the body) — "Calculate"
 *    in ComparisonPage.tsx therefore calls `useCalculateLandedCost` once
 *    per compared supplier's matched line for the currently-selected RFQ
 *    line, not a single batch call (api/v1/analysis.py's own module
 *    docstring point 2: no batch route exists).
 *  - **`ScoringConfigurationResponse.is_sample`** is the whole-config flag
 *    the "Sample weights (demonstration)" label reads (seeded via task
 *    5.10, `docs/planning/09-task-decomposition.md`) — `weights[]` also
 *    carries a per-criterion `is_sample_weight`, but the config-level flag
 *    is what the scoring-config `<select>` badges.
 *
 * ## GENUINE BACKEND GAP: ranked supplier scoring has no implemented route
 *
 * `POST /rfqs/{rfq_id}/scoring` below is **this agent's own placeholder
 * route, not a verified backend contract path** — flagged loudly because
 * every other endpoint in this file is real and working and this one is
 * not. `docs/planning/03-api-contract.md` §4.16 documents the actual
 * intended surface (`/comparison-scenarios`), but that entire resource is
 * Phase 6 ("Optimization and scenarios",
 * `docs/planning/09-task-decomposition.md` 6.1-6.13) and is **not
 * implemented anywhere in this backend build**: no
 * `comparison_scenarios`/`scenario_results` table
 * (`backend/src/app/models/analysis.py`'s own module docstring point 1
 * confirms "comparison_scenarios does not exist yet in this codebase"), no
 * service, no route mounted in `app/main.py`. The pure, deterministic
 * scorer that would answer this (`app/domain/scoring/scorer.py`'s
 * `ScorerV1`, principal-owned and fully spec'd) has no caller anywhere in
 * `app/services/` or `app/api/v1/` today — only `ScoringConfiguration` CRUD
 * (weights *definitions*) is wired up (`api/v1/analysis.py`'s
 * `scoring_config_router`, both hooks below it in this file).
 *
 * Per this task's own FORBIDDEN list, `backend/**` is out of scope for this
 * agent, so no route was added there. `useComputeScoring` calls a
 * best-guess synchronous route (`POST /rfqs/{id}/scoring`, no job envelope
 * — mirroring the same "this codebase has no job queue, so run inline"
 * precedent `api/v1/matching.py`'s `generate_quote_matches` already sets
 * for its own similarly-shaped compute-and-return action), shaped
 * field-for-field after the FROZEN `ScoringResult`/`SupplierScore`/
 * `CriterionScore` dataclasses in `domain/scoring/contracts.py` — the most
 * authoritative source for what a real endpoint's response would look like
 * once wired up. Until that backend route exists, every call here 404s in
 * a live deployment (a plain, un-enveloped FastAPI 404, not this app's own
 * error shape — `ComparisonPage.tsx`'s `ApiErrorBanner` still renders it
 * safely via `api()`'s generic-fallback branch, just with a less specific
 * message). `comparison.test.tsx` verifies the *rendering* contract via a
 * mocked response, independent of whether the real route exists yet.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../../api/client";

// -- landed cost -----------------------------------------------------------

export interface LandedCostAssumptionsInput {
  quality_risk_rate: string | null;
  delay_risk_per_day: string | null;
  promised_lead_time_days: string | null;
  required_lead_time_days: string | null;
  annual_rate: string | null;
  baseline_terms_days: string | null;
  tariff_rate: string | null;
  duty_rate: string | null;
  assume_missing_costs_zero: boolean;
}

export const EMPTY_ASSUMPTIONS: LandedCostAssumptionsInput = {
  quality_risk_rate: null,
  delay_risk_per_day: null,
  promised_lead_time_days: null,
  required_lead_time_days: null,
  annual_rate: null,
  baseline_terms_days: null,
  tariff_rate: null,
  duty_rate: null,
  assume_missing_costs_zero: false,
};

/** All seven `CostComponent` values, stable order
 * (`domain/landed_cost/contracts.py`'s `CostComponent` StrEnum) —
 * `LandedCostResult.components` always carries all seven
 * (`LandedCostResult`'s own docstring: "always all seven, stable order"). */
export type CostComponentKind =
  | "extended_material"
  | "allocated_fixed"
  | "logistics"
  | "import"
  | "quality_risk"
  | "delay_risk"
  | "financing";

export interface ComponentResponse {
  component: CostComponentKind;
  amount: string;
  formula: string;
  inputs: Record<string, string>;
  provenance: string;
  is_assumed: boolean;
  is_missing: boolean;
}

export interface MissingInputResponse {
  component: string;
  input_name: string;
  consequence: string;
}

export interface AssumptionResponse {
  key: string;
  value: string;
  description: string;
  provenance: string;
}

export type Completeness = "complete" | "assumption_dependent" | "incomplete";

export interface LandedCostResultResponse {
  id: string;
  quote_line_id: string;
  accepted_quantity: string;
  currency: string;
  total_landed_cost: string;
  effective_unit_cost: string;
  completeness: Completeness;
  calculation_version: string;
  calculated_at: string;
  calculated_by_id: string;
  missing_inputs: MissingInputResponse[];
  assumptions: AssumptionResponse[];
  components: ComponentResponse[];
}

export interface LandedCostResultListResponse {
  items: LandedCostResultResponse[];
}

export const landedCostKeys = {
  all: ["landed-costs"] as const,
  rfqLists: () => [...landedCostKeys.all, "rfq-list"] as const,
  rfqList: (rfqId: string) => [...landedCostKeys.rfqLists(), rfqId] as const,
};

export function useRfqLandedCosts(rfqId: string | null) {
  return useQuery({
    queryKey: landedCostKeys.rfqList(rfqId ?? ""),
    queryFn: () => get<LandedCostResultListResponse>(`/api/v1/rfqs/${rfqId}/landed-costs`),
    enabled: rfqId !== null,
  });
}

export interface CalculateLandedCostVars {
  rfqId: string;
  quoteLineId: string;
  assumptions: LandedCostAssumptionsInput;
}

export function useCalculateLandedCost() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rfqId, quoteLineId, assumptions }: CalculateLandedCostVars) =>
      post<LandedCostResultResponse>(`/api/v1/rfqs/${rfqId}/landed-costs`, {
        quote_line_id: quoteLineId,
        assumptions,
      }),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: landedCostKeys.rfqList(vars.rfqId) });
    },
  });
}

// -- scoring configurations (real, wired routes) ----------------------------

export interface CriterionSpecResponse {
  criterion: string;
  weight: string;
  direction: string;
  label: string | null;
  is_sample_weight: boolean;
}

export interface ScoringConfigurationResponse {
  id: string;
  name: string;
  weights: CriterionSpecResponse[];
  weight_sum: string;
  is_sample: boolean;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  notes: string[];
}

export interface ScoringConfigurationListResponse {
  items: ScoringConfigurationResponse[];
}

export function useScoringConfigurations() {
  return useQuery({
    queryKey: ["scoring-configurations"] as const,
    queryFn: () => get<ScoringConfigurationListResponse>("/api/v1/scoring-configurations"),
  });
}

// -- scoring compute (see this file's header: GENUINE BACKEND GAP) ---------

export interface CriterionScoreResponse {
  criterion: string;
  raw_value: string | null;
  normalized_score: string | null;
  effective_weight: string;
  weighted_contribution: string;
  reason: string;
}

export interface SupplierScoreResponse {
  supplier_id: string;
  supplier_name: string;
  total_score: string;
  rank: number;
  criterion_scores: CriterionScoreResponse[];
  missing_criteria: string[];
  weights_renormalized: boolean;
  excluded: boolean;
  exclusion_reason: string | null;
}

export interface ScoringRunResponse {
  scores: SupplierScoreResponse[];
  weights_used: CriterionSpecResponse[];
  cohort_size: number;
  notes: string[];
  scoring_version: string;
}

export interface ComputeScoringVars {
  rfqId: string;
  scoringConfigurationId: string;
  quoteLineIds: string[];
}

export function useComputeScoring() {
  return useMutation({
    mutationFn: ({ rfqId, scoringConfigurationId, quoteLineIds }: ComputeScoringVars) =>
      post<ScoringRunResponse>(`/api/v1/rfqs/${rfqId}/scoring`, {
        scoring_configuration_id: scoringConfigurationId,
        quote_line_ids: quoteLineIds,
      }),
  });
}
