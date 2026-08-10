"""Exchange rate API integration tests (docs/planning/03-api-contract.md
§4.13): manual override wins over the synthetic fixture, as-of date
selection, cross-org isolation, validation, audit coverage, decimal wire
format.

Fixture pattern mirrors test_suppliers_api.py: a committed org + one user per
role, built directly against the migrated database."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy import text as sqltext

from app.core.config import Environment, Settings
from app.core.security import hash_password
from app.main import create_app
from app.models.identity import Role

ORIGIN = "http://localhost:5173"
PASSWORD = "correct-horse-battery-fx"


@pytest.fixture(scope="module")
def client(database_url: str, migrated_engine: Engine) -> Generator[TestClient, None, None]:
    settings = Settings(
        environment=Environment.TEST,
        database_url=database_url,
        allowed_origins=[ORIGIN],
        rate_limit_auth_per_minute=100_000,
        rate_limit_per_minute=100_000,
    )
    app = create_app(settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield c
    app.state.engine.dispose()


def _make_org_with_members(migrated_engine: Engine, roles: list[Role]) -> dict[str, Any]:
    from sqlalchemy.orm import Session as SaSession

    from app.models.identity import Organization, OrganizationMembership, User

    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    with SaSession(migrated_engine) as s:
        org = Organization(
            id=uuid.uuid4(),
            slug=f"fx-org-{suffix}",
            name=f"FX Org {suffix}",
            base_currency="USD",
            is_demo=False,
            created_at=now,
            updated_at=now,
        )
        s.add(org)
        users: dict[str, dict[str, str]] = {}
        for role in roles:
            email = f"{role.value}-{suffix}-{uuid.uuid4().hex[:6]}@example.com"
            user = User(
                id=uuid.uuid4(),
                email=email,
                password_hash=hash_password(PASSWORD),
                full_name=f"{role.value.title()} User",
                created_at=now,
                updated_at=now,
            )
            s.add(user)
            s.add(
                OrganizationMembership(
                    id=uuid.uuid4(),
                    organization_id=org.id,
                    user_id=user.id,
                    role=role,
                    accepted_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            users[role.value] = {"email": email, "user_id": str(user.id)}
        s.commit()
        return {"org_id": str(org.id), "users": users}


@pytest.fixture()
def org_a(migrated_engine: Engine) -> dict[str, Any]:
    return _make_org_with_members(
        migrated_engine, [Role.ANALYST, Role.ADMINISTRATOR, Role.VIEWER]
    )


@pytest.fixture()
def org_b(migrated_engine: Engine) -> dict[str, Any]:
    return _make_org_with_members(
        migrated_engine, [Role.ANALYST, Role.ADMINISTRATOR, Role.VIEWER]
    )


def _login_as(client: TestClient, org: dict[str, Any], role: Role) -> dict[str, str]:
    creds = org["users"][role.value]
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _headers(login_body: dict[str, str]) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": login_body["csrf_token"]}


def _override_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "base_currency": "USD",
        "quote_currency": "EUR",
        "rate": "0.850000000000",
        "effective_date": "2026-03-01",
        "override_reason": "negotiated treasury rate for Q1 hedging",
    }
    payload.update(overrides)
    return payload


class TestEffectiveLookup:
    def test_falls_back_to_synthetic_fixture_when_no_override_exists(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "JPY", "as_of": "2026-05-01"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "synthetic_fixture"
        assert body["is_manual_override"] is False
        assert body["exchange_rate_id"] is None
        assert body["rate"] == "149.500000000000"
        assert isinstance(body["rate"], str)

    def test_manual_override_wins_over_synthetic_fixture(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(
                base_currency="USD", quote_currency="EUR", effective_date="2026-03-01"
            ),
            headers=analyst_headers,
        )
        assert create_resp.status_code == 201, create_resp.text

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "EUR", "as_of": "2026-03-01"},
            headers=viewer_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "manual"
        assert body["is_manual_override"] is True
        assert body["rate"] == "0.850000000000"
        assert body["exchange_rate_id"] is not None
        # the synthetic fixture would have said 0.920000000000 — the override
        # must have actually replaced it, not merely coexisted
        assert body["rate"] != "0.920000000000"

    def test_as_of_before_any_override_falls_back_to_synthetic(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(
                base_currency="USD", quote_currency="GBP", effective_date="2026-06-01"
            ),
            headers=analyst_headers,
        )

        resp = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "GBP", "as_of": "2026-01-01"},
            headers=analyst_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["source"] == "synthetic_fixture"

    def test_as_of_date_selection_picks_latest_row_on_or_before(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        for effective_date, rate in (
            ("2026-01-01", "17.000000000000"),
            ("2026-04-01", "17.500000000000"),
            ("2026-08-01", "18.000000000000"),
        ):
            resp = client.post(
                "/api/v1/exchange-rates",
                json=_override_payload(
                    base_currency="USD",
                    quote_currency="MXN",
                    effective_date=effective_date,
                    rate=rate,
                ),
                headers=analyst_headers,
            )
            assert resp.status_code == 201, resp.text

        # exactly between the first two rows -> the later-but-still-earlier row wins
        mid = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "MXN", "as_of": "2026-06-01"},
            headers=analyst_headers,
        ).json()
        assert mid["rate"] == "17.500000000000"
        assert mid["effective_date"] == "2026-04-01"

        # before the earliest row -> synthetic fallback, not the earliest row
        early = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "MXN", "as_of": "2025-12-01"},
            headers=analyst_headers,
        ).json()
        assert early["source"] == "synthetic_fixture"

        # after every row -> the most recent one
        late = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "MXN", "as_of": "2026-12-01"},
            headers=analyst_headers,
        ).json()
        assert late["rate"] == "18.000000000000"

    def test_base_and_quote_without_as_of_is_a_list_not_an_effective_lookup(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        # base+quote alone (no as_of) is the "history for this pair" listing
        # need, distinct from an effective-rate lookup — see api/v1/fx.py
        # module docstring for why all three params are required together.
        headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.get(
            "/api/v1/exchange-rates", params={"base": "USD", "quote": "CNY"}, headers=headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body and "page" in body

    def test_unknown_currency_pair_is_404(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "ZZZ", "as_of": "2026-03-01"},
            headers=headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


class TestListing:
    def test_lists_only_stored_override_rows_for_this_org(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(base_currency="USD", quote_currency="INR"),
            headers=analyst_headers,
        )

        resp = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "INR"},
            headers=analyst_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "items" in body and "page" in body
        assert len(body["items"]) == 1
        assert body["items"][0]["source"] == "manual"


class TestCrossOrgIsolation:
    def test_org_b_never_sees_org_a_overrides_in_list(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        analyst_a = _headers(_login_as(client, org_a, Role.ANALYST))
        client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(base_currency="USD", quote_currency="EUR"),
            headers=analyst_a,
        )

        viewer_b = _headers(_login_as(client, org_b, Role.VIEWER))
        listing = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "EUR"},
            headers=viewer_b,
        ).json()
        assert listing["items"] == []

    def test_org_b_effective_lookup_falls_back_to_synthetic_not_org_a_override(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        analyst_a = _headers(_login_as(client, org_a, Role.ANALYST))
        client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(
                base_currency="USD",
                quote_currency="EUR",
                rate="0.010000000000",  # implausible value: unmistakable if leaked
                effective_date="2026-03-01",
            ),
            headers=analyst_a,
        )

        viewer_b = _headers(_login_as(client, org_b, Role.VIEWER))
        effective = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "EUR", "as_of": "2026-03-01"},
            headers=viewer_b,
        ).json()
        assert effective["source"] == "synthetic_fixture"
        assert effective["rate"] != "0.010000000000"


class TestValidation:
    def test_override_without_reason_is_422(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        payload = _override_payload()
        del payload["override_reason"]
        resp = client.post("/api/v1/exchange-rates", json=payload, headers=headers)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_override_with_blank_reason_is_422(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(override_reason="   "),
            headers=headers,
        )
        assert resp.status_code == 422

    def test_same_base_and_quote_is_422(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(base_currency="USD", quote_currency="USD"),
            headers=headers,
        )
        assert resp.status_code == 422


class TestRoleGating:
    def test_viewer_can_read_but_not_create_override(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.VIEWER))
        read_resp = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "JPY", "as_of": "2026-05-01"},
            headers=headers,
        )
        assert read_resp.status_code == 200

        write_resp = client.post(
            "/api/v1/exchange-rates", json=_override_payload(), headers=headers
        )
        assert write_resp.status_code == 403
        assert write_resp.json()["error"]["code"] == "forbidden_role"

    def test_analyst_cannot_refresh(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post("/api/v1/exchange-rates/refresh", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden_role"

    def test_administrator_can_refresh(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        resp = client.post("/api/v1/exchange-rates/refresh", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"source": "synthetic_fixture", "simulated": True}


class TestAuditTrail:
    def test_override_writes_exactly_one_audit_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(base_currency="USD", quote_currency="MXN"),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        rate_id = resp.json()["id"]

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'exchange_rate' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": rate_id},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("fx.override_set") == 1


class TestDecimalWireFormat:
    def test_rate_is_a_json_string_everywhere(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post(
            "/api/v1/exchange-rates",
            json=_override_payload(base_currency="USD", quote_currency="GBP"),
            headers=headers,
        )
        assert isinstance(create_resp.json()["rate"], str)

        list_resp = client.get(
            "/api/v1/exchange-rates", params={"base": "USD", "quote": "GBP"}, headers=headers
        )
        assert isinstance(list_resp.json()["items"][0]["rate"], str)

        effective_resp = client.get(
            "/api/v1/exchange-rates",
            params={"base": "USD", "quote": "GBP", "as_of": "2026-03-01"},
            headers=headers,
        )
        assert isinstance(effective_resp.json()["rate"], str)
