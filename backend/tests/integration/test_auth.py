"""Auth flow integration tests: login/logout/me, CSRF double-submit, Origin
allowlist, envelope shape. Runs against the pgserver-migrated database."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import Environment, Settings
from app.core.security import hash_password
from app.main import create_app

ORIGIN = "http://localhost:5173"
PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="module")
def client(database_url: str, migrated_engine: Engine) -> Generator[TestClient, None, None]:
    settings = Settings(
        environment=Environment.TEST,
        database_url=database_url,
        allowed_origins=[ORIGIN],
        rate_limit_auth_per_minute=1000,  # rate limiting has its own test
    )
    app = create_app(settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield c
    app.state.engine.dispose()


@pytest.fixture()
def account(migrated_engine: Engine) -> dict[str, str]:
    """A committed org + user + membership with unique identifiers."""
    from sqlalchemy.orm import Session as SaSession

    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    email = f"analyst-{suffix}@example.com"
    with SaSession(migrated_engine) as s:
        from app.models.identity import Organization, OrganizationMembership, Role, User

        org = Organization(
            id=uuid.uuid4(),
            slug=f"acme-{suffix}",
            name="Acme Fabrication",
            base_currency="USD",
            is_demo=False,
            created_at=now,
            updated_at=now,
        )
        user = User(
            id=uuid.uuid4(),
            email=email,
            password_hash=hash_password(PASSWORD),
            full_name="Ada Analyst",
            created_at=now,
            updated_at=now,
        )
        s.add_all([org, user])
        s.add(
            OrganizationMembership(
                id=uuid.uuid4(),
                organization_id=org.id,
                user_id=user.id,
                role=Role.ANALYST,
                accepted_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
        return {"email": email, "org_id": str(org.id)}


def _login(client: TestClient, account: dict[str, str]) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestLogin:
    def test_login_success_sets_cookie_and_csrf(self, client: TestClient, account) -> None:
        body = _login(client, account)
        assert body["role"] == "analyst"
        assert body["organization_id"] == account["org_id"]
        assert body["csrf_token"]
        assert "po_session" in client.cookies

    def test_login_wrong_password_is_401_with_envelope(
        self, client: TestClient, account
    ) -> None:
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": "wrong"},
            headers={"Origin": ORIGIN},
        )
        assert resp.status_code == 401
        err = resp.json()["error"]
        assert err["code"] == "unauthenticated"
        assert err["request_id"]
        assert "wrong" not in err["message"]

    def test_unknown_email_same_error_as_wrong_password(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "whatever"},
            headers={"Origin": ORIGIN},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthenticated"

    def test_login_without_origin_is_rejected(self, client: TestClient, account) -> None:
        # login has no session yet, so the double-submit token cannot apply;
        # the Origin allowlist (middleware) is what blocks login-CSRF
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": PASSWORD},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_failed"

    def test_login_from_the_servers_own_origin_is_allowed(
        self, client: TestClient, account
    ) -> None:
        """Single-origin deploys (app/api/spa.py) serve the SPA from this same
        host, so the browser sends our own origin. Browsers set Origin and page
        JS cannot forge it, so this is genuinely same-origin and must work even
        though the deployed URL is not in PO_ALLOWED_ORIGINS - otherwise a
        correct password reads as a 403 until that variable is set by hand."""
        own_origin = "http://testserver"  # TestClient's base_url
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": PASSWORD},
            headers={"Origin": own_origin},
        )
        assert resp.status_code == 200, resp.text

    def test_login_from_a_foreign_origin_is_still_rejected(
        self, client: TestClient, account
    ) -> None:
        """The same-origin allowance must not become a blanket bypass."""
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": PASSWORD},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_failed"


class TestSessionUse:
    def test_me_without_cookie_is_401(self, client: TestClient) -> None:
        client.cookies.clear()
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_me_with_cookie(self, client: TestClient, account) -> None:
        _login(client, account)
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["email"] == account["email"]

    def test_mutation_without_csrf_header_fails(self, client: TestClient, account) -> None:
        _login(client, account)
        resp = client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_failed"

    def test_mutation_with_bad_origin_fails(self, client: TestClient, account) -> None:
        body = _login(client, account)
        resp = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "https://evil.example", "X-CSRF-Token": body["csrf_token"]},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_failed"

    def test_logout_with_csrf_revokes_session(self, client: TestClient, account) -> None:
        body = _login(client, account)
        resp = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": body["csrf_token"]},
        )
        assert resp.status_code == 200
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_security_headers_present(self, client: TestClient) -> None:
        resp = client.get("/api/health")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Request-ID"]


class TestReviewFindings:
    """Regression tests for the Phase 1 independent-review HIGH findings."""

    def test_failed_login_counter_survives_rollback(
        self, client: TestClient, account, migrated_engine
    ) -> None:
        # finding #1: the counter is written in its own transaction, so it must
        # persist even though the login request's transaction rolls back
        from sqlalchemy import text as sqltext

        for _ in range(3):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": account["email"], "password": "wrong"},
                headers={"Origin": ORIGIN},
            )
            assert resp.status_code == 401
        with migrated_engine.connect() as conn:
            count = conn.execute(
                sqltext("SELECT failed_login_count FROM users WHERE email = :e"),
                {"e": account["email"]},
            ).scalar_one()
        assert count == 3

    def test_lockout_engages_and_blocks_correct_password(
        self, client: TestClient, account
    ) -> None:
        from app.services.auth import MAX_FAILED_LOGINS

        for _ in range(MAX_FAILED_LOGINS):
            client.post(
                "/api/v1/auth/login",
                json={"email": account["email"], "password": "wrong"},
                headers={"Origin": ORIGIN},
            )
        # correct password now fails with the SAME generic error (no oracle)
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["message"] == "Invalid email or password."

    def test_me_returns_csrf_token_for_refresh_recovery(
        self, client: TestClient, account
    ) -> None:
        # finding #4: after a page refresh the SPA restores the CSRF token
        # from /me; simulate by discarding the login response's token
        _login(client, account)
        me = client.get("/api/v1/auth/me").json()
        assert me["csrf_token"]
        assert me["organization_name"]
        resp = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": me["csrf_token"]},
        )
        assert resp.status_code == 200

    def test_login_success_writes_audit_event(
        self, client: TestClient, account, migrated_engine
    ) -> None:
        from sqlalchemy import text as sqltext

        _login(client, account)
        with migrated_engine.connect() as conn:
            n = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events WHERE organization_id = :org"
                    " AND event_type = 'auth.login_succeeded'"
                ),
                {"org": account["org_id"]},
            ).scalar_one()
        assert n >= 1
