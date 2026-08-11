"""Quote-document API integration tests (docs/planning/03-api-contract.md
§4.9): upload success with metadata (sha256/safe filename/state), duplicate
content (409), oversized upload (413), wrong media type (415), download
round-trip (bytes + headers), cross-org 404 everywhere, role enforcement
(viewer reads/downloads but cannot upload or archive), and audit coverage.

Fixture pattern mirrors test_quotes_api.py / test_part_imports_api.py: a
committed org + user + membership, built directly against the migrated
database, driven entirely through the HTTP API from there. The oversized-
upload test uses a second app instance with a tiny `max_upload_bytes`
(`tiny_limit_client`), the same technique test_part_imports_api.py already
uses, for the same reason: a real 20 MiB body would make the test slow
without exercising any more of the streamed-read code path than a small one
does.
"""

from __future__ import annotations

import hashlib
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

PDF_HEAD = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
PDF_BYTES = PDF_HEAD + b"1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n" + b"filler " * 20
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64  # PE header magic, not an allowed kind


@pytest.fixture(autouse=True)
def _cleanup_quote_documents(migrated_engine: Engine) -> Generator[None, None, None]:
    """This module is the first integration test file to commit real
    `quote_documents` rows through the live API (every other API test file's
    `client` fixture behaves identically — real commits against the shared,
    session-scoped `migrated_engine` — but no earlier test file happens to
    write to *this* table). Left in place across tests, those rows would
    leak into `test_documents_schema.py::
    test_content_sha256_duplicate_allowed_across_orgs`'s deliberately
    unscoped `SELECT count(*) FROM quote_documents` (Postgres READ COMMITTED
    visibility does not care which connection committed a row). That file is
    outside this task's allowed-edits list, so the fix lives here instead:
    delete this module's own rows after every test, leaving the shared table
    exactly as any other module would find it."""
    yield
    with migrated_engine.begin() as conn:
        conn.execute(sqltext("DELETE FROM quote_documents"))


@pytest.fixture(scope="module")
def client(
    database_url: str, migrated_engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> Generator[TestClient, None, None]:
    storage_root = tmp_path_factory.mktemp("doc-storage")
    settings = Settings(
        environment=Environment.TEST,
        database_url=database_url,
        allowed_origins=[ORIGIN],
        rate_limit_auth_per_minute=100_000,
        rate_limit_per_minute=100_000,
        storage_root=str(storage_root),
    )
    app = create_app(settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield c
    app.state.engine.dispose()


@pytest.fixture(scope="module")
def tiny_limit_client(
    database_url: str, migrated_engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> Generator[TestClient, None, None]:
    """A second app instance with a tiny `max_upload_bytes`, isolated to the
    413 test below (same technique as test_part_imports_api.py's fixture of
    the same name)."""
    storage_root = tmp_path_factory.mktemp("doc-storage-tiny")
    settings = Settings(
        environment=Environment.TEST,
        database_url=database_url,
        allowed_origins=[ORIGIN],
        rate_limit_auth_per_minute=100_000,
        rate_limit_per_minute=100_000,
        storage_root=str(storage_root),
        max_upload_bytes=64,
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
    return _make_org_with_members(migrated_engine, [Role.ANALYST, Role.ADMINISTRATOR, Role.VIEWER])


@pytest.fixture()
def org_b(migrated_engine: Engine) -> dict[str, Any]:
    return _make_org_with_members(migrated_engine, [Role.ANALYST, Role.ADMINISTRATOR, Role.VIEWER])


def _seed_unit(migrated_engine: Engine, organization_id: str) -> str:
    from sqlalchemy.orm import Session as SaSession

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


def _login_as(client: TestClient, org: dict[str, Any], role: Role) -> dict[str, str]:
    creds = org["users"][role.value]
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


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


def _create_part(client: TestClient, headers: dict[str, str], unit_id: str) -> dict[str, Any]:
    resp = client.post("/api/v1/parts", json=_part_payload(unit_id), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _rfq_payload(part_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": f"RFQ-{uuid.uuid4().hex[:8].upper()}",
        "internal_reference": f"REF-{uuid.uuid4().hex[:8].upper()}",
        "base_currency": "USD",
        "due_date": "2026-12-01",
        "lines": [{"part_id": part_id, "required_quantity": "10.000000"}],
    }
    payload.update(overrides)
    return payload


def _create_rfq(client: TestClient, headers: dict[str, str], part_id: str) -> dict[str, Any]:
    resp = client.post("/api/v1/rfqs", json=_rfq_payload(part_id), headers=headers)
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


def _create_supplier(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    resp = client.post("/api/v1/suppliers", json=_supplier_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _setup_rfq(
    client: TestClient, headers: dict[str, str], migrated_engine: Engine, org: dict[str, Any]
) -> dict[str, Any]:
    unit_id = _seed_unit(migrated_engine, org["org_id"])
    part = _create_part(client, headers, unit_id)
    rfq = _create_rfq(client, headers, part["id"])
    return {"unit_id": unit_id, "part": part, "rfq": rfq}


def _upload(
    client: TestClient,
    headers: dict[str, str],
    rfq_id: str,
    *,
    filename: str = "quote.pdf",
    data: bytes = PDF_BYTES,
    content_type: str = "application/pdf",
    supplier_id: str | None = None,
) -> Any:
    form: dict[str, str] = {}
    if supplier_id is not None:
        form["supplier_id"] = supplier_id
    return client.post(
        f"/api/v1/rfqs/{rfq_id}/quote-documents",
        files={"file": (filename, data, content_type)},
        data=form,
        headers=headers,
    )


class TestDocumentUpload:
    def test_upload_pdf_returns_201_with_metadata(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)

        resp = _upload(client, headers, ctx["rfq"]["id"], filename="Supplier Quote (rev1).pdf")
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["rfq_id"] == ctx["rfq"]["id"]
        assert body["supplier_id"] is None
        assert body["original_filename"] == "Supplier Quote (rev1).pdf"
        assert body["state"] == "stored"
        assert body["size_bytes"] == len(PDF_BYTES)
        assert body["content_sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
        assert body["detected_mime"] == "application/pdf"
        assert "storage_key" not in body  # never exposed (never a storage URL, §4.9)
        assert uuid.UUID(body["id"])

    def test_upload_with_supplier_id(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        supplier = _create_supplier(client, headers)

        resp = _upload(
            client,
            headers,
            ctx["rfq"]["id"],
            filename="supplier-quote.pdf",
            supplier_id=supplier["id"],
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["supplier_id"] == supplier["id"]

    def test_duplicate_content_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)

        first = _upload(client, headers, ctx["rfq"]["id"], filename="one.pdf")
        assert first.status_code == 201, first.text

        second = _upload(client, headers, ctx["rfq"]["id"], filename="two.pdf")
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["error"]["code"] == "conflict_duplicate"
        detail = body["error"]["details"][0]
        assert detail["value_hint"] == first.json()["id"]

    def test_oversized_file_is_413(
        self, tiny_limit_client: TestClient, migrated_engine: Engine
    ) -> None:
        org = _make_org_with_members(migrated_engine, [Role.ANALYST])
        headers = _headers(_login_as(tiny_limit_client, org, Role.ANALYST))
        ctx = _setup_rfq(tiny_limit_client, headers, migrated_engine, org)

        big = PDF_HEAD + b"x" * 500  # well over the 64-byte tiny cap
        resp = _upload(tiny_limit_client, headers, ctx["rfq"]["id"], data=big)
        assert resp.status_code == 413, resp.text
        assert resp.json()["error"]["code"] == "payload_too_large"

    def test_wrong_type_is_415(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)

        resp = _upload(
            client,
            headers,
            ctx["rfq"]["id"],
            filename="malware.exe",
            data=EXE_BYTES,
            content_type="application/octet-stream",
        )
        assert resp.status_code == 415, resp.text
        assert resp.json()["error"]["code"] == "unsupported_media_type"

    def test_upload_to_unknown_rfq_is_404(self, client: TestClient, org_a: dict[str, Any]) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = _upload(client, headers, str(uuid.uuid4()))
        assert resp.status_code == 404, resp.text

    def test_upload_with_unknown_supplier_is_404(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        resp = _upload(client, headers, ctx["rfq"]["id"], supplier_id=str(uuid.uuid4()))
        assert resp.status_code == 404, resp.text

    def test_viewer_cannot_upload(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, analyst_headers, migrated_engine, org_a)

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = _upload(client, viewer_headers, ctx["rfq"]["id"], filename="viewer-try.pdf")
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "forbidden_role"


class TestDocumentDownload:
    def test_download_round_trips_bytes_and_headers(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        uploaded = _upload(client, headers, ctx["rfq"]["id"], filename="Round Trip.pdf").json()

        resp = client.get(
            f"/api/v1/quote-documents/{uploaded['id']}/content", headers={"Origin": ORIGIN}
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == PDF_BYTES
        assert resp.headers["content-type"].startswith("application/pdf")
        disposition = resp.headers["content-disposition"]
        # RFC 6266 (2026-08 security audit MEDIUM-3): ASCII fallback plus the
        # UTF-8 filename* form, and always latin-1-encodable.
        assert disposition.startswith('attachment; filename="Round Trip.pdf"')
        assert "filename*=UTF-8''Round%20Trip.pdf" in disposition
        disposition.encode("latin-1")
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_viewer_can_download(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, analyst_headers, migrated_engine, org_a)
        uploaded = _upload(
            client, analyst_headers, ctx["rfq"]["id"], filename="viewer-dl.pdf"
        ).json()

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.get(
            f"/api/v1/quote-documents/{uploaded['id']}/content", headers={"Origin": ORIGIN}
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == PDF_BYTES
        # headers dict above intentionally unused for GET (no CSRF needed on
        # safe methods); viewer_headers still proves the earlier login works
        assert viewer_headers["Origin"] == ORIGIN


class TestDocumentList:
    def test_list_returns_uploaded_document(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        uploaded = _upload(client, headers, ctx["rfq"]["id"], filename="listed.pdf").json()

        resp = client.get(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/quote-documents", headers={"Origin": ORIGIN}
        )
        assert resp.status_code == 200, resp.text
        ids = [item["id"] for item in resp.json()["items"]]
        assert uploaded["id"] in ids

    def test_list_filters_by_state(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        _upload(client, headers, ctx["rfq"]["id"], filename="stateful.pdf")

        resp = client.get(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/quote-documents",
            params={"state": "stored"},
            headers={"Origin": ORIGIN},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["items"]) >= 1

        resp_none = client.get(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/quote-documents",
            params={"state": "quarantined"},
            headers={"Origin": ORIGIN},
        )
        assert resp_none.status_code == 200, resp_none.text
        assert resp_none.json()["items"] == []


class TestCrossOrgIsolation:
    def test_cross_org_get_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers_a, migrated_engine, org_a)
        uploaded = _upload(client, headers_a, ctx["rfq"]["id"], filename="private.pdf").json()

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = client.get(f"/api/v1/quote-documents/{uploaded['id']}", headers=headers_b)
        assert resp.status_code == 404, resp.text

    def test_cross_org_content_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers_a, migrated_engine, org_a)
        uploaded = _upload(client, headers_a, ctx["rfq"]["id"], filename="private2.pdf").json()

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = client.get(f"/api/v1/quote-documents/{uploaded['id']}/content", headers=headers_b)
        assert resp.status_code == 404, resp.text

    def test_cross_org_list_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers_a, migrated_engine, org_a)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = client.get(f"/api/v1/rfqs/{ctx['rfq']['id']}/quote-documents", headers=headers_b)
        assert resp.status_code == 404, resp.text

    def test_cross_org_upload_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers_a, migrated_engine, org_a)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = _upload(client, headers_b, ctx["rfq"]["id"], filename="cross-org.pdf")
        assert resp.status_code == 404, resp.text


class TestArchive:
    def test_archive_by_administrator(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        uploaded = _upload(client, headers, ctx["rfq"]["id"], filename="archive-me.pdf").json()

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        resp = client.post(
            f"/api/v1/quote-documents/{uploaded['id']}/archive",
            json={"reason": "superseded by a newer scan"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_archived"] is True
        assert body["archive_reason"] == "superseded by a newer scan"

    def test_analyst_cannot_archive(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        uploaded = _upload(client, headers, ctx["rfq"]["id"], filename="no-archive.pdf").json()

        resp = client.post(
            f"/api/v1/quote-documents/{uploaded['id']}/archive",
            json={"reason": "not allowed"},
            headers=headers,
        )
        assert resp.status_code == 403, resp.text

    def test_archiving_twice_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        uploaded = _upload(client, headers, ctx["rfq"]["id"], filename="double-archive.pdf").json()

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        first = client.post(
            f"/api/v1/quote-documents/{uploaded['id']}/archive",
            json={"reason": "first"},
            headers=admin_headers,
        )
        assert first.status_code == 200, first.text

        second = client.post(
            f"/api/v1/quote-documents/{uploaded['id']}/archive",
            json={"reason": "second"},
            headers=admin_headers,
        )
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "conflict_state"


class TestAudit:
    def test_audit_event_written_on_upload(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        uploaded = _upload(client, headers, ctx["rfq"]["id"], filename="audited.pdf").json()

        with migrated_engine.connect() as conn:
            row = conn.execute(
                sqltext(
                    "SELECT event_type, entity_id FROM audit_events"
                    " WHERE entity_id = :id AND event_type = 'document.uploaded'"
                ),
                {"id": uploaded["id"]},
            ).one()
        assert row.event_type == "document.uploaded"
        assert str(row.entity_id) == uploaded["id"]

    def test_audit_event_written_on_archive(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_rfq(client, headers, migrated_engine, org_a)
        uploaded = _upload(client, headers, ctx["rfq"]["id"], filename="audited-archive.pdf").json()

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        client.post(
            f"/api/v1/quote-documents/{uploaded['id']}/archive",
            json={"reason": "audit check"},
            headers=admin_headers,
        )

        with migrated_engine.connect() as conn:
            row = conn.execute(
                sqltext(
                    "SELECT event_type FROM audit_events"
                    " WHERE entity_id = :id AND event_type = 'document.archived'"
                ),
                {"id": uploaded["id"]},
            ).one()
        assert row.event_type == "document.archived"
