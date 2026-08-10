"""Supplier sub-resource (contacts, performance) CRUD API integration tests:
org isolation, role gates, parent-supplier existence, period validation,
audit coverage, decimal wire format.

Fixture pattern mirrors test_suppliers_api.py: a committed org + user +
membership, built directly against the migrated database."""

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
        "is_active": True,
    }
    payload.update(overrides)
    return payload


def _contact_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Jane Buyer",
        "email": "jane@example.com",
        "phone": "+1-555-0100",
        "role_title": "Account Manager",
        "is_primary": True,
    }
    payload.update(overrides)
    return payload


def _performance_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "on_time_delivery_rate": "0.950000",
        "defect_rate": "0.010000",
        "quality_score": "4.500000",
        "orders_count": 42,
        "source": "user_entered",
        "notes": "Q1 review",
    }
    payload.update(overrides)
    return payload


def _create_supplier(client: TestClient, headers: dict[str, str], **overrides: Any) -> str:
    resp = client.post(
        "/api/v1/suppliers", json=_supplier_payload(**overrides), headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]  # type: ignore[no-any-return]


class TestSupplierContactCrudRoundTrip:
    def test_analyst_full_crud_round_trip(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        create_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/contacts",
            json=_contact_payload(),
            headers=headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        created = create_resp.json()
        contact_id = created["id"]
        assert created["supplier_id"] == supplier_id
        assert created["name"] == "Jane Buyer"
        assert created["is_archived"] is False

        list_resp = client.get(
            f"/api/v1/suppliers/{supplier_id}/contacts", headers={"Origin": ORIGIN}
        )
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert any(item["id"] == contact_id for item in body["items"])
        assert body["page"]["limit"] == 50
        assert body["page"]["offset"] == 0
        assert body["page"]["total"] >= 1

        update_resp = client.patch(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}",
            json={"name": "Jane Updated", "is_primary": False},
            headers=headers,
        )
        assert update_resp.status_code == 200, update_resp.text
        updated = update_resp.json()
        assert updated["name"] == "Jane Updated"
        assert updated["is_primary"] is False
        # fields not sent in the PATCH are preserved unchanged
        assert updated["email"] == created["email"]

        archive_resp = client.delete(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}", headers=headers
        )
        assert archive_resp.status_code == 200, archive_resp.text
        assert archive_resp.json()["is_archived"] is True
        assert archive_resp.json()["archived_at"] is not None

        # archived contacts drop out of the default (non-include_archived) list
        list_after = client.get(
            f"/api/v1/suppliers/{supplier_id}/contacts", headers={"Origin": ORIGIN}
        )
        ids_after = [item["id"] for item in list_after.json()["items"]]
        assert contact_id not in ids_after

        list_incl = client.get(
            f"/api/v1/suppliers/{supplier_id}/contacts?include_archived=true",
            headers={"Origin": ORIGIN},
        )
        ids_incl = [item["id"] for item in list_incl.json()["items"]]
        assert contact_id in ids_incl


class TestSupplierContactOrgIsolation:
    def test_cross_org_access_is_404_for_every_operation(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers_a)
        create_contact = client.post(
            f"/api/v1/suppliers/{supplier_id}/contacts",
            json=_contact_payload(),
            headers=headers_a,
        )
        contact_id = create_contact.json()["id"]

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))

        list_resp_b = client.get(
            f"/api/v1/suppliers/{supplier_id}/contacts", headers=headers_b
        )
        assert list_resp_b.status_code == 404
        assert list_resp_b.json()["error"]["code"] == "not_found"

        create_resp_b = client.post(
            f"/api/v1/suppliers/{supplier_id}/contacts",
            json=_contact_payload(),
            headers=headers_b,
        )
        assert create_resp_b.status_code == 404
        assert create_resp_b.json()["error"]["code"] == "not_found"

        update_resp_b = client.patch(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}",
            json={"name": "Hijacked"},
            headers=headers_b,
        )
        assert update_resp_b.status_code == 404
        assert update_resp_b.json()["error"]["code"] == "not_found"

        archive_resp_b = client.delete(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}", headers=headers_b
        )
        assert archive_resp_b.status_code == 404
        assert archive_resp_b.json()["error"]["code"] == "not_found"

        # sanity: org A's own view is unaffected by org B's failed attempts
        # (re-login as org A since the shared client's session cookie now
        # points at org B, per the last _login_as call above)
        headers_a_again = _headers(_login_as(client, org_a, Role.ANALYST))
        list_resp_a = client.get(
            f"/api/v1/suppliers/{supplier_id}/contacts", headers=headers_a_again
        )
        assert list_resp_a.status_code == 200
        assert any(item["id"] == contact_id for item in list_resp_a.json()["items"])


class TestSupplierContactRoleEnforcement:
    def test_viewer_can_list_but_not_mutate(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers_a)
        create_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/contacts",
            json=_contact_payload(),
            headers=headers_a,
        )
        contact_id = create_resp.json()["id"]

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))

        list_resp = client.get(
            f"/api/v1/suppliers/{supplier_id}/contacts", headers={"Origin": ORIGIN}
        )
        assert list_resp.status_code == 200

        create_attempt = client.post(
            f"/api/v1/suppliers/{supplier_id}/contacts",
            json=_contact_payload(),
            headers=viewer_headers,
        )
        assert create_attempt.status_code == 403
        assert create_attempt.json()["error"]["code"] == "forbidden_role"

        update_attempt = client.patch(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}",
            json={"name": "nope"},
            headers=viewer_headers,
        )
        assert update_attempt.status_code == 403
        assert update_attempt.json()["error"]["code"] == "forbidden_role"

        archive_attempt = client.delete(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}", headers=viewer_headers
        )
        assert archive_attempt.status_code == 403
        assert archive_attempt.json()["error"]["code"] == "forbidden_role"

    def test_analyst_can_archive_contact(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        # unlike supplier archive (admin+ only), contact delete=archive is
        # analyst+ per the contract's route table (§4.4: "O A N")
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers_a)
        create_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/contacts",
            json=_contact_payload(),
            headers=headers_a,
        )
        contact_id = create_resp.json()["id"]

        archive_resp = client.delete(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}", headers=headers_a
        )
        assert archive_resp.status_code == 200, archive_resp.text
        assert archive_resp.json()["is_archived"] is True


class TestSupplierContactParentMissing:
    def test_missing_supplier_is_404(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        missing_id = str(uuid.uuid4())

        create_resp = client.post(
            f"/api/v1/suppliers/{missing_id}/contacts",
            json=_contact_payload(),
            headers=headers,
        )
        assert create_resp.status_code == 404
        assert create_resp.json()["error"]["code"] == "not_found"

        list_resp = client.get(
            f"/api/v1/suppliers/{missing_id}/contacts", headers={"Origin": ORIGIN}
        )
        assert list_resp.status_code == 404
        assert list_resp.json()["error"]["code"] == "not_found"


class TestSupplierPerformanceCrudRoundTrip:
    def test_analyst_create_and_list(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        create_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(),
            headers=headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        created = create_resp.json()
        assert created["supplier_id"] == supplier_id
        assert created["on_time_delivery_rate"] == "0.950000"
        assert created["defect_rate"] == "0.010000"
        assert created["quality_score"] == "4.500000"
        assert created["orders_count"] == 42
        assert created["source"] == "user_entered"
        assert isinstance(created["on_time_delivery_rate"], str)

        list_resp = client.get(
            f"/api/v1/suppliers/{supplier_id}/performance", headers={"Origin": ORIGIN}
        )
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert any(item["id"] == created["id"] for item in body["items"])
        assert body["page"]["total"] >= 1


class TestSupplierPerformanceOrgIsolation:
    def test_cross_org_access_is_404(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers_a)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))

        list_resp_b = client.get(
            f"/api/v1/suppliers/{supplier_id}/performance", headers=headers_b
        )
        assert list_resp_b.status_code == 404
        assert list_resp_b.json()["error"]["code"] == "not_found"

        create_resp_b = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(),
            headers=headers_b,
        )
        assert create_resp_b.status_code == 404
        assert create_resp_b.json()["error"]["code"] == "not_found"


class TestSupplierPerformanceRoleEnforcement:
    def test_viewer_can_list_but_not_create(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers_a)

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))

        list_resp = client.get(
            f"/api/v1/suppliers/{supplier_id}/performance", headers={"Origin": ORIGIN}
        )
        assert list_resp.status_code == 200

        create_attempt = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(),
            headers=viewer_headers,
        )
        assert create_attempt.status_code == 403
        assert create_attempt.json()["error"]["code"] == "forbidden_role"


class TestSupplierPerformanceParentMissing:
    def test_missing_supplier_is_404(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        missing_id = str(uuid.uuid4())

        create_resp = client.post(
            f"/api/v1/suppliers/{missing_id}/performance",
            json=_performance_payload(),
            headers=headers,
        )
        assert create_resp.status_code == 404
        assert create_resp.json()["error"]["code"] == "not_found"


class TestSupplierPerformanceValidation:
    def test_period_end_before_period_start_is_422(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(period_start="2026-03-31", period_end="2026-01-01"),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    @pytest.mark.parametrize("field", ["on_time_delivery_rate", "defect_rate"])
    def test_rate_above_one_is_422(
        self, client: TestClient, org_a: dict[str, Any], field: str
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(**{field: "1.500000"}),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    @pytest.mark.parametrize("field", ["on_time_delivery_rate", "defect_rate", "quality_score"])
    def test_negative_rate_is_422(
        self, client: TestClient, org_a: dict[str, Any], field: str
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(**{field: "-0.100000"}),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_quality_score_above_one_is_allowed(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        # quality_score has no contract-stated upper bound; only the model's
        # non-negativity check applies (see schemas/supplier_performance.py)
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(quality_score="9.900000"),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["quality_score"] == "9.900000"

    def test_invalid_source_is_422(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(source="made_up_source"),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_scale_exceeded_rate_is_422(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            # 7dp > RATIO_SCALE (6dp)
            json=_performance_payload(on_time_delivery_rate="0.1234567"),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"


class TestSupplierPerformanceDecimalRoundTrip:
    def test_decimal_fields_are_strings_on_the_wire(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        create_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(
                on_time_delivery_rate="0.5", defect_rate="1", quality_score="0"
            ),
            headers=headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        body = create_resp.json()
        assert body["on_time_delivery_rate"] == "0.500000"
        assert body["defect_rate"] == "1.000000"
        assert body["quality_score"] == "0.000000"
        assert isinstance(body["on_time_delivery_rate"], str)


class TestAuditTrail:
    def test_contact_mutations_write_audit_events(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        create_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/contacts",
            json=_contact_payload(),
            headers=headers,
        )
        contact_id = create_resp.json()["id"]

        update_resp = client.patch(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}",
            json={"name": "Renamed"},
            headers=headers,
        )
        assert update_resp.status_code == 200

        archive_resp = client.delete(
            f"/api/v1/suppliers/{supplier_id}/contacts/{contact_id}", headers=headers
        )
        assert archive_resp.status_code == 200

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'supplier_contact' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": contact_id},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("supplier_contact.created") == 1
        assert counts.get("supplier_contact.updated") == 1
        assert counts.get("supplier_contact.archived") == 1

    def test_performance_create_writes_audit_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_id = _create_supplier(client, headers)

        create_resp = client.post(
            f"/api/v1/suppliers/{supplier_id}/performance",
            json=_performance_payload(),
            headers=headers,
        )
        record_id = create_resp.json()["id"]

        with migrated_engine.connect() as conn:
            after_state = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE entity_type = 'supplier_performance_record' AND entity_id = :id"
                    " AND event_type = 'supplier_performance.recorded'"
                ),
                {"id": record_id},
            ).scalar_one()
        assert after_state["source"] == "user_entered"
        assert after_state["on_time_delivery_rate"] == create_resp.json()["on_time_delivery_rate"]
