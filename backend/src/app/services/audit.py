"""AuditRecorder — writes audit events inside the caller's transaction.

An audit trail that can diverge from the data it describes is worse than none:
the recorder never opens its own transaction and never swallows failures.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.ids import IdGenerator
from app.models.audit import AuditEvent


class AuditRecorder:
    def __init__(
        self,
        session: SaSession,
        clock: Clock,
        ids: IdGenerator,
        *,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        request_id: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._ids = ids
        self._organization_id = organization_id
        self._actor_user_id = actor_user_id
        self._request_id = request_id
        self._ip_address = ip_address

    def record(
        self,
        event_type: str,
        *,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        explanation: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            actor_user_id=self._actor_user_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            before_state=before_state,
            after_state=after_state,
            explanation=explanation,
            request_id=self._request_id,
            ip_address=self._ip_address,
            occurred_at=self._clock.now(),
        )
        self._session.add(event)
        return event
