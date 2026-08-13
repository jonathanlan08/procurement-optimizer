"""Supplier performance record repository - org-scoped data access
(docs/planning/02-erd.md §4).

Extends OrgScopedRepository, so every query is filtered by organization_id
before any other predicate (isolation control #2). Records are additionally
scoped to a single parent supplier by the service layer, which confirms the
parent exists in-org (via SupplierRepository.get_or_raise) before any of the
methods here are called. Records have no archived_at/version (02-erd.md §4):
they are historical entries, not a mutable aggregate with a lifecycle.
"""

from __future__ import annotations

import uuid

from app.models.suppliers import SupplierPerformanceRecord
from app.repositories.base import OrgScopedRepository


class SupplierPerformanceRepository(OrgScopedRepository[SupplierPerformanceRecord]):
    model = SupplierPerformanceRecord

    def list_for_supplier(
        self,
        supplier_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SupplierPerformanceRecord]:
        return self.list_page(
            SupplierPerformanceRecord.supplier_id == supplier_id,
            order_by=SupplierPerformanceRecord.period_start.desc(),
            limit=limit,
            offset=offset,
        )

    def count_for_supplier(self, supplier_id: uuid.UUID) -> int:
        return self.count(SupplierPerformanceRecord.supplier_id == supplier_id)

    def get_for_supplier(
        self, supplier_id: uuid.UUID, record_id: uuid.UUID
    ) -> SupplierPerformanceRecord | None:
        """None when the record is absent, other-org, or belongs to a
        different supplier - all indistinguishable, surfacing as 404."""
        record = self.get(record_id)
        if record is None or record.supplier_id != supplier_id:
            return None
        return record


__all__ = ["SupplierPerformanceRepository"]
