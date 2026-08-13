"""Audit-event read API integration tests (docs/planning/03-api-contract.md
§4.19, app/services/audit_read_service.py).

Fixture pattern mirrors test_briefs_api.py: a committed org + one user per
role, built directly against the migrated database, driven through the HTTP
API. Audit events used for these tests are produced as a SIDE EFFECT of
ordinary mutations (`supplier.created` from `POST /suppliers`, `rfq.created`
from `POST /rfqs`) rather than inserted directly - there is no audit-event
create endpoint (§4.19: "no write ... route"), so this is the only way to
populate real rows through the HTTP surface, and it exercises the same
`AuditRecorder` path production traffic would.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as SaSession

from app.core.config import Environment, Settings
from app.core.security import hash_password
from app.main import create_app
from app.models.identity import Role

ORIGIN = "http://localhost:5173"
PASSWORD = "correct-horse-battery-audit"


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
    from app.models.identity import Organization, OrganizationMembership, User

    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    with SaSession(migrated_engine) as s:
        org = Organization(
            id=uuid.uuid4(),
            slug=f"audit-org-{suffix}",
            name=f"Audit Org {suffix}",
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
    return _make_org_with_members(migrated_engine, [Role.ANALYST, Role.VIEWER])


@pytest.fixture()
def org_b(migrated_engine: Engine) -> dict[str, Any]:
    return _make_org_with_members(migrated_engine, [Role.ANALYST, Role.VIEWER])


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


def _create_supplier(client: TestClient, headers: dict[str, str], *, name: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/suppliers",
        json={
            "code": f"SUP-{uuid.uuid4().hex[:8].upper()}",
            "name": name,
            "country_code": "US",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _seed_unit(migrated_engine: Engine, organization_id: str) -> str:
    from app.models.units import UnitDefinition, UnitDimension

    unit_id = uuid.uuid4()
    code = f"unit-{uuid.uuid4().hex[:12]}"
    with SaSession(migrated_engine) as s:
        s.add(
            UnitDefinition(
                id=unit_id,
                organization_id=uuid.UUID(organization_id),
                code=code,
                display_name=code,
                dimension=UnitDimension.COUNT,
                to_canonical_factor=Decimal("1"),
                is_user_defined=True,
            )
        )
        s.commit()
    return str(unit_id)


def _create_part(client: TestClient, headers: dict[str, str], unit_id: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/parts",
        json={
            "internal_part_number": f"PN-{uuid.uuid4().hex[:8].upper()}",
            "name": "Audit test bracket",
            "unit_definition_id": unit_id,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _create_rfq(client: TestClient, headers: dict[str, str], part_id: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/rfqs",
        json={
            "name": f"RFQ-{uuid.uuid4().hex[:8].upper()}",
            "internal_reference": f"REF-{uuid.uuid4().hex[:8].upper()}",
            "base_currency": "USD",
            "due_date": "2026-12-01",
            "lines": [{"part_id": part_id, "required_quantity": "10.000000"}],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _list_audit_events(
    client: TestClient, headers: dict[str, str], **params: Any
) -> dict[str, Any]:
    resp = client.get("/api/v1/audit-events", headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------


class TestNoWriteRoutes:
    def test_audit_paths_expose_get_only(self) -> None:
        settings = Settings(
            environment=Environment.TEST, database_url="postgresql+psycopg://x/x"
        )
        app = create_app(settings)
        schema = app.openapi()
        checked = 0
        for path, operations in schema["paths"].items():
            if "/audit-events" not in path:
                continue
            methods = {
                m.upper()
                for m in operations
                if m.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            }
            assert methods == {"GET"}, f"{path} exposes non-GET methods: {methods}"
            checked += 1
        assert checked >= 2, "expected both /audit-events and the entity-timeline path"


class TestListFilters:
    def test_event_type_filter_single_and_multi_value(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        _create_supplier(client, headers, name="Filter Supplier One")
        _create_supplier(client, headers, name="Filter Supplier Two")
        unit_id = _seed_unit(migrated_engine, org_a["org_id"])
        part = _create_part(client, headers, unit_id)
        _create_rfq(client, headers, part["id"])

        only_suppliers = _list_audit_events(
            client, headers, event_type="supplier.created", limit=200
        )
        assert len(only_suppliers["items"]) >= 2
        assert all(i["event_type"] == "supplier.created" for i in only_suppliers["items"])

        combined = client.get(
            "/api/v1/audit-events",
            headers=headers,
            params=[
                ("event_type", "supplier.created"),
                ("event_type", "rfq.created"),
                ("limit", 200),
            ],
        )
        assert combined.status_code == 200, combined.text
        combined_types = {i["event_type"] for i in combined.json()["items"]}
        assert combined_types <= {"supplier.created", "rfq.created"}
        assert "rfq.created" in combined_types
        assert "supplier.created" in combined_types

    def test_entity_type_filter(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        _create_supplier(client, headers, name="Entity Filter Supplier")

        result = _list_audit_events(client, headers, entity_type="supplier", limit=200)
        assert result["items"]
        assert all(i["entity_type"] == "supplier" for i in result["items"])

    def test_from_to_filter(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        early = _create_supplier(client, headers, name="Early Supplier")
        time.sleep(0.05)
        cutoff = datetime.now(UTC)
        time.sleep(0.05)
        late = _create_supplier(client, headers, name="Late Supplier")

        cutoff_iso = cutoff.isoformat()

        before = _list_audit_events(
            client, headers, entity_type="supplier", to=cutoff_iso, limit=200
        )
        before_ids = {i["entity_id"] for i in before["items"]}
        assert early["id"] in before_ids
        assert late["id"] not in before_ids

        after = _list_audit_events(
            client, headers, entity_type="supplier", **{"from": cutoff_iso}, limit=200
        )
        after_ids = {i["entity_id"] for i in after["items"]}
        assert late["id"] in after_ids
        assert early["id"] not in after_ids


class TestKeysetPagination:
    def test_walks_all_pages_without_duplicates_or_gaps(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created_ids = {
            _create_supplier(client, headers, name=f"Page Supplier {i}")["id"]
            for i in range(25)
        }

        seen_ids: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"entity_type": "supplier", "limit": 10}
            if cursor is not None:
                params["cursor"] = cursor
            page = _list_audit_events(client, headers, **params)
            pages += 1
            assert len(page["items"]) <= 10
            seen_ids.extend(i["entity_id"] for i in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert pages < 20, "pagination did not terminate"

        assert pages >= 3  # 25 rows / 10 per page
        assert len(seen_ids) == len(set(seen_ids)), "duplicate rows across pages"
        assert created_ids <= set(seen_ids)

    def test_invalid_cursor_is_a_validation_error(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.get(
            "/api/v1/audit-events", headers=headers, params={"cursor": "not-valid-base64!!"}
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"


class TestEntityTimeline:
    def test_returns_only_that_entitys_events(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier_one = _create_supplier(client, headers, name="Timeline Supplier One")
        supplier_two = _create_supplier(client, headers, name="Timeline Supplier Two")

        resp = client.get(
            f"/api/v1/entities/supplier/{supplier_one['id']}/audit-events", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["items"]
        assert all(i["entity_id"] == supplier_one["id"] for i in body["items"])
        assert all(i["entity_id"] != supplier_two["id"] for i in body["items"])


class TestGetAndRoleGating:
    def test_viewer_can_list_and_get(self, client: TestClient, org_a: dict[str, Any]) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier = _create_supplier(client, analyst_headers, name="Viewer Read Supplier")

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        listed = _list_audit_events(
            client, viewer_headers, entity_type="supplier", entity_id=supplier["id"]
        )
        assert listed["items"]
        event_id = listed["items"][0]["id"]

        got = client.get(f"/api/v1/audit-events/{event_id}", headers=viewer_headers)
        assert got.status_code == 200, got.text
        assert got.json()["id"] == event_id


class TestCrossOrgIsolation:
    def test_org_b_cannot_read_org_a_event(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier = _create_supplier(client, headers_a, name="Cross Org Supplier")
        listed = _list_audit_events(
            client, headers_a, entity_type="supplier", entity_id=supplier["id"]
        )
        event_id = listed["items"][0]["id"]

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = client.get(f"/api/v1/audit-events/{event_id}", headers=headers_b)
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    def test_org_b_list_never_includes_org_a_events(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        supplier = _create_supplier(client, headers_a, name="Isolation Supplier")

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        result = _list_audit_events(client, headers_b, entity_type="supplier", limit=200)
        assert all(i["entity_id"] != supplier["id"] for i in result["items"])
