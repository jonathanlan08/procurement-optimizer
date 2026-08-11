"""Scenario service — Phase 6 assembly: scoring + order-allocation over one
RFQ's quotes, persisted as `ComparisonScenario` + `ScenarioResult` +
`AllocationResultRecord` (docs/planning/03-api-contract.md §4.16,
app/models/scenarios.py FROZEN, app/domain/optimization/{contracts,solver}.py
FROZEN, app/domain/scoring/{contracts,scorer}.py FROZEN).

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception.

## Deviation 1 — one synchronous call does BOTH scoring and allocation

§4.16's literal route table is a two-step, async-job design: `POST
.../comparison-scenarios` (`202` job, scores only) then a separate `POST
.../{id}/optimize` (`202` job, allocates). This module does not follow that
shape. Two independent reasons, both stronger than "the contract's literal
table wins" (which this task's own READ FIRST list states applies to
§4.16's *routes*, not to whether scoring and allocation are one transaction
or two):

1. **No job queue exists anywhere in this codebase.** `services/
   extraction_service.py`'s own module docstring establishes `JOB_RUNNER=
   inline` as the only shape this v0.1 build implements anywhere, and
   `api/v1/extractions.py`/`api/v1/matching.py` already apply that precedent
   to collapse their own `202`-job contract entries into synchronous calls.
   This module does the same.
2. **The FROZEN persistence shape has no state for "scored but not yet
   allocated."** `ComparisonScenario.state` is `draft -> running ->
   complete|failed` (one machine, not two), and both `ScenarioResult` and
   `AllocationResultRecord` carry `UNIQUE (organization_id, scenario_id)` —
   "re-scoring/re-solving creates a NEW scenario entirely" (models/
   scenarios.py module docstring, "Immutability"). There is structurally no
   row shape for "this scenario has been scored but not solved" to live in.
   `create_and_run` therefore does steps (a)-(e) of this task's own OBJECTIVE
   in one request, one transaction: resolve landed costs, snapshot, score,
   solve, persist all three rows together.

`POST .../{id}/optimize` and `GET .../{id}/results` /`GET .../{id}/allocation`
are still mounted in `api/v1/scenarios.py` (contract's literal paths kept for
compatibility) — `/optimize` is idempotent there, simply returning the
allocation half of the *already-solved* scenario (this service exposes no
"solve again in place" operation; `rerun` below is the only way to get a
fresh solve, and it always produces a new scenario id, never mutates one in
place, per the immutability rule above).

## Deviation 2 — `POST .../clone` IS this task's own `rerun`

§4.16 names this route `clone` ("new scenario pre-filled from an old one —
the supported way to change assumptions without mutating history"); this
task's own OBJECTIVE names the same operation `rerun` and is far more
specific about its behavior ("creates a NEW scenario cloned from the stored
snapshots... with a FRESH SOLVE... audit scenario.rerun"). Both describe the
same shape (new scenario, old inputs); the OBJECTIVE's fresh-solve behavior
is adopted verbatim (an unsolved "draft copy" has nowhere to live, per
Deviation 1) under the contract's route name: `POST .../{id}/clone` in the
API layer calls `ScenarioService.rerun` here. `rerun` reuses the ORIGINAL
scenario's `quote_snapshot_refs` byte-for-byte (does not re-resolve "latest"
landed-cost results) — this is what makes "rerun reproduces identical
scores/allocations (same model_hash) when nothing changed" true: the scorer
and solver are pure functions of their inputs, so identical
`SupplierScoringInput`/`AllocationProblem` construction from the identical
stored refs yields an identical `model_hash`.

## Deviation 3 — quote eligibility: not-superseded, not-rejected (NOT "confirmed")

06-optimization-methodology.md §2 lists "quote not confirmed... " as a
pre-solve filter. `QuoteStatus` is `draft|in_review|confirmed|superseded|
rejected`, but `services/quote_service.py`'s own module docstring confirms
this build's manual-entry-only quote pipeline never transitions a quote past
`draft`/`in_review` — there is no route anywhere in `api/v1/quotes.py` that
reaches `CONFIRMED`. Gating scenario eligibility on `CONFIRMED` would make
`create_and_run` permanently unusable (409 "no eligible quote lines" on every
RFQ, forever). This module instead treats any quote returned by
`QuoteRepository.list_by_rfq` (already excludes `SUPERSEDED`) that is not
`REJECTED` as eligible — `DRAFT`/`IN_REVIEW`/`CONFIRMED` all count.

## Deviation 4 — `Offer.incomplete_landed_cost` = INCOMPLETE, not "!= COMPLETE"

`app.domain.optimization.contracts.Offer`'s own docstring says
"completeness != COMPLETE -> excluded unless overridden." Followed literally,
every offer in every scenario would always be flagged incomplete:
`services/landed_cost_service.py`'s own module docstring establishes that
`documentation`/`handling` costs have **no source column anywhere in this
schema, ever** — not even `assume_missing_costs_zero` reaches them via an
assumption field — so `Completeness.COMPLETE` is structurally unreachable in
this codebase's current build (the best any result can do is
`ASSUMPTION_DEPENDENT`, per that same module's worked-example test). Literal
adherence would force `allow_incomplete_offers=True` on every real-world
scenario by default, which is not "honest uncertainty reporting" (the
product's stated ethos), it is a UX trap. This module maps
`incomplete_landed_cost = (completeness is Completeness.INCOMPLETE)`:
`ASSUMPTION_DEPENDENT` (every input present, either supplier-stated or a
recorded, human-visible assumption) is treated as usable by default;
`INCOMPLETE` (a required input has no value AND no assumption covers it) is
the only case requiring the explicit `allow_incomplete_offers` override.

## Strategy weighting (module constants below, `is_sample_weight=False`)

`lowest_unit_price`/`lowest_landed_cost`/`fastest_delivery` are
single-criterion; `lowest_risk` is quality/defect/on-time-delivery in
(documented, not-quite-equal-due-to-exact-decimal-limits) thirds
0.34/0.33/0.33. `balanced`/`custom` use the referenced `ScoringConfiguration`
verbatim. One deliberate, load-bearing choice: **`lowest_unit_price` scores
`Criterion.USER_DEFINED`** ("quoted unit price, FX/unit-normalized, BEFORE
landed-cost extras" — sourced from the `extended_material` component's own
`normalized_unit_price` input on each offer's landed-cost result), not
`Criterion.EFFECTIVE_UNIT_COST` (which IS the landed number, extras
included). The FROZEN `Criterion` enum has no literal "raw quoted price"
member; `EFFECTIVE_UNIT_COST` is the closest *named* fit but using it here
would make `lowest_unit_price` and `lowest_landed_cost` rank suppliers
near-identically, defeating this product's own signature thesis (SPEC: a
lower quoted price can still be a WORSE deal after landed-cost extras) and
the explicit test requirement that the two strategies disagree.

## Multi-line-per-supplier scoring aggregation

`ScenarioResult` is one row per SCENARIO with one `SupplierScore` per
SUPPLIER (models/scenarios.py module docstring point 7) — not per (supplier,
RFQ line) — but a supplier may have matched, landed-costed offers against
several RFQ lines. Aggregation policy per criterion (documented here because
none of it is spelled out upstream): `TOTAL_LANDED_COST` sums across the
supplier's offers (what buying everything from them costs);
`EFFECTIVE_UNIT_COST` is the quantity-weighted average; `LEAD_TIME` takes the
MAX (the order isn't done until the slowest line ships); `CAPACITY` takes the
MIN (the bottleneck line); `MOQ_FLEXIBILITY` inverts the MAX (most
restrictive) MOQ, per the FROZEN criterion's own "inverted upstream" note;
`PAYMENT_TERMS` reads the first available `quote_terms` row among the
supplier's offers; `QUALITY_HISTORY`/`DEFECT_RATE`/`ON_TIME_DELIVERY` are
supplier-level already (latest `SupplierPerformanceRecord`), no aggregation
needed. `SPEC_COMPLIANCE` has no source column anywhere in this schema and is
always missing (renormalized away), documented rather than silently omitted.
Allocation itself needs none of this: `AllocationProblem` is naturally
per-(RFQ line, offer), so the solver sees every offer individually.

## Price-break tier re-costing (v0.1 simplification, per this task's OBJECTIVE)

An offer's tiers are built from the landed-cost result's `effective_unit_cost`
plus a per-unit overhead spread: `overhead_per_unit = effective_unit_cost -
normalized_unit_price` (the material price actually used to compute that
result, read back off the `extended_material` component's own `inputs`).
When the quote line has price breaks, each tier's `landed_unit_cost =
tier.unit_price + overhead_per_unit` (assumes fixed/logistics/import/risk/
financing overhead is flat across tiers — a documented approximation, not a
re-run of the calculator at each tier's quantity, which is out of this task's
scope). When there are no price breaks, this collapses to exactly the
OBJECTIVE's own stated simplification: one tier at `effective_unit_cost`
`[1, None)`. `Offer.fixed_cost` is always `Decimal("0")`: `effective_unit_cost`
already amortizes tooling/setup across `accepted_quantity` (05
§"allocated_fixed_cost"), so passing the quote line's raw tooling/setup cost
again would double-count it.

## Quantity normalization (documented gap)

FROZEN `DemandLine.required_quantity`/`Offer` tier bounds/`moq`/`capacity`
are plain `int`. This schema stores quantities as `NUMERIC(18,6)`. Per
06-optimization-methodology.md §3 ("`QTY_MULT` default 1 for integral
parts"), this module always uses `QTY_MULT=1` — i.e. rounds to the nearest
whole unit (`ROUND_HALF_EVEN`) — and does not implement the methodology's
optional scaling for continuous-dimension (kg/m) parts. Flagged as a known,
accepted v0.1 gap, not a silent truncation.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Final

from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import ConflictStateError, NotFoundError, ValidationAppError
from app.core.ids import IdGenerator
from app.core.money import CALC_CONTEXT, quantize_unit_price, to_wire
from app.domain.landed_cost.contracts import CALCULATION_VERSION, CostComponent
from app.domain.optimization.contracts import (
    OPTIMIZATION_VERSION,
    AllocationConstraints,
    AllocationProblem,
    AllocationResult,
    AllocationStatus,
    BindingConstraint,
    DemandLine,
    EligibilityExclusion,
    InfeasibilityExplanation,
    LockedAllocation,
    Offer,
    OfferTier,
    SolverStats,
)
from app.domain.optimization.solver import AllocationSolver
from app.domain.scoring.contracts import (
    Criterion,
    CriterionScore,
    CriterionSpec,
    Direction,
    ScoringResult,
    SupplierCriterionValue,
    SupplierScore,
    SupplierScoringInput,
)
from app.domain.scoring.scorer import ScorerV1
from app.domain.values import Completeness
from app.models.analysis import LandedCostComponent, ScoringConfiguration
from app.models.analysis import LandedCostResult as LandedCostResultRow
from app.models.quotes import Quote, QuoteLine, QuotePriceBreak, QuoteStatus
from app.models.rfqs import Rfq, RfqLine
from app.models.scenarios import (
    AllocationResultRecord,
    ComparisonScenario,
    ComparisonStrategy,
    ScenarioResult,
    ScenarioState,
)
from app.models.suppliers import Supplier
from app.repositories.base import OrgScopedRepository
from app.repositories.matching_repository import QuoteLineRepository
from app.repositories.part_repository import PartRepository
from app.repositories.quote_repository import QuoteRepository
from app.repositories.rfq_repository import RfqRepository
from app.repositories.supplier_performance_repository import SupplierPerformanceRepository
from app.repositories.supplier_repository import SupplierRepository
from app.services.audit import AuditRecorder
from app.services.landed_cost_service import LandedCostAssumptions, LandedCostService
from app.services.scoring_config_service import ScoringConfigService

# -- strategy weight constants (module docstring "Strategy weighting") -----

LOWEST_UNIT_PRICE_WEIGHTS: Final[tuple[CriterionSpec, ...]] = (
    CriterionSpec(
        Criterion.USER_DEFINED,
        Decimal("1"),
        Direction.LOWER_IS_BETTER,
        "Quoted unit price (FX/unit normalized, before landed-cost extras)",
        False,
    ),
)
LOWEST_LANDED_COST_WEIGHTS: Final[tuple[CriterionSpec, ...]] = (
    CriterionSpec(
        Criterion.TOTAL_LANDED_COST, Decimal("1"), Direction.LOWER_IS_BETTER,
        "Total landed cost", False,
    ),
)
FASTEST_DELIVERY_WEIGHTS: Final[tuple[CriterionSpec, ...]] = (
    CriterionSpec(Criterion.LEAD_TIME, Decimal("1"), Direction.LOWER_IS_BETTER, "Lead time", False),
)
LOWEST_RISK_WEIGHTS: Final[tuple[CriterionSpec, ...]] = (
    CriterionSpec(
        Criterion.QUALITY_HISTORY, Decimal("0.34"), Direction.HIGHER_IS_BETTER,
        "Quality history", False,
    ),
    CriterionSpec(
        Criterion.DEFECT_RATE, Decimal("0.33"), Direction.LOWER_IS_BETTER, "Defect rate", False
    ),
    CriterionSpec(
        Criterion.ON_TIME_DELIVERY, Decimal("0.33"), Direction.HIGHER_IS_BETTER,
        "On-time delivery", False,
    ),
)

_STRATEGIES_NEEDING_CONFIG: Final[frozenset[ComparisonStrategy]] = frozenset(
    {ComparisonStrategy.BALANCED, ComparisonStrategy.CUSTOM}
)


def _to_int_qty(value: Decimal) -> int:
    """QTY_MULT=1 normalization — see module docstring."""
    return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))


# -- constraints input (mirrors LandedCostAssumptions's plain-dataclass
# convention: the API layer parses its Pydantic request into this) ---------


@dataclass(frozen=True, slots=True)
class LockedAllocationInput:
    rfq_line_id: uuid.UUID
    supplier_id: uuid.UUID
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ScenarioConstraintsInput:
    max_supplier_count: int | None = None
    max_concentration: Decimal | None = None
    budget_limit: Decimal | None = None
    locked_allocations: tuple[LockedAllocationInput, ...] = field(default_factory=tuple)
    excluded_supplier_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    allow_incomplete_offers: bool = False


@dataclass(frozen=True, slots=True)
class ScenarioPackage:
    scenario: ComparisonScenario
    result: ScenarioResult
    allocation: AllocationResultRecord
    rfq: Rfq


@dataclass(frozen=True, slots=True)
class _OfferContext:
    quote_line: QuoteLine
    quote: Quote
    rfq_line: RfqLine
    supplier: Supplier
    landed_row: LandedCostResultRow
    landed_components: list[LandedCostComponent]
    price_breaks: list[QuotePriceBreak]


# -- tiny repositories (module docstring: ScenarioResult/AllocationResultRecord
# are immutable, one row per scenario; these exist only for org-scoped
# .add()/lookup-by-scenario, the same "no dedicated repositories.py module
# exists for these two, so add the minimum here" pattern
# repositories/matching_repository.py's own docstring documents for
# QuoteLineRepository) ------------------------------------------------------


class _ScenarioRepository(OrgScopedRepository[ComparisonScenario]):
    model = ComparisonScenario


class _ScenarioResultRepository(OrgScopedRepository[ScenarioResult]):
    model = ScenarioResult

    def get_by_scenario(self, scenario_id: uuid.UUID) -> ScenarioResult | None:
        stmt = self._base_query().where(ScenarioResult.scenario_id == scenario_id)
        return self.session.execute(stmt).scalar_one_or_none()


class _AllocationResultRepository(OrgScopedRepository[AllocationResultRecord]):
    model = AllocationResultRecord

    def get_by_scenario(self, scenario_id: uuid.UUID) -> AllocationResultRecord | None:
        stmt = self._base_query().where(AllocationResultRecord.scenario_id == scenario_id)
        return self.session.execute(stmt).scalar_one_or_none()


# -- JSON serialization helpers (snapshots + persisted result blobs) -------


def _assumptions_json(a: LandedCostAssumptions) -> dict[str, Any]:
    return dict(asdict(a))


def _constraints_json(c: ScenarioConstraintsInput) -> dict[str, Any]:
    return {
        "max_supplier_count": c.max_supplier_count,
        "max_concentration": (
            to_wire(c.max_concentration) if c.max_concentration is not None else None
        ),
        "budget_limit": to_wire(c.budget_limit) if c.budget_limit is not None else None,
        "locked_allocations": [
            {
                "rfq_line_id": str(item.rfq_line_id),
                "supplier_id": str(item.supplier_id),
                "quantity": to_wire(item.quantity),
            }
            for item in c.locked_allocations
        ],
        "excluded_supplier_ids": [str(s) for s in c.excluded_supplier_ids],
        "allow_incomplete_offers": c.allow_incomplete_offers,
    }


def _assumptions_from_snapshot(raw: dict[str, Any]) -> LandedCostAssumptions:
    return LandedCostAssumptions(
        quality_risk_rate=raw.get("quality_risk_rate"),
        delay_risk_per_day=raw.get("delay_risk_per_day"),
        promised_lead_time_days=raw.get("promised_lead_time_days"),
        required_lead_time_days=raw.get("required_lead_time_days"),
        annual_rate=raw.get("annual_rate"),
        baseline_terms_days=raw.get("baseline_terms_days"),
        tariff_rate=raw.get("tariff_rate"),
        duty_rate=raw.get("duty_rate"),
        assume_missing_costs_zero=bool(raw.get("assume_missing_costs_zero", False)),
    )


def _constraints_from_snapshot(raw: dict[str, Any]) -> ScenarioConstraintsInput:
    return ScenarioConstraintsInput(
        max_supplier_count=raw.get("max_supplier_count"),
        max_concentration=(
            Decimal(raw["max_concentration"]) if raw.get("max_concentration") else None
        ),
        budget_limit=(Decimal(raw["budget_limit"]) if raw.get("budget_limit") else None),
        locked_allocations=tuple(
            LockedAllocationInput(
                rfq_line_id=uuid.UUID(item["rfq_line_id"]),
                supplier_id=uuid.UUID(item["supplier_id"]),
                quantity=Decimal(item["quantity"]),
            )
            for item in raw.get("locked_allocations", [])
        ),
        excluded_supplier_ids=tuple(uuid.UUID(s) for s in raw.get("excluded_supplier_ids", [])),
        allow_incomplete_offers=bool(raw.get("allow_incomplete_offers", False)),
    )


def _criterion_spec_json(spec: CriterionSpec) -> dict[str, Any]:
    return {
        "criterion": spec.criterion.value,
        "weight": to_wire(spec.weight),
        "direction": spec.direction.value,
        "label": spec.label,
        "is_sample_weight": spec.is_sample_weight,
    }


def _criterion_spec_from_config_json(raw: dict[str, Any]) -> CriterionSpec:
    return CriterionSpec(
        criterion=Criterion(raw["criterion"]),
        weight=Decimal(str(raw["weight"])),
        direction=Direction(raw["direction"]),
        label=raw.get("label") or raw["criterion"],
        is_sample_weight=bool(raw.get("is_sample_weight", False)),
    )


def _criterion_score_json(cs: CriterionScore) -> dict[str, Any]:
    return {
        "criterion": cs.criterion.value,
        "raw_value": to_wire(cs.raw_value) if cs.raw_value is not None else None,
        "normalized_score": (
            to_wire(cs.normalized_score) if cs.normalized_score is not None else None
        ),
        "effective_weight": to_wire(cs.effective_weight),
        "weighted_contribution": to_wire(cs.weighted_contribution),
        "reason": cs.reason,
    }


def _supplier_score_json(ss: SupplierScore) -> dict[str, Any]:
    return {
        "supplier_id": str(ss.supplier_id),
        "supplier_name": ss.supplier_name,
        "total_score": to_wire(ss.total_score),
        "rank": ss.rank,
        "criterion_scores": [_criterion_score_json(cs) for cs in ss.criterion_scores],
        "missing_criteria": [c.value for c in ss.missing_criteria],
        "weights_renormalized": ss.weights_renormalized,
        "excluded": ss.excluded,
        "exclusion_reason": ss.exclusion_reason,
    }


def _scoring_output_json(result: ScoringResult) -> dict[str, Any]:
    return {
        "scores": [_supplier_score_json(s) for s in result.scores],
        "weights_used": [_criterion_spec_json(w) for w in result.weights_used],
        "cohort_size": result.cohort_size,
        "notes": list(result.notes),
    }


def _binding_constraint_json(bc: BindingConstraint) -> dict[str, Any]:
    return {"name": bc.name, "detail": bc.detail}


def _infeasibility_json(inf: InfeasibilityExplanation | None) -> dict[str, Any] | None:
    if inf is None:
        return None
    return {
        "conflicting_groups": list(inf.conflicting_groups),
        "detail": inf.detail,
        "minimal_relaxation": inf.minimal_relaxation,
    }


def _solver_stats_json(stats: SolverStats | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    return {
        "status_raw": stats.status_raw,
        "deterministic_time": stats.deterministic_time,
        "model_hash": stats.model_hash,
        "num_variables": stats.num_variables,
        "num_constraints": stats.num_constraints,
        # §7.4 disclosure (2026-08 calculation audit F1); string on the wire
        "max_scaling_error": (
            to_wire(stats.max_scaling_error) if stats.max_scaling_error is not None else None
        ),
    }


def _allocation_entry_json(entry: Any) -> dict[str, Any]:
    return {
        "rfq_line_id": str(entry.rfq_line_id),
        "quote_line_id": str(entry.quote_line_id),
        "supplier_id": str(entry.supplier_id),
        "supplier_label": entry.supplier_label,
        "quantity": entry.quantity,
        "tier_applied": {
            "min_quantity": entry.tier_applied.min_quantity,
            "max_quantity": entry.tier_applied.max_quantity,
            "landed_unit_cost": to_wire(entry.tier_applied.landed_unit_cost),
        },
        "line_cost": to_wire(entry.line_cost),
    }


def _scenario_audit_state(scenario: ComparisonScenario) -> dict[str, Any]:
    return {
        "rfq_id": str(scenario.rfq_id),
        "name": scenario.name,
        "strategy": scenario.strategy.value,
        "scoring_configuration_id": (
            str(scenario.scoring_configuration_id)
            if scenario.scoring_configuration_id is not None
            else None
        ),
        "state": scenario.state.value,
        "version": scenario.version,
        "is_archived": scenario.is_archived,
        "archived_at": scenario.archived_at.isoformat() if scenario.archived_at else None,
        "archive_reason": scenario.archive_reason,
    }


def _weighted_avg(pairs: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Quantity-weighted average of (value, quantity) pairs; None if the
    total quantity is zero or the list is empty."""
    if not pairs:
        return None
    with localcontext(CALC_CONTEXT):
        total_qty = sum((q for _, q in pairs), Decimal("0"))
        if total_qty == 0:
            return None
        total = sum((v * q for v, q in pairs), Decimal("0"))
        return total / total_qty


class ScenarioService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids

        self._rfq_repo = RfqRepository(session, organization_id)
        self._quote_repo = QuoteRepository(session, organization_id)
        self._quote_line_repo = QuoteLineRepository(session, organization_id)
        self._supplier_repo = SupplierRepository(session, organization_id)
        self._part_repo = PartRepository(session, organization_id)
        self._performance_repo = SupplierPerformanceRepository(session, organization_id)
        self._scenario_repo = _ScenarioRepository(session, organization_id)
        self._result_repo = _ScenarioResultRepository(session, organization_id)
        self._allocation_repo = _AllocationResultRepository(session, organization_id)

        self._landed_cost = LandedCostService(session, organization_id, audit, clock, ids)
        self._scoring_configs = ScoringConfigService(session, organization_id, audit, clock, ids)

    # -- reads ---------------------------------------------------------

    def get(self, scenario_id: uuid.UUID) -> ScenarioPackage:
        scenario = self._scenario_repo.get_or_raise(scenario_id)
        rfq = self._rfq_repo.get_or_raise(scenario.rfq_id)
        return self._package(scenario, rfq)

    def list_for_rfq(
        self, rfq_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ComparisonScenario], int]:
        self._rfq_repo.get_or_raise(rfq_id)
        items = self._scenario_repo.list_page(
            ComparisonScenario.rfq_id == rfq_id,
            order_by=ComparisonScenario.created_at.desc(),
            limit=limit,
            offset=offset,
        )
        total = self._scenario_repo.count(ComparisonScenario.rfq_id == rfq_id)
        return items, total

    def _package(self, scenario: ComparisonScenario, rfq: Rfq) -> ScenarioPackage:
        result = self._result_repo.get_by_scenario(scenario.id)
        allocation = self._allocation_repo.get_by_scenario(scenario.id)
        if result is None or allocation is None:  # pragma: no cover - data integrity invariant
            raise NotFoundError("ScenarioResult")
        return ScenarioPackage(scenario=scenario, result=result, allocation=allocation, rfq=rfq)

    # -- archive ---------------------------------------------------------

    def archive(
        self, scenario_id: uuid.UUID, *, reason: str, actor_id: uuid.UUID
    ) -> ComparisonScenario:
        scenario = self._scenario_repo.get_or_raise(scenario_id)
        if scenario.is_archived:
            raise ValidationAppError("Scenario is already archived.")

        before = _scenario_audit_state(scenario)
        now = self._clock.now()
        scenario.archived_at = now
        scenario.archived_by_id = actor_id
        scenario.archive_reason = reason
        scenario.version += 1
        scenario.updated_at = now
        self._db.flush()

        self._audit.record(
            "scenario.archived",
            entity_type="comparison_scenario",
            entity_id=scenario.id,
            before_state=before,
            after_state=_scenario_audit_state(scenario),
            explanation=f"Scenario '{scenario.name}' archived: {reason}",
        )
        return scenario

    # -- scoring-configuration resolution --------------------------------

    def _resolve_scoring_config(
        self, strategy: ComparisonStrategy, scoring_configuration_id: uuid.UUID | None
    ) -> ScoringConfiguration | None:
        if strategy not in _STRATEGIES_NEEDING_CONFIG:
            return None  # ignored even if provided — module docstring
        if scoring_configuration_id is None:
            raise ValidationAppError(
                f"scoring_configuration_id is required for strategy '{strategy.value}'."
            )
        return self._scoring_configs.get(scoring_configuration_id)

    def _weights_for_strategy(
        self, strategy: ComparisonStrategy, scoring_config: ScoringConfiguration | None
    ) -> tuple[CriterionSpec, ...]:
        if strategy is ComparisonStrategy.LOWEST_UNIT_PRICE:
            return LOWEST_UNIT_PRICE_WEIGHTS
        if strategy is ComparisonStrategy.LOWEST_LANDED_COST:
            return LOWEST_LANDED_COST_WEIGHTS
        if strategy is ComparisonStrategy.FASTEST_DELIVERY:
            return FASTEST_DELIVERY_WEIGHTS
        if strategy is ComparisonStrategy.LOWEST_RISK:
            return LOWEST_RISK_WEIGHTS
        assert scoring_config is not None  # enforced by _resolve_scoring_config
        return tuple(_criterion_spec_from_config_json(w) for w in scoring_config.weights)

    # -- offer-context gathering (create_and_run path) --------------------

    def _gather_offer_contexts(
        self, rfq: Rfq, assumptions: LandedCostAssumptions, *, actor_id: uuid.UUID
    ) -> tuple[list[_OfferContext], list[EligibilityExclusion], list[dict[str, Any]]]:
        rfq_lines = {line.id: line for line in self._rfq_repo.list_lines(rfq.id)}
        excluded_supplier_ids = {
            rs.supplier_id
            for rs in self._rfq_repo.list_suppliers(rfq.id)
            if rs.excluded_at is not None
        }
        existing_by_line = {
            row.quote_line_id: (row, comps)
            for row, comps in self._landed_cost.latest_for_rfq(rfq.id)
        }

        offer_contexts: list[_OfferContext] = []
        pre_excluded: list[EligibilityExclusion] = []
        refs: list[dict[str, Any]] = []

        for quote in self._quote_repo.list_by_rfq(rfq.id):
            if quote.status == QuoteStatus.REJECTED:
                continue
            supplier = self._supplier_repo.get_or_raise(quote.supplier_id)
            for line in self._quote_repo.list_lines(quote.id):
                if line.matched_rfq_line_id is None:
                    continue
                rfq_line = rfq_lines.get(line.matched_rfq_line_id)
                if rfq_line is None:  # pragma: no cover - FK integrity
                    continue
                if quote.supplier_id in excluded_supplier_ids:
                    pre_excluded.append(
                        EligibilityExclusion(
                            quote_line_id=line.id,
                            supplier_label=supplier.name,
                            reason=f"{supplier.name} is excluded from this RFQ",
                        )
                    )
                    continue

                cached = existing_by_line.get(line.id)
                if cached is None:
                    row, comps = self._landed_cost.calculate_and_store(
                        line.id, assumptions, actor_id=actor_id, rfq_id=rfq.id
                    )
                else:
                    row, comps = cached
                price_breaks = self._quote_repo.list_price_breaks(line.id)
                offer_contexts.append(
                    _OfferContext(line, quote, rfq_line, supplier, row, comps, price_breaks)
                )
                refs.append(
                    {
                        "quote_line_id": str(line.id),
                        "quote_id": str(quote.id),
                        "supplier_id": str(quote.supplier_id),
                        "rfq_line_id": str(rfq_line.id),
                        "landed_cost_result_id": str(row.id),
                    }
                )

        if not offer_contexts:
            raise ConflictStateError(
                "This RFQ has no eligible quote lines with a computable landed cost; "
                "a scenario requires at least one."
            )
        return offer_contexts, pre_excluded, refs

    def _load_offer_contexts_from_refs(
        self, refs: list[Any]
    ) -> list[_OfferContext]:
        """rerun path — reconstructs offers from a scenario's OWN
        `quote_snapshot_refs`, never re-resolving "latest" (module docstring
        Deviation 2, reproducibility)."""
        contexts: list[_OfferContext] = []
        for ref in refs:
            line = self._quote_line_repo.get_or_raise(uuid.UUID(ref["quote_line_id"]))
            quote = self._quote_repo.get_or_raise(uuid.UUID(ref["quote_id"]))
            if line.matched_rfq_line_id is None:
                raise ConflictStateError(
                    f"Quote line {line.id} is no longer matched to an RFQ line; "
                    "this scenario cannot be rerun."
                )
            rfq_line = self._rfq_repo.get_line(quote.rfq_id, line.matched_rfq_line_id)
            if rfq_line is None:  # pragma: no cover - FK integrity
                raise NotFoundError("RfqLine")
            supplier = self._supplier_repo.get_or_raise(quote.supplier_id)
            row, comps = self._landed_cost.get(uuid.UUID(ref["landed_cost_result_id"]))
            price_breaks = self._quote_repo.list_price_breaks(line.id)
            contexts.append(
                _OfferContext(line, quote, rfq_line, supplier, row, comps, price_breaks)
            )
        return contexts

    # -- offer / demand-line construction ---------------------------------

    @staticmethod
    def _base_material_price(ctx: _OfferContext) -> Decimal | None:
        for comp in ctx.landed_components:
            if comp.component == CostComponent.EXTENDED_MATERIAL:
                raw = comp.inputs.get("normalized_unit_price")
                return Decimal(raw) if raw is not None else None
        return None

    def _build_offer(self, ctx: _OfferContext) -> Offer:
        base_price = self._base_material_price(ctx)
        with localcontext(CALC_CONTEXT):
            overhead = (
                ctx.landed_row.effective_unit_cost - base_price
                if base_price is not None
                else Decimal("0")
            )
            if ctx.price_breaks:
                tiers = tuple(
                    OfferTier(
                        min_quantity=_to_int_qty(pb.min_quantity),
                        max_quantity=(
                            _to_int_qty(pb.max_quantity) if pb.max_quantity is not None else None
                        ),
                        landed_unit_cost=pb.unit_price + overhead,
                    )
                    for pb in sorted(ctx.price_breaks, key=lambda pb: pb.min_quantity)
                )
            else:
                tiers = (
                    OfferTier(
                        min_quantity=1,
                        max_quantity=None,
                        landed_unit_cost=ctx.landed_row.effective_unit_cost,
                    ),
                )

        # module docstring Deviation 4
        incomplete = ctx.landed_row.completeness == Completeness.INCOMPLETE
        return Offer(
            quote_line_id=ctx.quote_line.id,
            rfq_line_id=ctx.rfq_line.id,
            supplier_id=ctx.supplier.id,
            supplier_label=ctx.supplier.name,
            tiers=tiers,
            moq=(_to_int_qty(ctx.quote_line.moq) if ctx.quote_line.moq is not None else None),
            capacity=(
                _to_int_qty(ctx.quote_line.production_capacity)
                if ctx.quote_line.production_capacity is not None
                else None
            ),
            fixed_cost=Decimal("0"),  # module docstring: already amortized into effective_unit_cost
            incomplete_landed_cost=incomplete,
        )

    def _build_demand_lines(self, rfq: Rfq) -> tuple[DemandLine, ...]:
        lines: list[DemandLine] = []
        for rl in self._rfq_repo.list_lines(rfq.id):
            part = self._part_repo.get(rl.part_id)
            label = part.name if part is not None else str(rl.id)
            lines.append(
                DemandLine(
                    rfq_line_id=rl.id,
                    part_label=label,
                    required_quantity=_to_int_qty(rl.required_quantity),
                )
            )
        return tuple(lines)

    # -- scoring input construction ---------------------------------------

    def _criterion_values_for_supplier(
        self, ctxs: list[_OfferContext], *, include_user_defined: bool
    ) -> tuple[SupplierCriterionValue, ...]:
        # In CALC_CONTEXT and quantized at the unit-price boundary, exactly as
        # brief_service/report_service compute the same figure — the three
        # paths must agree on the wire (2026-08 calculation audit F4).
        with localcontext(CALC_CONTEXT):
            total_landed = sum((c.landed_row.total_landed_cost for c in ctxs), Decimal("0"))
            total_qty = sum((c.landed_row.accepted_quantity for c in ctxs), Decimal("0"))
            effective_unit_cost = (
                quantize_unit_price(total_landed / total_qty) if total_qty else None
            )

        lead_times = [
            Decimal(c.quote_line.lead_time_days)
            for c in ctxs
            if c.quote_line.lead_time_days is not None
        ]
        lead_time_value = max(lead_times) if lead_times else None

        capacities = [
            c.quote_line.production_capacity
            for c in ctxs
            if c.quote_line.production_capacity is not None
        ]
        capacity_value = min(capacities) if capacities else None

        moqs = [c.quote_line.moq for c in ctxs if c.quote_line.moq is not None]
        # inverted upstream, per the FROZEN criterion's own "inverted upstream" note
        moq_flex_value = (-max(moqs)) if moqs else None

        payment_terms_value: Decimal | None = None
        for c in ctxs:
            terms = self._quote_repo.get_terms(c.quote.id)
            if terms is not None and terms.payment_terms_days is not None:
                payment_terms_value = Decimal(terms.payment_terms_days)
                break

        supplier_id = ctxs[0].supplier.id
        performance = self._performance_repo.list_for_supplier(supplier_id, limit=1)
        latest_perf = performance[0] if performance else None
        quality_value = latest_perf.quality_score if latest_perf is not None else None
        defect_value = latest_perf.defect_rate if latest_perf is not None else None
        otd_value = latest_perf.on_time_delivery_rate if latest_perf is not None else None

        values = [
            SupplierCriterionValue(
                Criterion.TOTAL_LANDED_COST, total_landed, "landed_cost_result"
            ),
            SupplierCriterionValue(
                Criterion.EFFECTIVE_UNIT_COST, effective_unit_cost, "landed_cost_result"
            ),
            SupplierCriterionValue(
                Criterion.LEAD_TIME, lead_time_value, "quote_line.lead_time_days"
            ),
            SupplierCriterionValue(
                Criterion.CAPACITY, capacity_value, "quote_line.production_capacity"
            ),
            SupplierCriterionValue(
                Criterion.MOQ_FLEXIBILITY, moq_flex_value, "quote_line.moq (inverted)"
            ),
            SupplierCriterionValue(
                Criterion.PAYMENT_TERMS, payment_terms_value, "quote_terms.payment_terms_days"
            ),
            SupplierCriterionValue(
                Criterion.QUALITY_HISTORY,
                quality_value,
                "supplier_performance_record.quality_score",
            ),
            SupplierCriterionValue(
                Criterion.DEFECT_RATE, defect_value, "supplier_performance_record.defect_rate"
            ),
            SupplierCriterionValue(
                Criterion.ON_TIME_DELIVERY,
                otd_value,
                "supplier_performance_record.on_time_delivery_rate",
            ),
            SupplierCriterionValue(Criterion.SPEC_COMPLIANCE, None, "no_source_column"),
        ]
        if include_user_defined:
            pairs = [
                (price, c.landed_row.accepted_quantity)
                for c in ctxs
                if (price := self._base_material_price(c)) is not None
            ]
            values.append(
                SupplierCriterionValue(
                    Criterion.USER_DEFINED,
                    _weighted_avg(pairs),
                    "landed_cost_result.extended_material.normalized_unit_price",
                )
            )
        return tuple(values)

    def _build_supplier_scoring_inputs(
        self,
        offer_contexts: list[_OfferContext],
        constraints: ScenarioConstraintsInput,
        *,
        include_user_defined: bool,
    ) -> tuple[SupplierScoringInput, ...]:
        by_supplier: dict[uuid.UUID, list[_OfferContext]] = defaultdict(list)
        for ctx in offer_contexts:
            by_supplier[ctx.supplier.id].append(ctx)

        inputs: list[SupplierScoringInput] = []
        for supplier_id, ctxs in by_supplier.items():
            supplier = ctxs[0].supplier
            if supplier_id in constraints.excluded_supplier_ids:
                inputs.append(
                    SupplierScoringInput(
                        supplier_id=supplier_id,
                        supplier_name=supplier.name,
                        values=(),
                        excluded=True,
                        exclusion_reason=f"{supplier.name} is excluded from this scenario",
                    )
                )
                continue
            values = self._criterion_values_for_supplier(
                ctxs, include_user_defined=include_user_defined
            )
            inputs.append(
                SupplierScoringInput(
                    supplier_id=supplier_id, supplier_name=supplier.name, values=values
                )
            )
        return tuple(sorted(inputs, key=lambda i: (i.supplier_name, i.supplier_id)))

    # -- FX snapshot --------------------------------------------------------

    @staticmethod
    def _collect_fx_snapshot(offer_contexts: list[_OfferContext]) -> list[dict[str, Any]]:
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        for ctx in offer_contexts:
            fx = ctx.landed_row.inputs_snapshot.get("fx")
            if not fx or fx.get("source") is None:
                continue  # no conversion happened for this line (same currency)
            key = (fx.get("source_currency"), fx.get("target_currency"), fx.get("provider_rate"))
            seen.setdefault(key, fx)
        return list(seen.values())

    # -- allocation problem construction ------------------------------------

    def _build_allocation_problem(
        self,
        rfq: Rfq,
        offer_contexts: list[_OfferContext],
        pre_excluded: list[EligibilityExclusion],
        constraints: ScenarioConstraintsInput,
    ) -> AllocationProblem:
        lines = self._build_demand_lines(rfq)
        offers = tuple(self._build_offer(ctx) for ctx in offer_contexts)
        ac = AllocationConstraints(
            max_supplier_count=constraints.max_supplier_count,
            max_concentration=constraints.max_concentration,
            budget_limit=constraints.budget_limit,
            locked_allocations=tuple(
                LockedAllocation(
                    rfq_line_id=item.rfq_line_id,
                    supplier_id=item.supplier_id,
                    quantity=_to_int_qty(item.quantity),
                )
                for item in constraints.locked_allocations
            ),
            excluded_supplier_ids=constraints.excluded_supplier_ids,
            allow_incomplete_offers=constraints.allow_incomplete_offers,
        )
        return AllocationProblem(
            lines=lines, offers=offers, constraints=ac, pre_excluded=tuple(pre_excluded)
        )

    # -- shared score+solve+persist core ------------------------------------

    def _score_and_solve(
        self,
        *,
        rfq: Rfq,
        name: str,
        strategy: ComparisonStrategy,
        scoring_config: ScoringConfiguration | None,
        assumptions: LandedCostAssumptions,
        constraints: ScenarioConstraintsInput,
        quote_snapshot_refs: list[dict[str, Any]],
        offer_contexts: list[_OfferContext],
        pre_excluded: list[EligibilityExclusion],
        actor_id: uuid.UUID,
        notes: str | None,
        audit_event: str,
    ) -> ScenarioPackage:
        weights = self._weights_for_strategy(strategy, scoring_config)
        include_user_defined = strategy is ComparisonStrategy.LOWEST_UNIT_PRICE
        supplier_inputs = self._build_supplier_scoring_inputs(
            offer_contexts, constraints, include_user_defined=include_user_defined
        )
        scoring_result = ScorerV1().score(supplier_inputs, weights)

        problem = self._build_allocation_problem(rfq, offer_contexts, pre_excluded, constraints)
        allocation: AllocationResult = AllocationSolver().solve(problem)

        now = self._clock.now()
        state = (
            ScenarioState.FAILED
            if allocation.status is AllocationStatus.ERROR
            else ScenarioState.COMPLETE
        )

        scenario = ComparisonScenario(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            rfq_id=rfq.id,
            name=name,
            strategy=strategy,
            scoring_configuration_id=(scoring_config.id if scoring_config is not None else None),
            constraints_snapshot=_constraints_json(constraints),
            assumptions_snapshot=_assumptions_json(assumptions),
            fx_snapshot=self._collect_fx_snapshot(offer_contexts),
            quote_snapshot_refs=quote_snapshot_refs,
            weights_snapshot=[_criterion_spec_json(w) for w in scoring_result.weights_used],
            calculation_version=CALCULATION_VERSION,
            solver_version=OPTIMIZATION_VERSION,
            state=state,
            created_by_id=actor_id,
            completed_at=now,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        self._scenario_repo.add(scenario)
        self._db.flush()

        result_row = ScenarioResult(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            scenario_id=scenario.id,
            scoring_output=_scoring_output_json(scoring_result),
            calculation_version=CALCULATION_VERSION,
            scoring_version=scoring_result.scoring_version,
            computed_at=now,
        )
        self._result_repo.add(result_row)

        alloc_row = AllocationResultRecord(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            scenario_id=scenario.id,
            status=allocation.status,
            allocations=[_allocation_entry_json(a) for a in allocation.allocations],
            expected_total_cost=allocation.expected_total_cost,
            supplier_count=allocation.supplier_count,
            binding_constraints=[
                _binding_constraint_json(b) for b in allocation.binding_constraints
            ],
            infeasibility=_infeasibility_json(allocation.infeasibility),
            rejected_alternatives=list(allocation.rejected_alternatives),
            stats=_solver_stats_json(allocation.stats),
            model_hash=(allocation.stats.model_hash if allocation.stats is not None else None),
            optimization_version=allocation.optimization_version,
            error_message=allocation.error_message,
            solved_at=now,
        )
        self._allocation_repo.add(alloc_row)
        self._db.flush()

        if audit_event == "scenario.rerun":
            self._audit.record(
                "scenario.rerun",
                entity_type="comparison_scenario",
                entity_id=scenario.id,
                after_state=_scenario_audit_state(scenario),
                explanation=(notes or f"Scenario {scenario.id} created as a rerun"),
            )
        else:
            self._audit.record(
                "scenario.created",
                entity_type="comparison_scenario",
                entity_id=scenario.id,
                after_state=_scenario_audit_state(scenario),
                explanation=(
                    f"Scenario '{scenario.name}' created for RFQ {rfq.id} ({strategy.value})"
                ),
            )
            self._audit.record(
                "scenario.solved",
                entity_type="comparison_scenario",
                entity_id=scenario.id,
                after_state={
                    "allocation_status": allocation.status.value,
                    "expected_total_cost": (
                        to_wire(allocation.expected_total_cost)
                        if allocation.expected_total_cost is not None
                        else None
                    ),
                    "supplier_count": allocation.supplier_count,
                },
                explanation=(
                    f"Scenario {scenario.id} solved: allocation status "
                    f"'{allocation.status.value}'"
                ),
            )

        return ScenarioPackage(scenario=scenario, result=result_row, allocation=alloc_row, rfq=rfq)

    # -- public entry points -----------------------------------------------

    def create_and_run(
        self,
        rfq_id: uuid.UUID,
        *,
        name: str,
        strategy: ComparisonStrategy,
        scoring_configuration_id: uuid.UUID | None,
        assumptions: LandedCostAssumptions,
        constraints: ScenarioConstraintsInput,
        notes: str | None,
        actor_id: uuid.UUID,
    ) -> ScenarioPackage:
        rfq = self._rfq_repo.get_or_raise(rfq_id)
        scoring_config = self._resolve_scoring_config(strategy, scoring_configuration_id)
        offer_contexts, pre_excluded, refs = self._gather_offer_contexts(
            rfq, assumptions, actor_id=actor_id
        )
        return self._score_and_solve(
            rfq=rfq,
            name=name,
            strategy=strategy,
            scoring_config=scoring_config,
            assumptions=assumptions,
            constraints=constraints,
            quote_snapshot_refs=refs,
            offer_contexts=offer_contexts,
            pre_excluded=pre_excluded,
            actor_id=actor_id,
            notes=notes,
            audit_event="scenario.created",
        )

    def rerun(
        self, scenario_id: uuid.UUID, *, notes: str | None, actor_id: uuid.UUID
    ) -> ScenarioPackage:
        original = self._scenario_repo.get_or_raise(scenario_id)
        rfq = self._rfq_repo.get_or_raise(original.rfq_id)
        scoring_config = (
            self._scoring_configs.get(original.scoring_configuration_id)
            if original.scoring_configuration_id is not None
            else None
        )
        assumptions = _assumptions_from_snapshot(original.assumptions_snapshot)
        constraints = _constraints_from_snapshot(original.constraints_snapshot)
        offer_contexts = self._load_offer_contexts_from_refs(original.quote_snapshot_refs)

        link_note = f"Rerun of scenario {original.id}"
        combined_notes = f"{link_note}: {notes}" if notes else link_note

        return self._score_and_solve(
            rfq=rfq,
            name=original.name,
            strategy=original.strategy,
            scoring_config=scoring_config,
            assumptions=assumptions,
            constraints=constraints,
            quote_snapshot_refs=list(original.quote_snapshot_refs),
            offer_contexts=offer_contexts,
            pre_excluded=[],
            actor_id=actor_id,
            notes=combined_notes,
            audit_event="scenario.rerun",
        )


__all__ = [
    "FASTEST_DELIVERY_WEIGHTS",
    "LOWEST_LANDED_COST_WEIGHTS",
    "LOWEST_RISK_WEIGHTS",
    "LOWEST_UNIT_PRICE_WEIGHTS",
    "LockedAllocationInput",
    "ScenarioConstraintsInput",
    "ScenarioPackage",
    "ScenarioService",
]
