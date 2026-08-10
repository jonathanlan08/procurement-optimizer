"""Supplier contact routes, nested under `/suppliers/{supplier_id}/contacts`
(docs/planning/03-api-contract.md §4.4). Sync `def` by policy, mirroring
api/v1/suppliers.py.

Roles per the contract's route table: `GET/POST /suppliers/{id}/contacts` is
`O A N (V read)` — list is viewer+, create is analyst+. `PATCH/DELETE
/suppliers/{id}/contacts/{cid}` is `O A N` — no viewer access, and "delete =
archive" (soft delete, never a hard delete).

Deviation from this task's prose (contract wins, per instructions): contacts
have no `version` column (see app.models.suppliers.SupplierContact and
app.schemas.supplier_contacts module docstring), so unlike
`PATCH /suppliers/{id}` there is no `If-Match`/`ETag` handling here — §1.7
does not list contacts among the resources that carry optimistic concurrency.

Deviation: the contract does not specify a request/response shape for
`DELETE /suppliers/{id}/contacts/{cid}`. This mirrors `POST
/suppliers/{id}/archive` (§4.4) by taking no request body and returning the
archived resource with `200` (rather than `204`), so callers can observe
`is_archived: true` without a follow-up GET.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.models.identity import Role
from app.schemas.supplier_contacts import (
    SupplierContactCreate,
    SupplierContactListResponse,
    SupplierContactPageInfo,
    SupplierContactResponse,
    SupplierContactUpdate,
)
from app.services.supplier_contact_service import SupplierContactService

router = APIRouter(prefix="/suppliers/{supplier_id}/contacts", tags=["supplier-contacts"])


def get_supplier_contact_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> SupplierContactService:
    return SupplierContactService(db, principal.organization_id, audit, clock, ids)


SupplierContactServiceDep = Annotated[
    SupplierContactService, Depends(get_supplier_contact_service)
]


@router.get("")
def list_supplier_contacts(
    supplier_id: UUID,
    service: SupplierContactServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    include_archived: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SupplierContactListResponse:
    items, total = service.list(
        supplier_id, include_archived=include_archived, limit=limit, offset=offset
    )
    return SupplierContactListResponse(
        items=[SupplierContactResponse.from_model(c) for c in items],
        page=SupplierContactPageInfo(limit=limit, offset=offset, total=total),
    )


@router.post("", status_code=201)
def create_supplier_contact(
    supplier_id: UUID,
    body: SupplierContactCreate,
    service: SupplierContactServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> SupplierContactResponse:
    contact = service.create(supplier_id, body)
    return SupplierContactResponse.from_model(contact)


@router.patch("/{contact_id}")
def update_supplier_contact(
    supplier_id: UUID,
    contact_id: UUID,
    body: SupplierContactUpdate,
    service: SupplierContactServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> SupplierContactResponse:
    contact = service.update(supplier_id, contact_id, body)
    return SupplierContactResponse.from_model(contact)


@router.delete("/{contact_id}")
def archive_supplier_contact(
    supplier_id: UUID,
    contact_id: UUID,
    service: SupplierContactServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> SupplierContactResponse:
    contact = service.archive(supplier_id, contact_id, actor_id=principal.user.id)
    return SupplierContactResponse.from_model(contact)


__all__ = ["router"]
