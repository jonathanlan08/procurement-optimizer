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
 * ## Scoring: now backed by the real `/comparison-scenarios` endpoint
 *
 * Phase 6 (`docs/planning/09-task-decomposition.md` 6.1-6.13) landed the
 * real backend surface this file's scoring hooks previously had to guess
 * at: `backend/src/app/services/scenario_service.py` +
 * `backend/src/app/api/v1/scenarios.py`, mounted in `app/main.py`. There is
 * still no separate "ranked scoring" endpoint on its own — scoring only
 * ever happens as half of creating a `ComparisonScenario`
 * (`POST /rfqs/{rfq_id}/comparison-scenarios`, per that service's own module
 * docstring: no job queue exists in this codebase, so scoring AND
 * allocation both run synchronously in one call, one transaction). This
 * file's `useComputeScoring` is kept as a thin adapter over that real
 * route rather than changed at its call site (`ComparisonPage.tsx`'s
 * `ScoringSection`, out of scope for this edit): it POSTs a `balanced`-
 * strategy scenario using the given scoring configuration and default
 * (empty) assumptions/constraints, then unwraps the response's
 * `scoring_result` — a field shaped, deliberately, exactly like this file's
 * pre-existing `ScoringRunResponse` (`backend/src/app/schemas/scenarios.py`'s
 * `ScoringResultResponse` says as much in its own docstring), so no field
 * remapping is needed beyond picking that one nested object out.
 *
 * Two behavioral changes from the old placeholder, both because the real
 * endpoint scores an entire RFQ's eligible supplier cohort, not an
 * explicit line list: `ComputeScoringVars.quoteLineIds` is no longer sent
 * (the real request has no such field — scope is the whole RFQ) and is
 * kept only so `ScoringSection`'s existing call site still type-checks
 * unmodified; each call also creates a new, persisted `ComparisonScenario`
 * row (audited, listed in scenario history) rather than a stateless
 * compute — an accepted side effect of reusing the real, transactional
 * endpoint rather than inventing a stateless one that does not exist.
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

// -- scoring compute (see this file's header: real /comparison-scenarios) --

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
  /** No longer sent — the real endpoint scores the whole RFQ's eligible
   * cohort, not an explicit line list. Kept so this interface (and
   * ScoringSection's existing call site) still type-check unmodified;
   * see this file's header comment. */
  quoteLineIds: string[];
}

/** The one nested field this hook actually needs from a real
 * `ComparisonScenarioResponse` (backend/src/app/schemas/scenarios.py) —
 * shaped, deliberately, exactly like `ScoringRunResponse` above. */
interface ComparisonScenarioScoringEnvelope {
  scoring_result: ScoringRunResponse;
}

export function useComputeScoring() {
  return useMutation({
    mutationFn: async ({ rfqId, scoringConfigurationId }: ComputeScoringVars) => {
      const scenario = await post<ComparisonScenarioScoringEnvelope>(
        `/api/v1/rfqs/${rfqId}/comparison-scenarios`,
        {
          name: `Ad-hoc scoring ${new Date().toISOString()}`,
          strategy: "balanced",
          scoring_configuration_id: scoringConfigurationId,
          assumptions: EMPTY_ASSUMPTIONS,
          constraints: {},
        },
      );
      return scenario.scoring_result;
    },
  });
}
