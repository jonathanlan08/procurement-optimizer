"""RFQ routes (docs/planning/03-api-contract.md §4.8). Sync `def` by policy,
mirroring api/v1/boms.py / api/v1/parts.py.

Concurrency (§1.7): RFQs are explicitly listed among the resources carrying
`ETag`/`If-Match` — `GET`/`POST`/`PATCH`/`POST .../status` all set
`ETag: "<version>"` on the response; only `PATCH /rfqs/{id}` requires
`If-Match` on the request, exactly the same split already established by
`api/v1/parts.py`/`api/v1/suppliers.py` (archive/unarchive there bump
`version` and return a new `ETag` without demanding `If-Match` as input;
`POST /rfqs/{id}/status` and the line/supplier sub-resource routes below
follow that same precedent — they mutate state and return a fresh `ETag`,
but only `PATCH` itself gates on the header matching).

Roles follow the contract's §4.8 route table exactly: list/get/status-
history/line-list/supplier-list are `O A N V`; create/update/status-change/
line & supplier mutations are `O A N` (no route here is admin-only — unlike
BOMs/parts, the contract does not carve out a separate admin-gated archive
endpoint for RFQs; reaching `archived` status happens through the same
`POST /rfqs/{id}/status` analyst-level route as any other transition).

Deviations from the contract's literal route table (§4.8), contract wins per
instructions but both are recorded here:

1. **`POST /rfqs/{id}/suppliers/{sid}/reinstate`** has no entry in §4.8's
   table. This task's brief explicitly asks for an un-exclude operation
   ("invitations: invite (only draft/open), exclude (requires reason),
   un-exclude") the same way `api/v1/boms.py`'s `POST /boms/{id}/activate`
   fills a brief-mandated, contract-silent gap. Gated `O A N` (analyst+),
   mirroring the gate the contract itself puts on `DELETE .../suppliers/
   {sid}` (exclude) — not lifted to administrator+ the way BOM/Part archive
   is, because the contract's own exclude gate is already analyst-level.
2. **`{sid}` in the suppliers sub-resource routes is the `RfqSupplier` join
   row's own id**, not the invited `Supplier`'s id — see app/schemas/rfqs.py
   module docstring for why (mirrors `/parts/{id}/alternatives/{aid}` and
   `/suppliers/{id}/contacts/{cid}`).
3. **`DELETE /rfqs/{id}/suppliers/{sid}` takes a JSON body** (`{exclusion_
   reason}`, required) despite DELETE conventionally being bodyless
   elsewhere in this codebase (`DELETE /parts/{id}/alternatives/{aid}` takes
   none) — the contract's own route table explicitly names a body for this
   one DELETE (`{exclusion_reason} -> status excluded, retained for audit`),
   so it is honored here rather than smoothed over to match sibling routes.
4. **`DELETE /rfqs/{id}/lines/{lid}` takes an optional JSON body**
   (`{override_reason}`) for the same reason `PATCH /rfqs/{id}` does — see
   app/schemas/rfqs.py module docstring on the draft-only-with-override gate.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.core.errors import ValidationAppError
from app.models.identity import Role
from app.models.rfqs import Rfq, RfqLine, RfqStatus
from app.schemas.rfqs import (
    RfqCreate,
    RfqLineBulkCreate,
    RfqLineDeleteRequest,
    RfqLineListResponse,
    RfqLineResponse,
    RfqLineUpdate,
    RfqListResponse,
    RfqPageInfo,
    RfqResponse,
    RfqStatusChangeRequest,
    RfqStatusHistoryListResponse,
    RfqStatusHistoryResponse,
    RfqSummaryResponse,
    RfqSupplierExcludeRequest,
    RfqSupplierInviteRequest,
    RfqSupplierListResponse,
    RfqSupplierResponse,
    RfqUpdate,
)
from app.services.rfq_service import RfqService
from app.services.user_lookup import resolve_user_names

router = APIRouter(prefix="/rfqs", tags=["rfqs"])


def get_rfq_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> RfqService:
    return RfqService(db, principal.organization_id, audit, clock, ids)


RfqServiceDep = Annotated[RfqService, Depends(get_rfq_service)]


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


def _rfq_response(
    service: RfqService, rfq: Rfq, lines: list[RfqLine], response: Response
) -> RfqResponse:
    invited = service.invited_supplier_count(rfq.id)
    _set_etag(response, rfq.version)
    return RfqResponse.from_model(rfq, lines, invited_supplier_count=invited)


# -- RFQ CRUD ------------------------------------------------------------


@router.get("")
def list_rfqs(
    service: RfqServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    # B008 is suppressed below — ruff's immutable-type allowlist for the
    # "function call in a default" check doesn't recognize `list[...]`/
    # `date` as immutable the way it does `str`/`bool`/`int`; `Query()`
    # itself is a side-effect-free, idempotent call either way (same
    # reasoning already applied to every other `= Query(...)` default in
    # this codebase).
    status: list[RfqStatus] | None = Query(default=None),  # noqa: B008
    q: str | None = Query(default=None, max_length=200),
    due_before: date | None = Query(default=None),  # noqa: B008
    due_after: date | None = Query(default=None),  # noqa: B008
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RfqListResponse:
    items, total = service.list(
        statuses=status,
        q=q,
        due_before=due_before,
        due_after=due_after,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return RfqListResponse(
        items=[
            RfqSummaryResponse.from_model(rfq, line_count=line_count)
            for rfq, line_count in items
        ],
        page=RfqPageInfo(limit=limit, offset=offset, total=total),
    )


@router.post("", status_code=201)
def create_rfq(
    body: RfqCreate,
    service: RfqServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
) -> RfqResponse:
    rfq, lines = service.create(body, actor_id=principal.user.id)
    return _rfq_response(service, rfq, lines, response)


@router.get("/{rfq_id}")
def get_rfq(
    rfq_id: UUID,
    service: RfqServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    response: Response,
) -> RfqResponse:
    rfq, lines = service.get(rfq_id)
    return _rfq_response(service, rfq, lines, response)


@router.patch("/{rfq_id}")
def update_rfq(
    rfq_id: UUID,
    body: RfqUpdate,
    service: RfqServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> RfqResponse:
    version = _parse_if_match(if_match)
    rfq, lines = service.update(
        rfq_id, body, if_match_version=version, actor_role=principal.role
    )
    return _rfq_response(service, rfq, lines, response)


# -- status workflow -------------------------------------------------------


@router.post("/{rfq_id}/status")
def change_rfq_status(
    rfq_id: UUID,
    body: RfqStatusChangeRequest,
    service: RfqServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
) -> RfqResponse:
    rfq, lines = service.change_status(rfq_id, body, actor_id=principal.user.id)
    return _rfq_response(service, rfq, lines, response)


@router.get("/{rfq_id}/status-history")
def get_rfq_status_history(
    rfq_id: UUID,
    service: RfqServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    db: DbDep,
) -> RfqStatusHistoryListResponse:
    history = service.status_history(rfq_id)
    names = resolve_user_names(
        db, principal.organization_id, (h.actor_user_id for h in history)
    )
    return RfqStatusHistoryListResponse(
        items=[
            RfqStatusHistoryResponse.from_model(h, actor_full_name=names.get(h.actor_user_id))
            for h in history
        ]
    )


# -- lines ---------------------------------------------------------------


@router.get("/{rfq_id}/lines")
def list_rfq_lines(
    rfq_id: UUID,
    service: RfqServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> RfqLineListResponse:
    lines = service.list_lines(rfq_id)
    return RfqLineListResponse(items=[RfqLineResponse.from_model(ln) for ln in lines])


@router.post("/{rfq_id}/lines", status_code=201)
def add_rfq_lines(
    rfq_id: UUID,
    body: RfqLineBulkCreate,
    service: RfqServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> RfqLineListResponse:
    lines = service.add_lines(rfq_id, body, actor_role=principal.role)
    return RfqLineListResponse(items=[RfqLineResponse.from_model(ln) for ln in lines])


@router.patch("/{rfq_id}/lines/{line_id}")
def update_rfq_line(
    rfq_id: UUID,
    line_id: UUID,
    body: RfqLineUpdate,
    service: RfqServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> RfqLineResponse:
    line = service.update_line(rfq_id, line_id, body, actor_role=principal.role)
    return RfqLineResponse.from_model(line)


@router.delete("/{rfq_id}/lines/{line_id}", status_code=204)
def remove_rfq_line(
    rfq_id: UUID,
    line_id: UUID,
    service: RfqServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    body: RfqLineDeleteRequest | None = None,
) -> None:
    override_reason = body.override_reason if body is not None else None
    service.remove_line(
        rfq_id, line_id, actor_role=principal.role, override_reason=override_reason
    )


# -- suppliers -----------------------------------------------------------


@router.get("/{rfq_id}/suppliers")
def list_rfq_suppliers(
    rfq_id: UUID,
    service: RfqServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> RfqSupplierListResponse:
    suppliers = service.list_suppliers(rfq_id)
    return RfqSupplierListResponse(
        items=[RfqSupplierResponse.from_model(s) for s in suppliers]
    )


@router.post("/{rfq_id}/suppliers", status_code=201)
def invite_rfq_suppliers(
    rfq_id: UUID,
    body: RfqSupplierInviteRequest,
    service: RfqServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> RfqSupplierListResponse:
    suppliers = service.invite_suppliers(rfq_id, body)
    return RfqSupplierListResponse(
        items=[RfqSupplierResponse.from_model(s) for s in suppliers]
    )


@router.delete("/{rfq_id}/suppliers/{rfq_supplier_id}")
def exclude_rfq_supplier(
    rfq_id: UUID,
    rfq_supplier_id: UUID,
    body: RfqSupplierExcludeRequest,
    service: RfqServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> RfqSupplierResponse:
    rs = service.exclude_supplier(rfq_id, rfq_supplier_id, reason=body.exclusion_reason)
    return RfqSupplierResponse.from_model(rs)


@router.post("/{rfq_id}/suppliers/{rfq_supplier_id}/reinstate")
def reinstate_rfq_supplier(
    rfq_id: UUID,
    rfq_supplier_id: UUID,
    service: RfqServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> RfqSupplierResponse:
    rs = service.reinstate_supplier(rfq_id, rfq_supplier_id)
    return RfqSupplierResponse.from_model(rs)


__all__ = ["router"]
