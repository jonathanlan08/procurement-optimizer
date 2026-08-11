"""Audit-event response schemas (docs/planning/03-api-contract.md §4.19,
app/models/audit.py FROZEN).

`AuditEventResponse` exposes `app.models.audit.AuditEvent`'s real column
names verbatim — `occurred_at` is already the model's own column name (not
a renamed timestamp needing an `AS occurred_at` alias), so no reconciliation
was needed there. No request/write schemas exist in this module: §4.19's own
words, "There is no write, update, or delete route for audit events," mean
there is nothing here but response shapes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.audit import AuditEvent


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    occurred_at: datetime
    event_type: str
    entity_type: str
    entity_id: str | None
    actor_user_id: str | None
    explanation: str | None
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    request_id: str | None

    @classmethod
    def from_model(cls, event: AuditEvent) -> AuditEventResponse:
        return cls(
            id=str(event.id),
            organization_id=str(event.organization_id),
            occurred_at=event.occurred_at,
            event_type=event.event_type,
            entity_type=event.entity_type,
            entity_id=(str(event.entity_id) if event.entity_id is not None else None),
            actor_user_id=(
                str(event.actor_user_id) if event.actor_user_id is not None else None
            ),
            explanation=event.explanation,
            before_state=event.before_state,
            after_state=event.after_state,
            request_id=event.request_id,
        )


class AuditEventListResponse(BaseModel):
    items: list[AuditEventResponse]
    next_cursor: str | None


__all__ = ["AuditEventListResponse", "AuditEventResponse"]
