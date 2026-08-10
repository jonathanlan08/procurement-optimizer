"""Supplier performance record service — org-scoped business logic for the
`/suppliers/{id}/performance` sub-resource (docs/planning/03-api-contract.md
§4.4).

Mirrors supplier_service.py: services never commit (the request-scoped
`get_db` dependency commits on success and rolls back on any raised
exception), and every mutation writes exactly one audit event in the same
transaction as the data change it describes.

The parent supplier must exist in the caller's organization for every
operation here — `SupplierRepository.get_or_raise` 404s on both a genuinely
absent supplier and a same-id-different-org supplier.

Records are point-in-time history (no VersionedMixin/ArchivableMixin on
SupplierPerformanceRecord — see app.models.suppliers), and the contract's
route table (§4.4) lists only GET and POST for this sub-resource, so there is
deliberately no update or delete method here.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.ids import IdGenerator
from app.core.money import RATIO_SCALE, quantize
from app.models.suppliers import SupplierPerformanceRecord
from app.repositories.supplier_performance_repository import SupplierPerformanceRepository
from app.repositories.supplier_repository import SupplierRepository
from app.schemas.supplier_performance import SupplierPerformanceCreate
from app.services.audit import AuditRecorder


def _state(record: SupplierPerformanceRecord) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "supplier_id": str(record.supplier_id),
        "period_start": record.period_start.isoformat(),
        "period_end": record.period_end.isoformat(),
        "on_time_delivery_rate": str(record.on_time_delivery_rate),
        "defect_rate": str(record.defect_rate),
        "quality_score": str(record.quality_score),
        "orders_count": record.orders_count,
        "source": record.source,
        "notes": record.notes,
        "recorded_at": record.recorded_at.isoformat(),
    }


class SupplierPerformanceService:
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
        self._suppliers = SupplierRepository(session, organization_id)
        self._repo = SupplierPerformanceRepository(session, organization_id)

    def _require_supplier(self, supplier_id: uuid.UUID) -> None:
        """404s for an absent or cross-org supplier (never 403)."""
        self._suppliers.get_or_raise(supplier_id)

    def list(
        self, supplier_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[SupplierPerformanceRecord], int]:
        self._require_supplier(supplier_id)
        items = self._repo.list_for_supplier(supplier_id, limit=limit, offset=offset)
        total = self._repo.count_for_supplier(supplier_id)
        return items, total

    def create(
        self, supplier_id: uuid.UUID, body: SupplierPerformanceCreate
    ) -> SupplierPerformanceRecord:
        self._require_supplier(supplier_id)

        now = self._clock.now()
        record = SupplierPerformanceRecord(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            supplier_id=supplier_id,
            period_start=body.period_start,
            period_end=body.period_end,
            on_time_delivery_rate=quantize(body.on_time_delivery_rate, RATIO_SCALE),
            defect_rate=quantize(body.defect_rate, RATIO_SCALE),
            quality_score=quantize(body.quality_score, RATIO_SCALE),
            orders_count=body.orders_count,
            source=body.source,
            notes=body.notes,
            recorded_at=now,
        )
        self._repo.add(record)
        self._db.flush()

        self._audit.record(
            "supplier_performance.recorded",
            entity_type="supplier_performance_record",
            entity_id=record.id,
            after_state=_state(record),
            explanation=f"Performance record added for supplier {supplier_id}",
        )
        return record


__all__ = ["SupplierPerformanceService"]
