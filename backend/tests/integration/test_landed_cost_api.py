"""Landed-cost API integration tests (docs/planning/03-api-contract.md
§4.15): the worked example from docs/planning/05-calculation-methodology.md
§9, reproduced end to end through the HTTP API and asserted against the
principal's hand-verified exact decimal strings — completeness, component
amounts, components-sum-to-total, cross-org 404, role gating (viewer may
preview but not persist), audit coverage, and "recalculation inserts a new
row, latest wins".

Fixture pattern mirrors test_quotes_api.py/test_fx_api.py: a committed org +
one user per role, built directly against the migrated database, driven
entirely through the HTTP API from there.
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
PASSWORD = "correct-horse-battery-landed"

# The hand-verified worked example (05-calculation-methodology.md §9).
EXPECTED_COMPONENTS = {
    "extended_material": "5706.521740",
    "allocated_fixed": "869.565217",
    "logistics": "375.000000",
    "import": "212.853261",
    "quality_risk": "114.130435",
    "delay_risk": "0.000000",
    "financing": "-37.522335",
}
EXPECTED_TOTAL = "7240.548318"
EXPECTED_EFFECTIVE_UNIT_COST = "14.48109664"

WORKED_ASSUMPTIONS = {
    "quality_risk_rate": "0.02",
    "delay_risk_per_day": "0",
    "promised_lead_time_days": "0",
    "required_lead_time_days": "0",
    "annual_rate": "0.08",
    "baseline_terms_days": "30",
    "tariff_rate": "0.035",
    "assume_missing_costs_zero": True,
}


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
            slug=f"lc-org-{suffix}",
            name=f"LC Org {suffix}",
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


def _create_part(client: TestClient, headers: dict[str, str], unit_id: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/parts",
        json={
            "internal_part_number": f"PN-{uuid.uuid4().hex[:8].upper()}",
            "name": "Worked-example bracket",
            "unit_definition_id": unit_id,
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
            "name": "Worked-example Supplier",
            "country_code": "DE",
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
            "lines": [{"part_id": part_id, "required_quantity": "500.000000"}],
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


def _set_rfq_status(
    client: TestClient, headers: dict[str, str], rfq_id: str, to_status: str
) -> None:
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/status",
        json={"to_status": to_status, "reason": "test"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def _create_worked_example_quote_line(
    client: TestClient, headers: dict[str, str], rfq: dict[str, Any], supplier_id: str, unit_id: str
) -> dict[str, Any]:
    """The full worked-example fixture (05-calculation-methodology.md §9):
    500 pcs at EUR 10.50, tooling EUR 800, shipping EUR 300, insurance
    EUR 45, Net 60 payment terms, matched to the RFQ's 500-unit line."""
    resp = client.post(
        f"/api/v1/rfqs/{rfq['id']}/quotes",
        json={
            "supplier_id": supplier_id,
            "quote_date": "2026-08-01",
            "currency": "EUR",
            "lines": [
                {
                    "matched_rfq_line_id": rfq["lines"][0]["id"],
                    "description": "Worked example line",
                    "quantity": "500.000000",
                    "unit_definition_id": unit_id,
                    "unit_price": "10.50000000",
                    "tooling_cost": "800.000000",
                    "shipping_cost": "300.000000",
                    "insurance_cost": "45.000000",
                }
            ],
            "terms": {"payment_terms": "Net 60", "payment_terms_days": 60},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _set_fx_override(client: TestClient, headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/exchange-rates",
        json={
            "base_currency": "USD",
            "quote_currency": "EUR",
            "rate": "0.920000000000",
            "effective_date": "2026-08-01",
            "override_reason": "worked example fixture rate (1 USD = 0.92 EUR)",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


def _setup_worked_example(
    client: TestClient, headers: dict[str, str], migrated_engine: Engine, org: dict[str, Any]
) -> dict[str, Any]:
    unit_id = _seed_unit(migrated_engine, org["org_id"])
    part = _create_part(client, headers, unit_id)
    rfq = _create_rfq(client, headers, part["id"])
    supplier = _create_supplier(client, headers)
    _invite_supplier(client, headers, rfq["id"], supplier["id"])
    _set_rfq_status(client, headers, rfq["id"], "open")
    quote = _create_worked_example_quote_line(client, headers, rfq, supplier["id"], unit_id)
    _set_fx_override(client, headers)
    return {"rfq": rfq, "supplier": supplier, "quote": quote, "unit_id": unit_id}


def _components_by_name(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["component"]: c for c in body["components"]}


class TestWorkedExamplePersist:
    def test_calculate_and_store_matches_hand_verified_values_exactly(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, headers, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]

        resp = client.post(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["currency"] == "USD"
        assert body["accepted_quantity"] == "500.000000"
        assert body["completeness"] == "assumption_dependent"
        assert body["missing_inputs"] == []
        assert len(body["assumptions"]) > 0
        assert body["total_landed_cost"] == EXPECTED_TOTAL
        assert body["effective_unit_cost"] == EXPECTED_EFFECTIVE_UNIT_COST

        components = _components_by_name(body)
        assert set(components.keys()) == set(EXPECTED_COMPONENTS.keys())
        for name, expected_amount in EXPECTED_COMPONENTS.items():
            assert components[name]["amount"] == expected_amount, (
                f"{name}: expected {expected_amount}, got {components[name]['amount']}"
            )

        # components sum exactly to the displayed total
        component_sum = sum(
            (Decimal(c["amount"]) for c in body["components"]), Decimal("0")
        )
        assert component_sum == Decimal(EXPECTED_TOTAL)
        assert component_sum == Decimal(body["total_landed_cost"])

        # persisted row is visible via GET /landed-cost-results/{id}
        get_resp = client.get(
            f"/api/v1/landed-cost-results/{body['id']}", headers=headers
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["total_landed_cost"] == EXPECTED_TOTAL

    def test_audit_event_written(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, headers, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]

        resp = client.post(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        result_id = resp.json()["id"]

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'landed_cost_result' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": result_id},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("landed_cost.calculated") == 1

    def test_recalculation_inserts_new_row_and_latest_wins(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, headers, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]
        rfq_id = ctx["rfq"]["id"]

        first = client.post(
            f"/api/v1/rfqs/{rfq_id}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=headers,
        )
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]

        # a different assumption set -> a different total, still a NEW row
        second_assumptions = {**WORKED_ASSUMPTIONS, "quality_risk_rate": "0.05"}
        second = client.post(
            f"/api/v1/rfqs/{rfq_id}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": second_assumptions},
            headers=headers,
        )
        assert second.status_code == 201, second.text
        second_id = second.json()["id"]
        assert second_id != first_id
        assert second.json()["total_landed_cost"] != first.json()["total_landed_cost"]

        with migrated_engine.connect() as conn:
            row_count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM landed_cost_results WHERE quote_line_id = :qlid"
                ),
                {"qlid": quote_line_id},
            ).scalar_one()
        assert row_count == 2

        listing = client.get(f"/api/v1/rfqs/{rfq_id}/landed-costs", headers=headers)
        assert listing.status_code == 200, listing.text
        items = listing.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == second_id


class TestPreview:
    def test_preview_matches_persisted_values_and_does_not_persist(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, headers, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]

        resp = client.post(
            "/api/v1/landed-costs:preview",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_landed_cost"] == EXPECTED_TOTAL
        assert body["completeness"] == "assumption_dependent"
        assert "id" not in body

        with migrated_engine.connect() as conn:
            row_count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM landed_cost_results WHERE quote_line_id = :qlid"
                ),
                {"qlid": quote_line_id},
            ).scalar_one()
            # Scoped by this test's own quote_line_id (embedded in the
            # explanation text) rather than a bare global count: other tests
            # in this module share the same committed database and may have
            # already written landed_cost.calculated events of their own.
            audit_count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events"
                    " WHERE event_type = 'landed_cost.calculated'"
                    " AND explanation LIKE :needle"
                ),
                {"needle": f"%{quote_line_id}%"},
            ).scalar_one()
        assert row_count == 0
        assert audit_count == 0

    def test_viewer_can_preview(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, analyst_headers, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.post(
            "/api/v1/landed-costs:preview",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=viewer_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["total_landed_cost"] == EXPECTED_TOTAL


class TestRoleGating:
    def test_viewer_cannot_calculate(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, analyst_headers, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.post(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=viewer_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden_role"

    def test_viewer_can_list_results(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, analyst_headers, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]
        client.post(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=analyst_headers,
        )

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.get(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs", headers=viewer_headers
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1


class TestCrossOrgIsolation:
    def test_org_b_cannot_calculate_against_org_a_rfq_and_line(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, headers_a, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = client.post(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=headers_b,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_org_b_cannot_read_org_a_result(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, headers_a, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]
        created = client.post(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=headers_a,
        ).json()

        headers_b = _headers(_login_as(client, org_b, Role.VIEWER))
        resp = client.get(
            f"/api/v1/landed-cost-results/{created['id']}", headers=headers_b
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_org_b_never_sees_org_a_results_in_rfq_listing(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, headers_a, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]
        client.post(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs",
            json={"quote_line_id": quote_line_id, "assumptions": WORKED_ASSUMPTIONS},
            headers=headers_a,
        )

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        # org_b has no such RFQ at all -> 404, not an empty list
        resp = client.get(f"/api/v1/rfqs/{ctx['rfq']['id']}/landed-costs", headers=headers_b)
        assert resp.status_code == 404


class TestIncompleteWithoutAssumptions:
    def test_missing_assumptions_yield_incomplete(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Without assume_missing_costs_zero (or any rate assumptions), the
        duty/customs fields (which have no assumption fallback of their own)
        and this line's unset documentation/handling costs (real columns as
        of migration 0016, but never populated by `_setup_worked_example`)
        stay genuinely missing, and completeness must reflect that honestly
        rather than silently defaulting to zero."""
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_worked_example(client, headers, migrated_engine, org_a)
        quote_line_id = ctx["quote"]["lines"][0]["id"]

        resp = client.post(
            "/api/v1/landed-costs:preview",
            json={"quote_line_id": quote_line_id, "assumptions": {}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["completeness"] == "incomplete"
        assert len(body["missing_inputs"]) > 0


class TestCompleteReachable:
    """2026-08 product-audit remediation: migration 0016 added
    `quote_lines.documentation_cost`/`handling_cost`, the two inputs that
    previously made `Completeness.COMPLETE` structurally unreachable through
    this service (docs/METHODOLOGY.md §7). This is the "it is now actually
    reachable" acceptance test the remediation task asked for: every
    commercial field on the quote line is populated (including the two new
    columns), same-currency/same-unit so FX/unit conversion never enter the
    picture, and every assumption-only input (quality/delay risk rate,
    required lead time, financing) is supplied so nothing is left missing —
    the result must be `complete`, not merely `assumption_dependent`.
    """

    def test_fully_specified_line_under_full_assumptions_is_complete(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        unit_id = _seed_unit(migrated_engine, org_a["org_id"])
        part = _create_part(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part["id"])
        supplier = _create_supplier(client, headers)
        _invite_supplier(client, headers, rfq["id"], supplier["id"])
        _set_rfq_status(client, headers, rfq["id"], "open")

        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/quotes",
            json={
                "supplier_id": supplier["id"],
                "quote_date": "2026-08-01",
                # same as the RFQ's own base_currency (_create_rfq): no FX
                # rate lookup needed, so FX can never be a MissingInput here.
                "currency": "USD",
                "lines": [
                    {
                        "matched_rfq_line_id": rfq["lines"][0]["id"],
                        "description": "Fully-specified line",
                        "quantity": "500.000000",
                        # same unit as the RFQ line (both default from the
                        # same part) -> no conversion, so it can never be a
                        # MissingInput or a USER_ASSUMPTION either.
                        "unit_definition_id": unit_id,
                        "unit_price": "10.50000000",
                        "lead_time_days": 5,
                        "tooling_cost": "800.000000",
                        "setup_cost": "100.000000",
                        "documentation_cost": "25.000000",
                        "packaging_cost": "15.000000",
                        "shipping_cost": "300.000000",
                        "insurance_cost": "45.000000",
                        "handling_cost": "20.000000",
                        "other_fixed_cost": "10.000000",
                        # amounts directly quoted -> "quoted amount wins"
                        # (calculator.py's own words), so no tariff_rate/
                        # duty_rate assumption is needed either.
                        "tariff_amount": "50.000000",
                        "duty_amount": "30.000000",
                        "customs_fee": "12.000000",
                    }
                ],
                "terms": {"payment_terms": "Net 30", "payment_terms_days": 30},
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        quote = resp.json()
        quote_line_id = quote["lines"][0]["id"]

        resp = client.post(
            "/api/v1/landed-costs:preview",
            json={
                "quote_line_id": quote_line_id,
                "assumptions": {
                    # these five have no source column anywhere in this
                    # schema by design (module docstrings of
                    # services/landed_cost_service.py and
                    # services/scenario_service.py) — under "full
                    # assumptions" they are supplied, not omitted.
                    "quality_risk_rate": "0.02",
                    "delay_risk_per_day": "5",
                    "required_lead_time_days": "3",
                    "annual_rate": "0.08",
                    "baseline_terms_days": "30",
                },
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["completeness"] == "complete", body
        assert body["missing_inputs"] == []
        assert body["assumptions"] == []
        for component in body["components"]:
            assert component["is_missing"] is False, component

        # persisting goes through the identical assembly path and must land
        # on the wire the same way.
        resp = client.post(
            f"/api/v1/rfqs/{rfq['id']}/landed-costs",
            json={
                "quote_line_id": quote_line_id,
                "assumptions": {
                    "quality_risk_rate": "0.02",
                    "delay_risk_per_day": "5",
                    "required_lead_time_days": "3",
                    "annual_rate": "0.08",
                    "baseline_terms_days": "30",
                },
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        stored = resp.json()
        assert stored["completeness"] == "complete", stored
