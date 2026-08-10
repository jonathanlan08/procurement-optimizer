"""Audit events (append-only) and jobs."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, SmallInteger, Text
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PkMixin


class AuditEvent(PkMixin, Base):
    """Append-only. No updated_at, no archived_at. UPDATE/DELETE are blocked by a
    database trigger installed in migration 0001; the ORM must never mutate rows."""

    __tablename__ = "audit_events"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    event_type: Mapped[str] = mapped_column(Text(), index=True)  # verb.noun vocabulary
    entity_type: Mapped[str] = mapped_column(Text())
    entity_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    explanation: Mapped[str | None] = mapped_column(Text(), default=None)
    request_id: Mapped[str | None] = mapped_column(Text(), default=None)
    ip_address: Mapped[str | None] = mapped_column(INET(), default=None)
    occurred_at: Mapped[datetime] = mapped_column(index=True)


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


JOB_STATE_ENUM = SaEnum(
    JobState,
    name="job_state_enum",
    values_callable=lambda e: [m.value for m in e],
)


class Job(PkMixin, Base):
    __tablename__ = "jobs"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(Text())
    state: Mapped[JobState] = mapped_column(JOB_STATE_ENUM, default=JobState.PENDING)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    error_code: Mapped[str | None] = mapped_column(Text(), default=None)
    error_message: Mapped[str | None] = mapped_column(Text(), default=None)
    attempts: Mapped[int] = mapped_column(SmallInteger(), default=0)
    locked_by: Mapped[str | None] = mapped_column(Text(), default=None)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None] = mapped_column(default=None)

# UOW insert-ordering relationships (see identity.py note)
AuditEvent.organization = relationship("Organization", lazy="select")
Job.organization = relationship("Organization", lazy="select")
