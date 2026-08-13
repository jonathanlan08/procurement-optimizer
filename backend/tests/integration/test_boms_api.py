"""BOM CRUD + copy-on-write versioning API integration tests: create v1 with
lines, fork a new version, activate (old version superseded), version-chain
ordering, line immutability, org isolation (including cross-org part_id in a
line), role enforcement, quantity validation, audit coverage, and decimal
wire format.

Fixture pattern mirrors test_parts_api.py: a committed org + user +
membership, built directly against the migrated database."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
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


def _seed_unit(migrated_engine: Engine, organization_id: str | None = None) -> str:
    """An org-scoped unit_definitions row. See test_parts_api.py's
    `_seed_unit` docstring for why every call here passes an explicit
    `organization_id` rather than relying on the global-catalogue default:
    a `None` here would durably pollute the shared, session-scoped
    `migrated_engine` for every other test file in the session."""
    from sqlalchemy.orm import Session as SaSession

    from app.models.units import UnitDefinition, UnitDimension

    unit_id = uuid.uuid4()
    code = f"unit-{uuid.uuid4().hex[:12]}"
    with SaSession(migrated_engine) as s:
        s.add(
            UnitDefinition(
                id=unit_id,
                organization_id=uuid.UUID(organization_id) if organization_id else None,
                code=code,
                display_name=code,
                dimension=UnitDimension.COUNT,
                to_canonical_factor=Decimal("1"),
                is_user_defined=organization_id is not None,
            )
        )
        s.commit()
    return str(unit_id)


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


def _part_payload(unit_definition_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "internal_part_number": f"PN-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Component",
        "unit_definition_id": unit_definition_id,
    }
    payload.update(overrides)
    return payload


def _create_part(
    client: TestClient, headers: dict[str, str], unit_id: str, **overrides: Any
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/parts", json=_part_payload(unit_id, **overrides), headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _bom_line_payload(part_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "part_id": part_id,
        "quantity_per_assembly": "2.000000",
    }
    payload.update(overrides)
    return payload


def _bom_payload(part_ids: list[str], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": f"BOM-{uuid.uuid4().hex[:8].upper()}",
        "product_name": "Widget Assembly",
        "notes": "test bom",
        "lines": [
            _bom_line_payload(pid, quantity_per_assembly=f"{i + 1}.000000")
            for i, pid in enumerate(part_ids)
        ],
    }
    payload.update(overrides)
    return payload


def _create_bom(
    client: TestClient, headers: dict[str, str], part_ids: list[str], **overrides: Any
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/boms", json=_bom_payload(part_ids, **overrides), headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _three_parts(client: TestClient, headers: dict[str, str], unit_id: str) -> list[str]:
    return [_create_part(client, headers, unit_id)["id"] for _ in range(3)]


class TestBomCrudRoundTrip:
    def test_search_filters_and_total_agree(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Regression (2026-08 external review P2): search was client-side over
        the loaded page while the pagination footer kept the unfiltered server
        total. `q` now filters server-side on name AND product name, and the
        reported total counts the SAME filtered set."""
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        _create_bom(client, headers, part_ids, name="Rack Assembly", product_name="Server Rack")
        _create_bom(client, headers, part_ids, name="Panel Kit", product_name="Enclosure")
        _create_bom(client, headers, part_ids, name="Cable Loom", product_name="Rack Wiring")

        body = client.get("/api/v1/boms?q=rack", headers={"Origin": ORIGIN}).json()
        names = sorted(item["name"] for item in body["items"])
        # matches on name ("Rack Assembly") AND product_name ("Rack Wiring")
        assert names == ["Cable Loom", "Rack Assembly"]
        assert body["page"]["total"] == 2

        none = client.get("/api/v1/boms?q=zzz-no-match", headers={"Origin": ORIGIN}).json()
        assert none["items"] == []
        assert none["page"]["total"] == 0

    def test_create_v1_with_three_lines_and_get(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)

        created = _create_bom(client, headers, part_ids)
        assert created["version_number"] == 1
        assert created["root_bom_id"] == created["id"]
        assert created["previous_version_id"] is None
        assert created["status"] == "draft"
        assert len(created["lines"]) == 3
        assert [ln["line_number"] for ln in created["lines"]] == [1, 2, 3]
        assert [ln["part_id"] for ln in created["lines"]] == part_ids

        get_resp = client.get(f"/api/v1/boms/{created['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 200
        fetched = get_resp.json()
        assert fetched["id"] == created["id"]
        assert len(fetched["lines"]) == 3

    def test_line_defaults_unit_from_part(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        created = _create_bom(
            client,
            headers,
            [],
            lines=[{"part_id": part["id"], "quantity_per_assembly": "5.000000"}],
        )
        assert created["lines"][0]["unit_definition_id"] == unit_id

    def test_list_defaults_to_latest_version_per_root(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        version_resp = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json=_bom_payload(part_ids[:2]),
            headers=headers,
        )
        assert version_resp.status_code == 201, version_resp.text
        v2 = version_resp.json()

        default_list = client.get("/api/v1/boms", headers={"Origin": ORIGIN}).json()
        ids = [item["id"] for item in default_list["items"]]
        assert v2["id"] in ids
        assert v1["id"] not in ids

        all_versions = client.get(
            "/api/v1/boms?all_versions=true", headers={"Origin": ORIGIN}
        ).json()
        all_ids = [item["id"] for item in all_versions["items"]]
        assert v1["id"] in all_ids
        assert v2["id"] in all_ids

    def test_empty_lines_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/boms",
            json={"name": "Empty BOM", "product_name": "Nothing", "lines": []},
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"


class TestBomVersioning:
    def test_new_version_copies_metadata_and_replaces_lines(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        new_part = _create_part(client, headers, unit_id)
        version_resp = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json={"lines": [_bom_line_payload(new_part["id"], quantity_per_assembly="9.000000")]},
            headers=headers,
        )
        assert version_resp.status_code == 201, version_resp.text
        v2 = version_resp.json()

        assert v2["version_number"] == 2
        assert v2["root_bom_id"] == v1["root_bom_id"]
        assert v2["previous_version_id"] == v1["id"]
        assert v2["status"] == "draft"
        # metadata not overridden in the payload copies forward from v1
        assert v2["name"] == v1["name"]
        assert v2["product_name"] == v1["product_name"]
        # lines are a full replacement, not a merge
        assert len(v2["lines"]) == 1
        assert v2["lines"][0]["part_id"] == new_part["id"]

        # v1's own lines are untouched
        v1_after = client.get(f"/api/v1/boms/{v1['id']}", headers={"Origin": ORIGIN}).json()
        assert len(v1_after["lines"]) == 3
        assert [ln["part_id"] for ln in v1_after["lines"]] == part_ids
        assert v1_after["status"] == "draft"  # not yet activated/superseded

    def test_new_version_can_override_metadata(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        version_resp = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json={
                "name": "Renamed BOM",
                "lines": [_bom_line_payload(part_ids[0])],
            },
            headers=headers,
        )
        assert version_resp.status_code == 201, version_resp.text
        v2 = version_resp.json()
        assert v2["name"] == "Renamed BOM"
        assert v2["product_name"] == v1["product_name"]

    def test_activate_marks_previous_version_superseded(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        activate_v1 = client.post(f"/api/v1/boms/{v1['id']}/activate", headers=headers)
        assert activate_v1.status_code == 200, activate_v1.text
        assert activate_v1.json()["status"] == "active"

        version_resp = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json=_bom_payload(part_ids[:2]),
            headers=headers,
        )
        v2 = version_resp.json()
        assert v2["status"] == "draft"

        activate_v2 = client.post(f"/api/v1/boms/{v2['id']}/activate", headers=headers)
        assert activate_v2.status_code == 200, activate_v2.text
        assert activate_v2.json()["status"] == "active"

        v1_after = client.get(f"/api/v1/boms/{v1['id']}", headers={"Origin": ORIGIN}).json()
        assert v1_after["status"] == "superseded"

    def test_cannot_activate_non_draft(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        first = client.post(f"/api/v1/boms/{v1['id']}/activate", headers=headers)
        assert first.status_code == 200

        second = client.post(f"/api/v1/boms/{v1['id']}/activate", headers=headers)
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "conflict_state"

    def test_cannot_version_from_non_head(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        v2_resp = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json=_bom_payload(part_ids[:1]),
            headers=headers,
        )
        assert v2_resp.status_code == 201

        # v1 is no longer the head of its root's chain
        stale_version_resp = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json=_bom_payload(part_ids[:1]),
            headers=headers,
        )
        assert stale_version_resp.status_code == 409, stale_version_resp.text
        assert stale_version_resp.json()["error"]["code"] == "conflict_state"

    def test_version_chain_is_ordered_ascending(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)
        v2 = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json=_bom_payload(part_ids[:2]),
            headers=headers,
        ).json()
        v3 = client.post(
            f"/api/v1/boms/{v2['id']}/versions",
            json=_bom_payload(part_ids[:1]),
            headers=headers,
        ).json()

        chain_from_v1 = client.get(
            f"/api/v1/boms/{v1['id']}/versions", headers={"Origin": ORIGIN}
        ).json()
        chain_ids = [item["id"] for item in chain_from_v1["items"]]
        assert chain_ids == [v1["id"], v2["id"], v3["id"]]
        assert [item["version_number"] for item in chain_from_v1["items"]] == [1, 2, 3]

        # resolving via any version's id (not only the root's) returns the
        # identical chain
        chain_from_v3 = client.get(
            f"/api/v1/boms/{v3['id']}/versions", headers={"Origin": ORIGIN}
        ).json()
        assert [item["id"] for item in chain_from_v3["items"]] == chain_ids


class TestLineImmutability:
    def test_no_line_edit_route_exists(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        # no metadata-only PATCH and no per-line edit route: both must be
        # absent from the route table (405 method-not-allowed, since the
        # bare /boms/{id} path does exist for GET).
        patch_resp = client.patch(
            f"/api/v1/boms/{v1['id']}", json={"name": "hijacked"}, headers=headers
        )
        assert patch_resp.status_code in (404, 405)

        line_id = v1["lines"][0]["id"]
        line_patch_resp = client.patch(
            f"/api/v1/boms/{v1['id']}/lines/{line_id}",
            json={"quantity_per_assembly": "99.000000"},
            headers=headers,
        )
        assert line_patch_resp.status_code == 404

    def test_editing_a_line_requires_a_new_version(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)
        original_quantities = [ln["quantity_per_assembly"] for ln in v1["lines"]]

        client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json={
                "lines": [
                    _bom_line_payload(part_ids[0], quantity_per_assembly="123.000000"),
                ]
            },
            headers=headers,
        )

        v1_after = client.get(f"/api/v1/boms/{v1['id']}", headers={"Origin": ORIGIN}).json()
        assert [ln["quantity_per_assembly"] for ln in v1_after["lines"]] == original_quantities


class TestBomOrgIsolation:
    def test_cross_org_access_is_404_everywhere(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        unit_a = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers_a, unit_a)
        v1 = _create_bom(client, headers_a, part_ids)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))

        get_resp = client.get(f"/api/v1/boms/{v1['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 404
        assert get_resp.json()["error"]["code"] == "not_found"

        versions_resp = client.get(
            f"/api/v1/boms/{v1['id']}/versions", headers={"Origin": ORIGIN}
        )
        assert versions_resp.status_code == 404

        new_version_resp = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json=_bom_payload(part_ids[:1]),
            headers=headers_b,
        )
        assert new_version_resp.status_code == 404
        assert new_version_resp.json()["error"]["code"] == "not_found"

        activate_resp = client.post(f"/api/v1/boms/{v1['id']}/activate", headers=headers_b)
        assert activate_resp.status_code == 404

        admin_b_headers = _headers(_login_as(client, org_b, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/boms/{v1['id']}/archive",
            json={"reason": "cross-org attempt"},
            headers=admin_b_headers,
        )
        assert archive_resp.status_code == 404
        assert archive_resp.json()["error"]["code"] == "not_found"

        list_resp = client.get("/api/v1/boms", headers={"Origin": ORIGIN})
        ids = [item["id"] for item in list_resp.json()["items"]]
        assert v1["id"] not in ids

    def test_part_id_from_another_org_in_a_line_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        unit_b = _seed_unit(migrated_engine, organization_id=org_b["org_id"])
        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        foreign_part = _create_part(client, headers_b, unit_b)

        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/boms",
            json=_bom_payload([foreign_part["id"]]),
            headers=headers_a,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    def test_substitute_part_from_another_org_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        unit_a = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        unit_b = _seed_unit(migrated_engine, organization_id=org_b["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        part_a = _create_part(client, headers_a, unit_a)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        foreign_part = _create_part(client, headers_b, unit_b)

        headers_a_again = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/boms",
            json=_bom_payload(
                [], lines=[
                    _bom_line_payload(part_a["id"], substitute_part_id=foreign_part["id"])
                ]
            ),
            headers=headers_a_again,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"


class TestBomRoleEnforcement:
    def test_viewer_can_read_but_not_mutate(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers_a, unit_id)
        v1 = _create_bom(client, headers_a, part_ids)

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))

        get_resp = client.get(f"/api/v1/boms/{v1['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 200

        list_resp = client.get("/api/v1/boms", headers={"Origin": ORIGIN})
        assert list_resp.status_code == 200

        create_attempt = client.post(
            "/api/v1/boms", json=_bom_payload(part_ids), headers=viewer_headers
        )
        assert create_attempt.status_code == 403
        assert create_attempt.json()["error"]["code"] == "forbidden_role"

        version_attempt = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json=_bom_payload(part_ids[:1]),
            headers=viewer_headers,
        )
        assert version_attempt.status_code == 403

        activate_attempt = client.post(
            f"/api/v1/boms/{v1['id']}/activate", headers=viewer_headers
        )
        assert activate_attempt.status_code == 403

        archive_attempt = client.post(
            f"/api/v1/boms/{v1['id']}/archive",
            json={"reason": "nope"},
            headers=viewer_headers,
        )
        assert archive_attempt.status_code == 403

    def test_analyst_can_mutate_but_not_archive(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        activate_resp = client.post(f"/api/v1/boms/{v1['id']}/activate", headers=headers)
        assert activate_resp.status_code == 200

        archive_attempt = client.post(
            f"/api/v1/boms/{v1['id']}/archive", json={"reason": "nope"}, headers=headers
        )
        assert archive_attempt.status_code == 403
        assert archive_attempt.json()["error"]["code"] == "forbidden_role"

    def test_admin_can_archive(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/boms/{v1['id']}/archive",
            json={"reason": "discontinued"},
            headers=admin_headers,
        )
        assert archive_resp.status_code == 200, archive_resp.text
        body = archive_resp.json()
        assert body["is_archived"] is True
        assert body["status"] == "archived"
        assert body["archive_reason"] == "discontinued"

        double_archive = client.post(
            f"/api/v1/boms/{v1['id']}/archive",
            json={"reason": "again"},
            headers=admin_headers,
        )
        assert double_archive.status_code == 409
        assert double_archive.json()["error"]["code"] == "conflict_state"


class TestQuantityValidation:
    def test_zero_quantity_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.post(
            "/api/v1/boms",
            json=_bom_payload(
                [], lines=[_bom_line_payload(part["id"], quantity_per_assembly="0")]
            ),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_negative_quantity_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.post(
            "/api/v1/boms",
            json=_bom_payload(
                [], lines=[_bom_line_payload(part["id"], quantity_per_assembly="-1.5")]
            ),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_scale_exceeded_quantity_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.post(
            "/api/v1/boms",
            json=_bom_payload(
                # 7dp > QTY_SCALE (6dp)
                [], lines=[_bom_line_payload(part["id"], quantity_per_assembly="1.1234567")]
            ),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_nonexistent_part_id_is_404(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/boms",
            json=_bom_payload([], lines=[_bom_line_payload(str(uuid.uuid4()))]),
            headers=headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    def test_substitute_equal_to_part_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.post(
            "/api/v1/boms",
            json=_bom_payload(
                [],
                lines=[
                    _bom_line_payload(part["id"], substitute_part_id=part["id"])
                ],
            ),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"


class TestAuditTrail:
    def test_each_bom_mutation_writes_exactly_one_audit_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        v1 = _create_bom(client, headers, part_ids)

        version_resp = client.post(
            f"/api/v1/boms/{v1['id']}/versions",
            json=_bom_payload(part_ids[:1]),
            headers=headers,
        )
        assert version_resp.status_code == 201
        v2 = version_resp.json()

        activate_resp = client.post(f"/api/v1/boms/{v2['id']}/activate", headers=headers)
        assert activate_resp.status_code == 200

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/boms/{v2['id']}/archive", json={"reason": "test"}, headers=admin_headers
        )
        assert archive_resp.status_code == 200

        def _event_counts(entity_id: str) -> dict[str, int]:
            with migrated_engine.connect() as conn:
                rows = conn.execute(
                    sqltext(
                        "SELECT event_type, count(*) FROM audit_events"
                        " WHERE entity_type = 'bom' AND entity_id = :id"
                        " GROUP BY event_type"
                    ),
                    {"id": entity_id},
                ).all()
            return {row[0]: row[1] for row in rows}

        v1_counts = _event_counts(v1["id"])
        assert v1_counts.get("bom.created") == 1

        v2_counts = _event_counts(v2["id"])
        assert v2_counts.get("bom.version_created") == 1
        assert v2_counts.get("bom.activated") == 1
        assert v2_counts.get("bom.archived") == 1

        with migrated_engine.connect() as conn:
            after_state = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE entity_type = 'bom' AND entity_id = :id"
                    " AND event_type = 'bom.created'"
                ),
                {"id": v1["id"]},
            ).scalar_one()
        assert after_state["version_number"] == 1
        assert len(after_state["lines"]) == 3


class TestDecimalRoundTrip:
    def test_quantity_is_string_at_6dp_on_the_wire(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        created = _create_bom(
            client,
            headers,
            [],
            lines=[_bom_line_payload(part["id"], quantity_per_assembly="2.5")],
        )
        assert created["lines"][0]["quantity_per_assembly"] == "2.500000"
        assert isinstance(created["lines"][0]["quantity_per_assembly"], str)

        get_resp = client.get(f"/api/v1/boms/{created['id']}", headers={"Origin": ORIGIN})
        assert get_resp.json()["lines"][0]["quantity_per_assembly"] == "2.500000"
