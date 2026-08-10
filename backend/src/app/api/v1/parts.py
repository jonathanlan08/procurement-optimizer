"""Part routes (docs/planning/03-api-contract.md §4.5). Sync `def` by policy,
mirroring api/v1/suppliers.py.

Concurrency (§1.7): parts are explicitly listed among the resources carrying
`ETag`/`If-Match` (suppliers, parts, RFQs, quotes, scoring configurations) —
`GET` sets `ETag: "<version>"`; `PATCH` requires `If-Match` and returns `409
conflict_version` on mismatch, exactly like suppliers.

Alternatives (`GET/POST /parts/{id}/alternatives`, `DELETE
/parts/{id}/alternatives/{aid}`) are folded into this single router rather
than split into a separate module the way `supplier_contacts.py`/
`supplier_performance.py` split off from `suppliers.py` — this task's
file-creation allowlist names only one new API module for parts.
`DELETE .../alternatives/{aid}` returns `204` with no body: unlike supplier
contacts ("delete = archive", a soft delete with a resulting state worth
returning), `PartAlternative` has no `archived_at` (see
app.models.parts.PartAlternative docstring — "a decision record ... not a
mutable aggregate with a lifecycle of its own"), so removal is a genuine hard
delete with nothing left to show the caller.

Deviations from this task's prose (contract wins, per instructions):
1. The contract's route table (§4.5) lists only `PATCH /parts/{id}`, not
   `PUT` — same deviation already recorded in `api/v1/suppliers.py`.
2. The contract's route table (§4.5) lists only `POST /parts/{id}/archive` —
   no `unarchive` route, unlike suppliers (§4.4) which lists both
   directions explicitly. Every other archivable resource in the contract
   (BOMs, RFQs, scoring-configurations, comparison-scenarios, quote-
   documents, reports, negotiation-briefs) is equally silent on unarchive,
   so this reads as the contract's general non-exhaustive listing habit
   rather than a deliberate one-way lifecycle carved out for parts
   specifically — `Part` mixes in the identical `ArchivableMixin` as
   `Supplier`, with the same reversible `archived_at`/`archived_by_id`/
   `archive_reason` shape and no DB constraint preventing un-archiving.
   This router still implements `POST /parts/{id}/unarchive`, mirroring
   suppliers 1:1, per this task's explicit instruction. Worth the same
   principal confirmation the contract already asks for on its own listed
   gaps (§6).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.core.errors import ValidationAppError
from app.models.identity import Role
from app.schemas.parts import (
    PageInfo,
    PartAlternativeCreate,
    PartAlternativeListResponse,
    PartAlternativeResponse,
    PartArchiveRequest,
    PartCreate,
    PartListResponse,
    PartResponse,
    PartUpdate,
)
from app.services.part_service import PartService

router = APIRouter(prefix="/parts", tags=["parts"])


def get_part_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> PartService:
    return PartService(db, principal.organization_id, audit, clock, ids)


PartServiceDep = Annotated[PartService, Depends(get_part_service)]


def _parse_if_match(if_match: str) -> int:
    raw = if_match.strip().strip('"')
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationAppError(
            "If-Match must be the resource's current integer version, e.g. \"3\"."
        ) from exc


def _set_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = f'"{version}"'


@router.get("")
def list_parts(
    service: PartServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=100),
    is_active: bool | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PartListResponse:
    items, total = service.list(
        q=q,
        category=category,
        is_active=is_active,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return PartListResponse(
        items=[PartResponse.from_model(p) for p in items],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.post("", status_code=201)
def create_part(
    body: PartCreate,
    service: PartServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
) -> PartResponse:
    part = service.create(body)
    _set_etag(response, part.version)
    return PartResponse.from_model(part)


@router.get("/{part_id}")
def get_part(
    part_id: UUID,
    service: PartServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    response: Response,
) -> PartResponse:
    part = service.get(part_id)
    _set_etag(response, part.version)
    return PartResponse.from_model(part)


@router.patch("/{part_id}")
def update_part(
    part_id: UUID,
    body: PartUpdate,
    service: PartServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> PartResponse:
    version = _parse_if_match(if_match)
    part = service.update(part_id, body, if_match_version=version)
    _set_etag(response, part.version)
    return PartResponse.from_model(part)


@router.post("/{part_id}/archive")
def archive_part(
    part_id: UUID,
    body: PartArchiveRequest,
    service: PartServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
    response: Response,
) -> PartResponse:
    part = service.archive(part_id, reason=body.reason, actor_id=principal.user.id)
    _set_etag(response, part.version)
    return PartResponse.from_model(part)


@router.post("/{part_id}/unarchive")
def unarchive_part(
    part_id: UUID,
    body: PartArchiveRequest,
    service: PartServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
    response: Response,
) -> PartResponse:
    part = service.unarchive(part_id, reason=body.reason)
    _set_etag(response, part.version)
    return PartResponse.from_model(part)


@router.get("/{part_id}/alternatives")
def list_part_alternatives(
    part_id: UUID,
    service: PartServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PartAlternativeListResponse:
    items, total = service.list_alternatives(part_id, limit=limit, offset=offset)
    return PartAlternativeListResponse(
        items=[PartAlternativeResponse.from_model(a) for a in items],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.post("/{part_id}/alternatives", status_code=201)
def add_part_alternative(
    part_id: UUID,
    body: PartAlternativeCreate,
    service: PartServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> PartAlternativeResponse:
    alt = service.add_alternative(part_id, body, actor_id=principal.user.id)
    return PartAlternativeResponse.from_model(alt)


@router.delete("/{part_id}/alternatives/{alternative_id}", status_code=204)
def remove_part_alternative(
    part_id: UUID,
    alternative_id: UUID,
    service: PartServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> None:
    service.remove_alternative(part_id, alternative_id)


__all__ = ["router"]
