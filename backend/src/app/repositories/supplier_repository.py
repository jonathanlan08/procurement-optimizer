"""Supplier repository - org-scoped data access (docs/planning/02-erd.md §4).

Extends OrgScopedRepository, so every query is filtered by organization_id
before any other predicate (isolation control #2).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_

from app.models.suppliers import Supplier
from app.repositories.base import OrgScopedRepository


class SupplierRepository(OrgScopedRepository[Supplier]):
    model = Supplier

    def get_by_code(self, code: str) -> Supplier | None:
        """Case-insensitive lookup among ACTIVE (non-archived) suppliers only.

        Mirrors the partial unique index `uq_suppliers_org_code_active`
        (migration 0002: unique on `lower(code)` WHERE `archived_at IS NULL`).
        Archiving a supplier frees its code for reuse.
        """
        stmt = self._base_query().where(
            func.lower(Supplier.code) == code.strip().lower(),
            Supplier.archived_at.is_(None),
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def search(
        self,
        *,
        q: str | None = None,
        country: str | None = None,
        is_active: bool | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Supplier]:
        clauses = self._filters(
            q=q, country=country, is_active=is_active, include_archived=include_archived
        )
        return self.list_page(*clauses, order_by=Supplier.name, limit=limit, offset=offset)

    def count_matching(
        self,
        *,
        q: str | None = None,
        country: str | None = None,
        is_active: bool | None = None,
        include_archived: bool = False,
    ) -> int:
        clauses = self._filters(
            q=q, country=country, is_active=is_active, include_archived=include_archived
        )
        return self.count(*clauses)

    def _filters(
        self,
        *,
        q: str | None,
        country: str | None,
        is_active: bool | None,
        include_archived: bool,
    ) -> list[Any]:
        clauses: list[Any] = []
        if not include_archived:
            clauses.append(Supplier.archived_at.is_(None))
        if q:
            pattern = f"%{q.strip()}%"
            clauses.append(or_(Supplier.name.ilike(pattern), Supplier.code.ilike(pattern)))
        if country:
            clauses.append(Supplier.country_code == country.strip().upper())
        if is_active is not None:
            clauses.append(Supplier.is_active == is_active)
        return clauses


__all__ = ["SupplierRepository"]
