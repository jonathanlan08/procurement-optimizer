"""Identity and tenancy models: organizations, users, memberships, sessions."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, SmallInteger, Text
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    CURRENCY_CHAR,
    ArchivableMixin,
    Base,
    PkMixin,
    TimestampedMixin,
    VersionedMixin,
)


class Role(StrEnum):
    OWNER = "owner"
    ADMINISTRATOR = "administrator"
    ANALYST = "analyst"
    VIEWER = "viewer"


ROLE_ENUM = SaEnum(
    Role,
    name="role_enum",
    values_callable=lambda e: [m.value for m in e],
)


class Organization(PkMixin, TimestampedMixin, VersionedMixin, Base):
    __tablename__ = "organizations"

    # uniqueness is a functional index on lower(slug) in migration 0001;
    # the app writes slugs lowercased
    slug: Mapped[str] = mapped_column(Text())
    name: Mapped[str] = mapped_column(Text())
    base_currency: Mapped[str] = mapped_column(CURRENCY_CHAR, default="USD")
    is_demo: Mapped[bool] = mapped_column(Boolean(), default=False)
    archived_at: Mapped[datetime | None] = mapped_column(default=None)


class User(PkMixin, TimestampedMixin, Base):
    __tablename__ = "users"

    # uniqueness is a functional index on lower(email) in migration 0001;
    # the app normalizes emails to lowercase at write time and lookup
    email: Mapped[str] = mapped_column(Text())
    password_hash: Mapped[str] = mapped_column(Text())
    full_name: Mapped[str] = mapped_column(Text())
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(default=None)
    failed_login_count: Mapped[int] = mapped_column(SmallInteger(), default=0)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    archived_at: Mapped[datetime | None] = mapped_column(default=None)


class OrganizationMembership(PkMixin, TimestampedMixin, VersionedMixin, Base):
    __tablename__ = "organization_memberships"
    # one live membership per user per org: partial unique index (revoked_at IS NULL)
    # is created in migration 0001; revoked memberships remain as history rows

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[Role] = mapped_column(ROLE_ENUM)
    invited_at: Mapped[datetime | None] = mapped_column(default=None)
    accepted_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)

    # relationships are declared so the unit of work orders inserts by
    # dependency; without them SQLAlchemy does not FK-sort across mappers
    user: Mapped[User] = relationship(lazy="joined", innerjoin=True)
    organization: Mapped[Organization] = relationship(lazy="joined", innerjoin=True)


class Session(PkMixin, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    active_organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    token_sha256: Mapped[str] = mapped_column(Text(), unique=True)
    csrf_secret: Mapped[str] = mapped_column(Text())
    ip_address: Mapped[str | None] = mapped_column(INET(), default=None)
    user_agent: Mapped[str | None] = mapped_column(Text(), default=None)
    created_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    absolute_expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)

    user: Mapped[User] = relationship(lazy="select")
    organization: Mapped[Organization] = relationship(lazy="select")


__all__ = [
    "Role",
    "ROLE_ENUM",
    "Organization",
    "User",
    "OrganizationMembership",
    "Session",
    "ArchivableMixin",
]
