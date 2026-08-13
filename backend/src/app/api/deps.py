"""FastAPI dependency chain. Isolation control #1.

Chain: cookie -> AuthService.resolve -> Principal(user, org, role)
       mutating request -> CSRF double-submit + Origin allowlist
       route -> require_roles(...) -> handler receives a Principal
       repositories are constructed ONLY from principal.organization_id

The client never chooses the organization: any organization_id in a request body
or query string is ignored. Cross-org resources 404.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import CsrfError, ForbiddenRoleError
from app.core.ids import IdGenerator
from app.core.security import constant_time_equals, origin_allowed
from app.models.identity import Role, User
from app.services.audit import AuditRecorder
from app.services.auth import AuthService

SESSION_COOKIE = "po_session"
CSRF_HEADER = "x-csrf-token"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def client_ip(request: Request) -> str | None:
    """The client address, only if it parses as a real IP (inet column)."""
    import ipaddress

    host = request.client.host if request.client else None
    if host is None:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host

# role hierarchy: any role listed grants access; hierarchy resolved explicitly
ROLE_ORDER: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.ANALYST: 1,
    Role.ADMINISTRATOR: 2,
    Role.OWNER: 3,
}


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_clock(request: Request) -> Clock:
    return request.app.state.clock  # type: ignore[no-any-return]


def get_ids(request: Request) -> IdGenerator:
    return request.app.state.ids  # type: ignore[no-any-return]


def get_db(request: Request) -> Generator[SaSession, None, None]:
    """Request-scoped unit of work: one transaction, commit on success."""
    session_factory = request.app.state.session_factory
    session: SaSession = session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


DbDep = Annotated[SaSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ClockDep = Annotated[Clock, Depends(get_clock)]
IdsDep = Annotated[IdGenerator, Depends(get_ids)]


def get_auth_service(
    db: DbDep, settings: SettingsDep, clock: ClockDep, ids: IdsDep
) -> AuthService:
    return AuthService(db, settings, clock, ids)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


@dataclass(frozen=True, slots=True)
class Principal:
    user: User
    organization_id: uuid.UUID
    role: Role
    session_id: uuid.UUID
    request_id: str


def current_principal(
    request: Request,
    auth: AuthServiceDep,
    settings: SettingsDep,
) -> Principal:
    """Authentication + CSRF for mutating methods. Raises AppError subclasses."""
    raw_token = request.cookies.get(SESSION_COOKIE)
    resolved = auth.resolve(raw_token)

    if request.method in MUTATING_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin_allowed(origin, settings.allowed_origins):
            raise CsrfError("Request origin not allowed.")
        header_token = request.headers.get(CSRF_HEADER)
        if not header_token or not constant_time_equals(
            header_token, resolved.session.csrf_secret
        ):
            raise CsrfError()

    return Principal(
        user=resolved.user,
        organization_id=resolved.session.active_organization_id,
        role=resolved.role,
        session_id=resolved.session.id,
        request_id=getattr(request.state, "request_id", ""),
    )


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_role(minimum: Role) -> Callable[..., Principal]:
    """Dependency factory: require at least `minimum` in the role hierarchy."""

    def checker(principal: PrincipalDep) -> Principal:
        if ROLE_ORDER[principal.role] < ROLE_ORDER[minimum]:
            raise ForbiddenRoleError()
        return principal

    return checker


def get_audit(
    request: Request,
    db: DbDep,
    clock: ClockDep,
    ids: IdsDep,
    principal: PrincipalDep,
) -> AuditRecorder:
    return AuditRecorder(
        db,
        clock,
        ids,
        organization_id=principal.organization_id,
        actor_user_id=principal.user.id,
        request_id=principal.request_id,
        ip_address=client_ip(request),
    )


AuditDep = Annotated[AuditRecorder, Depends(get_audit)]
