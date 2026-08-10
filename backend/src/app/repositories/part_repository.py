"""Part repository — org-scoped data access (docs/planning/02-erd.md §4).

Extends OrgScopedRepository, so every query is filtered by organization_id
before any other predicate (isolation control #2).

`PartAlternativeRepository` lives here too, rather than in its own module
like `SupplierContactRepository`/`SupplierPerformanceRepository`: this task's
file-creation allowlist names only `repositories/part_repository.py`, so the
sub-resource repository is co-located instead of getting its own file. It
follows the same org-scoped pattern as everything else.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import func, or_

from app.models.parts import Part, PartAlternative
from app.repositories.base import OrgScopedRepository

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _normalize(raw: str) -> str:
    """Mirror the DB-generated `Part.normalized_key` expression
    (`regexp_replace(lower(internal_part_number), '[^a-z0-9]', '', 'g')`,
    see app/models/parts.py) so an application-side search term can be
    compared against the stored column with ordinary equality.
    """
    return _NON_ALNUM_RE.sub("", raw.lower())


class PartRepository(OrgScopedRepository[Part]):
    model = Part

    def get_by_internal_part_number(self, internal_part_number: str) -> Part | None:
        """Case-insensitive lookup among ACTIVE (non-archived) parts only.

        Mirrors the partial unique index `uq_parts_org_ipn_active`
        (migration 0004: unique on `lower(internal_part_number)` WHERE
        `archived_at IS NULL`). Archiving a part frees its number for reuse.
        """
        stmt = self._base_query().where(
            func.lower(Part.internal_part_number) == internal_part_number.strip().lower(),
            Part.archived_at.is_(None),
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def search(
        self,
        *,
        q: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Part]:
        clauses = self._filters(
            q=q, category=category, is_active=is_active, include_archived=include_archived
        )
        return self.list_page(*clauses, order_by=Part.name, limit=limit, offset=offset)

    def count_matching(
        self,
        *,
        q: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
        include_archived: bool = False,
    ) -> int:
        clauses = self._filters(
            q=q, category=category, is_active=is_active, include_archived=include_archived
        )
        return self.count(*clauses)

    def _filters(
        self,
        *,
        q: str | None,
        category: str | None,
        is_active: bool | None,
        include_archived: bool,
    ) -> list[Any]:
        clauses: list[Any] = []
        if not include_archived:
            clauses.append(Part.archived_at.is_(None))
        if q:
            term = q.strip()
            or_clauses: list[Any] = [
                Part.internal_part_number.ilike(f"%{term}%"),
                Part.name.ilike(f"%{term}%"),
                Part.manufacturer_part_number.ilike(f"%{term}%"),
            ]
            normalized = _normalize(term)
            if normalized:
                # equality match on the generated normalized_key column
                # (docs/planning/04-document-pipeline.md §10 strategy 4) —
                # tolerant of punctuation/casing differences an ilike substring
                # match would miss, e.g. "ACME-100" vs "acme100".
                or_clauses.append(Part.normalized_key == normalized)
            clauses.append(or_(*or_clauses))
        if category:
            clauses.append(Part.category == category.strip())
        if is_active is not None:
            clauses.append(Part.is_active == is_active)
        return clauses


class PartAlternativeRepository(OrgScopedRepository[PartAlternative]):
    model = PartAlternative

    def list_for_part(
        self, part_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> list[PartAlternative]:
        return self.list_page(
            PartAlternative.part_id == part_id,
            order_by=PartAlternative.id,
            limit=limit,
            offset=offset,
        )

    def count_for_part(self, part_id: uuid.UUID) -> int:
        return self.count(PartAlternative.part_id == part_id)

    def get_for_part(
        self, part_id: uuid.UUID, alternative_id: uuid.UUID
    ) -> PartAlternative | None:
        """None when the row is absent, other-org, or belongs to a different
        part — all indistinguishable, surfacing as 404."""
        alt = self.get(alternative_id)
        if alt is None or alt.part_id != part_id:
            return None
        return alt

    def get_by_part_and_alternative(
        self, part_id: uuid.UUID, alternative_part_id: uuid.UUID
    ) -> PartAlternative | None:
        """Existing row for this (part, alternative part) pair, if any.

        Mirrors the unique index `uq_part_alternatives_org_part_alternative`
        (migration 0004) so the service can raise a clean 409 before ever
        reaching the database constraint.
        """
        stmt = self._base_query().where(
            PartAlternative.part_id == part_id,
            PartAlternative.alternative_part_id == alternative_part_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()


__all__ = ["PartAlternativeRepository", "PartRepository"]
