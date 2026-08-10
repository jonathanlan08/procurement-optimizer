"""Authentication routes. All DB-touching routes are sync `def` by policy."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.api.deps import (
    SESSION_COOKIE,
    AuthServiceDep,
    ClockDep,
    DbDep,
    IdsDep,
    PrincipalDep,
    SettingsDep,
    client_ip,
)
from app.models.identity import Organization
from app.models.identity import Session as DbSession
from app.services.audit import AuditRecorder

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=512)


class SessionInfo(BaseModel):
    user_id: str
    email: str
    full_name: str
    organization_id: str
    organization_name: str
    organization_slug: str
    role: str
    csrf_token: str | None = None
    demo_mode: bool


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    auth: AuthServiceDep,
    settings: SettingsDep,
    db: DbDep,
    clock: ClockDep,
    ids: IdsDep,
) -> SessionInfo:
    try:
        result = auth.login(
            body.email,
            body.password,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        # failed logins may have no organization, so they go to the security
        # log rather than the org-scoped audit table (documented deviation)
        logging.getLogger("app.security").warning(
            "login failed",
            extra={
                "event": "auth.login_failed",
                "request_id": getattr(request.state, "request_id", ""),
                "ip": client_ip(request) or "unknown",
            },
        )
        raise

    AuditRecorder(
        db,
        clock,
        ids,
        organization_id=result.organization.id,
        actor_user_id=result.user.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=client_ip(request),
    ).record(
        "auth.login_succeeded",
        entity_type="session",
        explanation=f"{result.user.email} signed in",
    )
    response.set_cookie(
        SESSION_COOKIE,
        result.session_token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return SessionInfo(
        user_id=str(result.user.id),
        email=result.user.email,
        full_name=result.user.full_name,
        organization_id=str(result.organization.id),
        organization_name=result.organization.name,
        organization_slug=result.organization.slug,
        role=result.role.value,
        csrf_token=result.csrf_token,
        demo_mode=settings.demo_mode,
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    auth: AuthServiceDep,
    principal: PrincipalDep,  # requires a valid session + CSRF to log out
    db: DbDep,
    clock: ClockDep,
    ids: IdsDep,
) -> dict[str, bool]:
    auth.logout(request.cookies.get(SESSION_COOKIE))
    AuditRecorder(
        db,
        clock,
        ids,
        organization_id=principal.organization_id,
        actor_user_id=principal.user.id,
        request_id=principal.request_id,
        ip_address=client_ip(request),
    ).record(
        "auth.logout",
        entity_type="session",
        entity_id=principal.session_id,
        explanation=f"{principal.user.email} signed out",
    )
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(db: DbDep, principal: PrincipalDep, settings: SettingsDep) -> SessionInfo:
    # csrf_token IS returned here: a page refresh keeps the session cookie but
    # loses the in-memory token, and this same-origin GET is how the SPA
    # restores it (double-submit tokens are readable by same-origin JS by design)
    sess = db.execute(
        select(DbSession).where(DbSession.id == principal.session_id)
    ).scalar_one()
    org = db.execute(
        select(Organization).where(Organization.id == principal.organization_id)
    ).scalar_one()
    return SessionInfo(
        user_id=str(principal.user.id),
        email=principal.user.email,
        full_name=principal.user.full_name,
        organization_id=str(principal.organization_id),
        organization_name=org.name,
        organization_slug=org.slug,
        role=principal.role.value,
        csrf_token=sess.csrf_secret,
        demo_mode=settings.demo_mode,
    )
