"""Authentication service: login, logout, session validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import UnauthenticatedError
from app.core.ids import IdGenerator
from app.core.security import (
    hash_session_token,
    new_csrf_token,
    new_session_token,
    verify_password,
)
from app.models.identity import Organization, OrganizationMembership, Role, Session, User

MAX_FAILED_LOGINS = 8
LOCKOUT = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_token: str  # goes into the HttpOnly cookie; never stored or logged
    csrf_token: str  # returned to the SPA, echoed in X-CSRF-Token
    user: User
    organization: Organization
    role: Role


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session: Session
    user: User
    role: Role


class AuthService:
    def __init__(
        self, session: SaSession, settings: Settings, clock: Clock, ids: IdGenerator
    ) -> None:
        self._db = session
        self._settings = settings
        self._clock = clock
        self._ids = ids

    def login(
        self,
        email: str,
        password: str,
        *,
        ip_address: str | None,
        user_agent: str | None,
    ) -> LoginResult:
        """Constant-shape failure: every failure path raises the same error."""
        now = self._clock.now()
        user = self._db.execute(
            select(User).where(User.email == email.strip().lower())
        ).scalar_one_or_none()

        failure = UnauthenticatedError("Invalid email or password.")
        if user is None or not user.is_active or user.archived_at is not None:
            raise failure
        if user.locked_until is not None and user.locked_until > now:
            raise UnauthenticatedError("Account temporarily locked. Try again later.")
        if not verify_password(user.password_hash, password):
            user.failed_login_count += 1
            if user.failed_login_count >= MAX_FAILED_LOGINS:
                user.locked_until = now + LOCKOUT
                user.failed_login_count = 0
            user.updated_at = now
            raise failure

        membership = self._db.execute(
            select(OrganizationMembership, Organization)
            .join(Organization, OrganizationMembership.organization_id == Organization.id)
            .where(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.revoked_at.is_(None),
                Organization.archived_at.is_(None),
            )
            .order_by(OrganizationMembership.created_at)
        ).first()
        if membership is None:
            raise failure  # a user with no live membership cannot log in

        member, organization = membership

        user.failed_login_count = 0
        user.locked_until = None
        user.updated_at = now

        token = new_session_token()
        csrf = new_csrf_token()
        self._db.add(
            Session(
                id=self._ids.new_id(),
                user_id=user.id,
                active_organization_id=organization.id,
                token_sha256=hash_session_token(token),
                csrf_secret=csrf,
                ip_address=ip_address,
                user_agent=(user_agent or "")[:512] or None,
                created_at=now,
                last_seen_at=now,
                absolute_expires_at=now + timedelta(hours=self._settings.session_ttl_hours),
            )
        )
        return LoginResult(
            session_token=token,
            csrf_token=csrf,
            user=user,
            organization=organization,
            role=member.role,
        )

    def resolve(self, raw_token: str | None) -> AuthenticatedSession:
        """Cookie token -> live session + user + role, or UnauthenticatedError."""
        if not raw_token:
            raise UnauthenticatedError()
        now = self._clock.now()
        sess = self._db.execute(
            select(Session).where(Session.token_sha256 == hash_session_token(raw_token))
        ).scalar_one_or_none()
        if sess is None or sess.revoked_at is not None or sess.absolute_expires_at <= now:
            raise UnauthenticatedError()

        row = self._db.execute(
            select(User, OrganizationMembership)
            .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
            .where(
                User.id == sess.user_id,
                User.is_active.is_(True),
                User.archived_at.is_(None),
                OrganizationMembership.organization_id == sess.active_organization_id,
                OrganizationMembership.revoked_at.is_(None),
            )
        ).first()
        if row is None:
            raise UnauthenticatedError()
        user, membership = row

        sess.last_seen_at = now
        return AuthenticatedSession(session=sess, user=user, role=membership.role)

    def logout(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        sess = self._db.execute(
            select(Session).where(Session.token_sha256 == hash_session_token(raw_token))
        ).scalar_one_or_none()
        if sess is not None and sess.revoked_at is None:
            sess.revoked_at = self._clock.now()
