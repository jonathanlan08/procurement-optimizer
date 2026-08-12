"""Supplier CRUD API integration tests: org isolation, role gates, optimistic
concurrency, duplicate-code handling, audit coverage, decimal wire format.

Fixture pattern mirrors test_auth.py's `account` fixture: a committed org +
user + membership, built directly against the migrated database."""

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
PASSWORD = "correct-horse-battery-2"


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
    """One org with one user per role in `roles`. Returns
    {"org_id": ..., "users": {role.value: {"email": ..., "user_id": ...}}}."""
    from sqlalchemy.orm import Session as SaSession

    from app.models.identity import Organization, OrganizationMembership, User

    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    with SaSession(migrated_engine) as s:
        org = Organization(
            id=uuid.uuid4(),
            slug=f"org-{suffix}",
            name=f"Org {suffix}",
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


def _supplier_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": f"SUP-{uuid.uuid4().hex[:8].upper()}",
        "name": "Acme Fasteners",
        "country_code": "US",
        "supported_currencies": ["USD", "EUR"],
        "standard_payment_terms": "Net 30",
        "standard_incoterm": "FOB",
        "typical_lead_time_days": 14,
        "capacity_units_per_month": "10000.000000",
        "default_moq": "500.000000",
        "is_active": True,
    }
    payload.update(overrides)
    return payload


class TestSupplierCrudRoundTrip:
    def test_analyst_full_crud_round_trip(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        create_resp = client.post("/api/v1/suppliers", json=_supplier_payload(), headers=headers)
        assert create_resp.status_code == 201, create_resp.text
        created = create_resp.json()
        supplier_id = created["id"]
        assert created["version"] == 1
        assert create_resp.headers["ETag"] == '"1"'

        get_resp = client.get(
            f"/api/v1/suppliers/{supplier_id}", headers={"Origin": ORIGIN}
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["code"] == created["code"]
        assert get_resp.headers["ETag"] == '"1"'

        update_resp = client.patch(
            f"/api/v1/suppliers/{supplier_id}",
            json={"name": "Acme Fasteners Updated", "typical_lead_time_days": 21},
            headers={**headers, "If-Match": '"1"'},
        )
        assert update_resp.status_code == 200, update_resp.text
        updated = update_resp.json()
        assert updated["name"] == "Acme Fasteners Updated"
        assert updated["typical_lead_time_days"] == 21
        assert updated["version"] == 2
        # fields not sent in the PATCH are preserved unchanged
        assert updated["code"] == created["code"]
        assert updated["standard_incoterm"] == created["standard_incoterm"]

        list_resp = client.get("/api/v1/suppliers", headers={"Origin": ORIGIN})
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert any(item["id"] == supplier_id for item in body["items"])
        assert body["page"]["limit"] == 50
        assert body["page"]["offset"] == 0
        assert body["page"]["total"] >= 1

        # analyst-owned lifecycle continues under an administrator (archive is
        # O/A only per the contract's route table — see module docstring)
        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/archive",
            json={"reason": "consolidating vendor list"},
            headers=admin_headers,
        )
        assert archive_resp.status_code == 200, archive_resp.text
        assert archive_resp.json()["is_archived"] is True
        assert archive_resp.json()["archive_reason"] == "consolidating vendor list"

        unarchive_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/unarchive",
            json={"reason": "vendor reinstated"},
            headers=admin_headers,
        )
        assert unarchive_resp.status_code == 200, unarchive_resp.text
        assert unarchive_resp.json()["is_archived"] is False
        assert unarchive_resp.json()["archive_reason"] is None


    def test_patch_decimal_fields_round_trip(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        """Regression: PATCHing a `DecimalString` field 500'd — the service's
        `model_dump()` re-serialized the parsed Decimal back to its wire string
        (PlainSerializer's `when_used` defaults to "always"), and
        `quantize_qty()` then blew up on the str."""
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post("/api/v1/suppliers", json=_supplier_payload(), headers=headers)
        assert create_resp.status_code == 201, create_resp.text
        supplier_id = create_resp.json()["id"]

        update_resp = client.patch(
            f"/api/v1/suppliers/{supplier_id}",
            json={"default_moq": "100.5", "capacity_units_per_month": "2500"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert update_resp.status_code == 200, update_resp.text
        updated = update_resp.json()
        assert updated["default_moq"] == "100.500000"
        assert updated["capacity_units_per_month"] == "2500.000000"
        assert updated["version"] == 2


class TestOrgIsolation:
    def test_cross_org_access_is_404_for_every_mutation(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post(
            "/api/v1/suppliers", json=_supplier_payload(), headers=headers_a
        )
        assert create_resp.status_code == 201
        supplier_id = create_resp.json()["id"]

        analyst_b_headers = _headers(_login_as(client, org_b, Role.ANALYST))

        get_resp = client.get(
            f"/api/v1/suppliers/{supplier_id}", headers={"Origin": ORIGIN}
        )
        assert get_resp.status_code == 404
        assert get_resp.json()["error"]["code"] == "not_found"

        patch_resp = client.patch(
            f"/api/v1/suppliers/{supplier_id}",
            json={"name": "Hijacked"},
            headers={**analyst_b_headers, "If-Match": '"1"'},
        )
        assert patch_resp.status_code == 404
        assert patch_resp.json()["error"]["code"] == "not_found"

        admin_b_headers = _headers(_login_as(client, org_b, Role.ADMINISTRATOR))

        archive_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/archive",
            json={"reason": "cross-org attempt"},
            headers=admin_b_headers,
        )
        assert archive_resp.status_code == 404
        assert archive_resp.json()["error"]["code"] == "not_found"

        unarchive_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/unarchive",
            json={"reason": "cross-org attempt"},
            headers=admin_b_headers,
        )
        assert unarchive_resp.status_code == 404
        assert unarchive_resp.json()["error"]["code"] == "not_found"

        # sanity: org B's own list never contains org A's supplier
        list_resp = client.get("/api/v1/suppliers", headers={"Origin": ORIGIN})
        ids = [item["id"] for item in list_resp.json()["items"]]
        assert supplier_id not in ids


class TestViewerReadOnly:
    def test_viewer_can_read_but_not_mutate(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post(
            "/api/v1/suppliers", json=_supplier_payload(), headers=headers_a
        )
        supplier_id = create_resp.json()["id"]

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))

        get_resp = client.get(
            f"/api/v1/suppliers/{supplier_id}", headers={"Origin": ORIGIN}
        )
        assert get_resp.status_code == 200

        list_resp = client.get("/api/v1/suppliers", headers={"Origin": ORIGIN})
        assert list_resp.status_code == 200

        create_attempt = client.post(
            "/api/v1/suppliers", json=_supplier_payload(), headers=viewer_headers
        )
        assert create_attempt.status_code == 403
        assert create_attempt.json()["error"]["code"] == "forbidden_role"

        update_attempt = client.patch(
            f"/api/v1/suppliers/{supplier_id}",
            json={"name": "nope"},
            headers={**viewer_headers, "If-Match": '"1"'},
        )
        assert update_attempt.status_code == 403
        assert update_attempt.json()["error"]["code"] == "forbidden_role"

        archive_attempt = client.post(
            f"/api/v1/suppliers/{supplier_id}/archive",
            json={"reason": "nope"},
            headers=viewer_headers,
        )
        assert archive_attempt.status_code == 403
        assert archive_attempt.json()["error"]["code"] == "forbidden_role"

    def test_analyst_cannot_archive(self, client: TestClient, org_a: dict[str, Any]) -> None:
        # archive/unarchive are O/A only per the contract's route table, so an
        # analyst — who may create/read/update — is still blocked here
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post(
            "/api/v1/suppliers", json=_supplier_payload(), headers=headers_a
        )
        supplier_id = create_resp.json()["id"]

        archive_attempt = client.post(
            f"/api/v1/suppliers/{supplier_id}/archive",
            json={"reason": "nope"},
            headers=headers_a,
        )
        assert archive_attempt.status_code == 403
        assert archive_attempt.json()["error"]["code"] == "forbidden_role"


class TestDuplicateCode:
    def test_duplicate_active_code_conflicts_then_reusable_after_archive(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        code = f"DUP-{uuid.uuid4().hex[:8].upper()}"

        first = client.post(
            "/api/v1/suppliers", json=_supplier_payload(code=code), headers=analyst_headers
        )
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]

        dup = client.post(
            "/api/v1/suppliers",
            json=_supplier_payload(code=code.lower()),
            headers=analyst_headers,
        )
        assert dup.status_code == 409, dup.text
        assert dup.json()["error"]["code"] == "conflict_duplicate"

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/suppliers/{first_id}/archive",
            json={"reason": "consolidating"},
            headers=admin_headers,
        )
        assert archive_resp.status_code == 200

        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        second = client.post(
            "/api/v1/suppliers", json=_supplier_payload(code=code), headers=analyst_headers
        )
        assert second.status_code == 201, second.text
        assert second.json()["id"] != first_id

    def test_double_archive_is_conflict_state(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post(
            "/api/v1/suppliers", json=_supplier_payload(), headers=analyst_headers
        )
        supplier_id = create_resp.json()["id"]

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        first_archive = client.post(
            f"/api/v1/suppliers/{supplier_id}/archive",
            json={"reason": "first"},
            headers=admin_headers,
        )
        assert first_archive.status_code == 200

        second_archive = client.post(
            f"/api/v1/suppliers/{supplier_id}/archive",
            json={"reason": "second"},
            headers=admin_headers,
        )
        assert second_archive.status_code == 409
        assert second_archive.json()["error"]["code"] == "conflict_state"


class TestVersionConflict:
    def test_stale_if_match_returns_conflict_version(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post("/api/v1/suppliers", json=_supplier_payload(), headers=headers)
        supplier_id = create_resp.json()["id"]

        first_update = client.patch(
            f"/api/v1/suppliers/{supplier_id}",
            json={"name": "First Update"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert first_update.status_code == 200
        assert first_update.json()["version"] == 2

        stale_update = client.patch(
            f"/api/v1/suppliers/{supplier_id}",
            json={"name": "Second Update"},
            headers={**headers, "If-Match": '"1"'},  # stale: current version is now 2
        )
        assert stale_update.status_code == 409, stale_update.text
        assert stale_update.json()["error"]["code"] == "conflict_version"


class TestAuditTrail:
    def test_each_mutation_writes_exactly_one_audit_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post("/api/v1/suppliers", json=_supplier_payload(), headers=headers)
        supplier_id = create_resp.json()["id"]

        update_resp = client.patch(
            f"/api/v1/suppliers/{supplier_id}",
            json={"name": "Renamed"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert update_resp.status_code == 200

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/archive",
            json={"reason": "test"},
            headers=admin_headers,
        )
        assert archive_resp.status_code == 200

        unarchive_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/unarchive",
            json={"reason": "test"},
            headers=admin_headers,
        )
        assert unarchive_resp.status_code == 200

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'supplier' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": supplier_id},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("supplier.created") == 1
        assert counts.get("supplier.updated") == 1
        assert counts.get("supplier.archived") == 1
        assert counts.get("supplier.unarchived") == 1

        with migrated_engine.connect() as conn:
            after_state = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE entity_type = 'supplier' AND entity_id = :id"
                    " AND event_type = 'supplier.created'"
                ),
                {"id": supplier_id},
            ).scalar_one()
        assert after_state["code"] == create_resp.json()["code"]


class TestDecimalRoundTrip:
    def test_decimal_fields_are_strings_on_the_wire(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        create_resp = client.post(
            "/api/v1/suppliers",
            json=_supplier_payload(capacity_units_per_month="12345.5", default_moq="10"),
            headers=headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        body = create_resp.json()
        assert body["capacity_units_per_month"] == "12345.500000"
        assert body["default_moq"] == "10.000000"
        assert isinstance(body["capacity_units_per_month"], str)
        assert isinstance(body["default_moq"], str)

        get_resp = client.get(
            f"/api/v1/suppliers/{body['id']}", headers={"Origin": ORIGIN}
        )
        assert get_resp.json()["capacity_units_per_month"] == "12345.500000"
        assert get_resp.json()["default_moq"] == "10.000000"

    def test_negative_decimal_is_rejected(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/suppliers",
            json=_supplier_payload(default_moq="-1"),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_scale_exceeded_is_rejected(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/suppliers",
            json=_supplier_payload(default_moq="1.1234567"),  # 7dp > QTY_SCALE (6dp)
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
