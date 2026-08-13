"""Audit-event read routes (docs/planning/03-api-contract.md §4.19). Sync
`def` by policy, mirroring every other router in this codebase.

**No write/update/delete route anywhere in this module** - §4.19's own
words: "There is no write, update, or delete route for audit events, by
design." Every route below is `GET`; `app/services/audit_read_service.py`'s
own module docstring explains the two independent enforcement layers this
already sits behind (the recorder is the only writer anywhere in the
codebase; a DB trigger blocks `UPDATE`/`DELETE` at the SQL level). A
contract test (`tests/integration/test_audit_api.py`) walks
`app.openapi()["paths"]` and asserts only `GET` methods exist under
`/audit-events` and the entity-timeline path, as a mechanical backstop
against ever adding one by accident.

**Two routers, one module** - the same shape `api/v1/briefs.py`/
`api/v1/documents.py` already establish: `audit_events_router` (prefix
`/audit-events`) owns list/get; `entity_audit_router` (prefix `/entities`)
owns the entity-timeline route, since it does not nest under
`/audit-events` at all (§4.19: `GET /entities/{entity_type}/{entity_id}/
audit-events`). Both exported and mounted in `app/main.py`.

**`from`/`to` are reserved-word-adjacent** (`from` is a Python keyword) -
the route parameter is `from_`, aliased to the wire name `from` via
`Query(alias="from")`, the same `Header(alias=...)` technique every
`If-Match`-consuming route in this codebase already uses for the same
"the wire name isn't a valid Python identifier" reason.

Role: every route here is `VIEWER` - §4.19's own route table marks all
three "O A N V", including list (its own parenthetical spells out "(V read
own-org read-only: yes)" as a deliberate contract answer to §6 gap #1, not
an accident).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import DbDep, Principal, PrincipalDep, require_role
from app.models.identity import Role
from app.schemas.audit import AuditEventListResponse, AuditEventResponse
from app.services.audit_read_service import AuditEventFilters, AuditReadService

audit_events_router = APIRouter(prefix="/audit-events", tags=["audit"])
entity_audit_router = APIRouter(prefix="/entities", tags=["audit"])


def get_audit_read_service(db: DbDep, principal: PrincipalDep) -> AuditReadService:
    return AuditReadService(db, principal.organization_id)


AuditReadServiceDep = Annotated[AuditReadService, Depends(get_audit_read_service)]


# -- list / get, addressed directly -----------------------------------------


@audit_events_router.get("")
def list_audit_events(
    service: AuditReadServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    # B008 suppressed below - ruff's immutable-type allowlist for the
    # "function call in a default" check doesn't recognize `list[...]`/
    # `UUID`/`datetime` as immutable the way it does `str`/`bool`/`int`;
    # `Query()` itself is a side-effect-free, idempotent call either way
    # (same reasoning already applied to every other `= Query(...)` default
    # in this codebase, e.g. api/v1/rfqs.py's `list_rfqs`).
    event_type: list[str] | None = Query(default=None),  # noqa: B008
    entity_type: str | None = Query(default=None, max_length=200),
    entity_id: UUID | None = Query(default=None),  # noqa: B008
    actor_user_id: UUID | None = Query(default=None),  # noqa: B008
    from_: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    to: datetime | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> AuditEventListResponse:
    filters = AuditEventFilters(
        event_types=tuple(event_type) if event_type else (),
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        occurred_from=from_,
        occurred_to=to,
    )
    items, next_cursor = service.list_events(filters, limit=limit, cursor=cursor)
    return AuditEventListResponse(
        items=[AuditEventResponse.from_model(e) for e in items], next_cursor=next_cursor
    )


@audit_events_router.get("/{event_id}")
def get_audit_event(
    event_id: UUID,
    service: AuditReadServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> AuditEventResponse:
    return AuditEventResponse.from_model(service.get(event_id))


# -- entity timeline ----------------------------------------------------


@entity_audit_router.get("/{entity_type}/{entity_id}/audit-events")
def list_entity_audit_events(
    entity_type: str,
    entity_id: UUID,
    service: AuditReadServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> AuditEventListResponse:
    items, next_cursor = service.list_for_entity(
        entity_type, entity_id, limit=limit, cursor=cursor
    )
    return AuditEventListResponse(
        items=[AuditEventResponse.from_model(e) for e in items], next_cursor=next_cursor
    )


__all__ = ["audit_events_router", "entity_audit_router"]
