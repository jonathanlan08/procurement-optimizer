"""Scoring-configuration API integration tests (docs/planning/
03-api-contract.md §4.14): CRUD, weight-validation matrix (unknown criterion
422, weight >1 422, direction mismatch 422), sample-configuration seeding
(idempotent, present on first GET), cross-org 404, role gating, audit
coverage, decimal wire format.

Fixture pattern mirrors test_fx_api.py/test_quotes_api.py: a committed org +
one user per role, built directly against the migrated database."""

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
PASSWORD = "correct-horse-battery-scoring"
SAMPLE_NAME = "Sample weights (demonstration)"


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
            slug=f"score-org-{suffix}",
            name=f"Score Org {suffix}",
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


def _valid_weights(**overrides: Any) -> list[dict[str, Any]]:
    weights = [
        {
            "criterion": "total_landed_cost",
            "weight": "0.600000",
            "direction": "lower_is_better",
            "label": "Total landed cost",
        },
        {
            "criterion": "spec_compliance",
            "weight": "0.400000",
            "direction": "higher_is_better",
            "label": "Spec compliance",
        },
    ]
    if overrides:
        weights = overrides.get("weights", weights)
    return weights


def _config_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": f"Config-{uuid.uuid4().hex[:8].upper()}",
        "weights": _valid_weights(),
    }
    payload.update(overrides)
    return payload


def _create_config(
    client: TestClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/scoring-configurations", json=_config_payload(**overrides), headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


class TestCreate:
    def test_create_full_configuration(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, headers)
        assert created["is_sample"] is False
        assert created["version"] == 1
        assert created["weight_sum"] == "1.000000"
        assert len(created["weights"]) == 2
        assert isinstance(created["weights"][0]["weight"], str)
        assert created["notes"] == []

    def test_weight_sum_not_one_is_still_created_with_a_note(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        weights = [
            {
                "criterion": "total_landed_cost",
                "weight": "0.500000",
                "direction": "lower_is_better",
            },
            {
                "criterion": "lead_time",
                "weight": "0.100000",
                "direction": "lower_is_better",
            },
        ]
        created = _create_config(client, headers, weights=weights)
        assert created["weight_sum"] == "0.600000"
        assert len(created["notes"]) == 1
        assert "not 1" in created["notes"][0]

    def test_zero_weight_criterion_is_allowed(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        weights = [
            {
                "criterion": "total_landed_cost",
                "weight": "1.000000",
                "direction": "lower_is_better",
            },
            {"criterion": "lead_time", "weight": "0.000000", "direction": "lower_is_better"},
        ]
        created = _create_config(client, headers, weights=weights)
        assert created["weight_sum"] == "1.000000"

    def test_user_defined_criterion_requires_label(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        weights = [
            {"criterion": "user_defined", "weight": "1.000000", "direction": "higher_is_better"},
        ]
        resp = client.post(
            "/api/v1/scoring-configurations",
            json=_config_payload(weights=weights),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_user_defined_criterion_with_label_succeeds(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        weights = [
            {
                "criterion": "user_defined",
                "weight": "1.000000",
                "direction": "higher_is_better",
                "label": "Regional preference",
            },
        ]
        created = _create_config(client, headers, weights=weights)
        assert created["weights"][0]["label"] == "Regional preference"


class TestValidation:
    def test_unknown_criterion_is_422(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        weights = [
            {
                "criterion": "not_a_real_criterion",
                "weight": "1.000000",
                "direction": "lower_is_better",
            }
        ]
        resp = client.post(
            "/api/v1/scoring-configurations",
            json=_config_payload(weights=weights),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_weight_over_one_is_422(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        weights = [
            {
                "criterion": "total_landed_cost",
                "weight": "1.500000",
                "direction": "lower_is_better",
            }
        ]
        resp = client.post(
            "/api/v1/scoring-configurations",
            json=_config_payload(weights=weights),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_negative_weight_is_422(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        weights = [
            {
                "criterion": "total_landed_cost",
                "weight": "-0.100000",
                "direction": "lower_is_better",
            }
        ]
        resp = client.post(
            "/api/v1/scoring-configurations",
            json=_config_payload(weights=weights),
            headers=headers,
        )
        assert resp.status_code == 422

    def test_wrong_direction_for_known_criterion_is_422(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        # total_landed_cost is canonically lower_is_better
        weights = [
            {
                "criterion": "total_landed_cost",
                "weight": "1.000000",
                "direction": "higher_is_better",
            }
        ]
        resp = client.post(
            "/api/v1/scoring-configurations",
            json=_config_payload(weights=weights),
            headers=headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_empty_weights_is_422(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/scoring-configurations",
            json=_config_payload(weights=[]),
            headers=headers,
        )
        assert resp.status_code == 422


class TestSampleSeeding:
    def test_sample_configuration_present_on_first_list(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.get("/api/v1/scoring-configurations", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        samples = [i for i in items if i["is_sample"]]
        assert len(samples) == 1
        assert samples[0]["name"] == SAMPLE_NAME
        assert len(samples[0]["weights"]) == 7

    def test_sample_seeding_is_idempotent(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.VIEWER))
        client.get("/api/v1/scoring-configurations", headers=headers)
        client.get("/api/v1/scoring-configurations", headers=headers)
        resp = client.get("/api/v1/scoring-configurations", headers=headers)
        items = resp.json()["items"]
        samples = [i for i in items if i["is_sample"]]
        assert len(samples) == 1

        with migrated_engine.connect() as conn:
            count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM scoring_configurations"
                    " WHERE organization_id = :org AND is_sample = true"
                ),
                {"org": org_a["org_id"]},
            ).scalar_one()
        assert count == 1


class TestUpdate:
    def test_update_name_and_weights_with_correct_if_match(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, headers)
        resp = client.patch(
            f"/api/v1/scoring-configurations/{created['id']}",
            json={"name": "Renamed config"},
            headers={**headers, "If-Match": f'"{created["version"]}"'},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Renamed config"
        assert body["version"] == created["version"] + 1

    def test_update_with_stale_if_match_is_409(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, headers)
        resp = client.patch(
            f"/api/v1/scoring-configurations/{created['id']}",
            json={"name": "Stale update"},
            headers={**headers, "If-Match": '"999"'},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict_version"

    def test_update_weights_revalidates(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, headers)
        bad_weights = [
            {"criterion": "unknown_thing", "weight": "1.000000", "direction": "lower_is_better"}
        ]
        resp = client.patch(
            f"/api/v1/scoring-configurations/{created['id']}",
            json={"weights": bad_weights},
            headers={**headers, "If-Match": f'"{created["version"]}"'},
        )
        assert resp.status_code == 422


class TestArchive:
    def test_admin_can_archive(self, client: TestClient, org_a: dict[str, Any]) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, analyst_headers)

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        resp = client.post(
            f"/api/v1/scoring-configurations/{created['id']}/archive",
            json={"reason": "superseded by new methodology"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_archived"] is True
        assert body["archive_reason"] == "superseded by new methodology"

        listed = client.get(
            "/api/v1/scoring-configurations", headers=admin_headers
        ).json()["items"]
        assert created["id"] not in [i["id"] for i in listed]

        listed_all = client.get(
            "/api/v1/scoring-configurations",
            params={"include_archived": True},
            headers=admin_headers,
        ).json()["items"]
        assert created["id"] in [i["id"] for i in listed_all]

    def test_analyst_cannot_archive(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, headers)
        resp = client.post(
            f"/api/v1/scoring-configurations/{created['id']}/archive",
            json={"reason": "test"},
            headers=headers,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden_role"


class TestRoleGating:
    def test_viewer_can_read_but_not_write(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.VIEWER))
        read_resp = client.get("/api/v1/scoring-configurations", headers=headers)
        assert read_resp.status_code == 200

        write_resp = client.post(
            "/api/v1/scoring-configurations", json=_config_payload(), headers=headers
        )
        assert write_resp.status_code == 403
        assert write_resp.json()["error"]["code"] == "forbidden_role"


class TestCrossOrgIsolation:
    def test_org_b_cannot_read_org_a_configuration(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, headers_a)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = client.patch(
            f"/api/v1/scoring-configurations/{created['id']}",
            json={"name": "hijacked"},
            headers={**headers_b, "If-Match": '"1"'},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_org_b_never_sees_org_a_configuration_in_list(
        self, client: TestClient, org_a: dict[str, Any], org_b: dict[str, Any]
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, headers_a)

        headers_b = _headers(_login_as(client, org_b, Role.VIEWER))
        listed = client.get("/api/v1/scoring-configurations", headers=headers_b).json()["items"]
        assert created["id"] not in [i["id"] for i in listed]


class TestAuditTrail:
    def test_create_writes_exactly_one_audit_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        created = _create_config(client, headers)

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'scoring_configuration' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": created["id"]},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("scoring_configuration.created") == 1
