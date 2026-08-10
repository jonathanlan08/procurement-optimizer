"""Supplier routes (docs/planning/03-api-contract.md §4.4). Sync `def` by policy.

Concurrency (§1.7): `GET` sets `ETag: "<version>"`; `PATCH` requires `If-Match`
and returns `409 conflict_version` on mismatch. Archive/unarchive are
restricted to administrator+ per the contract's route table (`O A`, not
analyst) — analysts may create/read/update but not archive.

Deviation from this task's prose (contract wins, per instructions): the
contract's route table lists `PATCH /suppliers/{id}`, not `PUT`; this router
implements PATCH.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.core.errors import ValidationAppError
from app.models.identity import Role
from app.schemas.suppliers import (
    PageInfo,
    SupplierArchiveRequest,
    SupplierCreate,
    SupplierListResponse,
    SupplierResponse,
    SupplierUpdate,
)
from app.services.supplier_service import SupplierService

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


def get_supplier_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> SupplierService:
    return SupplierService(db, principal.organization_id, audit, clock, ids)


SupplierServiceDep = Annotated[SupplierService, Depends(get_supplier_service)]


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
def list_suppliers(
    service: SupplierServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    q: str | None = Query(default=None, max_length=200),
    country: str | None = Query(default=None, min_length=2, max_length=2),
    is_active: bool | None = Query(default=None),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SupplierListResponse:
    items, total = service.list(
        q=q,
        country=country,
        is_active=is_active,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return SupplierListResponse(
        items=[SupplierResponse.from_model(s) for s in items],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.post("", status_code=201)
def create_supplier(
    body: SupplierCreate,
    service: SupplierServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
) -> SupplierResponse:
    supplier = service.create(body)
    _set_etag(response, supplier.version)
    return SupplierResponse.from_model(supplier)


@router.get("/{supplier_id}")
def get_supplier(
    supplier_id: UUID,
    service: SupplierServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    response: Response,
) -> SupplierResponse:
    supplier = service.get(supplier_id)
    _set_etag(response, supplier.version)
    return SupplierResponse.from_model(supplier)


@router.patch("/{supplier_id}")
def update_supplier(
    supplier_id: UUID,
    body: SupplierUpdate,
    service: SupplierServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> SupplierResponse:
    version = _parse_if_match(if_match)
    supplier = service.update(supplier_id, body, if_match_version=version)
    _set_etag(response, supplier.version)
    return SupplierResponse.from_model(supplier)


@router.post("/{supplier_id}/archive")
def archive_supplier(
    supplier_id: UUID,
    body: SupplierArchiveRequest,
    service: SupplierServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
    response: Response,
) -> SupplierResponse:
    supplier = service.archive(supplier_id, reason=body.reason, actor_id=principal.user.id)
    _set_etag(response, supplier.version)
    return SupplierResponse.from_model(supplier)


@router.post("/{supplier_id}/unarchive")
def unarchive_supplier(
    supplier_id: UUID,
    body: SupplierArchiveRequest,
    service: SupplierServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
    response: Response,
) -> SupplierResponse:
    supplier = service.unarchive(supplier_id, reason=body.reason)
    _set_etag(response, supplier.version)
    return SupplierResponse.from_model(supplier)


__all__ = ["router"]
