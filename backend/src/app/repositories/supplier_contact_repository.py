"""Supplier contact repository - org-scoped data access (docs/planning/02-erd.md §4).

Extends OrgScopedRepository, so every query is filtered by organization_id
before any other predicate (isolation control #2). Contacts are additionally
scoped to a single parent supplier by the service layer, which confirms the
parent exists in-org (via SupplierRepository.get_or_raise) before any of the
methods here are called.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.models.suppliers import SupplierContact
from app.repositories.base import OrgScopedRepository


class SupplierContactRepository(OrgScopedRepository[SupplierContact]):
    model = SupplierContact

    def list_for_supplier(
        self,
        supplier_id: uuid.UUID,
        *,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SupplierContact]:
        clauses = self._filters(supplier_id, include_archived=include_archived)
        return self.list_page(
            *clauses, order_by=SupplierContact.name, limit=limit, offset=offset
        )

    def count_for_supplier(
        self, supplier_id: uuid.UUID, *, include_archived: bool = False
    ) -> int:
        clauses = self._filters(supplier_id, include_archived=include_archived)
        return self.count(*clauses)

    def get_for_supplier(
        self, supplier_id: uuid.UUID, contact_id: uuid.UUID
    ) -> SupplierContact | None:
        """None when the contact is absent, other-org, or belongs to a
        different supplier - all indistinguishable, surfacing as 404."""
        contact = self.get(contact_id)
        if contact is None or contact.supplier_id != supplier_id:
            return None
        return contact

    def _filters(self, supplier_id: uuid.UUID, *, include_archived: bool) -> list[Any]:
        clauses: list[Any] = [SupplierContact.supplier_id == supplier_id]
        if not include_archived:
            clauses.append(SupplierContact.archived_at.is_(None))
        return clauses


__all__ = ["SupplierContactRepository"]
