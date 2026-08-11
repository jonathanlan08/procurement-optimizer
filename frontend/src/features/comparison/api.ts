/** Comparison workspace data layer — TanStack Query hooks over the live
 * API: landed-cost calculation, scoring-configuration CRUD, and the full
 * comparison-scenario surface (create/list/get/clone/archive — scoring AND
 * allocation together, see the "Scenarios" section of this header) are all
 * real, wired routes.
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
 * ## Scenarios: the real `/comparison-scenarios` surface (Phase 7)
 *
 * Phase 6 (`docs/planning/09-task-decomposition.md` 6.1-6.13) landed the
 * real backend surface below: `backend/src/app/services/scenario_service.py`
 * + `backend/src/app/api/v1/scenarios.py`, mounted in `app/main.py`. There is
 * no separate "ranked scoring" endpoint on its own — scoring only ever
 * happens as half of creating a `ComparisonScenario`
 * (`POST /rfqs/{rfq_id}/comparison-scenarios`, per that service's own module
 * docstring: no job queue exists in this codebase, so scoring AND
 * allocation both run synchronously in one call, one transaction, and the
 * response already carries both `scoring_result` and `allocation_result` —
 * this file no longer needs a thin ad-hoc adapter that throws the
 * allocation half away).
 *
 * Response shapes below mirror `backend/src/app/schemas/scenarios.py`
 * field-for-field:
 *  - **`AllocationEntryResponse.quantity`/`PriceBreakResponse.min_quantity`/
 *    `.max_quantity`** are genuine JSON integers (`int` in the Pydantic
 *    schema, not a scaled decimal wire string) — safe to use as `number`
 *    without violating lib/money.ts's no-`Number()`-on-decimals rule, which
 *    only covers money/quantity *decimal* strings.
 *  - **`ScenarioConstraintsRequest.max_supplier_count`** is likewise a
 *    genuine `int`, not a decimal string — `ScenarioConstraintsInput` below
 *    carries it as `number | null`.
 *  - **`ScenarioConstraintsRequest.max_concentration`** is a `FractionString`
 *    (RATIO_SCALE = 6dp, e.g. `"0.350000"` for 35%) — ScenarioControls.tsx
 *    collects this as a percent in the UI and converts with a pure-string
 *    `percentToFraction` helper, never `Number()`/`parseFloat`.
 *  - **`ComparisonScenario.state`** (draft/running/complete/failed) is a
 *    coarser signal than `AllocationResultResponse.solver_status`
 *    (optimal/feasible/infeasible/error): `scenario_service.py`'s
 *    `_run_and_persist` maps `state = FAILED` only when
 *    `allocation.status is ERROR` — an INFEASIBLE allocation still reports
 *    `state = "complete"`. `ScenarioSummaryResponse` (the list-row shape)
 *    carries only `state`, not the granular solver status or the expected
 *    cost — both require the full `GET /comparison-scenarios/{id}` fetch.
 *    ScenarioHistory.tsx therefore shows the state badge in the collapsed
 *    row (an honest label, not a fabricated solver-status badge) and only
 *    fetches+shows the true solver status/expected-cost once a row is
 *    expanded — the same "don't N+1 the list, flag the shape gap" judgement
 *    RfqsPage.tsx's own file header already documents for its own list-vs-
 *    detail gap.
 *  - **`constraints_snapshot`/`assumptions_snapshot`/`weights_snapshot`/
 *    `fx_snapshot`** are `dict[str, Any]`/`list[Any]` JSONB columns in the
 *    FROZEN model (`app/models/scenarios.py`) — genuinely untyped on the
 *    backend, not just under-documented here. Typed loosely
 *    (`Record<string, unknown>`/`unknown[]`) and narrowed defensively at the
 *    one render site (ScenarioHistory.tsx's snapshot summary), the same way
 *    the backend itself treats them as opaque JSON, not a FROZEN contract.
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

// -- scoring result (nested inside a scenario response) --------------------

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

/** Mirrors `backend/src/app/schemas/scenarios.py`'s `ScoringResultResponse`
 * (its own docstring: shaped field-for-field after the FROZEN
 * `ScoringResult` dataclass). Nested under `ScenarioResponse.scoring_result`. */
export interface ScoringResultResponse {
  scores: SupplierScoreResponse[];
  weights_used: CriterionSpecResponse[];
  cohort_size: number;
  notes: string[];
  scoring_version: string;
}

// -- comparison strategies --------------------------------------------------

/** Mirrors `app.models.scenarios.ComparisonStrategy` (FROZEN StrEnum). */
export type ComparisonStrategyValue =
  | "lowest_unit_price"
  | "lowest_landed_cost"
  | "fastest_delivery"
  | "lowest_risk"
  | "balanced"
  | "custom";

export interface ComparisonStrategyDescriptor {
  value: ComparisonStrategyValue;
  label: string;
  description: string;
  /** Mirrors `scenario_service.py`'s `_STRATEGIES_NEEDING_CONFIG` — only
   * `balanced`/`custom` read `scoring_configuration_id`; the server ignores
   * it entirely for the other four (a fixed built-in weight set is used
   * instead), and 422s if it's missing for these two. */
  needsScoringConfig: boolean;
}

/** One-line descriptions paraphrase the fixed weight sets
 * `scenario_service.py`'s `_weights_for_strategy` actually applies
 * (`LOWEST_UNIT_PRICE_WEIGHTS`/`LOWEST_LANDED_COST_WEIGHTS`/
 * `FASTEST_DELIVERY_WEIGHTS`/`LOWEST_RISK_WEIGHTS` module constants). */
export const COMPARISON_STRATEGIES: ComparisonStrategyDescriptor[] = [
  {
    value: "lowest_unit_price",
    label: "Lowest unit price",
    description: "Ranks suppliers by quoted unit price alone, before landed-cost extras.",
    needsScoringConfig: false,
  },
  {
    value: "lowest_landed_cost",
    label: "Lowest landed cost",
    description: "Ranks suppliers by total landed cost — material, logistics, risk, and financing.",
    needsScoringConfig: false,
  },
  {
    value: "fastest_delivery",
    label: "Fastest delivery",
    description: "Ranks suppliers by quoted lead time alone.",
    needsScoringConfig: false,
  },
  {
    value: "lowest_risk",
    label: "Lowest risk",
    description: "Weighs quality history, defect rate, and on-time delivery roughly evenly.",
    needsScoringConfig: false,
  },
  {
    value: "balanced",
    label: "Balanced",
    description: "Uses a scoring configuration's weighted criteria across cost, risk, and delivery.",
    needsScoringConfig: true,
  },
  {
    value: "custom",
    label: "Custom",
    description: "Uses the exact weights of a scoring configuration you select.",
    needsScoringConfig: true,
  },
];

// -- allocation result -------------------------------------------------

/** Mirrors `app.domain.optimization.contracts.AllocationStatus` (FROZEN). */
export type AllocationStatus = "optimal" | "feasible" | "infeasible" | "error";

export interface PriceBreakResponse {
  min_quantity: number;
  max_quantity: number | null;
  /** Decimal wire string (landed unit cost for this tier), not a JSON number. */
  unit_price: string;
}

export interface AllocationEntryResponse {
  rfq_line_id: string;
  quote_line_id: string;
  supplier_id: string;
  supplier_label: string;
  quantity: number;
  price_break: PriceBreakResponse;
  line_landed_cost: string;
}

export interface BindingConstraintResponse {
  name: string;
  detail: string;
}

export interface InfeasibilityExplanationResponse {
  conflicting_constraint_groups: string[];
  narrative: string;
  /** Singular — the solver never computes more than one relaxation option
   * (backend/src/app/schemas/scenarios.py's own module docstring). */
  minimal_relaxation: string | null;
}

export interface SolverStatsResponse {
  status_raw: string;
  deterministic_time: number;
  model_hash: string;
  num_variables: number;
  num_constraints: number;
  /** Optimality is proven for costs rounded to 0.0001 currency units; an
   * unconsidered alternative can be at most this much cheaper (decimal
   * string). Null on results saved before the field existed. */
  max_scaling_error?: string | null;
}

export interface MoneyAmount {
  amount: string;
  currency: string;
}

export interface AllocationResultResponse {
  solver_status: AllocationStatus;
  status_explanation: string;
  objective_total_cost: MoneyAmount | null;
  objective_source: string;
  allocations: AllocationEntryResponse[];
  binding_constraints: BindingConstraintResponse[];
  infeasibility_explanation: InfeasibilityExplanationResponse | null;
  rejected_alternatives: string[];
  stats: SolverStatsResponse | null;
  optimization_version: string;
  error_message: string | null;
}

// -- scenarios ------------------------------------------------------------

/** Mirrors `app.models.scenarios.ScenarioState` (FROZEN) — see this file's
 * header note on why this is coarser than `AllocationStatus`. */
export type ScenarioState = "draft" | "running" | "complete" | "failed";

export interface ScenarioResponse {
  id: string;
  rfq_id: string;
  name: string;
  strategy: string;
  scoring_configuration_id: string | null;
  state: ScenarioState;
  notes: string | null;
  calculation_version: string;
  solver_version: string;
  constraints_snapshot: Record<string, unknown>;
  assumptions_snapshot: Record<string, unknown>;
  fx_snapshot: unknown[];
  quote_snapshot_refs: unknown[];
  weights_snapshot: unknown[];
  version: number;
  created_by_id: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  is_archived: boolean;
  archived_at: string | null;
  archive_reason: string | null;
  scoring_result: ScoringResultResponse;
  allocation_result: AllocationResultResponse;
}

/** `GET /rfqs/{id}/comparison-scenarios` list rows — deliberately lean (no
 * embedded scoring/allocation payload); see this file's header note on the
 * `state`-vs-`solver_status`/expected-cost gap this leaves in the list view. */
export interface ScenarioSummaryResponse {
  id: string;
  rfq_id: string;
  name: string;
  strategy: string;
  state: ScenarioState;
  created_by_id: string;
  created_at: string;
  completed_at: string | null;
  is_archived: boolean;
}

export interface ScenarioListResponse {
  items: ScenarioSummaryResponse[];
  page: PageInfo;
}

export interface PageInfo {
  limit: number;
  offset: number;
  total: number;
}

export interface ScenarioConstraintsInput {
  /** Genuine JSON integer — see this file's header note. */
  max_supplier_count: number | null;
  /** `FractionString` wire format (RATIO_SCALE = 6dp), e.g. `"0.350000"`. */
  max_concentration: string | null;
  /** Unscaled-looking decimal wire string; server parses/validates at scale. */
  budget_limit: string | null;
  excluded_supplier_ids: string[];
  allow_incomplete_offers: boolean;
}

export const EMPTY_CONSTRAINTS: ScenarioConstraintsInput = {
  max_supplier_count: null,
  max_concentration: null,
  budget_limit: null,
  excluded_supplier_ids: [],
  allow_incomplete_offers: false,
};

export const scenarioKeys = {
  all: ["comparison-scenarios"] as const,
  rfqLists: () => [...scenarioKeys.all, "rfq-list"] as const,
  rfqList: (rfqId: string) => [...scenarioKeys.rfqLists(), rfqId] as const,
  details: () => [...scenarioKeys.all, "detail"] as const,
  detail: (id: string) => [...scenarioKeys.details(), id] as const,
};

export function useRfqScenarios(rfqId: string | null) {
  return useQuery({
    queryKey: scenarioKeys.rfqList(rfqId ?? ""),
    queryFn: () =>
      get<ScenarioListResponse>(`/api/v1/rfqs/${rfqId}/comparison-scenarios?limit=50&offset=0`),
    enabled: rfqId !== null,
  });
}

/** Full scenario package (scoring + allocation + snapshots) — used both for
 * the "load a history row into the panels" flow and for the snapshot-summary
 * detail expansion in ScenarioHistory.tsx. */
export function useScenario(scenarioId: string | null) {
  return useQuery({
    queryKey: scenarioKeys.detail(scenarioId ?? ""),
    queryFn: () => get<ScenarioResponse>(`/api/v1/comparison-scenarios/${scenarioId}`),
    enabled: scenarioId !== null,
  });
}

export interface CreateScenarioVars {
  rfqId: string;
  name: string;
  strategy: ComparisonStrategyValue;
  scoringConfigurationId: string | null;
  constraints: ScenarioConstraintsInput;
}

export function useCreateScenario() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rfqId, name, strategy, scoringConfigurationId, constraints }: CreateScenarioVars) =>
      post<ScenarioResponse>(`/api/v1/rfqs/${rfqId}/comparison-scenarios`, {
        name,
        strategy,
        scoring_configuration_id: scoringConfigurationId,
        assumptions: EMPTY_ASSUMPTIONS,
        constraints: {
          max_supplier_count: constraints.max_supplier_count,
          max_concentration: constraints.max_concentration,
          budget_limit: constraints.budget_limit,
          locked_allocations: [],
          excluded_supplier_ids: constraints.excluded_supplier_ids,
          allow_incomplete_offers: constraints.allow_incomplete_offers,
        },
      }),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: scenarioKeys.rfqList(vars.rfqId) });
    },
  });
}

export interface CloneScenarioVars {
  rfqId: string;
  scenarioId: string;
}

/** `POST /comparison-scenarios/{id}/clone` — this codebase's "rerun": a NEW
 * scenario, freshly solved from the original's stored snapshots (backend/
 * src/app/api/v1/scenarios.py's own module docstring, "Deviation 2"). */
export function useCloneScenario() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ scenarioId }: CloneScenarioVars) =>
      post<ScenarioResponse>(`/api/v1/comparison-scenarios/${scenarioId}/clone`, { notes: null }),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: scenarioKeys.rfqList(vars.rfqId) });
    },
  });
}

export interface ArchiveScenarioVars {
  rfqId: string;
  scenarioId: string;
  reason: string;
}

export function useArchiveScenario() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ scenarioId, reason }: ArchiveScenarioVars) =>
      post<ScenarioSummaryResponse>(`/api/v1/comparison-scenarios/${scenarioId}/archive`, { reason }),
    onSuccess: (_result, vars) => {
      void queryClient.invalidateQueries({ queryKey: scenarioKeys.rfqList(vars.rfqId) });
      void queryClient.invalidateQueries({ queryKey: scenarioKeys.detail(vars.scenarioId) });
    },
  });
}
