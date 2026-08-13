"""Read-only queries over `app.models.audit.AuditEvent`
(docs/planning/03-api-contract.md §4.19, docs/SPEC.md §Audit trail).

**No write path here, by design** - §4.19's own prose: "There is no write,
update, or delete route for audit events." `app.services.audit.AuditRecorder`
(existing, PRINCIPAL-adjacent, not touched by this task) is the only writer
anywhere in this codebase, and a database trigger installed in migration
0001 additionally blocks `UPDATE`/`DELETE` on `audit_events` at the SQL
level (`app/models/audit.py`'s own docstring) - this module cannot violate
that even by accident, since it never issues anything but `SELECT`.

**Cursor pagination** (§4.19: "cursor pagination on `(occurred_at, id)`"):
keyset, not offset - stable under concurrent inserts, unlike
`OFFSET`-based paging, which is exactly what an append-only, high-volume
audit log needs. The cursor is an opaque base64 encoding of
`"{occurred_at_iso}|{id}"`; `_AuditEventRepository.list_page_keyset` orders
`(occurred_at DESC, id DESC)` and filters strictly past the cursor's
position on that same compound key (never on `occurred_at` alone - two
events in the same instant would otherwise be dropped or repeated across
pages). One extra row is always fetched to decide `has_more`/`next_cursor`
without a second round trip.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session as SaSession

from app.core.errors import ValidationAppError
from app.models.audit import AuditEvent
from app.repositories.base import OrgScopedRepository

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def encode_cursor(occurred_at: datetime, event_id: uuid.UUID) -> str:
    raw = f"{occurred_at.isoformat()}|{event_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        iso_ts, id_str = raw.split("|", 1)
        return datetime.fromisoformat(iso_ts), uuid.UUID(id_str)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValidationAppError("Invalid cursor.") from exc


@dataclass(frozen=True, slots=True)
class AuditEventFilters:
    event_types: tuple[str, ...] = ()
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    actor_user_id: uuid.UUID | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None


class _AuditEventRepository(OrgScopedRepository[AuditEvent]):
    model = AuditEvent

    def list_page_keyset(
        self,
        filters: AuditEventFilters,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[AuditEvent]:
        stmt = self._base_query()
        if filters.event_types:
            stmt = stmt.where(AuditEvent.event_type.in_(filters.event_types))
        if filters.entity_type is not None:
            stmt = stmt.where(AuditEvent.entity_type == filters.entity_type)
        if filters.entity_id is not None:
            stmt = stmt.where(AuditEvent.entity_id == filters.entity_id)
        if filters.actor_user_id is not None:
            stmt = stmt.where(AuditEvent.actor_user_id == filters.actor_user_id)
        if filters.occurred_from is not None:
            stmt = stmt.where(AuditEvent.occurred_at >= filters.occurred_from)
        if filters.occurred_to is not None:
            stmt = stmt.where(AuditEvent.occurred_at <= filters.occurred_to)
        if after is not None:
            occurred_at, event_id = after
            stmt = stmt.where(
                or_(
                    AuditEvent.occurred_at < occurred_at,
                    and_(AuditEvent.occurred_at == occurred_at, AuditEvent.id < event_id),
                )
            )
        stmt = stmt.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())


class AuditReadService:
    def __init__(self, session: SaSession, organization_id: uuid.UUID) -> None:
        self._repo = _AuditEventRepository(session, organization_id)

    def get(self, event_id: uuid.UUID) -> AuditEvent:
        return self._repo.get_or_raise(event_id)

    # NOT named `list` - a method named identically to the `list` builtin
    # shadows it for mypy's annotation resolution in every method defined
    # afterward in this same class (a real mypy quirk, not a style
    # preference: `list_for_entity`'s own `-> tuple[list[AuditEvent], ...]`
    # return type fails to resolve if this method is called `list`).
    def list_events(
        self,
        filters: AuditEventFilters,
        *,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> tuple[list[AuditEvent], str | None]:
        bounded_limit = max(1, min(limit, MAX_LIMIT))
        after = decode_cursor(cursor) if cursor is not None else None
        # fetch one extra row to learn has_more without a second round trip
        rows = self._repo.list_page_keyset(filters, after=after, limit=bounded_limit + 1)
        has_more = len(rows) > bounded_limit
        page = rows[:bounded_limit]
        next_cursor = (
            encode_cursor(page[-1].occurred_at, page[-1].id) if has_more and page else None
        )
        return page, next_cursor

    def list_for_entity(
        self,
        entity_type: str,
        entity_id: uuid.UUID,
        *,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> tuple[list[AuditEvent], str | None]:
        filters = AuditEventFilters(entity_type=entity_type, entity_id=entity_id)
        return self.list_events(filters, limit=limit, cursor=cursor)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "AuditEventFilters",
    "AuditReadService",
    "decode_cursor",
    "encode_cursor",
]
