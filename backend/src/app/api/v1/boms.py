"""BOM routes (docs/planning/03-api-contract.md §4.7). Sync `def` by policy,
mirroring api/v1/parts.py / api/v1/suppliers.py.

No `ETag`/`If-Match` here: §1.7 explicitly enumerates which resources carry
optimistic-concurrency headers ("suppliers, parts, RFQs, quotes, scoring
configurations") and BOMs are not among them - consistent with
`BillOfMaterials` carrying no `VersionedMixin` (see app/models/boms.py
module docstring: the copy-on-write chain itself is the concurrency
control for the one mutation that matters, forking the next version).

Deviations from the contract's literal route table (§4.7), contract wins
per instructions but both are recorded here:

1. **`POST /boms/{id}/activate`** has no entry in §4.7's table at all. The
   contract lists `PATCH /boms/{id}` ("metadata only while draft") but never
   specifies how a BOM actually moves `draft -> active`, nor when a
   predecessor becomes `superseded` - see `services/bom_service.py`'s
   module docstring for the full status-transition design this fills that
   gap with. The spec explicitly asks for this route ("if
   genuinely undefined: draft on create, activate endpoint promotes
   draft->active"), the same kind of contract-silent-but-reasonable
   addition already made for `POST /parts/{id}/unarchive` in
   `api/v1/parts.py`.
2. **No `PATCH /boms/{id}`.** The spec lists the six
   routes this module implements (GET/POST /boms, GET /boms/{id}, POST
   .../versions, POST .../activate, POST .../archive, GET .../versions) and
   does not include metadata-only `PATCH`. Lines are already fully
   immutable in place (no line-edit route exists anywhere - the model has
   no columns to update a line with, per app/models/boms.py); a metadata-
   only `PATCH` would be a legitimate follow-up but is out of scope for
   the allowlisted route set.
3. **`GET /boms/{id}/versions`, not `GET /boms/{root_id}/versions`.** The
   contract names the path parameter `{root_id}`, implying the caller must
   already know the root id. This router accepts *any* version's id in the
   chain and resolves it to its root internally
   (`BomService.version_chain`) - a strict superset of the contract's
   literal behavior: passing the root id still works (a chain's v1 row has
   `id == root_bom_id`), and passing any other version's id additionally
   works, which is more convenient for a client that only has "the BOM I'm
   looking at right now," not necessarily its root.

Roles (§4.7 route table + the stated default "viewer reads, analyst
mutates, admin archives"): list/get/version-chain are `O A N V`; create,
new-version, and activate are `O A N`; archive is `O A`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.models.identity import Role
from app.schemas.boms import (
    BomArchiveRequest,
    BomCreate,
    BomListResponse,
    BomPageInfo,
    BomResponse,
    BomSummaryResponse,
    BomVersionChainResponse,
    BomVersionCreate,
)
from app.services.bom_service import BomService

router = APIRouter(prefix="/boms", tags=["boms"])


def get_bom_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> BomService:
    return BomService(db, principal.organization_id, audit, clock, ids)


BomServiceDep = Annotated[BomService, Depends(get_bom_service)]


@router.get("")
def list_boms(
    service: BomServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    q: str | None = Query(default=None, max_length=200),
    all_versions: bool = Query(default=False),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> BomListResponse:
    items, total = service.list(
        q=q,
        all_versions=all_versions,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return BomListResponse(
        items=[BomSummaryResponse.from_model(b) for b in items],
        page=BomPageInfo(limit=limit, offset=offset, total=total),
    )


@router.post("", status_code=201)
def create_bom(
    body: BomCreate,
    service: BomServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> BomResponse:
    bom, lines = service.create(body, actor_id=principal.user.id)
    return BomResponse.from_model(bom, lines)


@router.get("/{bom_id}")
def get_bom(
    bom_id: UUID,
    service: BomServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> BomResponse:
    bom, lines = service.get(bom_id)
    return BomResponse.from_model(bom, lines)


@router.get("/{bom_id}/versions")
def get_bom_version_chain(
    bom_id: UUID,
    service: BomServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> BomVersionChainResponse:
    chain = service.version_chain(bom_id)
    return BomVersionChainResponse(items=[BomSummaryResponse.from_model(b) for b in chain])


@router.post("/{bom_id}/versions", status_code=201)
def create_bom_version(
    bom_id: UUID,
    body: BomVersionCreate,
    service: BomServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> BomResponse:
    bom, lines = service.new_version(bom_id, body, actor_id=principal.user.id)
    return BomResponse.from_model(bom, lines)


@router.post("/{bom_id}/activate")
def activate_bom(
    bom_id: UUID,
    service: BomServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> BomResponse:
    bom, lines = service.activate(bom_id)
    return BomResponse.from_model(bom, lines)


@router.post("/{bom_id}/archive")
def archive_bom(
    bom_id: UUID,
    body: BomArchiveRequest,
    service: BomServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
) -> BomResponse:
    bom, lines = service.archive(bom_id, reason=body.reason, actor_id=principal.user.id)
    return BomResponse.from_model(bom, lines)


__all__ = ["router"]
