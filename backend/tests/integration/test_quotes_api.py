"""Quote (manual entry) API integration tests: full create with lines/price-
breaks/terms, RFQ-status and supplier-eligibility gates (409), the price-
break validation matrix (422: overlapping ranges, non-final open-ended tier,
duplicate min_quantity), supersession chain (list excludes superseded by
default), archive, PATCH concurrency (If-Match) and status gating, cross-org
404 everywhere, role enforcement, audit coverage, and 8dp decimal round-trip.

Fixture pattern mirrors test_rfqs_api.py: a committed org + user + membership,
built directly against the migrated database, driven entirely through the
HTTP API from there."""

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


def _invite_supplier(
    client: TestClient, headers: dict[str, str], rfq_id: str, supplier_id: str
) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/suppliers",
        json={"supplier_ids": [supplier_id]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["items"][0]  # type: ignore[no-any-return]


def _set_rfq_status(
    client: TestClient, headers: dict[str, str], rfq_id: str, to_status: str
) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/status",
        json={"to_status": to_status, "reason": "test"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _quote_line_payload(unit_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "quantity": "100.000000",
        "unit_definition_id": unit_id,
        "unit_price": "9.99000000",
    }
    payload.update(overrides)
    return payload


def _quote_payload(supplier_id: str, unit_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "supplier_id": supplier_id,
        "quote_date": "2026-08-01",
        "currency": "USD",
        "lines": [_quote_line_payload(unit_id)],
    }
    payload.update(overrides)
    return payload


def _quote_supersede_payload(unit_id: str, **overrides: Any) -> dict[str, Any]:
    """`POST /quotes/{id}/supersede` body: same shape as `_quote_payload`
    minus `supplier_id` - `QuoteSupersedeRequest` forbids it (the replacement
    inherits the old quote's supplier, see app/schemas/quotes.py)."""
    payload: dict[str, Any] = {
        "quote_date": "2026-08-01",
        "currency": "USD",
        "lines": [_quote_line_payload(unit_id)],
    }
    payload.update(overrides)
    return payload


def _create_quote(
    client: TestClient,
    headers: dict[str, str],
    rfq_id: str,
    supplier_id: str,
    unit_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/quotes",
        json=_quote_payload(supplier_id, unit_id, **overrides),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _setup_open_rfq_with_invited_supplier(
    client: TestClient, headers: dict[str, str], migrated_engine: Engine, org: dict[str, Any]
) -> dict[str, Any]:
    """One org's worth of scaffolding a quote needs: a unit, a part, a
    supplier invited to a draft RFQ, moved to `open`."""
    unit_id = _seed_unit(migrated_engine, organization_id=org["org_id"])
    part = _create_part(client, headers, unit_id)
    rfq = _create_rfq(client, headers, [part["id"]])
    supplier = _create_supplier(client, headers)
    _invite_supplier(client, headers, rfq["id"], supplier["id"])
    rfq = _set_rfq_status(client, headers, rfq["id"], "open")
    return {"unit_id": unit_id, "part": part, "rfq": rfq, "supplier": supplier}


class TestQuoteCreate:
    def test_create_full_quote_with_lines_breaks_and_terms(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id, part = ctx["rfq"], ctx["supplier"], ctx["unit_id"], ctx["part"]

        payload = _quote_payload(
            supplier["id"],
            unit_id,
            quote_number="Q-1001",
            expiration_date="2026-09-01",
            notes="net terms pending",
            lines=[
                _quote_line_payload(
                    unit_id,
                    part_id=part["id"],
                    matched_rfq_line_id=rfq["lines"][0]["id"],
                    description="Widget bracket",
                    quantity="500.000000",
                    unit_price="9.99000000",
                    moq="100.000000",
                    lead_time_days=21,
                    country_of_origin="US",
                    production_capacity="10000.000000",
                    tooling_cost="500.000000",
                    setup_cost="50.000000",
                    price_breaks=[
                        {"min_quantity": "1", "max_quantity": "99", "unit_price": "12.00000000"},
                        {
                            "min_quantity": "100",
                            "max_quantity": "499",
                            "unit_price": "10.50000000",
                        },
                        {"min_quantity": "500", "max_quantity": None, "unit_price": "9.20000000"},
                    ],
                ),
                _quote_line_payload(unit_id, description="Line 2 - no breaks"),
                _quote_line_payload(
                    unit_id, description="Line 3 - unit price missing", unit_price=None
                ),
            ],
            terms={
                "payment_terms": "net 30",
                "incoterm": "FOB",
                "shipping_terms": "prepaid",
                "warranty_terms": "1 year",
                "exceptions": "none",
                "exclusions": "none",
            },
        )
        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes", json=payload, headers=headers
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["status"] == "draft"
        assert created["source"] == "manual"
        assert created["version"] == 1
        assert len(created["lines"]) == 3
        assert created["lines"][0]["matched_rfq_line_id"] == rfq["lines"][0]["id"]
        assert created["lines"][0]["part_id"] == part["id"]
        assert len(created["lines"][0]["price_breaks"]) == 3
        assert created["terms"]["payment_terms"] == "net 30"

        get_resp = client.get(f"/api/v1/quotes/{created['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 200
        assert get_resp.headers["etag"] == '"1"'
        fetched = get_resp.json()
        assert fetched["id"] == created["id"]
        assert len(fetched["lines"]) == 3

    def test_decimal_round_trip_8dp(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(
            client,
            headers,
            rfq["id"],
            supplier["id"],
            unit_id,
            lines=[_quote_line_payload(unit_id, unit_price="12.34500000")],
        )
        assert quote["lines"][0]["unit_price"] == "12.34500000"
        assert isinstance(quote["lines"][0]["unit_price"], str)

    def test_missing_optional_fields_stay_null_never_zero(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(
            client,
            headers,
            rfq["id"],
            supplier["id"],
            unit_id,
            lines=[_quote_line_payload(unit_id, unit_price=None)],
        )
        line = quote["lines"][0]
        assert line["unit_price"] is None
        assert line["moq"] is None
        assert line["tooling_cost"] is None
        assert line["tariff_amount"] is None
        assert quote["terms"] is None
        assert quote["quote_number"] is None
        assert quote["expiration_date"] is None

    def test_rfq_not_open_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        part = _create_part(client, headers, unit_id)
        rfq = _create_rfq(client, headers, [part["id"]])  # still draft
        supplier = _create_supplier(client, headers)
        _invite_supplier(client, headers, rfq["id"], supplier["id"])

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(supplier["id"], unit_id),
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_uninvited_supplier_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        unit_id = _seed_unit(migrated_engine, organization_id=org_a["org_id"])
        part = _create_part(client, headers, unit_id)
        rfq = _create_rfq(client, headers, [part["id"]])
        rfq = _set_rfq_status(client, headers, rfq["id"], "open")
        supplier = _create_supplier(client, headers)  # never invited

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(supplier["id"], unit_id),
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_excluded_supplier_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        suppliers_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/suppliers", headers={"Origin": ORIGIN}
        )
        rfq_supplier_id = suppliers_resp.json()["items"][0]["id"]
        exclude_resp = client.request(
            "DELETE",
            f"/api/v1/rfqs/{rfq['id']}/suppliers/{rfq_supplier_id}",
            json={"exclusion_reason": "no longer qualified"},
            headers=headers,
        )
        assert exclude_resp.status_code == 200, exclude_resp.text

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(supplier["id"], unit_id),
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_state"

    def test_cross_org_supplier_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers_a, migrated_engine, org_a)
        rfq, unit_id = ctx["rfq"], ctx["unit_id"]

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        foreign_supplier = _create_supplier(client, headers_b)

        # TestClient shares one cookie jar: logging in as org_b above replaced
        # the session cookie, so the earlier `headers_a`'s CSRF token no
        # longer matches the active session. Re-authenticate as org_a to get
        # a fresh, valid session before making the actual assertion request -
        # same reasoning as test_rfqs_api.py's cross-org tests always issuing
        # the final request from whichever org logged in *last*.
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(foreign_supplier["id"], unit_id),
            headers=headers_a,
        )
        assert resp.status_code == 404, resp.text

    def test_cross_org_part_id_in_a_line_is_404(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers_a, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        unit_b = _seed_unit(migrated_engine, organization_id=org_b["org_id"])
        foreign_part = _create_part(client, headers_b, unit_b)

        # see test_cross_org_supplier_is_404 for why org_a must re-authenticate
        # here rather than reusing the now-stale `headers_a`.
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(
                supplier["id"],
                unit_id,
                lines=[_quote_line_payload(unit_id, part_id=foreign_part["id"])],
            ),
            headers=headers_a,
        )
        assert resp.status_code == 404, resp.text

    def test_matched_rfq_line_id_from_a_different_rfq_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id, part = ctx["rfq"], ctx["supplier"], ctx["unit_id"], ctx["part"]

        other_rfq = _create_rfq(client, headers, [part["id"]])

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(
                supplier["id"],
                unit_id,
                lines=[
                    _quote_line_payload(
                        unit_id, matched_rfq_line_id=other_rfq["lines"][0]["id"]
                    )
                ],
            ),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"


class TestQuotePriceBreakValidation:
    """The price-break validation matrix (see services/quote_service.py's
    module docstring for the full rule set)."""

    def _line_with_breaks(self, unit_id: str, breaks: list[dict[str, Any]]) -> dict[str, Any]:
        return _quote_line_payload(unit_id, price_breaks=breaks)

    def test_overlapping_ranges_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(
                supplier["id"],
                unit_id,
                lines=[
                    self._line_with_breaks(
                        unit_id,
                        [
                            {"min_quantity": "1", "max_quantity": "500", "unit_price": "10"},
                            # overlaps [1,500]: starts at 300, inside the prior range
                            {"min_quantity": "300", "max_quantity": "900", "unit_price": "9"},
                        ],
                    )
                ],
            ),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["error"]["code"] == "validation_error"
        assert "price_breaks" in body["error"]["details"][0]["field"]

    def test_null_max_on_non_final_tier_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(
                supplier["id"],
                unit_id,
                lines=[
                    self._line_with_breaks(
                        unit_id,
                        [
                            {"min_quantity": "1", "max_quantity": None, "unit_price": "10"},
                            {"min_quantity": "100", "max_quantity": None, "unit_price": "9"},
                        ],
                    )
                ],
            ),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_duplicate_min_quantity_via_api_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(
                supplier["id"],
                unit_id,
                lines=[
                    self._line_with_breaks(
                        unit_id,
                        [
                            {"min_quantity": "100", "max_quantity": "499", "unit_price": "10"},
                            {"min_quantity": "100", "max_quantity": "999", "unit_price": "9"},
                        ],
                    )
                ],
            ),
            headers=headers,
        )
        # Caught by QuoteService._validate_price_breaks ahead of the DB's own
        # uq_quote_price_breaks_org_line_min_quantity constraint, so this is
        # deterministically 422 (validation_error), never a raw 409 from an
        # IntegrityError.
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_contiguous_non_overlapping_tiers_accepted(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(
            client,
            headers,
            rfq["id"],
            supplier["id"],
            unit_id,
            lines=[
                self._line_with_breaks(
                    unit_id,
                    [
                        {"min_quantity": "1", "max_quantity": "99", "unit_price": "12.00000000"},
                        {
                            "min_quantity": "100",
                            "max_quantity": "499",
                            "unit_price": "10.50000000",
                        },
                        {"min_quantity": "500", "max_quantity": None, "unit_price": "9.20000000"},
                    ],
                )
            ],
        )
        assert len(quote["lines"][0]["price_breaks"]) == 3

    def test_price_increasing_across_tiers_is_accepted(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Task brief: 'do not enforce price direction' - a tier's unit price
        rising at a higher volume (e.g. a tooling-amortization break) is
        structurally valid."""
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(
            client,
            headers,
            rfq["id"],
            supplier["id"],
            unit_id,
            lines=[
                self._line_with_breaks(
                    unit_id,
                    [
                        {"min_quantity": "1", "max_quantity": "99", "unit_price": "5.00000000"},
                        {"min_quantity": "100", "max_quantity": None, "unit_price": "8.00000000"},
                    ],
                )
            ],
        )
        assert len(quote["lines"][0]["price_breaks"]) == 2

    def test_gap_between_tiers_is_accepted(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Only overlap is forbidden; a gap is legal structurally (the
        contract's own PUT .../price-breaks row treats a gap as a warning,
        not a block - the same non-blocking posture applies here)."""
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(
            client,
            headers,
            rfq["id"],
            supplier["id"],
            unit_id,
            lines=[
                self._line_with_breaks(
                    unit_id,
                    [
                        {"min_quantity": "1", "max_quantity": "50", "unit_price": "10"},
                        {"min_quantity": "200", "max_quantity": None, "unit_price": "8"},
                    ],
                )
            ],
        )
        assert len(quote["lines"][0]["price_breaks"]) == 2

    def test_min_quantity_below_one_is_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(
                supplier["id"],
                unit_id,
                lines=[
                    self._line_with_breaks(
                        unit_id, [{"min_quantity": "0.5", "max_quantity": None, "unit_price": "10"}]
                    )
                ],
            ),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text


class TestQuoteSupersede:
    def test_supersede_chain_and_list_exclusion(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        old_quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)

        supersede_resp = client.post(
            f"/api/v1/quotes/{old_quote['id']}/supersede",
            json=_quote_supersede_payload(
                unit_id,
                quote_number="Q-REV-2",
                lines=[_quote_line_payload(unit_id, unit_price="8.50000000")],
            ),
            headers=headers,
        )
        assert supersede_resp.status_code == 201, supersede_resp.text
        new_quote = supersede_resp.json()
        assert new_quote["id"] != old_quote["id"]
        assert new_quote["status"] == "draft"
        assert new_quote["quote_number"] == "Q-REV-2"
        assert new_quote["rfq_id"] == rfq["id"]
        assert new_quote["supplier_id"] == supplier["id"]

        old_get = client.get(
            f"/api/v1/quotes/{old_quote['id']}", headers={"Origin": ORIGIN}
        ).json()
        assert old_get["status"] == "superseded"
        assert old_get["superseded_by_id"] == new_quote["id"]

        default_list = client.get(
            f"/api/v1/rfqs/{rfq['id']}/quotes", headers={"Origin": ORIGIN}
        ).json()
        ids = [item["id"] for item in default_list["items"]]
        assert new_quote["id"] in ids
        assert old_quote["id"] not in ids

        full_list = client.get(
            f"/api/v1/rfqs/{rfq['id']}/quotes?include_superseded=true",
            headers={"Origin": ORIGIN},
        ).json()
        full_ids = [item["id"] for item in full_list["items"]]
        assert new_quote["id"] in full_ids
        assert old_quote["id"] in full_ids

    def test_superseding_an_already_superseded_quote_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        old_quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)
        client.post(
            f"/api/v1/quotes/{old_quote['id']}/supersede",
            json=_quote_supersede_payload(unit_id),
            headers=headers,
        )

        second_attempt = client.post(
            f"/api/v1/quotes/{old_quote['id']}/supersede",
            json=_quote_supersede_payload(unit_id),
            headers=headers,
        )
        assert second_attempt.status_code == 409, second_attempt.text
        assert second_attempt.json()["error"]["code"] == "conflict_state"

    def test_update_blocked_on_a_superseded_quote(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        old_quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)
        supersede_resp = client.post(
            f"/api/v1/quotes/{old_quote['id']}/supersede",
            json=_quote_supersede_payload(unit_id),
            headers=headers,
        )
        assert supersede_resp.status_code == 201, supersede_resp.text

        # `supersede()` bumps the old quote's version (1 -> 2) as part of
        # setting its status/superseded_by_id, so the current version must be
        # sent as If-Match to get past the version check and actually reach
        # the status gate this test means to exercise.
        old_get = client.get(
            f"/api/v1/quotes/{old_quote['id']}", headers={"Origin": ORIGIN}
        ).json()
        assert old_get["version"] == 2

        patch_resp = client.patch(
            f"/api/v1/quotes/{old_quote['id']}",
            json={"notes": "too late"},
            headers={**headers, "If-Match": '"2"'},
        )
        assert patch_resp.status_code == 409, patch_resp.text
        assert patch_resp.json()["error"]["code"] == "conflict_state"


class TestQuoteUpdate:
    def test_update_with_if_match_and_etag(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)
        assert quote["version"] == 1

        patch_resp = client.patch(
            f"/api/v1/quotes/{quote['id']}",
            json={"notes": "revised note", "quote_number": "Q-2"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        updated = patch_resp.json()
        assert updated["notes"] == "revised note"
        assert updated["quote_number"] == "Q-2"
        assert updated["version"] == 2
        assert patch_resp.headers["etag"] == '"2"'

    def test_stale_if_match_is_409(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)

        resp = client.patch(
            f"/api/v1/quotes/{quote['id']}",
            json={"notes": "x"},
            headers={**headers, "If-Match": '"99"'},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict_version"


class TestQuoteArchive:
    def test_archive_by_administrator(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        resp = client.post(
            f"/api/v1/quotes/{quote['id']}/archive",
            json={"reason": "duplicate entry"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        archived = resp.json()
        assert archived["is_archived"] is True
        assert archived["archive_reason"] == "duplicate entry"
        assert archived["version"] == 2

    def test_analyst_cannot_archive(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)

        resp = client.post(
            f"/api/v1/quotes/{quote['id']}/archive",
            json={"reason": "nope"},
            headers=headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "forbidden_role"


class TestQuoteOrgIsolation:
    def test_cross_org_access_is_404_everywhere(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers_a, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]
        quote = _create_quote(client, headers_a, rfq["id"], supplier["id"], unit_id)

        # Only one org_b login here (not a second one for the admin-gated
        # archive check until the very end) - TestClient shares one cookie
        # jar, so a second login mid-test would replace the session and
        # invalidate `headers_b`'s already-issued CSRF token before it's
        # used, exactly the bug already worked around in TestQuoteCreate's
        # cross-org tests above.
        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))

        get_resp = client.get(f"/api/v1/quotes/{quote['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 404

        patch_resp = client.patch(
            f"/api/v1/quotes/{quote['id']}",
            json={"notes": "hijack"},
            headers={**headers_b, "If-Match": '"1"'},
        )
        assert patch_resp.status_code == 404

        supersede_resp = client.post(
            f"/api/v1/quotes/{quote['id']}/supersede",
            json=_quote_supersede_payload(unit_id),
            headers=headers_b,
        )
        assert supersede_resp.status_code == 404

        list_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/quotes", headers={"Origin": ORIGIN}
        )
        assert list_resp.status_code == 404

        create_resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(supplier["id"], unit_id),
            headers=headers_b,
        )
        assert create_resp.status_code == 404

        # Admin-gated archive check last, with its own fresh login.
        admin_headers_b = _headers(_login_as(client, org_b, Role.ADMINISTRATOR))
        archive_resp = client.post(
            f"/api/v1/quotes/{quote['id']}/archive",
            json={"reason": "x"},
            headers=admin_headers_b,
        )
        assert archive_resp.status_code == 404


class TestQuoteRoleEnforcement:
    def test_viewer_can_read_but_not_mutate(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]
        quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))

        get_resp = client.get(f"/api/v1/quotes/{quote['id']}", headers={"Origin": ORIGIN})
        assert get_resp.status_code == 200

        list_resp = client.get(
            f"/api/v1/rfqs/{rfq['id']}/quotes", headers={"Origin": ORIGIN}
        )
        assert list_resp.status_code == 200

        create_attempt = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json=_quote_payload(supplier["id"], unit_id),
            headers=viewer_headers,
        )
        assert create_attempt.status_code == 403
        assert create_attempt.json()["error"]["code"] == "forbidden_role"

        patch_attempt = client.patch(
            f"/api/v1/quotes/{quote['id']}",
            json={"notes": "nope"},
            headers={**viewer_headers, "If-Match": '"1"'},
        )
        assert patch_attempt.status_code == 403

        supersede_attempt = client.post(
            f"/api/v1/quotes/{quote['id']}/supersede",
            json=_quote_supersede_payload(unit_id),
            headers=viewer_headers,
        )
        assert supersede_attempt.status_code == 403


class TestQuoteAuditTrail:
    def test_audit_events_for_lifecycle_mutations(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_open_rfq_with_invited_supplier(client, headers, migrated_engine, org_a)
        rfq, supplier, unit_id = ctx["rfq"], ctx["supplier"], ctx["unit_id"]

        quote = _create_quote(client, headers, rfq["id"], supplier["id"], unit_id)
        client.patch(
            f"/api/v1/quotes/{quote['id']}",
            json={"notes": "revised"},
            headers={**headers, "If-Match": '"1"'},
        )
        supersede_resp = client.post(
            f"/api/v1/quotes/{quote['id']}/supersede",
            json=_quote_supersede_payload(unit_id),
            headers=headers,
        )
        new_quote_id = supersede_resp.json()["id"]

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        client.post(
            f"/api/v1/quotes/{new_quote_id}/archive",
            json={"reason": "test archive"},
            headers=admin_headers,
        )

        def _event_types(entity_id: str) -> list[str]:
            with migrated_engine.connect() as conn:
                rows = conn.execute(
                    sqltext(
                        "SELECT event_type FROM audit_events"
                        " WHERE entity_type = 'quote' AND entity_id = :id"
                        " ORDER BY occurred_at"
                    ),
                    {"id": entity_id},
                ).all()
            return [row[0] for row in rows]

        assert _event_types(quote["id"]) == ["quote.created", "quote.updated", "quote.superseded"]
        # The replacement quote gets no `quote.created` event of its own -
        # `supersede()` writes exactly one audit event (`quote.superseded`,
        # keyed to the OLD quote by design), with the new
        # quote's full graph nested inside that event's after_state instead
        # (see services/quote_service.py's `supersede()` comment). Only its
        # own later `archive()` call gets an event keyed to its own id.
        assert _event_types(new_quote_id) == ["quote.archived"]

        with migrated_engine.connect() as conn:
            after_state = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE entity_type = 'quote' AND entity_id = :id"
                    " AND event_type = 'quote.created'"
                ),
                {"id": quote["id"]},
            ).scalar_one()
        assert after_state["status"] == "draft"
        assert after_state["source"] == "manual"
        assert len(after_state["lines"]) == 1

        with migrated_engine.connect() as conn:
            superseded_after = conn.execute(
                sqltext(
                    "SELECT after_state FROM audit_events"
                    " WHERE entity_type = 'quote' AND entity_id = :id"
                    " AND event_type = 'quote.superseded'"
                ),
                {"id": quote["id"]},
            ).scalar_one()
        assert superseded_after["status"] == "superseded"
        assert superseded_after["replacement_quote_id"] == new_quote_id
        assert superseded_after["replacement_quote"]["status"] == "draft"
