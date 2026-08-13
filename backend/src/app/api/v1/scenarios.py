"""Comparison-scenario routes (docs/planning/03-api-contract.md §4.16). Sync
`def` by policy, mirroring every other router in this codebase.

**Contract's literal paths kept, but `POST .../comparison-scenarios` and
`POST .../{id}/clone` run synchronously and do MORE than their contract-table
one-line description says** - see `app/services/scenario_service.py`'s own
module docstring ("Deviation 1"/"Deviation 2") for the full reasoning: this
codebase has no job queue anywhere (`services/extraction_service.py`'s
`JOB_RUNNER=inline` precedent, already applied the same way by
`api/v1/extractions.py`/`api/v1/matching.py`), and the FROZEN
`ComparisonScenario.state` machine (`draft -> running -> complete|failed`,
no "scored but not solved" state) makes scoring and allocation one
transaction, not two. Route-by-route:

- `POST /rfqs/{id}/comparison-scenarios` runs `ScenarioService.
  create_and_run` (scoring AND allocation) and returns `201` with the full
  `ScenarioResponse`, not `202` + a job envelope.
- `POST /comparison-scenarios/{id}/optimize` is **idempotent**, not a
  "solve now" trigger: since allocation already happened at creation and
  `AllocationResultRecord` is immutable (module docstring "Immutability"),
  this route simply returns the allocation half of the already-solved
  scenario. Mounted for contract-path compatibility; it never re-solves.
- `POST /comparison-scenarios/{id}/clone` is this task's own `rerun`
  (scenario_service.py "Deviation 2"): a NEW scenario, freshly solved from
  the original's stored snapshots - not a draft pre-fill.
- `GET .../results` / `GET .../allocation` are the scoring/allocation
  halves of `GET .../{id}`'s full package, addressable individually per
  §4.16's own route table.

**Two routers, one module** - same shape `api/v1/extractions.py`/
`api/v1/matching.py` already establish: `rfq_scenarios_router` (prefix
`/rfqs`) owns list/create under the parent RFQ; `scenarios_router` (prefix
`/comparison-scenarios`) owns every route that addresses a scenario
directly. Both exported and mounted in `app/main.py`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.core.errors import ErrorDetail, ValidationAppError
from app.models.identity import Role
from app.models.scenarios import ComparisonStrategy
from app.schemas.scenarios import (
    AllocationResultResponse,
    ScenarioArchiveRequest,
    ScenarioCreateRequest,
    ScenarioListResponse,
    ScenarioRerunRequest,
    ScenarioResponse,
    ScenarioSummaryResponse,
    ScoringResultResponse,
)
from app.services.landed_cost_service import LandedCostAssumptions
from app.services.scenario_service import (
    LockedAllocationInput,
    ScenarioConstraintsInput,
    ScenarioPackage,
    ScenarioService,
)

rfq_scenarios_router = APIRouter(prefix="/rfqs", tags=["comparison-scenarios"])
scenarios_router = APIRouter(prefix="/comparison-scenarios", tags=["comparison-scenarios"])


def get_scenario_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> ScenarioService:
    return ScenarioService(db, principal.organization_id, audit, clock, ids)


ScenarioServiceDep = Annotated[ScenarioService, Depends(get_scenario_service)]


def _parse_strategy(raw: str) -> ComparisonStrategy:
    try:
        return ComparisonStrategy(raw)
    except ValueError as exc:
        raise ValidationAppError(
            f"Unknown strategy '{raw}'.",
            details=[
                ErrorDetail(
                    field="strategy",
                    issue=f"must be one of {[s.value for s in ComparisonStrategy]}",
                )
            ],
        ) from exc


def _to_assumptions(req: ScenarioCreateRequest) -> LandedCostAssumptions:
    a = req.assumptions
    return LandedCostAssumptions(
        quality_risk_rate=(str(a.quality_risk_rate) if a.quality_risk_rate is not None else None),
        delay_risk_per_day=(
            str(a.delay_risk_per_day) if a.delay_risk_per_day is not None else None
        ),
        promised_lead_time_days=(
            str(a.promised_lead_time_days) if a.promised_lead_time_days is not None else None
        ),
        required_lead_time_days=(
            str(a.required_lead_time_days) if a.required_lead_time_days is not None else None
        ),
        annual_rate=(str(a.annual_rate) if a.annual_rate is not None else None),
        baseline_terms_days=(
            str(a.baseline_terms_days) if a.baseline_terms_days is not None else None
        ),
        tariff_rate=(str(a.tariff_rate) if a.tariff_rate is not None else None),
        duty_rate=(str(a.duty_rate) if a.duty_rate is not None else None),
        assume_missing_costs_zero=a.assume_missing_costs_zero,
    )


def _to_constraints(req: ScenarioCreateRequest) -> ScenarioConstraintsInput:
    c = req.constraints
    return ScenarioConstraintsInput(
        max_supplier_count=c.max_supplier_count,
        max_concentration=c.max_concentration,
        budget_limit=c.budget_limit,
        locked_allocations=tuple(
            LockedAllocationInput(
                rfq_line_id=item.rfq_line_id, supplier_id=item.supplier_id, quantity=item.quantity
            )
            for item in c.locked_allocations
        ),
        excluded_supplier_ids=tuple(c.excluded_supplier_ids),
        allow_incomplete_offers=c.allow_incomplete_offers,
    )


def _to_response(pkg: ScenarioPackage) -> ScenarioResponse:
    return ScenarioResponse.from_model(
        pkg.scenario, pkg.result, pkg.allocation, base_currency=pkg.rfq.base_currency
    )


# -- under the RFQ ----------------------------------------------------------


@rfq_scenarios_router.get("/{rfq_id}/comparison-scenarios")
def list_scenarios(
    rfq_id: UUID,
    service: ScenarioServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    limit: int = 50,
    offset: int = 0,
) -> ScenarioListResponse:
    items, total = service.list_for_rfq(rfq_id, limit=limit, offset=offset)
    return ScenarioListResponse(
        items=[ScenarioSummaryResponse.from_model(s) for s in items],
        page={"limit": limit, "offset": offset, "total": total},
    )


@rfq_scenarios_router.post("/{rfq_id}/comparison-scenarios", status_code=201)
def create_scenario(
    rfq_id: UUID,
    body: ScenarioCreateRequest,
    service: ScenarioServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> ScenarioResponse:
    strategy = _parse_strategy(body.strategy)
    pkg = service.create_and_run(
        rfq_id,
        name=body.name,
        strategy=strategy,
        scoring_configuration_id=body.scoring_configuration_id,
        assumptions=_to_assumptions(body),
        constraints=_to_constraints(body),
        notes=body.notes,
        actor_id=principal.user.id,
    )
    return _to_response(pkg)


# -- addressed directly -----------------------------------------------------


@scenarios_router.get("/{scenario_id}")
def get_scenario(
    scenario_id: UUID,
    service: ScenarioServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> ScenarioResponse:
    return _to_response(service.get(scenario_id))


@scenarios_router.get("/{scenario_id}/results")
def get_scenario_results(
    scenario_id: UUID,
    service: ScenarioServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> ScoringResultResponse:
    pkg = service.get(scenario_id)
    return ScoringResultResponse.from_model(pkg.result)


@scenarios_router.get("/{scenario_id}/allocation")
def get_scenario_allocation(
    scenario_id: UUID,
    service: ScenarioServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> AllocationResultResponse:
    pkg = service.get(scenario_id)
    return AllocationResultResponse.from_model(pkg.allocation, base_currency=pkg.rfq.base_currency)


@scenarios_router.post("/{scenario_id}/optimize")
def optimize_scenario(
    scenario_id: UUID,
    service: ScenarioServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> AllocationResultResponse:
    """Idempotent - see module docstring. Allocation already happened at
    scenario creation; this returns it rather than re-solving."""
    pkg = service.get(scenario_id)
    return AllocationResultResponse.from_model(pkg.allocation, base_currency=pkg.rfq.base_currency)


@scenarios_router.post("/{scenario_id}/clone", status_code=201)
def clone_scenario(
    scenario_id: UUID,
    body: ScenarioRerunRequest,
    service: ScenarioServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> ScenarioResponse:
    """`ScenarioService.rerun` - see module docstring "Deviation 2"."""
    pkg = service.rerun(scenario_id, notes=body.notes, actor_id=principal.user.id)
    return _to_response(pkg)


@scenarios_router.post("/{scenario_id}/archive")
def archive_scenario(
    scenario_id: UUID,
    body: ScenarioArchiveRequest,
    service: ScenarioServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
) -> ScenarioSummaryResponse:
    scenario = service.archive(scenario_id, reason=body.reason, actor_id=principal.user.id)
    return ScenarioSummaryResponse.from_model(scenario)


__all__ = ["rfq_scenarios_router", "scenarios_router"]
