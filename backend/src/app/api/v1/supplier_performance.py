"""Supplier performance routes, nested under `/suppliers/{supplier_id}/performance`
(docs/planning/03-api-contract.md §4.4). Sync `def` by policy, mirroring
api/v1/suppliers.py.

Roles per the contract's route table: `GET /suppliers/{id}/performance` is
`O A N V`; `POST /suppliers/{id}/performance` is `O A N`.

Deviation from this task's prose (contract wins, per instructions): the
contract's route table (§4.4) lists only GET and POST for this sub-resource —
no PATCH/PUT/DELETE. Performance records are point-in-time history (no
`version`/`archived_at` — see app.models.suppliers.SupplierPerformanceRecord),
so this router intentionally has no update or delete route, and there is no
`supplier_performance.deleted` audit event.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.models.identity import Role
from app.schemas.supplier_performance import (
    SupplierPerformanceCreate,
    SupplierPerformanceListResponse,
    SupplierPerformancePageInfo,
    SupplierPerformanceResponse,
)
from app.services.supplier_performance_service import SupplierPerformanceService

router = APIRouter(
    prefix="/suppliers/{supplier_id}/performance", tags=["supplier-performance"]
)


def get_supplier_performance_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> SupplierPerformanceService:
    return SupplierPerformanceService(db, principal.organization_id, audit, clock, ids)


SupplierPerformanceServiceDep = Annotated[
    SupplierPerformanceService, Depends(get_supplier_performance_service)
]


@router.get("")
def list_supplier_performance(
    supplier_id: UUID,
    service: SupplierPerformanceServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SupplierPerformanceListResponse:
    items, total = service.list(supplier_id, limit=limit, offset=offset)
    return SupplierPerformanceListResponse(
        items=[SupplierPerformanceResponse.from_model(r) for r in items],
        page=SupplierPerformancePageInfo(limit=limit, offset=offset, total=total),
    )


@router.post("", status_code=201)
def create_supplier_performance(
    supplier_id: UUID,
    body: SupplierPerformanceCreate,
    service: SupplierPerformanceServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> SupplierPerformanceResponse:
    record = service.create(supplier_id, body)
    return SupplierPerformanceResponse.from_model(record)


__all__ = ["router"]
