"""Declarative base and mixins — PRINCIPAL-OWNED.

Conventions (docs/planning/02-erd.md §1, ratified):
- UUID primary keys generated in the application (IdGenerator), never in the DB
- timestamptz UTC everywhere, named *_at
- every business table carries organization_id and UNIQUE (organization_id, id)
  so children can enforce same-org references with composite foreign keys
- soft delete via archived_at; business data is never hard-deleted
- optimistic locking via an integer version column
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    ForeignKey,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012 — SQLAlchemy declarative API contract
        uuid.UUID: UUID(as_uuid=True),
        datetime: TIMESTAMP(timezone=True),
    }


class PkMixin:
    """Application-generated UUID primary key (no server default)."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)


class TimestampedMixin:
    """created_at / updated_at, set by the application via the injected Clock."""

    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class OrgOwnedMixin:
    """organization_id on every business table. RESTRICT: orgs are never deleted
    out from under their data."""

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )


def org_identity_constraint(table_name: str) -> UniqueConstraint:
    """UNIQUE (organization_id, id) — the target for composite org FKs.

    Include in __table_args__ of every OrgOwned business table.
    """
    return UniqueConstraint("organization_id", "id", name=f"uq_{table_name}_org_identity")


class OrgOwnedBase(PkMixin, OrgOwnedMixin, Base):
    """Abstract base for every org-owned table: typed UUID pk + organization_id.

    OrgScopedRepository is generic over subclasses of this, which is what lets
    mypy verify the org filter statically instead of trusting a runtime check.
    """

    __abstract__ = True


class ArchivableMixin:
    """Soft delete. Business data is never hard-deleted."""

    archived_at: Mapped[datetime | None] = mapped_column(default=None)
    archived_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    archive_reason: Mapped[str | None] = mapped_column(Text(), default=None)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class VersionedMixin:
    """Optimistic locking; UPDATE ... WHERE version = :expected."""

    version: Mapped[int] = mapped_column(Integer(), default=1)


CURRENCY_CHAR = CHAR(3)
