"""Part CRUD + alternatives API integration tests: org isolation, role gates,
optimistic concurrency, duplicate-part-number handling, audit coverage,
decimal wire format, unit_definition cross-org validation, and the
internal/external alternative XOR rule.

Fixture pattern mirrors test_suppliers_api.py: a committed org + user +
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
    """A unit_definition row: global (organization_id=None) or an org's own
    private unit, per app.models.units.UnitDefinition ("null = global
    catalog"). Codes are unique per call so repeated invocations across the
    session-scoped `migrated_engine` never collide with
    uq_unit_definitions_global_code_lower / uq_unit_definitions_org_code_lower.

    Every call site in this file below passes an explicit `organization_id`
    (almost always `org_a["org_id"]`) rather than relying on the `None`
    default: this commits directly against the shared, session-scoped
    `migrated_engine`, so a `None` here would durably add a row to the
    *global* catalogue (organization_id IS NULL) for the lifetime of the
    whole test session - polluting
    test_units_schema.py::test_seed_unit_catalog_idempotent's exact-count
    assertion for every test file that happens to run after this one. See
    TestUnitDefinitionValidation.test_global_unit_is_accepted for the one
    scenario that genuinely needs a global unit, done at the service layer
    on a rolled-back `db`-fixture session instead, where it never persists.
    """
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
        "manufacturer_part_number": "MPN-1000",
        "manufacturer": "Acme Corp",
        "name": "Widget Bracket",
        "description": "A test bracket",
        "category": "hardware",
        "unit_definition_id": unit_definition_id,
        "required_specifications": {"length_mm": 42, "finish": "anodized", "is_plated": True},
        "target_price": "10.50000000",
        "target_price_currency": "USD",
        "is_active": True,
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


class TestPartCrudRoundTrip:
    def test_analyst_full_crud_round_trip(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        created = _create_part(client, headers, unit_id)
        part_id = created["id"]
        assert created["version"] == 1
        assert created["unit_definition_id"] == unit_id
        assert created["required_specifications"] == {
            "length_mm": 42,
            "finish": "anodized",
            "is_plated": True,
        }

        get_resp = client.get(f"/api/v1/parts/{part_id}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 200
        assert get_resp.json()["internal_part_number"] == created["internal_part_number"]
        assert get_resp.headers["ETag"] == '"1"'

        update_resp = client.patch(
            f"/api/v1/parts/{part_id}",
            json={"name": "Widget Bracket Updated", "category": "fasteners"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert update_resp.status_code == 200, update_resp.text
        updated = update_resp.json()
        assert updated["name"] == "Widget Bracket Updated"
        assert updated["category"] == "fasteners"
        assert updated["version"] == 2
        # fields not sent in the PATCH are preserved unchanged
        assert updated["internal_part_number"] == created["internal_part_number"]
        assert updated["target_price"] == created["target_price"]

        list_resp = client.get("/api/v1/parts", headers={"Origin": ORIGIN})
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert any(item["id"] == part_id for item in body["items"])
        assert body["page"]["limit"] == 50
        assert body["page"]["offset"] == 0
        assert body["page"]["total"] >= 1

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/parts/{part_id}/archive",
            json={"reason": "discontinued"},
            headers=admin_headers,
        )
        assert archive_resp.status_code == 200, archive_resp.text
        assert archive_resp.json()["is_archived"] is True
        assert archive_resp.json()["archive_reason"] == "discontinued"

        unarchive_resp = client.post(
            f"/api/v1/parts/{part_id}/unarchive",
            json={"reason": "reinstated"},
            headers=admin_headers,
        )
        assert unarchive_resp.status_code == 200, unarchive_resp.text
        assert unarchive_resp.json()["is_archived"] is False
        assert unarchive_resp.json()["archive_reason"] is None

    def test_patch_target_price_round_trip(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Regression: PATCHing `target_price` 500'd - the service's
        `model_dump()` re-serialized the parsed Decimal back to its wire string
        (PlainSerializer's `when_used` defaults to "always"), and
        `quantize_unit_price()` then blew up on the str."""
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(client, headers, unit_id)

        update_resp = client.patch(
            f"/api/v1/parts/{created['id']}",
            json={"target_price": "12.75", "target_price_currency": "USD"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert update_resp.status_code == 200, update_resp.text
        updated = update_resp.json()
        assert updated["target_price"] == "12.75000000"
        assert updated["version"] == 2

    def test_search_matches_normalized_key(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(
            client, headers, unit_id, internal_part_number="ACME-100-Rev.B"
        )

        # "acme100revb" normalizes identically to "ACME-100-Rev.B" (lower +
        # strip non-alphanumerics), matching via normalized_key equality
        # even though it is not an ilike substring of the stored value.
        resp = client.get(
            "/api/v1/parts?q=acme100revb", headers={"Origin": ORIGIN}
        )
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["items"]]
        assert created["id"] in ids


class TestPartOrgIsolation:
    def test_cross_org_access_is_404_for_every_route(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(client, headers_a, unit_id)
        part_id = created["id"]

        analyst_b_headers = _headers(_login_as(client, org_b, Role.ANALYST))

        get_resp = client.get(f"/api/v1/parts/{part_id}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 404
        assert get_resp.json()["error"]["code"] == "not_found"

        patch_resp = client.patch(
            f"/api/v1/parts/{part_id}",
            json={"name": "Hijacked"},
            headers={**analyst_b_headers, "If-Match": '"1"'},
        )
        assert patch_resp.status_code == 404
        assert patch_resp.json()["error"]["code"] == "not_found"

        list_alt_resp = client.get(
            f"/api/v1/parts/{part_id}/alternatives", headers=analyst_b_headers
        )
        assert list_alt_resp.status_code == 404
        assert list_alt_resp.json()["error"]["code"] == "not_found"

        add_alt_resp = client.post(
            f"/api/v1/parts/{part_id}/alternatives",
            json={"alternative_mpn": "EXT-1", "approval_status": "approved"},
            headers=analyst_b_headers,
        )
        assert add_alt_resp.status_code == 404
        assert add_alt_resp.json()["error"]["code"] == "not_found"

        remove_alt_resp = client.delete(
            f"/api/v1/parts/{part_id}/alternatives/{uuid.uuid4()}", headers=analyst_b_headers
        )
        assert remove_alt_resp.status_code == 404
        assert remove_alt_resp.json()["error"]["code"] == "not_found"

        admin_b_headers = _headers(_login_as(client, org_b, Role.ADMINISTRATOR))

        archive_resp = client.post(
            f"/api/v1/parts/{part_id}/archive",
            json={"reason": "cross-org attempt"},
            headers=admin_b_headers,
        )
        assert archive_resp.status_code == 404
        assert archive_resp.json()["error"]["code"] == "not_found"

        unarchive_resp = client.post(
            f"/api/v1/parts/{part_id}/unarchive",
            json={"reason": "cross-org attempt"},
            headers=admin_b_headers,
        )
        assert unarchive_resp.status_code == 404
        assert unarchive_resp.json()["error"]["code"] == "not_found"

        # sanity: org B's own list never contains org A's part
        list_resp = client.get("/api/v1/parts", headers={"Origin": ORIGIN})
        ids = [item["id"] for item in list_resp.json()["items"]]
        assert part_id not in ids

    def test_alternative_referencing_other_org_part_is_404(
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
        part_b = _create_part(client, headers_b, unit_b)

        # re-login as org_a: the shared client's session cookie now points at
        # org_b after the login above (same TestClient, same cookie jar).
        headers_a_again = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            f"/api/v1/parts/{part_a['id']}/alternatives",
            json={"alternative_part_id": part_b["id"], "approval_status": "approved"},
            headers=headers_a_again,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"


class TestPartRoleEnforcement:
    def test_viewer_can_read_but_not_mutate(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(client, headers_a, unit_id)
        part_id = created["id"]

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))

        get_resp = client.get(f"/api/v1/parts/{part_id}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 200

        create_attempt = client.post(
            "/api/v1/parts", json=_part_payload(unit_id), headers=viewer_headers
        )
        assert create_attempt.status_code == 403
        assert create_attempt.json()["error"]["code"] == "forbidden_role"

        update_attempt = client.patch(
            f"/api/v1/parts/{part_id}",
            json={"name": "nope"},
            headers={**viewer_headers, "If-Match": '"1"'},
        )
        assert update_attempt.status_code == 403
        assert update_attempt.json()["error"]["code"] == "forbidden_role"

        archive_attempt = client.post(
            f"/api/v1/parts/{part_id}/archive",
            json={"reason": "nope"},
            headers=viewer_headers,
        )
        assert archive_attempt.status_code == 403
        assert archive_attempt.json()["error"]["code"] == "forbidden_role"

        add_alt_attempt = client.post(
            f"/api/v1/parts/{part_id}/alternatives",
            json={"alternative_mpn": "EXT-1", "approval_status": "approved"},
            headers=viewer_headers,
        )
        assert add_alt_attempt.status_code == 403
        assert add_alt_attempt.json()["error"]["code"] == "forbidden_role"

    def test_analyst_cannot_archive(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(client, headers_a, unit_id)

        archive_attempt = client.post(
            f"/api/v1/parts/{created['id']}/archive",
            json={"reason": "nope"},
            headers=headers_a,
        )
        assert archive_attempt.status_code == 403
        assert archive_attempt.json()["error"]["code"] == "forbidden_role"

    def test_analyst_can_add_and_remove_alternatives(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        # unlike part archive (admin+ only), alternatives are analyst+ per
        # the contract's route table (§4.5: "O A N")
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers_a, unit_id)

        add_resp = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={"alternative_mpn": "EXT-1", "approval_status": "approved"},
            headers=headers_a,
        )
        assert add_resp.status_code == 201, add_resp.text

        remove_resp = client.delete(
            f"/api/v1/parts/{part['id']}/alternatives/{add_resp.json()['id']}",
            headers=headers_a,
        )
        assert remove_resp.status_code == 204, remove_resp.text


class TestDuplicatePartNumber:
    def test_duplicate_active_number_conflicts_then_reusable_after_archive(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        number = f"DUP-{uuid.uuid4().hex[:8].upper()}"

        first = client.post(
            "/api/v1/parts",
            json=_part_payload(unit_id, internal_part_number=number),
            headers=analyst_headers,
        )
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]

        dup = client.post(
            "/api/v1/parts",
            json=_part_payload(unit_id, internal_part_number=number.lower()),
            headers=analyst_headers,
        )
        assert dup.status_code == 409, dup.text
        assert dup.json()["error"]["code"] == "conflict_duplicate"

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/parts/{first_id}/archive",
            json={"reason": "superseded"},
            headers=admin_headers,
        )
        assert archive_resp.status_code == 200

        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        second = client.post(
            "/api/v1/parts",
            json=_part_payload(unit_id, internal_part_number=number),
            headers=analyst_headers,
        )
        assert second.status_code == 201, second.text
        assert second.json()["id"] != first_id

    def test_double_archive_is_conflict_state(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(client, analyst_headers, unit_id)

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        first_archive = client.post(
            f"/api/v1/parts/{created['id']}/archive",
            json={"reason": "first"},
            headers=admin_headers,
        )
        assert first_archive.status_code == 200

        second_archive = client.post(
            f"/api/v1/parts/{created['id']}/archive",
            json={"reason": "second"},
            headers=admin_headers,
        )
        assert second_archive.status_code == 409
        assert second_archive.json()["error"]["code"] == "conflict_state"


class TestVersionConflict:
    def test_stale_if_match_returns_conflict_version(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(client, headers, unit_id)

        first_update = client.patch(
            f"/api/v1/parts/{created['id']}",
            json={"name": "First Update"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert first_update.status_code == 200
        assert first_update.json()["version"] == 2

        stale_update = client.patch(
            f"/api/v1/parts/{created['id']}",
            json={"name": "Second Update"},
            headers={**headers, "If-Match": '"1"'},  # stale: current version is now 2
        )
        assert stale_update.status_code == 409, stale_update.text
        assert stale_update.json()["error"]["code"] == "conflict_version"


class TestAuditTrail:
    def test_each_part_mutation_writes_exactly_one_audit_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(client, headers, unit_id)
        part_id = created["id"]

        update_resp = client.patch(
            f"/api/v1/parts/{part_id}",
            json={"name": "Renamed"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert update_resp.status_code == 200

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/parts/{part_id}/archive",
            json={"reason": "test"},
            headers=admin_headers,
        )
        assert archive_resp.status_code == 200

        unarchive_resp = client.post(
            f"/api/v1/parts/{part_id}/unarchive",
            json={"reason": "test"},
            headers=admin_headers,
        )
        assert unarchive_resp.status_code == 200

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'part' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": part_id},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("part.created") == 1
        assert counts.get("part.updated") == 1
        assert counts.get("part.archived") == 1
        assert counts.get("part.unarchived") == 1

        with migrated_engine.connect() as conn:
            after_state = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE entity_type = 'part' AND entity_id = :id"
                    " AND event_type = 'part.created'"
                ),
                {"id": part_id},
            ).scalar_one()
        assert after_state["internal_part_number"] == created["internal_part_number"]

    def test_alternative_mutations_write_audit_events(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        add_resp = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={"alternative_mpn": "EXT-1", "approval_status": "approved"},
            headers=headers,
        )
        assert add_resp.status_code == 201
        alt_id = add_resp.json()["id"]

        remove_resp = client.delete(
            f"/api/v1/parts/{part['id']}/alternatives/{alt_id}", headers=headers
        )
        assert remove_resp.status_code == 204

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'part_alternative' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": alt_id},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("part.alternative_added") == 1
        assert counts.get("part.alternative_removed") == 1


class TestDecimalRoundTrip:
    def test_target_price_is_string_at_8dp_on_the_wire(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(
            client, headers, unit_id, target_price="10.5", target_price_currency="USD"
        )
        assert created["target_price"] == "10.50000000"
        assert isinstance(created["target_price"], str)

        get_resp = client.get(f"/api/v1/parts/{created['id']}", headers={"Origin": ORIGIN})
        assert get_resp.json()["target_price"] == "10.50000000"

    def test_negative_target_price_is_rejected(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/parts",
            json=_part_payload(unit_id, target_price="-1", target_price_currency="USD"),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_scale_exceeded_target_price_is_rejected(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/parts",
            # 9dp > UNIT_PRICE_SCALE (8dp)
            json=_part_payload(
                unit_id, target_price="1.123456789", target_price_currency="USD"
            ),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"


class TestCurrencyPairing:
    def test_create_price_without_currency_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        payload = _part_payload(unit_id)
        del payload["target_price_currency"]
        resp = client.post("/api/v1/parts", json=payload, headers=headers)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_create_currency_without_price_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        payload = _part_payload(unit_id)
        del payload["target_price"]
        resp = client.post("/api/v1/parts", json=payload, headers=headers)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_update_price_without_currency_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_part(client, headers, unit_id)

        resp = client.patch(
            f"/api/v1/parts/{created['id']}",
            json={"target_price": "20.00000000"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"


class TestUnitDefinitionValidation:
    def test_global_unit_is_accepted(self, db, make_uuid) -> None:
        """Exercised at the service layer, not via TestClient/`migrated_engine`:
        a *global* (organization_id IS NULL) unit_definitions row must never
        be durably committed to the shared, session-scoped test database -
        test_units_schema.py::test_seed_unit_catalog_idempotent asserts an
        *exact* count of such rows, and TestClient requests run through a
        separate, already-committing session (see `_seed_unit`'s docstring
        below for why every other test in this file seeds an org-scoped
        unit instead). The `db` fixture's per-test rolled-back transaction
        is the one place a global row can exist long enough to prove
        PartService accepts it, without leaking it into any other test:
        PartService runs on this same session, so it sees the uncommitted
        row, and the fixture rolls everything back when the test ends.
        """
        from app.core.clock import FrozenClock
        from app.models.identity import Organization
        from app.models.units import UnitDefinition, UnitDimension
        from app.schemas.parts import PartCreate
        from app.services.audit import AuditRecorder
        from app.services.part_service import PartService

        now = datetime.now(UTC)
        org_id = make_uuid.new_id()
        db.add(
            Organization(
                id=org_id,
                slug=f"org-{org_id}",
                name="Test Org",
                base_currency="USD",
                is_demo=False,
                created_at=now,
                updated_at=now,
            )
        )
        unit_id = make_uuid.new_id()
        db.add(
            UnitDefinition(
                id=unit_id,
                organization_id=None,
                code=f"unit-{unit_id}",
                display_name="Test Global Unit",
                dimension=UnitDimension.COUNT,
                to_canonical_factor=Decimal("1"),
                is_user_defined=False,
            )
        )
        db.flush()

        clock = FrozenClock(now)
        audit = AuditRecorder(db, clock, make_uuid, organization_id=org_id, actor_user_id=None)
        service = PartService(db, org_id, audit, clock, make_uuid)

        part = service.create(
            PartCreate(
                internal_part_number="PN-GLOBAL-UNIT",
                name="Global Unit Part",
                unit_definition_id=unit_id,
            )
        )
        assert part.unit_definition_id == unit_id

    def test_same_org_private_unit_is_accepted(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        own_unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/parts", json=_part_payload(own_unit_id), headers=headers
        )
        assert resp.status_code == 201, resp.text

    def test_other_org_private_unit_is_rejected(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        other_org_unit_id = _seed_unit(migrated_engine, organization_id=org_b["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/parts", json=_part_payload(other_org_unit_id), headers=headers
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_nonexistent_unit_is_rejected(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/parts",
            json=_part_payload(str(uuid.uuid4())),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"


class TestPartAlternatives:
    def test_internal_alternative_full_round_trip(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)
        alternative_part = _create_part(client, headers, unit_id)

        add_resp = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={
                "alternative_part_id": alternative_part["id"],
                "approval_status": "approved",
                "rationale": "same form-fit-function",
            },
            headers=headers,
        )
        assert add_resp.status_code == 201, add_resp.text
        added = add_resp.json()
        assert added["part_id"] == part["id"]
        assert added["alternative_part_id"] == alternative_part["id"]
        assert added["alternative_mpn"] is None
        assert added["approval_status"] == "approved"

        list_resp = client.get(
            f"/api/v1/parts/{part['id']}/alternatives", headers={"Origin": ORIGIN}
        )
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert any(item["id"] == added["id"] for item in body["items"])
        assert body["page"]["total"] >= 1

        remove_resp = client.delete(
            f"/api/v1/parts/{part['id']}/alternatives/{added['id']}", headers=headers
        )
        assert remove_resp.status_code == 204

        list_after = client.get(
            f"/api/v1/parts/{part['id']}/alternatives", headers={"Origin": ORIGIN}
        )
        ids_after = [item["id"] for item in list_after.json()["items"]]
        assert added["id"] not in ids_after

    def test_external_alternative_requires_only_mpn(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={
                "alternative_mpn": "EXT-MPN-42",
                "approval_status": "conditional",
                "rationale": "pending qualification",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["alternative_part_id"] is None
        assert body["alternative_mpn"] == "EXT-MPN-42"
        assert body["approval_status"] == "conditional"

    def test_self_alternative_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={"alternative_part_id": part["id"], "approval_status": "approved"},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_neither_internal_nor_external_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={"approval_status": "approved"},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_both_internal_and_external_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)
        other = _create_part(client, headers, unit_id)

        resp = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={
                "alternative_part_id": other["id"],
                "alternative_mpn": "EXT-1",
                "approval_status": "approved",
            },
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_nonexistent_internal_alternative_is_404(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={"alternative_part_id": str(uuid.uuid4()), "approval_status": "approved"},
            headers=headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    def test_duplicate_alternative_pair_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)
        alternative_part = _create_part(client, headers, unit_id)

        first = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={"alternative_part_id": alternative_part["id"], "approval_status": "approved"},
            headers=headers,
        )
        assert first.status_code == 201, first.text

        dup = client.post(
            f"/api/v1/parts/{part['id']}/alternatives",
            json={"alternative_part_id": alternative_part["id"], "approval_status": "rejected"},
            headers=headers,
        )
        assert dup.status_code == 409, dup.text
        assert dup.json()["error"]["code"] == "conflict_duplicate"

    def test_remove_nonexistent_alternative_is_404(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        resp = client.delete(
            f"/api/v1/parts/{part['id']}/alternatives/{uuid.uuid4()}", headers=headers
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"
