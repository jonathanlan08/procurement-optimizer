"""Negotiation-brief routes (docs/planning/03-api-contract.md §4.17).
Sync `def` by policy, mirroring every other router in this codebase.

**Contract's literal paths kept for generate/get/review; `POST
.../negotiation-briefs` runs synchronously and returns `201` with the full
`NegotiationBriefResponse`, not `202` + a job envelope** — see
`app/services/brief_service.py` module docstring "Deviation 1" (same
`JOB_RUNNER=inline` precedent `api/v1/extractions.py`/`api/v1/matching.py`/
`api/v1/scenarios.py` already establish).

**No `PATCH /negotiation-briefs/{id}` and no `GET .../email-draft` route are
mounted here** — see `brief_service.py` module docstring "Deviation 2"/
"Deviation 3" for why: this task's own OBJECTIVE scopes the route set to
generate/get/review/archive/list and explicitly requires "NO send/email
endpoint (assert in tests that no such route exists)"; the draft email text
is already part of the full `GET /negotiation-briefs/{id}` response
(`draft_email_subject`/`draft_email_body`, plus the `draft_supplier_email`
entry under `sections`). There is no route anywhere in this module — or
this codebase — that sends an email; `never auto-send` (SPEC §Negotiation
brief) holds structurally, not just by convention.

**`POST .../{id}/archive` is this task's own explicit addition**, not in
§4.17's literal table at all — added per 02-erd.md §1/§11's global
"archived_at + archived_by_id + archive_reason ... never hard-delete ...
briefs" convention, gated administrator+ exactly like every other
archivable resource's archive route (suppliers/parts/BOMs/quotes/
comparison_scenarios) in this codebase.

**Three routers, one module** — the same "one module, several prefix-scoped
routers" shape `api/v1/scenarios.py`/`api/v1/documents.py` already
establish: `scenario_briefs_router` (prefix `/comparison-scenarios`) owns
generate + list-by-scenario; `rfq_briefs_router` (prefix `/rfqs`) owns
list-by-rfq; `briefs_router` (prefix `/negotiation-briefs`) owns every route
that addresses a brief directly. All three exported and mounted in
`app/main.py`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import (
    AuditDep,
    ClockDep,
    DbDep,
    IdsDep,
    Principal,
    PrincipalDep,
    SettingsDep,
    require_role,
)
from app.models.identity import Role
from app.schemas.briefs import (
    BriefArchiveRequest,
    BriefGenerateRequest,
    BriefReviewRequest,
    NegotiationBriefListResponse,
    NegotiationBriefResponse,
    NegotiationBriefSummaryResponse,
)
from app.services.brief_service import BriefService, build_narrative_provider

scenario_briefs_router = APIRouter(prefix="/comparison-scenarios", tags=["negotiation-briefs"])
rfq_briefs_router = APIRouter(prefix="/rfqs", tags=["negotiation-briefs"])
briefs_router = APIRouter(prefix="/negotiation-briefs", tags=["negotiation-briefs"])


def get_brief_service(
    db: DbDep,
    principal: PrincipalDep,
    audit: AuditDep,
    clock: ClockDep,
    ids: IdsDep,
    settings: SettingsDep,
) -> BriefService:
    provider = build_narrative_provider(settings)
    return BriefService(db, principal.organization_id, audit, clock, ids, provider=provider)


BriefServiceDep = Annotated[BriefService, Depends(get_brief_service)]


# -- under the scenario ------------------------------------------------


@scenario_briefs_router.post("/{scenario_id}/negotiation-briefs", status_code=201)
def generate_brief(
    scenario_id: UUID,
    body: BriefGenerateRequest,
    service: BriefServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> NegotiationBriefResponse:
    targets = body.targets
    brief = service.generate(
        scenario_id,
        body.supplier_id,
        actor_id=principal.user.id,
        objective_override=body.objective,
        price_target_override=(targets.price_target if targets is not None else None),
        stretch_target_override=(targets.stretch_target if targets is not None else None),
        walk_away_threshold_override=(
            targets.walk_away_threshold if targets is not None else None
        ),
    )
    return NegotiationBriefResponse.from_model(brief)


@scenario_briefs_router.get("/{scenario_id}/negotiation-briefs")
def list_briefs_for_scenario(
    scenario_id: UUID,
    service: BriefServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    limit: int = 50,
    offset: int = 0,
) -> NegotiationBriefListResponse:
    items, total = service.list_by_scenario(scenario_id, limit=limit, offset=offset)
    return NegotiationBriefListResponse(
        items=[NegotiationBriefSummaryResponse.from_model(b) for b in items],
        page={"limit": limit, "offset": offset, "total": total},
    )


# -- under the RFQ -------------------------------------------------------


@rfq_briefs_router.get("/{rfq_id}/negotiation-briefs")
def list_briefs_for_rfq(
    rfq_id: UUID,
    service: BriefServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    limit: int = 50,
    offset: int = 0,
) -> NegotiationBriefListResponse:
    items, total = service.list_by_rfq(rfq_id, limit=limit, offset=offset)
    return NegotiationBriefListResponse(
        items=[NegotiationBriefSummaryResponse.from_model(b) for b in items],
        page={"limit": limit, "offset": offset, "total": total},
    )


# -- addressed directly ---------------------------------------------------


@briefs_router.get("/{brief_id}")
def get_brief(
    brief_id: UUID,
    service: BriefServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> NegotiationBriefResponse:
    return NegotiationBriefResponse.from_model(service.get(brief_id))


@briefs_router.post("/{brief_id}/review")
def review_brief(
    brief_id: UUID,
    body: BriefReviewRequest,
    service: BriefServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> NegotiationBriefResponse:
    brief = service.mark_reviewed(
        brief_id,
        approved=body.approved,
        reviewer_notes=body.reviewer_notes,
        actor_id=principal.user.id,
    )
    return NegotiationBriefResponse.from_model(brief)


@briefs_router.post("/{brief_id}/archive")
def archive_brief(
    brief_id: UUID,
    body: BriefArchiveRequest,
    service: BriefServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
) -> NegotiationBriefResponse:
    brief = service.archive(brief_id, reason=body.reason, actor_id=principal.user.id)
    return NegotiationBriefResponse.from_model(brief)


__all__ = ["briefs_router", "rfq_briefs_router", "scenario_briefs_router"]
