"""Authentication routes. All DB-touching routes are sync `def` by policy."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import (
    SESSION_COOKIE,
    AuthServiceDep,
    PrincipalDep,
    SettingsDep,
    client_ip,
)

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
) -> SessionInfo:
    result = auth.login(
        body.email,
        body.password,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
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
) -> dict[str, bool]:
    auth.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(principal: PrincipalDep, settings: SettingsDep) -> SessionInfo:
    return SessionInfo(
        user_id=str(principal.user.id),
        email=principal.user.email,
        full_name=principal.user.full_name,
        organization_id=str(principal.organization_id),
        organization_name="",  # filled by the org routes; /me stays cheap
        organization_slug="",
        role=principal.role.value,
        demo_mode=settings.demo_mode,
    )
