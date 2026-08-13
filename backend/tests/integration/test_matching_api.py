"""Part-matching API integration tests (docs/planning/03-api-contract.md
§4.12, docs/planning/04-document-pipeline.md §10), driven end to end through
the HTTP API against a manually-entered quote - matching operates on real
`QuoteLine` rows regardless of whether the quote came from manual entry or
extraction/materialization (see `services/matching_service.py`'s own module
docstring), so a manual quote is the simplest fixture that exercises it.

Covers: an exact-internal-part-number line auto-confirms on generate; an
ambiguous (fuzzy-only) line yields ranked, unconfirmed candidates with
explanations; explicit confirm writes `part_id`/`matched_rfq_line_id`/
`match_status=confirmed`; unmatch reverses it; cross-org 404; role gating;
`matching.*` audit events. A separate section reuses the extraction-pipeline
fixture flow (mirroring test_extraction_api.py's own helpers) for the
`quote_corrections` nullable behavior migration 0012 introduces: correcting
a field *before* materialization now writes a real `QuoteCorrection` row
with `quote_id IS NULL` and `extraction_run_id` set, instead of no row at
all.

Fixture pattern mirrors test_quotes_api.py / test_extraction_api.py: a
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


# -- app / org / auth scaffolding (mirrors test_quotes_api.py) -----------


@pytest.fixture(scope="module")
def client(
    database_url: str, migrated_engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> Generator[TestClient, None, None]:
    storage_root = tmp_path_factory.mktemp("matching-doc-storage")
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


def _seed_unit(migrated_engine: Engine, organization_id: str) -> str:
    from sqlalchemy.orm import Session as SaSession

    from app.models.units import UnitDefinition, UnitDimension

    unit_id = uuid.uuid4()
    with SaSession(migrated_engine) as s:
        s.add(
            UnitDefinition(
                id=unit_id,
                organization_id=uuid.UUID(organization_id),
                code=f"each-{uuid.uuid4().hex[:8]}",
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
    return resp.json()  # type: ignore[no-any-return]


def _headers(login_body: dict[str, str]) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": login_body["csrf_token"]}


def _create_part(
    client: TestClient, headers: dict[str, str], unit_id: str, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "internal_part_number": f"PN-{uuid.uuid4().hex[:8].upper()}",
        "name": "Test Component",
        "unit_definition_id": unit_id,
    }
    payload.update(overrides)
    resp = client.post("/api/v1/parts", json=payload, headers=headers)
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


def _create_rfq(
    client: TestClient, headers: dict[str, str], part_ids: list[str]
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/rfqs",
        json={
            "name": f"RFQ-{uuid.uuid4().hex[:8].upper()}",
            "internal_reference": f"REF-{uuid.uuid4().hex[:8].upper()}",
            "base_currency": "USD",
            "due_date": "2026-12-01",
            "lines": [{"part_id": pid, "required_quantity": "10.000000"} for pid in part_ids],
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


def _create_quote(
    client: TestClient,
    headers: dict[str, str],
    rfq_id: str,
    supplier_id: str,
    lines: list[dict[str, Any]],
) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/quotes",
        json={
            "supplier_id": supplier_id,
            "quote_date": "2026-08-01",
            "currency": "USD",
            "lines": lines,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _generate_matches(client: TestClient, headers: dict[str, str], quote_id: str) -> Any:
    return client.post(f"/api/v1/quotes/{quote_id}/match", headers=headers)


def _list_matches(client: TestClient, headers: dict[str, str], quote_id: str) -> Any:
    return client.get(f"/api/v1/quotes/{quote_id}/matches", headers=headers)


def _confirm_line(
    client: TestClient,
    headers: dict[str, str],
    quote_line_id: str,
    rfq_line_id: str,
    *,
    reason: str | None = None,
) -> Any:
    body: dict[str, Any] = {"rfq_line_id": rfq_line_id, "confirmed": True}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"/api/v1/quote-lines/{quote_line_id}/match", json=body, headers=headers)


def _unmatch_line(
    client: TestClient, headers: dict[str, str], quote_line_id: str, *, reason: str | None = None
) -> Any:
    kwargs: dict[str, Any] = {"headers": headers}
    if reason is not None:
        kwargs["json"] = {"reason": reason}
    return client.request("DELETE", f"/api/v1/quote-lines/{quote_line_id}/match", **kwargs)


def _line_for(quote: dict[str, Any], description: str) -> dict[str, Any]:
    for line in quote["lines"]:
        if line["description"] == description:
            return line
    raise AssertionError(f"no line with description={description!r}")


def _setup_matching_scenario(
    client: TestClient, headers: dict[str, str], migrated_engine: Engine, org: dict[str, Any]
) -> dict[str, Any]:
    """One org's worth of scaffolding: two catalog parts, an open RFQ
    requesting both, an invited supplier, and a manual quote with two
    lines - one that will exact-match part A by internal part number, one
    that only fuzzy-matches part B (ambiguous, no auto-confirm)."""
    unit_id = _seed_unit(migrated_engine, org["org_id"])
    part_a = _create_part(
        client, headers, unit_id, internal_part_number="ACME-100", name="Acme Widget"
    )
    part_b = _create_part(
        client,
        headers,
        unit_id,
        internal_part_number="ACME-200",
        manufacturer_part_number="ZY-9988",
        name="Acme Widget Alt",
    )
    rfq = _create_rfq(client, headers, [part_a["id"], part_b["id"]])
    supplier = _create_supplier(client, headers)
    _invite_supplier(client, headers, rfq["id"], supplier["id"])
    _open_rfq(client, headers, rfq["id"])

    quote = _create_quote(
        client,
        headers,
        rfq["id"],
        supplier["id"],
        lines=[
            {
                "quantity": "100.000000",
                "unit_definition_id": unit_id,
                "quoted_part_number": "acme-100",  # exact, case-insensitive
                "description": "Exact match line",
            },
            {
                "quantity": "50.000000",
                "unit_definition_id": unit_id,
                "description": "Acme Widget Al",  # fuzzy-only, ambiguous
            },
        ],
    )
    rfq_line_a = next(rl for rl in rfq["lines"] if rl["part_id"] == part_a["id"])
    rfq_line_b = next(rl for rl in rfq["lines"] if rl["part_id"] == part_b["id"])
    return {
        "unit_id": unit_id,
        "part_a": part_a,
        "part_b": part_b,
        "rfq": rfq,
        "rfq_line_a": rfq_line_a,
        "rfq_line_b": rfq_line_b,
        "supplier": supplier,
        "quote": quote,
        "exact_line": _line_for(quote, "Exact match line"),
        "ambiguous_line": _line_for(quote, "Acme Widget Al"),
    }


# -- generate: exact auto-confirms, ambiguous stays ranked/unconfirmed ---


class TestGenerate:
    def test_exact_line_auto_confirms_ambiguous_line_gets_ranked_candidates(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers, migrated_engine, org_a)

        resp = _generate_matches(client, headers, ctx["quote"]["id"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        by_line = {item["quote_line_id"]: item for item in body["items"]}

        exact = by_line[ctx["exact_line"]["id"]]
        assert exact["match_status"] == "auto"
        assert exact["part_id"] == ctx["part_a"]["id"]
        assert exact["matched_rfq_line_id"] == ctx["rfq_line_a"]["id"]
        strategies = {c["strategy"] for c in exact["candidates"]}
        assert "internal_pn" in strategies
        internal_pn_cand = next(
            c for c in exact["candidates"] if c["strategy"] == "internal_pn"
        )
        assert internal_pn_cand["confidence"] == "1.000000"
        assert internal_pn_cand["human_confirmed"] is True
        assert internal_pn_cand["explanation"]
        assert "acme-100" in internal_pn_cand["explanation"]

        ambiguous = by_line[ctx["ambiguous_line"]["id"]]
        assert ambiguous["match_status"] == "unmatched"
        assert ambiguous["part_id"] is None
        assert len(ambiguous["candidates"]) >= 1
        for cand in ambiguous["candidates"]:
            assert cand["human_confirmed"] is False
            assert cand["explanation"]
        part_b_candidate = next(
            (c for c in ambiguous["candidates"] if c["part_id"] == ctx["part_b"]["id"]), None
        )
        assert part_b_candidate is not None
        assert Decimal(part_b_candidate["confidence"]) > 0

        with migrated_engine.connect() as conn:
            audit_row = conn.execute(
                sqltext(
                    "SELECT event_type FROM audit_events WHERE event_type = "
                    "'matching.candidates_generated' AND entity_id = :qid"
                ),
                {"qid": ctx["quote"]["id"]},
            ).one()
        assert audit_row.event_type == "matching.candidates_generated"

    def test_list_matches_mirrors_generate(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers, migrated_engine, org_a)
        _generate_matches(client, headers, ctx["quote"]["id"])

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = _list_matches(client, viewer_headers, ctx["quote"]["id"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) == 2

    def test_regeneration_is_sticky_for_auto_confirmed_line(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers, migrated_engine, org_a)
        _generate_matches(client, headers, ctx["quote"]["id"])

        # Re-running matching must not error (e.g. by re-inserting a
        # duplicate strategy row) and must leave the auto-confirmed line
        # exactly as it was.
        resp = _generate_matches(client, headers, ctx["quote"]["id"])
        assert resp.status_code == 200, resp.text
        by_line = {item["quote_line_id"]: item for item in resp.json()["items"]}
        exact = by_line[ctx["exact_line"]["id"]]
        assert exact["match_status"] == "auto"
        assert exact["part_id"] == ctx["part_a"]["id"]


# -- confirm / unmatch ---------------------------------------------------


class TestConfirmAndUnmatch:
    def test_confirm_writes_part_id_and_match_status(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers, migrated_engine, org_a)
        _generate_matches(client, headers, ctx["quote"]["id"])

        resp = _confirm_line(
            client,
            headers,
            ctx["ambiguous_line"]["id"],
            ctx["rfq_line_b"]["id"],
            reason="reviewer picked the alternate widget",
        )
        assert resp.status_code == 200, resp.text
        line = resp.json()
        assert line["part_id"] == ctx["part_b"]["id"]
        assert line["matched_rfq_line_id"] == ctx["rfq_line_b"]["id"]
        assert line["match_status"] == "confirmed"

        with migrated_engine.connect() as conn:
            audit_row = conn.execute(
                sqltext(
                    "SELECT event_type FROM audit_events WHERE event_type = "
                    "'matching.confirmed' AND entity_id = :lid"
                ),
                {"lid": ctx["ambiguous_line"]["id"]},
            ).one()
        assert audit_row.event_type == "matching.confirmed"

    def test_confirm_without_prior_generate_still_works(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """A reviewer may confirm a pairing the matcher never proposed -
        confirm does not require a pre-existing PartMatchCandidate row (see
        services/matching_service.py's module docstring)."""
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers, migrated_engine, org_a)

        resp = _confirm_line(
            client, headers, ctx["ambiguous_line"]["id"], ctx["rfq_line_b"]["id"]
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["part_id"] == ctx["part_b"]["id"]

    def test_unmatch_reverses_confirm(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers, migrated_engine, org_a)
        _generate_matches(client, headers, ctx["quote"]["id"])
        _confirm_line(client, headers, ctx["ambiguous_line"]["id"], ctx["rfq_line_b"]["id"])

        resp = _unmatch_line(
            client, headers, ctx["ambiguous_line"]["id"], reason="wrong pick"
        )
        assert resp.status_code == 200, resp.text
        line = resp.json()
        assert line["part_id"] is None
        assert line["matched_rfq_line_id"] is None
        assert line["match_status"] == "unmatched"

        with migrated_engine.connect() as conn:
            audit_row = conn.execute(
                sqltext(
                    "SELECT event_type FROM audit_events WHERE event_type = "
                    "'matching.unmatched' AND entity_id = :lid"
                ),
                {"lid": ctx["ambiguous_line"]["id"]},
            ).one()
        assert audit_row.event_type == "matching.unmatched"

    def test_unmatch_when_not_matched_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers, migrated_engine, org_a)

        resp = _unmatch_line(client, headers, ctx["ambiguous_line"]["id"])
        assert resp.status_code == 409, resp.text

    def test_confirm_unknown_rfq_line_is_404(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers, migrated_engine, org_a)

        resp = _confirm_line(
            client, headers, ctx["ambiguous_line"]["id"], str(uuid.uuid4())
        )
        assert resp.status_code == 404, resp.text


# -- cross-org isolation ---------------------------------------------------


class TestCrossOrgIsolation:
    def test_cross_org_404_on_quote_and_line(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, headers_a, migrated_engine, org_a)
        _generate_matches(client, headers_a, ctx["quote"]["id"])

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        assert _generate_matches(client, headers_b, ctx["quote"]["id"]).status_code == 404
        assert _list_matches(client, headers_b, ctx["quote"]["id"]).status_code == 404
        assert (
            _confirm_line(
                client, headers_b, ctx["ambiguous_line"]["id"], ctx["rfq_line_b"]["id"]
            ).status_code
            == 404
        )
        assert _unmatch_line(client, headers_b, ctx["exact_line"]["id"]).status_code == 404


# -- role gating -------------------------------------------------------


class TestRoleGating:
    def test_viewer_cannot_generate_confirm_or_unmatch(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_matching_scenario(client, analyst_headers, migrated_engine, org_a)
        _generate_matches(client, analyst_headers, ctx["quote"]["id"])

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        assert _generate_matches(client, viewer_headers, ctx["quote"]["id"]).status_code == 403
        assert (
            _confirm_line(
                client, viewer_headers, ctx["ambiguous_line"]["id"], ctx["rfq_line_b"]["id"]
            ).status_code
            == 403
        )
        assert _unmatch_line(client, viewer_headers, ctx["exact_line"]["id"]).status_code == 403
        # but list (GET) is viewer-readable
        assert _list_matches(client, viewer_headers, ctx["quote"]["id"]).status_code == 200


# -- quote_corrections nullable (migration 0012) --------------------------
# Reuses the extraction-pipeline fixture flow (test_extraction_api.py's own
# helpers, duplicated here per this codebase's established per-file
# duplication convention) to exercise correct_field BEFORE materialization.


def _create_rfq_with_part(
    client: TestClient, headers: dict[str, str], part_id: str
) -> dict[str, Any]:
    return _create_rfq(client, headers, [part_id])


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


def _list_fields(client: TestClient, run_id: str, **params: Any) -> list[dict[str, Any]]:
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


class TestCorrectionPreMaterialization:
    def test_correction_before_materialize_writes_row_with_null_quote_id(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        unit_id = _seed_unit(migrated_engine, org_a["org_id"])
        part = _create_part(client, headers, unit_id)
        rfq = _create_rfq_with_part(client, headers, part["id"])
        supplier = _create_supplier(client, headers)
        _invite_supplier(client, headers, rfq["id"], supplier["id"])
        _open_rfq(client, headers, rfq["id"])
        document = _upload_document(
            client, headers, rfq["id"], "shenzhen_precision_quote.pdf"
        )
        run = _start_run(client, headers, document["id"])

        # Correct a field WITHOUT confirming everything / materializing -
        # no Quote exists for this run yet.
        supplier_name_field = _field_by_path(client, run["id"], "supplier_name")
        resp = client.patch(
            f"/api/v1/extraction-runs/{run['id']}/fields/{supplier_name_field['id']}",
            json={
                "normalized_value": "Shenzhen Precision Manufacturing Co.",
                "is_confirmed": True,
                "reason": "pre-materialization correction",
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                sqltext(
                    "SELECT quote_id, extraction_run_id, before_value, after_value"
                    " FROM quote_corrections WHERE extraction_field_id = :fid"
                ),
                {"fid": supplier_name_field["id"]},
            ).one()
        assert row.quote_id is None
        assert str(row.extraction_run_id) == run["id"]
        assert row.after_value == "Shenzhen Precision Manufacturing Co."
