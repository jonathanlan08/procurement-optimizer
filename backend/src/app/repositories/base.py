"""Org-scoped repository base. Isolation control #2.

Every business-data repository extends OrgScopedRepository. Every query it emits
is filtered by organization_id; there is no unscoped accessor. Control #1 is the
OrgScope dependency (api/deps.py); control #3 is the composite org FKs in the
schema; control #4 is the route-matrix isolation test.

Cross-org lookups return None and surface as 404 (never 403) at the API layer.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session as SaSession

from app.models.base import OrgOwnedBase


class OrgIsolationViolation(RuntimeError):
    """Raised when repository code is constructed or used without an org scope.

    This is a programming error, never a user error: it must fail loudly in
    tests, not silently widen a query in production.
    """


class OrgScopedRepository[ModelT: OrgOwnedBase]:
    """Base repository bound to one organization for the life of the request.

    The ModelT bound is OrgOwnedBase, so `id` and `organization_id` are typed:
    mypy verifies the org filter statically, and the runtime checks catch what
    the type system cannot (wrong org id at construction, filter bypass).
    """

    model: type[ModelT]
    session: SaSession
    organization_id: uuid.UUID

    def __init__(self, session: SaSession, organization_id: uuid.UUID) -> None:
        if not isinstance(organization_id, uuid.UUID):
            raise OrgIsolationViolation(
                f"{type(self).__name__} requires a UUID organization_id, "
                f"got {type(organization_id).__name__}"
            )
        if not issubclass(self.model, OrgOwnedBase):
            raise OrgIsolationViolation(
                f"{self.model.__name__} is not org-owned; use a different repository base"
            )
        self.session = session
        self.organization_id = organization_id

    def _base_query(self) -> Select[tuple[ModelT]]:
        """The ONLY legitimate starting point for queries in subclasses."""
        return select(self.model).where(self.model.organization_id == self.organization_id)

    def get(self, entity_id: uuid.UUID) -> ModelT | None:
        """None for both absent and other-org ids - indistinguishable by design."""
        entity = self.session.execute(
            self._base_query().where(self.model.id == entity_id)
        ).scalar_one_or_none()
        if entity is not None and entity.organization_id != self.organization_id:
            # belt-and-braces post-fetch check (isolation control #2b); reaching
            # this line means _base_query was bypassed or broken
            raise OrgIsolationViolation(
                f"{self.model.__name__} {entity_id} escaped the org filter"
            )
        return entity

    def get_or_raise(self, entity_id: uuid.UUID) -> ModelT:
        """404 semantics: absent and other-org are indistinguishable."""
        from app.core.errors import NotFoundError

        entity = self.get(entity_id)
        if entity is None:
            raise NotFoundError(self.model.__name__)
        return entity

    def list_page(
        self,
        *where: Any,
        order_by: Any = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ModelT]:
        stmt = self._base_query().where(*where)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.limit(min(limit, 200)).offset(offset)
        return list(self.session.execute(stmt).scalars().all())

    def add(self, entity: ModelT) -> ModelT:
        if entity.organization_id != self.organization_id:
            raise OrgIsolationViolation(
                f"attempted to add {type(entity).__name__} with organization_id="
                f"{entity.organization_id!r} through a repository scoped to "
                f"{self.organization_id}"
            )
        self.session.add(entity)
        return entity

    def count(self, *where: Any) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.organization_id == self.organization_id, *where)
        )
        return int(self.session.execute(stmt).scalar_one())
