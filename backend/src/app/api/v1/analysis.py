"""Landed-cost + scoring-configuration routes (docs/planning/
03-api-contract.md §4.14-4.15). Sync `def` by policy, mirroring every other
route module in this codebase.

**Route shapes, and where this module departs from the contract's literal
table** (contract wins per instructions; every deviation recorded here):

1. `POST /landed-costs:preview` is mounted with **no router prefix** (a bare
   `APIRouter(tags=[...])` route, not nested under `/landed-costs`), because
   `:preview` is not a path parameter - it is a literal suffix on the
   `landed-costs` segment itself (§4.15's own Google-style `resource:verb`
   spelling). Gated `viewer+` (§4.15's own route table: "O A N V" - a
   stateless calculator with no audit row, §6 gap #2), **not** the
   "analyst+" this task's own paraphrase states; the contract's literal role
   column wins.
2. `POST /rfqs/{rfq_id}/landed-costs` persists **one** quote line's result
   per call (`quote_line_id` in the body), not "all matched lines" as the
   contract's prose describes - see `app/services/landed_cost_service.py`
   module docstring: `LandedCostService.calculate_and_store` is this task's
   own explicit per-line service surface, and this route is a thin wrapper
   over it. A future task can add a batch loop over matched lines without
   changing this route's shape.
3. `GET /rfqs/{rfq_id}/landed-costs` (latest result per line, with
   components) has no literal entry in §4.15's table at all - it is this
   task's own explicit bullet ("GET /rfqs/{rfq_id}/landed-costs (viewer+;
   latest per line incl. components)"), additive to, not contradicting, the
   contract's `GET /landed-cost-results/{id}` (also implemented below, for
   the single-result detail view §4.15 itself describes).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.core.errors import ValidationAppError
from app.models.identity import Role
from app.schemas.analysis import (
    LandedCostCalculateRequest,
    LandedCostPreviewRequest,
    LandedCostPreviewResponse,
    LandedCostResultListResponse,
    LandedCostResultResponse,
    ScoringConfigurationArchiveRequest,
    ScoringConfigurationCreate,
    ScoringConfigurationListResponse,
    ScoringConfigurationResponse,
    ScoringConfigurationUpdate,
)
from app.services.landed_cost_service import LandedCostAssumptions, LandedCostService
from app.services.scoring_config_service import ScoringConfigService

landed_cost_router = APIRouter(tags=["landed-costs"])
scoring_config_router = APIRouter(prefix="/scoring-configurations", tags=["scoring-configurations"])


def get_landed_cost_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> LandedCostService:
    return LandedCostService(db, principal.organization_id, audit, clock, ids)


LandedCostServiceDep = Annotated[LandedCostService, Depends(get_landed_cost_service)]


def get_scoring_config_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> ScoringConfigService:
    return ScoringConfigService(db, principal.organization_id, audit, clock, ids)


ScoringConfigServiceDep = Annotated[ScoringConfigService, Depends(get_scoring_config_service)]


def _to_assumptions(
    req: LandedCostPreviewRequest | LandedCostCalculateRequest,
) -> LandedCostAssumptions:
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


# -- landed costs --------------------------------------------------------


@landed_cost_router.post("/landed-costs:preview")
def preview_landed_cost(
    body: LandedCostPreviewRequest,
    service: LandedCostServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> LandedCostPreviewResponse:
    result, snapshot = service.preview(body.quote_line_id, _to_assumptions(body))
    accepted_quantity = Decimal(snapshot["accepted_quantity"])
    return LandedCostPreviewResponse.from_domain(result, accepted_quantity=accepted_quantity)


@landed_cost_router.post("/rfqs/{rfq_id}/landed-costs", status_code=201)
def calculate_and_store_landed_cost(
    rfq_id: UUID,
    body: LandedCostCalculateRequest,
    service: LandedCostServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> LandedCostResultResponse:
    row, components = service.calculate_and_store(
        body.quote_line_id, _to_assumptions(body), actor_id=principal.user.id, rfq_id=rfq_id
    )
    return LandedCostResultResponse.from_model(row, components)


@landed_cost_router.get("/rfqs/{rfq_id}/landed-costs")
def list_rfq_landed_costs(
    rfq_id: UUID,
    service: LandedCostServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> LandedCostResultListResponse:
    results = service.latest_for_rfq(rfq_id)
    return LandedCostResultListResponse(
        items=[LandedCostResultResponse.from_model(row, components) for row, components in results]
    )


@landed_cost_router.get("/landed-cost-results/{result_id}")
def get_landed_cost_result(
    result_id: UUID,
    service: LandedCostServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> LandedCostResultResponse:
    row, components = service.get(result_id)
    return LandedCostResultResponse.from_model(row, components)


# -- scoring configurations ------------------------------------------------


def _parse_if_match(if_match: str) -> int:
    raw = if_match.strip().strip('"')
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationAppError(
            'If-Match must be the resource\'s current integer version, e.g. "3".'
        ) from exc


@scoring_config_router.get("")
def list_scoring_configurations(
    service: ScoringConfigServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    include_archived: bool = False,
) -> ScoringConfigurationListResponse:
    rows = service.list(principal.user.id, include_archived=include_archived)
    return ScoringConfigurationListResponse(
        items=[ScoringConfigurationResponse.from_model(r) for r in rows]
    )


@scoring_config_router.post("", status_code=201)
def create_scoring_configuration(
    body: ScoringConfigurationCreate,
    service: ScoringConfigServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> ScoringConfigurationResponse:
    row, notes = service.create(
        body.name, [w.to_domain_input() for w in body.weights], actor_id=principal.user.id
    )
    return ScoringConfigurationResponse.from_model(row, notes=notes)


@scoring_config_router.patch("/{config_id}")
def update_scoring_configuration(
    config_id: UUID,
    body: ScoringConfigurationUpdate,
    service: ScoringConfigServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ScoringConfigurationResponse:
    version = _parse_if_match(if_match)
    weights = [w.to_domain_input() for w in body.weights] if body.weights is not None else None
    row, notes = service.update(
        config_id, name=body.name, weights=weights, if_match_version=version
    )
    return ScoringConfigurationResponse.from_model(row, notes=notes)


@scoring_config_router.post("/{config_id}/archive")
def archive_scoring_configuration(
    config_id: UUID,
    body: ScoringConfigurationArchiveRequest,
    service: ScoringConfigServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
) -> ScoringConfigurationResponse:
    row = service.archive(config_id, reason=body.reason, actor_id=principal.user.id)
    return ScoringConfigurationResponse.from_model(row)


__all__ = ["landed_cost_router", "scoring_config_router"]
