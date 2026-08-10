"""Org-scoped repository base — PRINCIPAL-OWNED. Isolation control #2.

Every business-data repository extends OrgScopedRepository. Every query it emits
is filtered by organization_id; there is no unscoped accessor. Control #1 is the
OrgScope dependency (api/deps.py); control #3 is the composite org FKs in the
schema; control #4 is the route-matrix isolation test.

Cross-org lookups return None and surface as 404 (never 403) at the API layer.
"""

from __future__ import annotations

import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session as SaSession

from app.models.base import Base, OrgOwnedMixin


class OrgIsolationViolation(RuntimeError):
    """Raised when repository code is constructed or used without an org scope.

    This is a programming error, never a user error: it must fail loudly in
    tests, not silently widen a query in production.
    """


ModelT = TypeVar("ModelT", bound=Base)


class OrgScopedRepository(Generic[ModelT]):
    """Base repository bound to one organization for the life of the request."""

    model: type[ModelT]

    def __init__(self, session: SaSession, organization_id: uuid.UUID) -> None:
        if not isinstance(organization_id, uuid.UUID):
            raise OrgIsolationViolation(
                f"{type(self).__name__} requires a UUID organization_id, "
                f"got {type(organization_id).__name__}"
            )
        if not issubclass(self.model, OrgOwnedMixin):
            raise OrgIsolationViolation(
                f"{self.model.__name__} is not org-owned; use a different repository base"
            )
        self.session = session
        self.organization_id = organization_id

    def _base_query(self) -> Select[tuple[ModelT]]:
        """The ONLY legitimate starting point for queries in subclasses."""
        return select(self.model).where(self.model.organization_id == self.organization_id)

    def get(self, entity_id: uuid.UUID) -> ModelT | None:
        """None for both absent and other-org ids — indistinguishable by design."""
        return self.session.execute(
            self._base_query().where(self.model.id == entity_id)
        ).scalar_one_or_none()

    def add(self, entity: ModelT) -> ModelT:
        entity_org = getattr(entity, "organization_id", None)
        if entity_org != self.organization_id:
            raise OrgIsolationViolation(
                f"attempted to add {type(entity).__name__} with organization_id="
                f"{entity_org!r} through a repository scoped to {self.organization_id}"
            )
        self.session.add(entity)
        return entity

    def count(self, *where: Any) -> int:
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.organization_id == self.organization_id, *where)
        )
        return self.session.execute(stmt).scalar_one()
