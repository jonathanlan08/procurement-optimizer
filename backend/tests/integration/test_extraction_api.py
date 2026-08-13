"""Extraction pipeline API integration tests (docs/planning/03-api-contract.md
§4.10, docs/planning/04-document-pipeline.md), driven end to end through the
HTTP API against the four committed synthetic fixtures
(`backend/tests/fixtures/documents/*`, `backend/tests/fixtures/extraction/*`,
`backend/scripts/generate_fixtures.py`):

- **Shenzhen Precision (PDF):** mostly-HIGH-confidence golden with a real
  sub-0.95 field (an open-ended price-break boundary at 0.30, plus
  `lead_time_days`/`country_of_origin` in the medium band) -> the run lands
  `needs_review`; confirming every pending field walks it to `ready`;
  materializing produces a real `Quote` with correct 8dp/6dp decimal values.
- **Pacific Metal (PNG):** has a field below the 0.60 "low" band
  (`lines[0].unit_price` at 0.55) -> materializing before it is confirmed is
  `409`; confirming it (and everything else pending) lets materialization
  succeed.
- **Nordic Fastener (CSV):** the SPEC's prompt-injection acceptance test -
  `injection_suspected=True` on the run, a `security.injection_suspected`
  audit event with a matched snippet, and the extracted/materialized price is
  the REAL `0.024`, never the injected `0.01`.
- **Baltic Casting (XLSX):** a genuinely blank `Payment Terms` cell ->
  materialized `terms.payment_terms` stays `NULL`, never invented; a
  correction made to an already-materialized run's field writes a real
  `QuoteCorrection` row (before/after) plus an audit event (see
  `services/extraction_service.py`'s own module docstring for why a
  correction can only produce a `QuoteCorrection` row once a quote exists -
  a genuine conflict with the FROZEN `QuoteCorrection.quote_id NOT NULL`
  constraint, flagged there in detail).

Plus cross-org 404 on runs/fields, viewer cannot confirm/correct/materialize,
and two state-machine `409`s (materializing a `needs_review` run; starting a
run on a document that is not `stored`).

Fixture pattern mirrors test_documents_api.py / test_quotes_api.py: a
committed org + user + membership, built directly against the migrated
database, driven entirely through the HTTP API from there.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
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

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "documents"

_CONTENT_TYPE_OF_SUFFIX = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _fixture_bytes(filename: str) -> bytes:
    return (FIXTURES_DIR / filename).read_bytes()


# -- app / org / auth scaffolding (mirrors test_documents_api.py / test_quotes_api.py) --


@pytest.fixture(scope="module")
def client(
    database_url: str, migrated_engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> Generator[TestClient, None, None]:
    storage_root = tmp_path_factory.mktemp("extraction-doc-storage")
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


def _seed_each_unit(migrated_engine: Engine, organization_id: str) -> str:
    """An org-scoped unit definition with code `"each"` - every golden
    fixture's `unit_of_measure` is either literally `"each"` or genuinely
    MISSING, both of which resolve through `_resolve_unit_definition_id`'s
    "each" fallback (see `services/extraction_service.py`'s own module
    docstring). Deliberately **org-scoped, not global**: a global
    (`organization_id IS NULL`) row is shared, real-committed, cross-test-
    file state (`unit_definitions` carries a partial unique index on
    `lower(code)` for global rows only - migration 0003) - the same
    `_seed_unit(..., organization_id=...)` technique
    test_documents_api.py/test_quotes_api.py already use for exactly this
    reason, just with a fixed code instead of a random one."""
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


def _create_part(client: TestClient, headers: dict[str, str], unit_id: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/parts",
        json={
            "internal_part_number": f"PN-{uuid.uuid4().hex[:8].upper()}",
            "name": "Test Component",
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


def _create_supplier(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/suppliers",
        json={
            "code": f"SUP-{uuid.uuid4().hex[:8].upper()}",
            "name": "Test Supplier",
            "country_code": "US",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _invite_supplier(
    client: TestClient, headers: dict[str, str], rfq_id: str, supplier_id: str
) -> None:
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/suppliers",
        json={"supplier_ids": [supplier_id]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


def _open_rfq(client: TestClient, headers: dict[str, str], rfq_id: str) -> None:
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/status",
        json={"to_status": "open", "reason": "test"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def _setup_open_rfq_with_supplier(
    client: TestClient, headers: dict[str, str], each_unit_id: str
) -> dict[str, Any]:
    part = _create_part(client, headers, each_unit_id)
    rfq = _create_rfq(client, headers, part["id"])
    supplier = _create_supplier(client, headers)
    _invite_supplier(client, headers, rfq["id"], supplier["id"])
    _open_rfq(client, headers, rfq["id"])
    return {"rfq": rfq, "supplier": supplier, "part": part}


def _upload_document(
    client: TestClient, headers: dict[str, str], rfq_id: str, filename: str
) -> dict[str, Any]:
    data = _fixture_bytes(filename)
    content_type = _CONTENT_TYPE_OF_SUFFIX[Path(filename).suffix]
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/quote-documents",
        files={"file": (filename, data, content_type)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _start_run(client: TestClient, headers: dict[str, str], document_id: str) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/quote-documents/{document_id}/extraction-runs", headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _get_run(client: TestClient, run_id: str) -> dict[str, Any]:
    resp = client.get(f"/api/v1/extraction-runs/{run_id}", headers={"Origin": ORIGIN})
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _list_fields(
    client: TestClient, run_id: str, **params: Any
) -> list[dict[str, Any]]:
    resp = client.get(
        f"/api/v1/extraction-runs/{run_id}/fields",
        params=params,
        headers={"Origin": ORIGIN},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]  # type: ignore[no-any-return]


def _field_by_path(client: TestClient, run_id: str, field_path: str) -> dict[str, Any]:
    for item in _list_fields(client, run_id):
        if item["field_path"] == field_path:
            return item
    raise AssertionError(f"no field with field_path={field_path!r} on run {run_id}")


def _confirm_all_pending(client: TestClient, headers: dict[str, str], run_id: str) -> int:
    pending = _list_fields(client, run_id, is_confirmed="false")
    for item in pending:
        resp = client.patch(
            f"/api/v1/extraction-runs/{run_id}/fields/{item['id']}",
            json={"is_confirmed": True},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    return len(pending)


def _materialize(
    client: TestClient, headers: dict[str, str], run_id: str, supplier_id: str
) -> Any:
    return client.post(
        f"/api/v1/extraction-runs/{run_id}/confirm",
        json={"supplier_id": supplier_id},
        headers=headers,
    )


# -- (a) Shenzhen Precision PDF: happy path, confirm -> ready -> materialize --


class TestPdfHappyPath:
    def test_full_pipeline_produces_correct_decimals(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers, each_unit_id)
        document = _upload_document(
            client, headers, ctx["rfq"]["id"], "shenzhen_precision_quote.pdf"
        )

        run = _start_run(client, headers, document["id"])
        assert run["state"] == "needs_review", run
        assert run["simulated"] is True
        assert run["provider"] == "mock"
        assert run["injection_suspected"] is False

        # A HIGH-band field (e.g. currency, 0.99 in the golden) needs no
        # confirmation on its own; the run only advances once every pending
        # field (the sub-0.95 ones) has been confirmed.
        currency_field = _field_by_path(client, run["id"], "currency")
        assert currency_field["band"] == "high"
        assert currency_field["requires_confirmation"] is False

        confirmed_count = _confirm_all_pending(client, headers, run["id"])
        assert confirmed_count > 0

        run = _get_run(client, run["id"])
        assert run["state"] == "ready"

        resp = _materialize(client, headers, run["id"], ctx["supplier"]["id"])
        assert resp.status_code == 201, resp.text
        quote = resp.json()

        assert quote["source"] == "extracted"
        assert quote["status"] == "draft"
        assert quote["quote_number"] == "SPM-Q-58231"
        assert quote["quote_date"] == "2026-03-10"
        assert quote["expiration_date"] == "2026-04-10"
        assert quote["currency"] == "USD"
        assert quote["supplier_id"] == ctx["supplier"]["id"]

        terms = quote["terms"]
        assert terms["payment_terms"] == "Net 60"
        assert terms["shipping_terms"] == "FOB Shenzhen"
        assert terms["warranty_terms"] == "12 months against manufacturing defects"
        # never stated in this golden -> never invented
        assert terms["exceptions"] is None
        assert terms["exclusions"] is None

        assert len(quote["lines"]) == 3
        line1 = next(ln for ln in quote["lines"] if ln["quoted_part_number"] == "MF-BRKT-010")
        assert line1["description"] == "L-Bracket, 3mm Steel, Zinc Plated"
        assert line1["quantity"] == "5000.000000"
        assert line1["unit_price"] == "0.42000000"
        assert line1["unit_definition_id"] == each_unit_id
        assert line1["lead_time_days"] == 35
        # "China" (a full country name, not ISO alpha-2) cannot be losslessly
        # written into the CHAR(2) column - see extraction_service.py's own
        # module docstring; left NULL rather than truncated/guessed.
        assert line1["country_of_origin"] is None

        breaks = sorted(line1["price_breaks"], key=lambda b: float(b["min_quantity"]))
        assert len(breaks) == 3
        assert breaks[0]["min_quantity"] == "1.000000"
        assert breaks[0]["max_quantity"] == "999.000000"
        assert breaks[0]["unit_price"] == "0.55000000"
        assert breaks[1]["min_quantity"] == "1000.000000"
        assert breaks[1]["max_quantity"] == "4999.000000"
        assert breaks[1]["unit_price"] == "0.48000000"
        assert breaks[2]["min_quantity"] == "5000.000000"
        # open-ended top tier: genuinely no stated upper bound.
        assert breaks[2]["max_quantity"] is None
        assert breaks[2]["unit_price"] == "0.42000000"

        line2 = next(ln for ln in quote["lines"] if ln["quoted_part_number"] == "MF-BRKT-014")
        assert line2["quantity"] == "2000.000000"
        assert line2["unit_price"] == "0.68000000"

        # Run is now terminal-ish (materialized).
        run_after = _get_run(client, run["id"])
        assert run_after["state"] == "materialized"

        # extraction.materialized audit event exists and names both the run
        # and the quote.
        with migrated_engine.connect() as conn:
            row = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE event_type = 'extraction.materialized' AND entity_id = :qid"
                ),
                {"qid": quote["id"]},
            ).one()
        assert row.after_state["extraction_run_id"] == run["id"]


# -- (b) Pacific Metal PNG: low-confidence gate ------------------------------


class TestPngLowConfidenceGate:
    def test_materialize_before_confirm_is_409_then_succeeds(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers, each_unit_id)
        document = _upload_document(client, headers, ctx["rfq"]["id"], "pacific_metal_quote.png")

        run = _start_run(client, headers, document["id"])
        assert run["state"] == "needs_review"

        low_price_field = _field_by_path(client, run["id"], "lines[0].unit_price")
        assert low_price_field["band"] == "low"
        assert low_price_field["normalized_value"] == "36.80"
        assert low_price_field["is_confirmed"] is False

        # materializing straight from needs_review is a state-machine
        # violation, 409, regardless of which fields are pending.
        resp = _materialize(client, headers, run["id"], ctx["supplier"]["id"])
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

        _confirm_all_pending(client, headers, run["id"])
        run = _get_run(client, run["id"])
        assert run["state"] == "ready"

        resp = _materialize(client, headers, run["id"], ctx["supplier"]["id"])
        assert resp.status_code == 201, resp.text
        quote = resp.json()
        line1 = next(ln for ln in quote["lines"] if ln["quoted_part_number"] == "MF-PLT-021")
        assert line1["unit_price"] == "36.80000000"


# -- (c) Nordic Fastener CSV: prompt-injection acceptance test --------------


class TestCsvInjection:
    def test_injection_flagged_but_real_price_extracted(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers, each_unit_id)
        document = _upload_document(
            client, headers, ctx["rfq"]["id"], "nordic_fastener_quote.csv"
        )

        run = _start_run(client, headers, document["id"])
        assert run["injection_suspected"] is True

        with migrated_engine.connect() as conn:
            row = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE event_type = 'security.injection_suspected' AND entity_id = :rid"
                ),
                {"rid": run["id"]},
            ).one()
        snippets = [s.upper() for s in row.after_state["matched_snippets"]]
        assert any("IGNORE" in s and "INSTRUCTIONS" in s for s in snippets)

        # The field extracted from the injected line is the REAL price
        # (0.024), not the "0.01" the injected text tried to set - there is
        # no schema slot the instruction could have landed in even in
        # principle (ExtractedLine has no "notes" field at all).
        injected_line_price = _field_by_path(client, run["id"], "lines[1].unit_price")
        assert injected_line_price["normalized_value"] == "0.024"
        assert injected_line_price["injection_flagged"] is True

        _confirm_all_pending(client, headers, run["id"])
        resp = _materialize(client, headers, run["id"], ctx["supplier"]["id"])
        assert resp.status_code == 201, resp.text
        quote = resp.json()
        injected_line = next(
            ln for ln in quote["lines"] if ln["quoted_part_number"] == "MF-SCR-101"
        )
        assert injected_line["unit_price"] == "0.02400000"
        assert injected_line["unit_price"] != "0.01000000"


# -- (d) Baltic Casting XLSX: missing term + correction flow ----------------


class TestXlsxMissingTermsAndCorrections:
    def test_missing_payment_terms_stays_null_and_correction_writes_row(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers, each_unit_id)
        document = _upload_document(
            client, headers, ctx["rfq"]["id"], "baltic_casting_quote.xlsx"
        )

        run = _start_run(client, headers, document["id"])
        payment_terms_field = _field_by_path(client, run["id"], "terms.payment_terms")
        assert payment_terms_field["normalized_value"] is None
        assert payment_terms_field["raw_value"] is None

        _confirm_all_pending(client, headers, run["id"])
        resp = _materialize(client, headers, run["id"], ctx["supplier"]["id"])
        assert resp.status_code == 201, resp.text
        quote = resp.json()
        assert quote["terms"]["payment_terms"] is None
        assert quote["terms"]["shipping_terms"] == "FOB Riga"

        # -- correction flow, on the now-materialized run --
        shipping_field = _field_by_path(client, run["id"], "terms.shipping_terms")
        resp = client.patch(
            f"/api/v1/extraction-runs/{run['id']}/fields/{shipping_field['id']}",
            json={
                "normalized_value": "FOB Riga, freight prepaid",
                "is_confirmed": True,
                "reason": "reviewer clarified shipping terms",
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        corrected = resp.json()
        assert corrected["normalized_value"] == "FOB Riga, freight prepaid"

        with migrated_engine.connect() as conn:
            correction_row = conn.execute(
                sqltext(
                    "SELECT before_value, after_value, reason, quote_id, extraction_field_id"
                    " FROM quote_corrections WHERE extraction_field_id = :fid"
                ),
                {"fid": shipping_field["id"]},
            ).one()
            audit_row = conn.execute(
                sqltext(
                    "SELECT event_type FROM audit_events"
                    " WHERE event_type = 'extraction.field_corrected' AND entity_id = :fid"
                ),
                {"fid": shipping_field["id"]},
            ).one()
        assert correction_row.before_value == "FOB Riga"
        assert correction_row.after_value == "FOB Riga, freight prepaid"
        assert correction_row.reason == "reviewer clarified shipping terms"
        assert str(correction_row.quote_id) == quote["id"]
        assert audit_row.event_type == "extraction.field_corrected"

    # -- cross-org isolation --------------------------------------------

    def test_cross_org_404_on_run_and_fields(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers_a, each_unit_id)
        document = _upload_document(
            client, headers_a, ctx["rfq"]["id"], "baltic_casting_quote.xlsx"
        )
        run = _start_run(client, headers_a, document["id"])
        field = _field_by_path(client, run["id"], "terms.shipping_terms")

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))

        resp = client.get(f"/api/v1/extraction-runs/{run['id']}", headers=headers_b)
        assert resp.status_code == 404, resp.text

        resp = client.get(f"/api/v1/extraction-runs/{run['id']}/fields", headers=headers_b)
        assert resp.status_code == 404, resp.text

        resp = client.patch(
            f"/api/v1/extraction-runs/{run['id']}/fields/{field['id']}",
            json={"is_confirmed": True},
            headers=headers_b,
        )
        assert resp.status_code == 404, resp.text

        resp = client.post(
            f"/api/v1/quote-documents/{document['id']}/extraction-runs", headers=headers_b
        )
        assert resp.status_code == 404, resp.text

    # -- role enforcement -------------------------------------------------

    def test_viewer_cannot_confirm_correct_or_materialize(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, analyst_headers, each_unit_id)
        document = _upload_document(
            client, analyst_headers, ctx["rfq"]["id"], "baltic_casting_quote.xlsx"
        )
        run = _start_run(client, analyst_headers, document["id"])
        field = _field_by_path(client, run["id"], "terms.shipping_terms")

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))

        resp = client.patch(
            f"/api/v1/extraction-runs/{run['id']}/fields/{field['id']}",
            json={"is_confirmed": True},
            headers=viewer_headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "forbidden_role"

        resp = client.patch(
            f"/api/v1/extraction-runs/{run['id']}/fields/{field['id']}",
            json={"normalized_value": "x", "is_confirmed": True},
            headers=viewer_headers,
        )
        assert resp.status_code == 403, resp.text

        resp = client.post(
            f"/api/v1/extraction-runs/{run['id']}/confirm",
            json={"supplier_id": ctx["supplier"]["id"]},
            headers=viewer_headers,
        )
        assert resp.status_code == 403, resp.text

        resp = client.post(
            f"/api/v1/quote-documents/{document['id']}/extraction-runs", headers=viewer_headers
        )
        assert resp.status_code == 403, resp.text

    # -- state-machine violations -----------------------------------------

    def test_materialize_needs_review_run_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers, each_unit_id)
        document = _upload_document(
            client, headers, ctx["rfq"]["id"], "baltic_casting_quote.xlsx"
        )
        run = _start_run(client, headers, document["id"])
        assert run["state"] == "needs_review"

        resp = _materialize(client, headers, run["id"], ctx["supplier"]["id"])
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_start_run_on_non_stored_document_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers, each_unit_id)
        document = _upload_document(
            client, headers, ctx["rfq"]["id"], "baltic_casting_quote.xlsx"
        )
        with migrated_engine.begin() as conn:
            conn.execute(
                sqltext("UPDATE quote_documents SET state = 'processing' WHERE id = :id"),
                {"id": document["id"]},
            )

        resp = client.post(
            f"/api/v1/quote-documents/{document['id']}/extraction-runs", headers=headers
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"


# -- (e) bulk field confirmation (2026-08 product-audit remediation, P2) ----


def _confirm_all_route(client: TestClient, headers: dict[str, str], run_id: str) -> Any:
    return client.post(
        f"/api/v1/extraction-runs/{run_id}/fields/confirm-all", headers=headers
    )


def _confirm_required_fields_one_by_one(
    client: TestClient, headers: dict[str, str], run_id: str
) -> int:
    """The one-at-a-time baseline `_confirm_all_pending` above does NOT
    reproduce: that helper confirms every field with `is_confirmed=false`
    (including fields that never required confirmation at all, e.g. HIGH-
    band fields), whereas the bulk route only touches fields that actually
    `requires_confirmation`. This helper matches the bulk route's own
    filter exactly, so it is the correct one-at-a-time baseline to compare
    the bulk route's resulting run state against."""
    pending = _list_fields(client, run_id, requires_confirmation="true", is_confirmed="false")
    for item in pending:
        resp = client.patch(
            f"/api/v1/extraction-runs/{run_id}/fields/{item['id']}",
            json={"is_confirmed": True},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    return len(pending)


class TestBulkConfirmFields:
    def test_confirm_all_reaches_ready_matching_field_by_field(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        """Two separate orgs upload the byte-identical Shenzhen Precision
        fixture (sha256 dedupe is per-org, so this is the only way to get
        two runs with genuinely identical extracted fields within one test)
        and drive one run to `ready` field-by-field, the other via the new
        bulk route - the run must land in the same state either way, and
        the same set of fields ends up confirmed."""
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_a = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx_a = _setup_open_rfq_with_supplier(client, headers_a, each_unit_a)
        doc_a = _upload_document(
            client, headers_a, ctx_a["rfq"]["id"], "shenzhen_precision_quote.pdf"
        )
        run_a = _start_run(client, headers_a, doc_a["id"])
        assert run_a["state"] == "needs_review"
        confirmed_count_a = _confirm_required_fields_one_by_one(client, headers_a, run_a["id"])
        assert confirmed_count_a > 0
        run_a = _get_run(client, run_a["id"])
        assert run_a["state"] == "ready"

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        each_unit_b = _seed_each_unit(migrated_engine, org_b["org_id"])
        ctx_b = _setup_open_rfq_with_supplier(client, headers_b, each_unit_b)
        doc_b = _upload_document(
            client, headers_b, ctx_b["rfq"]["id"], "shenzhen_precision_quote.pdf"
        )
        run_b = _start_run(client, headers_b, doc_b["id"])
        assert run_b["state"] == "needs_review"

        resp = _confirm_all_route(client, headers_b, run_b["id"])
        assert resp.status_code == 200, resp.text
        run_b_after = resp.json()
        assert run_b_after["id"] == run_b["id"]
        assert run_b_after["state"] == "ready"
        assert run_b_after["fields_requiring_review"] == 0

        # same fixture, same extracted fields -> the field-by-field run and
        # the bulk-confirmed run required confirming the same number of
        # fields to get there.
        fields_b = _list_fields(client, run_b["id"])
        still_pending_b = [
            f for f in fields_b if f["requires_confirmation"] and not f["is_confirmed"]
        ]
        assert still_pending_b == []
        confirmed_required_b = [
            f for f in fields_b if f["requires_confirmation"] and f["is_confirmed"]
        ]
        assert len(confirmed_required_b) == confirmed_count_a

        with migrated_engine.connect() as conn:
            audit_rows = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE event_type = 'extraction.fields_confirmed_bulk'"
                    " AND entity_id = :rid"
                ),
                {"rid": run_b["id"]},
            ).all()
        assert len(audit_rows) == 1
        assert audit_rows[0].after_state["confirmed_count"] == len(confirmed_required_b)

    def test_confirm_all_idempotent_second_call_is_noop(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers, each_unit_id)
        document = _upload_document(
            client, headers, ctx["rfq"]["id"], "shenzhen_precision_quote.pdf"
        )
        run = _start_run(client, headers, document["id"])

        resp = _confirm_all_route(client, headers, run["id"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "ready"

        # second call: nothing left requiring confirmation -> clean no-op,
        # not a 409, and no second audit event.
        resp2 = _confirm_all_route(client, headers, run["id"])
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["state"] == "ready"

        with migrated_engine.connect() as conn:
            count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events"
                    " WHERE event_type = 'extraction.fields_confirmed_bulk'"
                    " AND entity_id = :rid"
                ),
                {"rid": run["id"]},
            ).scalar_one()
        assert count == 1

    def test_confirm_all_viewer_is_403(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, analyst_headers, each_unit_id)
        document = _upload_document(
            client, analyst_headers, ctx["rfq"]["id"], "shenzhen_precision_quote.pdf"
        )
        run = _start_run(client, analyst_headers, document["id"])

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = _confirm_all_route(client, viewer_headers, run["id"])
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "forbidden_role"

    def test_confirm_all_cross_org_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        each_unit_id = _seed_each_unit(migrated_engine, org_a["org_id"])
        ctx = _setup_open_rfq_with_supplier(client, headers_a, each_unit_id)
        document = _upload_document(
            client, headers_a, ctx["rfq"]["id"], "shenzhen_precision_quote.pdf"
        )
        run = _start_run(client, headers_a, document["id"])

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = _confirm_all_route(client, headers_b, run["id"])
        assert resp.status_code == 404, resp.text
