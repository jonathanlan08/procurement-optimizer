"""Part import API integration tests (SPEC §3; docs/planning/03-api-contract.md
§4.5): preview -> commit happy path, transactional rollback on an invalid
row, duplicate-against-existing-part detection, cancel, cross-org isolation,
double-commit conflict, oversized upload, and wrong media type.

Fixture pattern mirrors test_parts_api.py: a committed org + user +
membership, built directly against the migrated database.
"""

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


@pytest.fixture(scope="module")
def tiny_limit_client(
    database_url: str, migrated_engine: Engine
) -> Generator[TestClient, None, None]:
    """A second app instance with a tiny `max_upload_bytes`, isolated to the
    413 test below - the default 20 MiB cap would make that test either slow
    (a real 20 MiB body) or not exercise the streamed-check code path at
    all."""
    settings = Settings(
        environment=Environment.TEST,
        database_url=database_url,
        allowed_origins=[ORIGIN],
        rate_limit_auth_per_minute=100_000,
        rate_limit_per_minute=100_000,
        max_upload_bytes=64,
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
    return _make_org_with_members(migrated_engine, [Role.ANALYST, Role.VIEWER])


@pytest.fixture()
def org_b(migrated_engine: Engine) -> dict[str, Any]:
    return _make_org_with_members(migrated_engine, [Role.ANALYST, Role.VIEWER])


def _seed_each_unit(migrated_engine: Engine, organization_id: str) -> str:
    """An org-scoped 'each' unit_definition - never a global (organization_id
    IS NULL) row, for the same reason test_parts_api.py's `_seed_unit` never
    uses one when committing directly against the shared, session-scoped
    `migrated_engine`: it would durably pollute the global catalogue for
    every test that runs afterward in this session
    (test_units_schema.py::test_seed_unit_catalog_idempotent asserts an
    exact count of global rows). Returns the new unit_definition id: there is
    no `GET /units` route in this codebase yet (03-api-contract.md §4.6 is
    unimplemented), so tests that need the id for a `POST /parts` payload
    cannot fetch it back over the API."""
    from sqlalchemy.orm import Session as SaSession

    from app.models.units import UnitDefinition, UnitDimension

    unit_id = uuid.uuid4()
    with SaSession(migrated_engine) as s:
        s.add(
            UnitDefinition(
                id=unit_id,
                organization_id=uuid.UUID(organization_id),
                code="each",
                display_name="Each",
                dimension=UnitDimension.COUNT,
                to_canonical_factor=Decimal("1"),
                is_user_defined=True,
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


def _upload(
    client: TestClient,
    headers: dict[str, str],
    csv_text: str,
    *,
    filename: str = "parts.csv",
) -> Any:
    return client.post(
        "/api/v1/part-imports",
        files={"file": (filename, csv_text.encode("utf-8"), "text/csv")},
        headers=headers,
    )


def _parts_count(migrated_engine: Engine, organization_id: str) -> int:
    with migrated_engine.connect() as conn:
        return conn.execute(
            sqltext("SELECT count(*) FROM parts WHERE organization_id = :org"),
            {"org": organization_id},
        ).scalar_one()


class TestPreviewCommitHappyPath:
    def test_preview_then_commit_creates_parts_and_audit_events(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        _seed_each_unit(migrated_engine, org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        csv_text = (
            "internal_part_number,name,unit_code,target_price,target_price_currency\n"
            "PN-HAPPY-A,Widget A,each,10.50000000,USD\n"
            "PN-HAPPY-B,Widget B,each,,\n"
        )
        preview_resp = _upload(client, headers, csv_text)
        assert preview_resp.status_code == 201, preview_resp.text
        preview_body = preview_resp.json()
        assert preview_body["rows_total"] == 2
        assert preview_body["rows_valid"] == 2
        assert preview_body["rows_invalid"] == 0
        assert preview_body["rows_duplicate"] == 0
        assert len(preview_body["sample_rows"]) == 2
        assert preview_body["errors"] == []
        batch_id = preview_body["batch_id"]

        get_resp = client.get(
            f"/api/v1/part-imports/{batch_id}", headers={"Origin": ORIGIN}
        )
        assert get_resp.status_code == 200, get_resp.text
        get_body = get_resp.json()
        assert get_body["state"] == "previewing"
        assert get_body["format"] == "csv"
        assert len(get_body["items"]) == 2
        assert get_body["page"]["has_more"] is False
        dispositions = {item["disposition"] for item in get_body["items"]}
        assert dispositions == {"create"}

        commit_resp = client.post(f"/api/v1/part-imports/{batch_id}/commit", headers=headers)
        assert commit_resp.status_code == 200, commit_resp.text
        commit_body = commit_resp.json()
        assert commit_body == {"created": 2, "updated": 0, "skipped": 0}

        after_commit = client.get(
            f"/api/v1/part-imports/{batch_id}", headers={"Origin": ORIGIN}
        )
        assert after_commit.json()["state"] == "committed"
        assert all(item["resulting_part_id"] for item in after_commit.json()["items"])

        parts_resp = client.get("/api/v1/parts?q=PN-HAPPY", headers={"Origin": ORIGIN})
        numbers = {p["internal_part_number"] for p in parts_resp.json()["items"]}
        assert {"PN-HAPPY-A", "PN-HAPPY-B"} <= numbers

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'part_import_batch' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": batch_id},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("part_import.previewed") == 1
        assert counts.get("part_import.committed") == 1

        with migrated_engine.connect() as conn:
            part_created_count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events"
                    " WHERE entity_type = 'part' AND event_type = 'part.created'"
                    " AND organization_id = :org"
                ),
                {"org": org_a["org_id"]},
            ).scalar_one()
        assert part_created_count == 2


class TestCommitRollsBackOnInvalidRow:
    def test_commit_with_invalid_row_creates_zero_parts(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        _seed_each_unit(migrated_engine, org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        baseline = _parts_count(migrated_engine, org_a["org_id"])

        csv_text = (
            "internal_part_number,name,unit_code\n"
            "PN-ROLLBACK-VALID,Widget Valid,each\n"
            ",Widget Missing IPN,each\n"
        )
        preview_resp = _upload(client, headers, csv_text, filename="rollback.csv")
        assert preview_resp.status_code == 201, preview_resp.text
        preview_body = preview_resp.json()
        assert preview_body["rows_total"] == 2
        assert preview_body["rows_valid"] == 1
        assert preview_body["rows_invalid"] == 1
        assert len(preview_body["errors"]) >= 1
        batch_id = preview_body["batch_id"]

        commit_resp = client.post(f"/api/v1/part-imports/{batch_id}/commit", headers=headers)
        assert commit_resp.status_code == 409, commit_resp.text
        assert commit_resp.json()["error"]["code"] == "conflict_state"

        # SPEC §3 "rollback after failure": zero new parts rows, including
        # the row that was individually valid - all-or-nothing per batch.
        assert _parts_count(migrated_engine, org_a["org_id"]) == baseline

        batch_after = client.get(
            f"/api/v1/part-imports/{batch_id}", headers={"Origin": ORIGIN}
        )
        assert batch_after.json()["state"] == "previewing"


class TestDuplicateAgainstExistingPart:
    def test_row_matching_existing_active_part_is_skipped_not_created(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        existing_number = f"DUP-EXIST-{uuid.uuid4().hex[:8].upper()}"
        create_resp = client.post(
            "/api/v1/parts",
            json={
                "internal_part_number": existing_number,
                "name": "Pre-existing Part",
                "unit_definition_id": unit_id,
            },
            headers=headers,
        )
        assert create_resp.status_code == 201, create_resp.text

        csv_text = (
            "internal_part_number,name,unit_code\n"
            f"{existing_number.lower()},Duplicate Attempt,each\n"
            f"PN-DUP-NEW-{uuid.uuid4().hex[:8].upper()},Brand New Widget,each\n"
        )
        preview_resp = _upload(client, headers, csv_text, filename="dup.csv")
        assert preview_resp.status_code == 201, preview_resp.text
        preview_body = preview_resp.json()
        assert preview_body["rows_total"] == 2
        assert preview_body["rows_valid"] == 1
        assert preview_body["rows_duplicate"] == 1
        assert preview_body["rows_invalid"] == 0
        batch_id = preview_body["batch_id"]

        commit_resp = client.post(f"/api/v1/part-imports/{batch_id}/commit", headers=headers)
        assert commit_resp.status_code == 200, commit_resp.text
        assert commit_resp.json() == {"created": 1, "updated": 0, "skipped": 1}

        with migrated_engine.connect() as conn:
            count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM parts"
                    " WHERE organization_id = :org AND lower(internal_part_number) = :ipn"
                ),
                {"org": org_a["org_id"], "ipn": existing_number.lower()},
            ).scalar_one()
        assert count == 1  # still just the original part, no second row created


class TestCancel:
    def test_cancel_marks_rolled_back_and_blocks_commit(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        _seed_each_unit(migrated_engine, org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        csv_text = "internal_part_number,name,unit_code\nPN-CANCEL-1,Widget,each\n"
        preview_resp = _upload(client, headers, csv_text, filename="cancel.csv")
        batch_id = preview_resp.json()["batch_id"]

        cancel_resp = client.post(f"/api/v1/part-imports/{batch_id}/cancel", headers=headers)
        assert cancel_resp.status_code == 200, cancel_resp.text
        assert cancel_resp.json()["state"] == "rolled_back"

        with migrated_engine.connect() as conn:
            event_count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events"
                    " WHERE entity_type = 'part_import_batch' AND entity_id = :id"
                    " AND event_type = 'part_import.cancelled'"
                ),
                {"id": batch_id},
            ).scalar_one()
        assert event_count == 1

        commit_resp = client.post(f"/api/v1/part-imports/{batch_id}/commit", headers=headers)
        assert commit_resp.status_code == 409, commit_resp.text
        assert commit_resp.json()["error"]["code"] == "conflict_state"


class TestCrossOrgIsolation:
    def test_batch_from_other_org_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        _seed_each_unit(migrated_engine, org_a["org_id"])
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        csv_text = "internal_part_number,name,unit_code\nPN-XORG-1,Widget,each\n"
        preview_resp = _upload(client, headers_a, csv_text, filename="xorg.csv")
        batch_id = preview_resp.json()["batch_id"]

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))

        get_resp = client.get(
            f"/api/v1/part-imports/{batch_id}", headers={"Origin": ORIGIN}
        )
        # the shared client's session cookie now points at org_b after the
        # login above (same TestClient, same cookie jar) - mirrors
        # test_parts_api.py's cross-org pattern
        assert get_resp.status_code == 404
        assert get_resp.json()["error"]["code"] == "not_found"

        commit_resp = client.post(f"/api/v1/part-imports/{batch_id}/commit", headers=headers_b)
        assert commit_resp.status_code == 404
        assert commit_resp.json()["error"]["code"] == "not_found"

        cancel_resp = client.post(f"/api/v1/part-imports/{batch_id}/cancel", headers=headers_b)
        assert cancel_resp.status_code == 404
        assert cancel_resp.json()["error"]["code"] == "not_found"


class TestDoubleCommit:
    def test_committing_twice_is_conflict_state(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        _seed_each_unit(migrated_engine, org_a["org_id"])
        headers = _headers(_login_as(client, org_a, Role.ANALYST))

        csv_text = "internal_part_number,name,unit_code\nPN-TWICE-1,Widget,each\n"
        preview_resp = _upload(client, headers, csv_text, filename="twice.csv")
        batch_id = preview_resp.json()["batch_id"]

        first_commit = client.post(f"/api/v1/part-imports/{batch_id}/commit", headers=headers)
        assert first_commit.status_code == 200, first_commit.text

        second_commit = client.post(f"/api/v1/part-imports/{batch_id}/commit", headers=headers)
        assert second_commit.status_code == 409, second_commit.text
        assert second_commit.json()["error"]["code"] == "conflict_state"


class TestUploadValidation:
    def test_oversized_file_is_413(
        self, tiny_limit_client: TestClient, migrated_engine: Engine
    ) -> None:
        org = _make_org_with_members(migrated_engine, [Role.ANALYST])
        headers = _headers(_login_as(tiny_limit_client, org, Role.ANALYST))

        csv_text = "internal_part_number,name,unit_code\n" + (
            "PN-BIG,Widget with a very long name to exceed the tiny cap,each\n" * 5
        )
        resp = _upload(tiny_limit_client, headers, csv_text, filename="big.csv")
        assert resp.status_code == 413, resp.text
        assert resp.json()["error"]["code"] == "payload_too_large"

    def test_wrong_extension_is_415(
        self, client: TestClient, org_a: dict[str, Any]
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            "/api/v1/part-imports",
            files={
                "file": (
                    "parts.txt",
                    b"internal_part_number,name\nPN-1,Widget\n",
                    "text/plain",
                )
            },
            headers=headers,
        )
        assert resp.status_code == 415, resp.text
        assert resp.json()["error"]["code"] == "unsupported_media_type"
