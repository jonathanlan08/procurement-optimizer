"""Comparison-scenario request/response schemas (docs/planning/
03-api-contract.md §4.16, app/models/scenarios.py FROZEN, app/domain/
optimization/contracts.py FROZEN, app/domain/scoring/contracts.py FROZEN).

Wire conventions (§1.2): every decimal crosses the wire as a JSON string,
never a number. Reuses `app.schemas.analysis.LandedCostAssumptionsRequest`
(unscaled decimal strings) for `assumptions` and `app.schemas.analysis.
CriterionSpecResponse` for weight specs — both already shaped exactly right,
duplicating them here would drift.

**§4.16's "Response sketch" is illustrative prose, not a schema** (its own
heading says "sketch"). Two of its fields are structurally incompatible with
the FROZEN domain dataclasses this API persists and cannot honestly be
reproduced:
- `infeasibility_explanation.minimal_relaxations` (sketch: a structured list
  of `{group, from, to, restores_feasibility}`) — `app.domain.optimization.
  contracts.InfeasibilityExplanation.minimal_relaxation` is a single,
  already-composed human sentence (`str | None`), not a list of relaxation
  *options*; the solver never computes more than one. `minimal_relaxation`
  (singular) is exposed instead of inventing structure nothing built.
- `rejected_alternatives` (sketch: `{description, total_cost, delta,
  reason}` objects) — `AllocationResult.rejected_alternatives: tuple[str,
  ...]` is plain human sentences (contracts.py's own docstring: "human
  sentences, e.g. next-best single-supplier"). Exposed as `list[str]`.
- `binding_constraints` (sketch: bare strings like `"max_supplier_count"`) —
  `BindingConstraint` is `{name, detail}`; `detail` carries the full
  sentence the sketch's bare strings cannot. Exposed as `{name, detail}[]`.
- `determinism` (sketch: `{solver, seed, workers, deterministic_time,
  model_hash}`) — `solver_version` is fixed, and `solver.py`'s own module
  docstring rules `random_seed=0` a constant configuration value, not
  per-result data ("nothing to persist per-row"). Exposed as `stats`,
  the FROZEN `SolverStats` shape verbatim (`status_raw`,
  `deterministic_time`, `model_hash`, `num_variables`, `num_constraints`).

`AllocationResultRecord` has no `currency` column (module docstring point 8:
"denominated in whatever ComparisonScenario's owning RFQ's base_currency
already is") — `AllocationResultResponse.from_model` therefore takes the
owning RFQ's `base_currency` as an explicit argument.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.money import to_wire
from app.domain.optimization.contracts import AllocationStatus
from app.models.scenarios import AllocationResultRecord, ComparisonScenario, ScenarioResult
from app.schemas.analysis import CriterionSpecResponse, FractionString, LandedCostAssumptionsRequest
from app.schemas.boms import QuantityString

_STATUS_EXPLANATIONS: dict[AllocationStatus, str] = {
    AllocationStatus.OPTIMAL: (
        "A provably optimal allocation was found within the deterministic search budget."
    ),
    AllocationStatus.FEASIBLE: (
        "A feasible allocation was found but optimality was not proven within the "
        "deterministic search budget."
    ),
    AllocationStatus.INFEASIBLE: (
        "No allocation satisfies every constraint; see infeasibility_explanation."
    ),
    AllocationStatus.ERROR: (
        "The solver failed before producing an allocation; see error_message."
    ),
}


# -- create / rerun / archive requests --------------------------------------


class LockedAllocationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    rfq_line_id: uuid.UUID
    supplier_id: uuid.UUID
    quantity: QuantityString = Field(gt=0)


class ScenarioConstraintsRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    max_supplier_count: int | None = Field(default=None, ge=1)
    max_concentration: FractionString | None = Field(default=None, gt=0, le=1)
    budget_limit: QuantityString | None = Field(default=None, ge=0)
    locked_allocations: list[LockedAllocationRequest] = Field(default_factory=list)
    excluded_supplier_ids: list[uuid.UUID] = Field(default_factory=list)
    allow_incomplete_offers: bool = False


class ScenarioCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    strategy: str
    scoring_configuration_id: uuid.UUID | None = None
    assumptions: LandedCostAssumptionsRequest = Field(default_factory=LandedCostAssumptionsRequest)
    constraints: ScenarioConstraintsRequest = Field(default_factory=ScenarioConstraintsRequest)
    notes: str | None = Field(default=None, max_length=4000)


class ScenarioRerunRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    notes: str | None = Field(default=None, max_length=4000)


class ScenarioArchiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


# -- scoring result -----------------------------------------------------


class CriterionScoreResponse(BaseModel):
    criterion: str
    raw_value: str | None
    normalized_score: str | None
    effective_weight: str
    weighted_contribution: str
    reason: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> CriterionScoreResponse:
        return cls(**raw)


class SupplierScoreResponse(BaseModel):
    supplier_id: str
    supplier_name: str
    total_score: str
    rank: int
    criterion_scores: list[CriterionScoreResponse]
    missing_criteria: list[str]
    weights_renormalized: bool
    excluded: bool
    exclusion_reason: str | None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> SupplierScoreResponse:
        return cls(
            supplier_id=raw["supplier_id"],
            supplier_name=raw["supplier_name"],
            total_score=raw["total_score"],
            rank=raw["rank"],
            criterion_scores=[CriterionScoreResponse.from_json(c) for c in raw["criterion_scores"]],
            missing_criteria=raw["missing_criteria"],
            weights_renormalized=raw["weights_renormalized"],
            excluded=raw["excluded"],
            exclusion_reason=raw["exclusion_reason"],
        )


class ScoringResultResponse(BaseModel):
    """Shaped field-for-field after the FROZEN `ScoringResult` dataclass —
    also, deliberately, the exact shape `frontend/src/features/comparison/
    api.ts`'s `ScoringRunResponse` already anticipated."""

    scores: list[SupplierScoreResponse]
    weights_used: list[CriterionSpecResponse]
    cohort_size: int
    notes: list[str]
    scoring_version: str

    @classmethod
    def from_model(cls, row: ScenarioResult) -> ScoringResultResponse:
        raw = row.scoring_output
        return cls(
            scores=[SupplierScoreResponse.from_json(s) for s in raw["scores"]],
            weights_used=[CriterionSpecResponse.from_json(w) for w in raw["weights_used"]],
            cohort_size=raw["cohort_size"],
            notes=raw["notes"],
            scoring_version=row.scoring_version,
        )


# -- allocation result ----------------------------------------------------


class PriceBreakResponse(BaseModel):
    min_quantity: int
    max_quantity: int | None
    unit_price: str


class AllocationEntryResponse(BaseModel):
    rfq_line_id: str
    quote_line_id: str
    supplier_id: str
    supplier_label: str
    quantity: int
    price_break: PriceBreakResponse
    line_landed_cost: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> AllocationEntryResponse:
        tier = raw["tier_applied"]
        return cls(
            rfq_line_id=raw["rfq_line_id"],
            quote_line_id=raw["quote_line_id"],
            supplier_id=raw["supplier_id"],
            supplier_label=raw["supplier_label"],
            quantity=raw["quantity"],
            price_break=PriceBreakResponse(
                min_quantity=tier["min_quantity"],
                max_quantity=tier["max_quantity"],
                unit_price=tier["landed_unit_cost"],
            ),
            line_landed_cost=raw["line_cost"],
        )


class BindingConstraintResponse(BaseModel):
    name: str
    detail: str


class InfeasibilityExplanationResponse(BaseModel):
    conflicting_constraint_groups: list[str]
    narrative: str
    minimal_relaxation: str | None


class SolverStatsResponse(BaseModel):
    status_raw: str
    deterministic_time: float
    model_hash: str
    num_variables: int
    num_constraints: int


class MoneyAmount(BaseModel):
    amount: str
    currency: str


class AllocationResultResponse(BaseModel):
    solver_status: str
    status_explanation: str
    objective_total_cost: MoneyAmount | None
    objective_source: str
    allocations: list[AllocationEntryResponse]
    binding_constraints: list[BindingConstraintResponse]
    infeasibility_explanation: InfeasibilityExplanationResponse | None
    rejected_alternatives: list[str]
    stats: SolverStatsResponse | None
    optimization_version: str
    error_message: str | None

    @classmethod
    def from_model(
        cls, row: AllocationResultRecord, *, base_currency: str
    ) -> AllocationResultResponse:
        status = row.status
        infeasibility = None
        if row.infeasibility is not None:
            infeasibility = InfeasibilityExplanationResponse(
                conflicting_constraint_groups=row.infeasibility["conflicting_groups"],
                narrative=row.infeasibility["detail"],
                minimal_relaxation=row.infeasibility["minimal_relaxation"],
            )
        stats = None
        if row.stats is not None:
            stats = SolverStatsResponse(**row.stats)
        return cls(
            solver_status=status.value,
            status_explanation=_STATUS_EXPLANATIONS[status],
            objective_total_cost=(
                MoneyAmount(amount=to_wire(row.expected_total_cost), currency=base_currency)
                if row.expected_total_cost is not None
                else None
            ),
            objective_source="exact_decimal_recomputation",
            allocations=[AllocationEntryResponse.from_json(a) for a in row.allocations],
            binding_constraints=[BindingConstraintResponse(**b) for b in row.binding_constraints],
            infeasibility_explanation=infeasibility,
            rejected_alternatives=list(row.rejected_alternatives),
            stats=stats,
            optimization_version=row.optimization_version,
            error_message=row.error_message,
        )


# -- scenario --------------------------------------------------------------


class ScenarioResponse(BaseModel):
    """`GET /comparison-scenarios/{id}`: full snapshot + state (§4.16)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    rfq_id: str
    name: str
    strategy: str
    scoring_configuration_id: str | None
    state: str
    notes: str | None
    calculation_version: str
    solver_version: str
    constraints_snapshot: dict[str, Any]
    assumptions_snapshot: dict[str, Any]
    fx_snapshot: list[Any]
    quote_snapshot_refs: list[Any]
    weights_snapshot: list[Any]
    version: int
    created_by_id: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    scoring_result: ScoringResultResponse
    allocation_result: AllocationResultResponse

    @classmethod
    def from_model(
        cls,
        scenario: ComparisonScenario,
        result: ScenarioResult,
        allocation: AllocationResultRecord,
        *,
        base_currency: str,
    ) -> ScenarioResponse:
        return cls(
            id=str(scenario.id),
            rfq_id=str(scenario.rfq_id),
            name=scenario.name,
            strategy=scenario.strategy.value,
            scoring_configuration_id=(
                str(scenario.scoring_configuration_id)
                if scenario.scoring_configuration_id is not None
                else None
            ),
            state=scenario.state.value,
            notes=scenario.notes,
            calculation_version=scenario.calculation_version,
            solver_version=scenario.solver_version,
            constraints_snapshot=scenario.constraints_snapshot,
            assumptions_snapshot=scenario.assumptions_snapshot,
            fx_snapshot=scenario.fx_snapshot,
            quote_snapshot_refs=scenario.quote_snapshot_refs,
            weights_snapshot=scenario.weights_snapshot,
            version=scenario.version,
            created_by_id=str(scenario.created_by_id),
            created_at=scenario.created_at,
            updated_at=scenario.updated_at,
            completed_at=scenario.completed_at,
            is_archived=scenario.is_archived,
            archived_at=scenario.archived_at,
            archive_reason=scenario.archive_reason,
            scoring_result=ScoringResultResponse.from_model(result),
            allocation_result=AllocationResultResponse.from_model(
                allocation, base_currency=base_currency
            ),
        )


class ScenarioSummaryResponse(BaseModel):
    """`GET /rfqs/{id}/comparison-scenarios`: history, newest first — a
    lean summary (no embedded scoring/allocation payload; fetch `GET
    /comparison-scenarios/{id}` for the full package)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    rfq_id: str
    name: str
    strategy: str
    state: str
    created_by_id: str
    created_at: datetime
    completed_at: datetime | None
    is_archived: bool

    @classmethod
    def from_model(cls, scenario: ComparisonScenario) -> ScenarioSummaryResponse:
        return cls(
            id=str(scenario.id),
            rfq_id=str(scenario.rfq_id),
            name=scenario.name,
            strategy=scenario.strategy.value,
            state=scenario.state.value,
            created_by_id=str(scenario.created_by_id),
            created_at=scenario.created_at,
            completed_at=scenario.completed_at,
            is_archived=scenario.is_archived,
        )


class ScenarioListResponse(BaseModel):
    items: list[ScenarioSummaryResponse]
    page: dict[str, Any]


__all__ = [
    "AllocationEntryResponse",
    "AllocationResultResponse",
    "BindingConstraintResponse",
    "CriterionScoreResponse",
    "InfeasibilityExplanationResponse",
    "LockedAllocationRequest",
    "MoneyAmount",
    "PriceBreakResponse",
    "ScenarioArchiveRequest",
    "ScenarioConstraintsRequest",
    "ScenarioCreateRequest",
    "ScenarioListResponse",
    "ScenarioRerunRequest",
    "ScenarioResponse",
    "ScenarioSummaryResponse",
    "ScoringResultResponse",
    "SolverStatsResponse",
    "SupplierScoreResponse",
]
