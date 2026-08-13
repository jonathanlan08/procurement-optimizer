"""RFQ CRUD + status workflow + invitations + BOM explosion API integration
tests: create with inline lines, create by exploding an ACTIVE BOM (quantity
multiplication as decimal strings), explosion from a draft BOM (409), the
full draft->open->under_review->awarded->closed status walk with history
rows asserted in order, an illegal transition naming its allowed targets,
edits blocked outside draft (and the administrator override), supplier
invitations/exclusions/reinstatement, cross-org 404 everywhere, role
enforcement, audit coverage, and If-Match concurrency.

Fixture pattern mirrors test_boms_api.py: a committed org + user + membership,
built directly against the migrated database."""

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


def _supplier_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": f"SUP-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Supplier",
        "country_code": "US",
    }
    payload.update(overrides)
    return payload


def _create_supplier(
    client: TestClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/suppliers", json=_supplier_payload(**overrides), headers=headers
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
        "lines": [
            _bom_line_payload(pid, quantity_per_assembly=f"{i + 1}.000000")
            for i, pid in enumerate(part_ids)
        ],
    }
    payload.update(overrides)
    return payload


def _create_active_bom(
    client: TestClient, headers: dict[str, str], part_ids: list[str], **overrides: Any
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/boms", json=_bom_payload(part_ids, **overrides), headers=headers
    )
    assert resp.status_code == 201, resp.text
    bom = resp.json()
    activate = client.post(f"/api/v1/boms/{bom['id']}/activate", headers=headers)
    assert activate.status_code == 200, activate.text
    return activate.json()  # type: ignore[no-any-return]


def _create_draft_bom(
    client: TestClient, headers: dict[str, str], part_ids: list[str], **overrides: Any
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/boms", json=_bom_payload(part_ids, **overrides), headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _rfq_line_payload(part_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "part_id": part_id,
        "required_quantity": "10.000000",
    }
    payload.update(overrides)
    return payload


def _rfq_payload(part_ids: list[str], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": f"RFQ-{uuid.uuid4().hex[:8].upper()}",
        "internal_reference": f"REF-{uuid.uuid4().hex[:8].upper()}",
        "base_currency": "USD",
        "due_date": "2026-12-01",
        "lines": [_rfq_line_payload(pid) for pid in part_ids],
    }
    payload.update(overrides)
    return payload


def _create_rfq(
    client: TestClient, headers: dict[str, str], part_ids: list[str], **overrides: Any
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/rfqs", json=_rfq_payload(part_ids, **overrides), headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _three_parts(client: TestClient, headers: dict[str, str], unit_id: str) -> list[str]:
    return [_create_part(client, headers, unit_id)["id"] for _ in range(3)]


class TestRfqCreate:
    def test_create_with_inline_lines_and_get(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)

        created = _create_rfq(client, headers, part_ids)
        assert created["status"] == "draft"
        assert created["version"] == 1
        assert created["line_count"] == 3
        assert len(created["lines"]) == 3
        assert [ln["line_number"] for ln in created["lines"]] == [1, 2, 3]
        assert [ln["part_id"] for ln in created["lines"]] == part_ids
        assert created["invited_supplier_count"] == 0
        assert created["source_bom_id"] is None

        get_resp = client.get(f"/api/v1/rfqs/{created['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 200
        assert get_resp.headers["etag"] == '"1"'
        fetched = get_resp.json()
        assert fetched["id"] == created["id"]
        assert len(fetched["lines"]) == 3

    def test_line_defaults_unit_from_part(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part = _create_part(client, headers, unit_id)

        created = _create_rfq(client, headers, [], lines=[_rfq_line_payload(part["id"])])
        assert created["lines"][0]["unit_definition_id"] == unit_id

    def test_missing_lines_and_bom_is_422(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/rfqs",
            json={
                "name": "Empty RFQ",
                "internal_reference": f"REF-{uuid.uuid4().hex[:8]}",
                "base_currency": "USD",
                "due_date": "2026-12-01",
                "lines": [],
            },
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_lines_and_bom_together_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        bom = _create_active_bom(client, headers, part_ids)

        resp = client.post(
            "/api/v1/rfqs",
            json=_rfq_payload(part_ids, source_bom_id=bom["id"]),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text

    def test_duplicate_internal_reference_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        first = _create_rfq(client, headers, part_ids)

        resp = client.post(
            "/api/v1/rfqs",
            json=_rfq_payload(part_ids, internal_reference=first["internal_reference"]),
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_duplicate"


class TestRfqListLineCounts:
    """2026-08 product-audit remediation, P2: "Lines column always displays
    a dash because the summary API omits the count" — `GET /rfqs` (the list/
    summary endpoint) must carry a correct `line_count` per row."""

    def test_list_line_count_reflects_actual_line_count(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        part_ids = _three_parts(client, headers, unit_id)
        multi_line_rfq = _create_rfq(client, headers, part_ids)

        one_part = _three_parts(client, headers, unit_id)[:1]
        single_line_rfq = _create_rfq(client, headers, one_part)
        # drain the only line to zero — no minimum-line guard on a draft RFQ
        # (RfqService.remove_line), so this is the only way to get a real,
        # persisted 0-line RFQ to assert the "0" case against.
        del_resp = client.delete(
            f"/api/v1/rfqs/{single_line_rfq['id']}/lines/{single_line_rfq['lines'][0]['id']}",
            headers=headers,
        )
        assert del_resp.status_code == 204, del_resp.text

        listing = client.get("/api/v1/rfqs", headers={"Origin": ORIGIN}).json()
        by_id = {item["id"]: item for item in listing["items"]}

        assert by_id[multi_line_rfq["id"]]["line_count"] == 3
        assert by_id[single_line_rfq["id"]]["line_count"] == 0

    def test_list_line_count_not_an_n_plus_1_of_other_rfqs_lines(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Each RFQ's `line_count` in the listing reflects only its OWN
        lines, never another RFQ's — a regression a naive unscoped `GROUP
        BY` (missing the per-org filter, or missing the join predicate)
        could produce."""
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        two_lines = _create_rfq(client, headers, _three_parts(client, headers, unit_id)[:2])
        one_line = _create_rfq(client, headers, _three_parts(client, headers, unit_id)[:1])

        listing = client.get("/api/v1/rfqs", headers={"Origin": ORIGIN}).json()
        by_id = {item["id"]: item for item in listing["items"]}
        assert by_id[two_lines["id"]]["line_count"] == 2
        assert by_id[one_line["id"]]["line_count"] == 1


class TestRfqBomExplosion:
    def test_explode_from_active_bom_multiplies_quantities(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        bom = _create_active_bom(client, headers, part_ids)
        # bom lines quantities: 1, 2, 3 (per _bom_payload's i+1 pattern)

        rfq = _create_rfq(
            client,
            headers,
            [],
            lines=[],
            source_bom_id=bom["id"],
            assembly_quantity="5.000000",
        )
        assert rfq["source_bom_id"] == bom["id"]
        assert rfq["line_count"] == 3
        quantities = sorted(Decimal(ln["required_quantity"]) for ln in rfq["lines"])
        assert quantities == [Decimal("5.000000"), Decimal("10.000000"), Decimal("15.000000")]
        for ln in rfq["lines"]:
            assert isinstance(ln["required_quantity"], str)

    def test_explode_default_assembly_quantity_is_one(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        bom = _create_active_bom(client, headers, part_ids)

        rfq = _create_rfq(client, headers, [], lines=[], source_bom_id=bom["id"])
        quantities = sorted(Decimal(ln["required_quantity"]) for ln in rfq["lines"])
        assert quantities == [Decimal("1.000000"), Decimal("2.000000"), Decimal("3.000000")]

    def test_explode_from_draft_bom_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        bom = _create_draft_bom(client, headers, part_ids)

        resp = client.post(
            "/api/v1/rfqs",
            json=_rfq_payload([], lines=[], source_bom_id=bom["id"]),
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_explode_from_cross_org_bom_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        unit_b = _seed_unit(migrated_engine, organization_id=org_b["org_id"])
        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        part_ids_b = _three_parts(client, headers_b, unit_b)
        bom_b = _create_active_bom(client, headers_b, part_ids_b)

        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/rfqs",
            json=_rfq_payload([], lines=[], source_bom_id=bom_b["id"]),
            headers=headers_a,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"


class TestRfqStatusWorkflow:
    def test_full_status_walk_with_history_in_order(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)

        walk = [
            ("open", "opening for quotes"),
            ("under_review", "reviewing quotes"),
            ("awarded", "awarding supplier"),
            ("closed", "closing out"),
        ]
        for to_status, reason in walk:
            resp = client.post(
                f"/api/v1/rfqs/{rfq['id']}/status",
                json={"to_status": to_status, "reason": reason},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == to_status

        history_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/status-history", headers={"Origin": ORIGIN}
        )
        assert history_resp.status_code == 200
        items = history_resp.json()["items"]
        assert len(items) == 4
        pairs = [(h["from_status"], h["to_status"]) for h in items]
        assert pairs == [
            ("draft", "open"),
            ("open", "under_review"),
            ("under_review", "awarded"),
            ("awarded", "closed"),
        ]
        assert [h["note"] for h in items] == [r for _, r in walk]
        # 2026-08 external review P3: the timeline resolves the actor's name
        # (org-scoped) instead of showing a bare UUID.
        assert all(h["actor_full_name"] for h in items), items
        assert all(h["actor_user_id"] for h in items)

    def test_status_history_actor_name_is_org_scoped(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        """Name resolution joins through this org's memberships only — an
        actor id belonging to another organization must never resolve to that
        org's user name (isolation control, 2026-08 review P3 fix)."""
        from sqlalchemy.orm import Session as SaSession

        from app.services.user_lookup import resolve_user_names

        b_user_id = uuid.UUID(next(iter(org_b["users"].values()))["user_id"])
        a_user_id = uuid.UUID(next(iter(org_a["users"].values()))["user_id"])
        with SaSession(migrated_engine) as session:
            names = resolve_user_names(
                session, uuid.UUID(org_a["org_id"]), [a_user_id, b_user_id]
            )
        # org A's own member resolves; org B's member is absent, never named
        assert a_user_id in names
        assert b_user_id not in names

    def test_reopen_from_under_review_to_open(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)

        client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open"},
            headers=headers,
        )
        client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "under_review"},
            headers=headers,
        )
        reopen = client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open", "reason": "need more quotes"},
            headers=headers,
        )
        assert reopen.status_code == 200, reopen.text
        assert reopen.json()["status"] == "open"

    def test_illegal_transition_is_409_naming_allowed_targets(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "awarded", "reason": "skip ahead"},
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["error"]["code"] == "conflict_state"
        message = body["error"]["message"]
        assert "draft" in message
        assert "awarded" in message
        # allowed targets from draft: open, archived
        assert "open" in message

    def test_archive_transition_syncs_archived_fields(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "archived", "reason": "no longer needed"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "archived"
        assert body["is_archived"] is True
        assert body["archive_reason"] == "no longer needed"

        # archived RFQ no longer shows in default listing
        listing = client.get("/api/v1/rfqs", headers={"Origin": ORIGIN}).json()
        assert rfq["id"] not in [item["id"] for item in listing["items"]]
        listing_all = client.get(
            "/api/v1/rfqs?include_archived=true", headers={"Origin": ORIGIN}
        ).json()
        assert rfq["id"] in [item["id"] for item in listing_all["items"]]


class TestRfqDraftOnlyEditGate:
    def test_update_blocked_outside_draft_without_override(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        opened = client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open"},
            headers=headers,
        ).json()

        resp = client.patch(
            f"/api/v1/rfqs/{rfq['id']}",
            json={"name": "hijacked"},
            headers={**headers, "If-Match": f'"{opened["version"]}"'},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_update_allowed_outside_draft_with_admin_override(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        opened = client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open"},
            headers=headers,
        ).json()

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        resp = client.patch(
            f"/api/v1/rfqs/{rfq['id']}",
            json={"name": "renamed by admin", "override_reason": "urgent fix"},
            headers={**admin_headers, "If-Match": f'"{opened["version"]}"'},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "renamed by admin"

    def test_analyst_cannot_override(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        opened = client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open"},
            headers=headers,
        ).json()

        resp = client.patch(
            f"/api/v1/rfqs/{rfq['id']}",
            json={"name": "renamed", "override_reason": "please let me"},
            headers={**headers, "If-Match": f'"{opened["version"]}"'},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_line_add_blocked_outside_draft(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open"},
            headers=headers,
        )
        new_part = _create_part(client, headers, unit_id)

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/lines",
            json={"lines": [_rfq_line_payload(new_part["id"])]},
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_line_add_update_remove_in_draft(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids[:1])
        new_part = _create_part(client, headers, unit_id)

        add_resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/lines",
            json={"lines": [_rfq_line_payload(new_part["id"], required_quantity="7.500000")]},
            headers=headers,
        )
        assert add_resp.status_code == 201, add_resp.text
        added_line = add_resp.json()["items"][0]
        assert added_line["line_number"] == 2
        assert added_line["required_quantity"] == "7.500000"

        update_resp = client.patch(
            f"/api/v1/rfqs/{rfq['id']}/lines/{added_line['id']}",
            json={"required_quantity": "3.000000", "notes": "revised"},
            headers=headers,
        )
        assert update_resp.status_code == 200, update_resp.text
        assert update_resp.json()["required_quantity"] == "3.000000"
        assert update_resp.json()["notes"] == "revised"

        remove_resp = client.delete(
            f"/api/v1/rfqs/{rfq['id']}/lines/{added_line['id']}", headers=headers
        )
        assert remove_resp.status_code == 204, remove_resp.text

        lines_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/lines", headers={"Origin": ORIGIN}
        )
        assert [ln["id"] for ln in lines_resp.json()["items"]] == [
            rfq["lines"][0]["id"]
        ]

    def test_if_match_conflict(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)

        resp = client.patch(
            f"/api/v1/rfqs/{rfq['id']}",
            json={"name": "stale write"},
            headers={**headers, "If-Match": '"99"'},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_version"


class TestRfqInvitationsExclusions:
    def test_invite_list_and_count(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        sup1 = _create_supplier(client, headers)
        sup2 = _create_supplier(client, headers)

        invite_resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [sup1["id"], sup2["id"]]},
            headers=headers,
        )
        assert invite_resp.status_code == 201, invite_resp.text
        invited = invite_resp.json()["items"]
        assert len(invited) == 2
        assert {i["supplier_id"] for i in invited} == {sup1["id"], sup2["id"]}

        get_resp = client.get(f"/api/v1/rfqs/{rfq['id']}", headers={"Origin": ORIGIN})
        assert get_resp.json()["invited_supplier_count"] == 2

        list_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/suppliers", headers={"Origin": ORIGIN}
        )
        assert len(list_resp.json()["items"]) == 2

    def test_invite_requires_draft_or_open_status(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        supplier = _create_supplier(client, headers)

        client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open"},
            headers=headers,
        )
        client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "under_review"},
            headers=headers,
        )

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [supplier["id"]]},
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_duplicate_invite_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        supplier = _create_supplier(client, headers)

        client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [supplier["id"]]},
            headers=headers,
        )
        dup = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [supplier["id"]]},
            headers=headers,
        )
        assert dup.status_code == 409, dup.text
        assert dup.json()["error"]["code"] == "conflict_duplicate"

    def test_exclude_requires_reason(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        supplier = _create_supplier(client, headers)
        invite = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [supplier["id"]]},
            headers=headers,
        ).json()
        rfq_supplier_id = invite["items"][0]["id"]

        missing_reason = client.request(
            "DELETE",
            f"/api/v1/rfqs/{rfq['id']}/suppliers/{rfq_supplier_id}",
            json={},
            headers=headers,
        )
        assert missing_reason.status_code == 422, missing_reason.text

        exclude_resp = client.request(
            "DELETE",
            f"/api/v1/rfqs/{rfq['id']}/suppliers/{rfq_supplier_id}",
            json={"exclusion_reason": "too expensive"},
            headers=headers,
        )
        assert exclude_resp.status_code == 200, exclude_resp.text
        body = exclude_resp.json()
        assert body["excluded_at"] is not None
        assert body["exclusion_reason"] == "too expensive"

        get_resp = client.get(f"/api/v1/rfqs/{rfq['id']}", headers={"Origin": ORIGIN})
        assert get_resp.json()["invited_supplier_count"] == 0

    def test_exclude_then_reinstate(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        supplier = _create_supplier(client, headers)
        invite = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [supplier["id"]]},
            headers=headers,
        ).json()
        rfq_supplier_id = invite["items"][0]["id"]

        client.request(
            "DELETE",
            f"/api/v1/rfqs/{rfq['id']}/suppliers/{rfq_supplier_id}",
            json={"exclusion_reason": "reconsidering"},
            headers=headers,
        )

        reinstate_resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers/{rfq_supplier_id}/reinstate",
            headers=headers,
        )
        assert reinstate_resp.status_code == 200, reinstate_resp.text
        body = reinstate_resp.json()
        assert body["excluded_at"] is None
        assert body["exclusion_reason"] is None

        get_resp = client.get(f"/api/v1/rfqs/{rfq['id']}", headers={"Origin": ORIGIN})
        assert get_resp.json()["invited_supplier_count"] == 1

    def test_cross_org_supplier_invite_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        unit_a = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers_a, unit_a)
        rfq = _create_rfq(client, headers_a, part_ids)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        foreign_supplier = _create_supplier(client, headers_b)

        # re-login as org_a last: the TestClient's cookie jar is shared and
        # a login replaces the active session cookie, so the request below
        # (using headers_a's CSRF token) needs org_a's session to be the
        # currently active one, not org_b's from the line above.
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [foreign_supplier["id"]]},
            headers=headers_a,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"


class TestRfqOrgIsolation:
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
        rfq = _create_rfq(client, headers_a, part_ids)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))

        get_resp = client.get(f"/api/v1/rfqs/{rfq['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 404

        patch_resp = client.patch(
            f"/api/v1/rfqs/{rfq['id']}",
            json={"name": "hijack"},
            headers={**headers_b, "If-Match": '"1"'},
        )
        assert patch_resp.status_code == 404

        status_resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open"},
            headers=headers_b,
        )
        assert status_resp.status_code == 404

        history_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/status-history", headers={"Origin": ORIGIN}
        )
        assert history_resp.status_code == 404

        lines_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/lines", headers={"Origin": ORIGIN}
        )
        assert lines_resp.status_code == 404

        add_line_resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/lines",
            json={"lines": [_rfq_line_payload(part_ids[0])]},
            headers=headers_b,
        )
        assert add_line_resp.status_code == 404

        suppliers_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/suppliers", headers={"Origin": ORIGIN}
        )
        assert suppliers_resp.status_code == 404

        invite_resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [str(uuid.uuid4())]},
            headers=headers_b,
        )
        assert invite_resp.status_code == 404

        list_resp = client.get("/api/v1/rfqs", headers={"Origin": ORIGIN})
        ids = [item["id"] for item in list_resp.json()["items"]]
        assert rfq["id"] not in ids

    def test_cross_org_part_id_in_a_line_is_404(
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
            "/api/v1/rfqs",
            json=_rfq_payload([foreign_part["id"]]),
            headers=headers_a,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"


class TestRfqRoleEnforcement:
    def test_viewer_can_read_but_not_mutate(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers_a, unit_id)
        rfq = _create_rfq(client, headers_a, part_ids)

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))

        get_resp = client.get(f"/api/v1/rfqs/{rfq['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 200

        list_resp = client.get("/api/v1/rfqs", headers={"Origin": ORIGIN})
        assert list_resp.status_code == 200

        create_attempt = client.post(
            "/api/v1/rfqs", json=_rfq_payload(part_ids), headers=viewer_headers
        )
        assert create_attempt.status_code == 403
        assert create_attempt.json()["error"]["code"] == "forbidden_role"

        patch_attempt = client.patch(
            f"/api/v1/rfqs/{rfq['id']}",
            json={"name": "nope"},
            headers={**viewer_headers, "If-Match": '"1"'},
        )
        assert patch_attempt.status_code == 403

        status_attempt = client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open"},
            headers=viewer_headers,
        )
        assert status_attempt.status_code == 403

        invite_attempt = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [str(uuid.uuid4())]},
            headers=viewer_headers,
        )
        assert invite_attempt.status_code == 403


class TestRfqAuditTrail:
    def test_audit_events_for_lifecycle_mutations(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        part_ids = _three_parts(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part_ids)
        supplier = _create_supplier(client, headers)

        invite = client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers",
            json={"supplier_ids": [supplier["id"]]},
            headers=headers,
        ).json()
        rfq_supplier_id = invite["items"][0]["id"]

        client.request(
            "DELETE",
            f"/api/v1/rfqs/{rfq['id']}/suppliers/{rfq_supplier_id}",
            json={"exclusion_reason": "test"},
            headers=headers,
        )
        client.post(
            f"/api/v1/rfqs/{rfq['id']}/suppliers/{rfq_supplier_id}/reinstate",
            headers=headers,
        )
        client.post(
            f"/api/v1/rfqs/{rfq['id']}/status",
            json={"to_status": "open", "reason": "go"},
            headers=headers,
        )

        def _event_types(entity_type: str, entity_id: str) -> list[str]:
            with migrated_engine.connect() as conn:
                rows = conn.execute(
                    sqltext(
                        "SELECT event_type FROM audit_events"
                        " WHERE entity_type = :et AND entity_id = :id"
                        " ORDER BY occurred_at"
                    ),
                    {"et": entity_type, "id": entity_id},
                ).all()
            return [row[0] for row in rows]

        assert _event_types("rfq", rfq["id"]) == ["rfq.created", "rfq.status_changed"]
        assert _event_types("rfq_supplier", rfq_supplier_id) == [
            "rfq.supplier_invited",
            "rfq.supplier_excluded",
            "rfq.supplier_reinstated",
        ]

        with migrated_engine.connect() as conn:
            after_state = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE entity_type = 'rfq' AND entity_id = :id"
                    " AND event_type = 'rfq.created'"
                ),
                {"id": rfq["id"]},
            ).scalar_one()
        assert after_state["status"] == "draft"
        assert len(after_state["lines"]) == 3
