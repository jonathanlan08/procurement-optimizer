"""Comparison-scenario API integration tests (docs/planning/
03-api-contract.md §4.16, app/services/scenario_service.py).

Fixture pattern mirrors test_landed_cost_api.py/test_matching_api.py: a
committed org + one user per role, built directly against the migrated
database, driven entirely through the HTTP API from there.

**The signature case** (`_setup_signature_case`): two suppliers quoting the
same RFQ line, deliberately built so the two comparison strategies disagree
- this IS the product's thesis (SPEC: a lower quoted unit price can still be
a worse deal after landed-cost extras):

- "Acme Low-Price": unit_price 8.00 (LOWEST raw price), but a huge shipping
  cost (3000.00) - with a 30% tariff on (material+logistics) piled on top,
  its landed cost ends up far above its raw price would suggest.
- "Beta Premium": unit_price 11.00 (higher raw price), but minimal shipping
  (100.00) - its landed cost stays close to its raw price.

At 500 units: Acme's landed total is ~9180.00 (effective unit cost ~18.36);
Beta's is ~7390.00 (effective unit cost ~14.78). Acme wins on raw quoted
price; Beta wins on landed cost. Exact figures are not hand-verified to the
last decimal here (that discipline belongs to `tests/unit/test_landed_cost.py`
and `test_landed_cost_api.py`'s own worked example) - these tests assert the
*ordering*, which is what the product's strategy-comparison feature is for.

Note: the allocation solver ALWAYS minimizes landed cost - `strategy` only
changes which criterion the SCORING half ranks suppliers by (scenario_service
module docstring, "Multi-line-per-supplier scoring aggregation" section);
allocation is never strategy-dependent. So under `lowest_unit_price`, the
*scoring* ranks Acme first, but the *recommended allocation* still goes to
Beta - asserted explicitly below.
"""

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
PASSWORD = "correct-horse-battery-scenario"

SHARED_ASSUMPTIONS: dict[str, Any] = {
    "quality_risk_rate": "0.02",
    "delay_risk_per_day": "0",
    "promised_lead_time_days": "0",
    "required_lead_time_days": "0",
    "annual_rate": "0.08",
    "baseline_terms_days": "30",
    "tariff_rate": "0.30",
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
            slug=f"sc-org-{suffix}",
            name=f"Scenario Org {suffix}",
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
    from decimal import Decimal

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
            "name": "Scenario test bracket",
            "unit_definition_id": unit_id,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _create_supplier(client: TestClient, headers: dict[str, str], *, name: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/suppliers",
        json={
            "code": f"SUP-{uuid.uuid4().hex[:8].upper()}",
            "name": name,
            "country_code": "US",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _create_rfq(
    client: TestClient, headers: dict[str, str], part_id: str, *, quantity: str
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/rfqs",
        json={
            "name": f"RFQ-{uuid.uuid4().hex[:8].upper()}",
            "internal_reference": f"REF-{uuid.uuid4().hex[:8].upper()}",
            "base_currency": "USD",
            "due_date": "2026-12-01",
            "lines": [{"part_id": part_id, "required_quantity": quantity}],
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


def _create_quote(
    client: TestClient,
    headers: dict[str, str],
    rfq: dict[str, Any],
    supplier_id: str,
    unit_id: str,
    *,
    quantity: str,
    unit_price: str,
    shipping_cost: str = "0.000000",
    production_capacity: str | None = None,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "matched_rfq_line_id": rfq["lines"][0]["id"],
        "description": "Scenario test line",
        "quantity": quantity,
        "unit_definition_id": unit_id,
        "unit_price": unit_price,
        "shipping_cost": shipping_cost,
    }
    if production_capacity is not None:
        line["production_capacity"] = production_capacity
    resp = client.post(
        f"/api/v1/rfqs/{rfq['id']}/quotes",
        json={
            "supplier_id": supplier_id,
            "quote_date": "2026-08-01",
            "currency": "USD",
            "lines": [line],
            "terms": {"payment_terms": "Net 30", "payment_terms_days": 30},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _setup_signature_case(
    client: TestClient, headers: dict[str, str], migrated_engine: Engine, org: dict[str, Any]
) -> dict[str, Any]:
    unit_id = _seed_unit(migrated_engine, org["org_id"])
    part = _create_part(client, headers, unit_id)
    rfq = _create_rfq(client, headers, part["id"], quantity="500.000000")
    supplier_a = _create_supplier(client, headers, name="Acme Low-Price")
    supplier_b = _create_supplier(client, headers, name="Beta Premium")
    _invite_supplier(client, headers, rfq["id"], supplier_a["id"])
    _invite_supplier(client, headers, rfq["id"], supplier_b["id"])
    _set_rfq_status(client, headers, rfq["id"], "open")
    quote_a = _create_quote(
        client, headers, rfq, supplier_a["id"], unit_id,
        quantity="500.000000", unit_price="8.00000000", shipping_cost="3000.000000",
    )
    quote_b = _create_quote(
        client, headers, rfq, supplier_b["id"], unit_id,
        quantity="500.000000", unit_price="11.00000000", shipping_cost="100.000000",
    )
    return {
        "rfq": rfq,
        "supplier_a": supplier_a,
        "supplier_b": supplier_b,
        "quote_a": quote_a,
        "quote_b": quote_b,
    }


def _create_scenario(
    client: TestClient,
    headers: dict[str, str],
    rfq_id: str,
    *,
    strategy: str,
    name: str | None = None,
    scoring_configuration_id: str | None = None,
    constraints: dict[str, Any] | None = None,
    assumptions: dict[str, Any] | None = None,
) -> Any:
    body = {
        "name": name or f"Scenario ({strategy})",
        "strategy": strategy,
        "scoring_configuration_id": scoring_configuration_id,
        "assumptions": assumptions if assumptions is not None else SHARED_ASSUMPTIONS,
        "constraints": constraints or {},
    }
    return client.post(f"/api/v1/rfqs/{rfq_id}/comparison-scenarios", json=body, headers=headers)


# ---------------------------------------------------------------------------


class TestSignatureCaseStrategiesDisagree:
    def test_lowest_landed_cost_picks_the_landed_cost_winner(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers, migrated_engine, org_a)

        resp = _create_scenario(client, headers, ctx["rfq"]["id"], strategy="lowest_landed_cost")
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["scoring_result"]["scores"][0]["supplier_name"] == "Beta Premium"
        assert body["allocation_result"]["solver_status"] in ("optimal", "feasible")
        assert len(body["allocation_result"]["allocations"]) == 1
        assert body["allocation_result"]["allocations"][0]["supplier_label"] == "Beta Premium"
        assert body["allocation_result"]["allocations"][0]["quantity"] == 500
        assert body["state"] == "complete"

    def test_lowest_unit_price_ranks_the_other_supplier_first(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers, migrated_engine, org_a)

        unit_price_resp = _create_scenario(
            client, headers, ctx["rfq"]["id"], strategy="lowest_unit_price"
        )
        assert unit_price_resp.status_code == 201, unit_price_resp.text
        unit_price_body = unit_price_resp.json()

        landed_resp = _create_scenario(
            client, headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        )
        assert landed_resp.status_code == 201, landed_resp.text
        landed_body = landed_resp.json()

        unit_price_winner = unit_price_body["scoring_result"]["scores"][0]["supplier_name"]
        landed_winner = landed_body["scoring_result"]["scores"][0]["supplier_name"]

        # THE product's thesis: the two strategies disagree.
        assert unit_price_winner == "Acme Low-Price"
        assert landed_winner == "Beta Premium"
        assert unit_price_winner != landed_winner

        # allocation is never strategy-dependent: it always minimizes landed
        # cost, so even the lowest_unit_price scenario recommends Beta.
        assert (
            unit_price_body["allocation_result"]["allocations"][0]["supplier_label"]
            == "Beta Premium"
        )


class TestInfeasible:
    def test_budget_below_minimum_is_infeasible_and_honest(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers, migrated_engine, org_a)

        resp = _create_scenario(
            client,
            headers,
            ctx["rfq"]["id"],
            strategy="lowest_landed_cost",
            constraints={"budget_limit": "10.000000"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        alloc = body["allocation_result"]

        assert alloc["solver_status"] == "infeasible"
        assert alloc["allocations"] == []
        assert alloc["objective_total_cost"] is None
        assert alloc["infeasibility_explanation"] is not None
        assert "budget" in alloc["infeasibility_explanation"]["conflicting_constraint_groups"]
        assert alloc["infeasibility_explanation"]["minimal_relaxation"] is not None
        # persisted honestly: a valid "no solution" answer is COMPLETE, not FAILED
        assert body["state"] == "complete"


class TestSplitAllocation:
    def test_capacity_forces_split_with_rejected_single_supplier_alternative(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        unit_id = _seed_unit(migrated_engine, org_a["org_id"])
        part = _create_part(client, headers, unit_id)
        rfq = _create_rfq(client, headers, part["id"], quantity="800.000000")
        supplier_x = _create_supplier(client, headers, name="Supplier X")
        supplier_y = _create_supplier(client, headers, name="Supplier Y")
        _invite_supplier(client, headers, rfq["id"], supplier_x["id"])
        _invite_supplier(client, headers, rfq["id"], supplier_y["id"])
        _set_rfq_status(client, headers, rfq["id"], "open")
        _create_quote(
            client, headers, rfq, supplier_x["id"], unit_id,
            quantity="800.000000", unit_price="10.00000000", production_capacity="500.000000",
        )
        _create_quote(
            client, headers, rfq, supplier_y["id"], unit_id,
            quantity="800.000000", unit_price="10.50000000", production_capacity="500.000000",
        )

        resp = _create_scenario(client, headers, rfq["id"], strategy="lowest_landed_cost")
        assert resp.status_code == 201, resp.text
        alloc = resp.json()["allocation_result"]

        assert alloc["solver_status"] in ("optimal", "feasible")
        assert len(alloc["allocations"]) == 2
        suppliers_used = {a["supplier_label"] for a in alloc["allocations"]}
        assert suppliers_used == {"Supplier X", "Supplier Y"}
        assert sum(a["quantity"] for a in alloc["allocations"]) == 800
        assert any("single-supplier" in s for s in alloc["rejected_alternatives"])


class TestRerunAndHistory:
    def test_rerun_reproduces_identical_scores_and_model_hash(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers, migrated_engine, org_a)

        original_resp = _create_scenario(
            client, headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        )
        assert original_resp.status_code == 201, original_resp.text
        original = original_resp.json()

        rerun_resp = client.post(
            f"/api/v1/comparison-scenarios/{original['id']}/clone",
            json={"notes": "reproducibility check"},
            headers=headers,
        )
        assert rerun_resp.status_code == 201, rerun_resp.text
        rerun = rerun_resp.json()

        assert rerun["id"] != original["id"]
        assert rerun["notes"] is not None
        assert original["id"] in rerun["notes"]

        assert (
            rerun["allocation_result"]["stats"]["model_hash"]
            == original["allocation_result"]["stats"]["model_hash"]
        )
        assert rerun["scoring_result"]["scores"] == original["scoring_result"]["scores"]
        assert (
            rerun["allocation_result"]["allocations"]
            == original["allocation_result"]["allocations"]
        )

        # scenario history survives: list shows both runs, newest first
        listing = client.get(
            f"/api/v1/rfqs/{ctx['rfq']['id']}/comparison-scenarios", headers=headers
        )
        assert listing.status_code == 200, listing.text
        items = listing.json()["items"]
        ids = {item["id"] for item in items}
        assert original["id"] in ids
        assert rerun["id"] in ids
        assert len(items) == 2
        assert items[0]["id"] == rerun["id"]  # newest first


class TestValidation:
    def test_balanced_strategy_requires_scoring_configuration_id(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers, migrated_engine, org_a)

        resp = _create_scenario(client, headers, ctx["rfq"]["id"], strategy="balanced")
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"


class TestCrossOrgIsolation:
    def test_org_b_cannot_create_scenario_against_org_a_rfq(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers_a, migrated_engine, org_a)

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = _create_scenario(client, headers_b, ctx["rfq"]["id"], strategy="lowest_landed_cost")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_org_b_cannot_read_org_a_scenario(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers_a, migrated_engine, org_a)
        created = _create_scenario(
            client, headers_a, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        ).json()

        headers_b = _headers(_login_as(client, org_b, Role.VIEWER))
        resp = client.get(f"/api/v1/comparison-scenarios/{created['id']}", headers=headers_b)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


class TestRoleGating:
    def test_viewer_cannot_create(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, analyst_headers, migrated_engine, org_a)

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = _create_scenario(
            client, viewer_headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden_role"

    def test_viewer_can_read(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, analyst_headers, migrated_engine, org_a)
        created = _create_scenario(
            client, analyst_headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        ).json()

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = client.get(f"/api/v1/comparison-scenarios/{created['id']}", headers=viewer_headers)
        assert resp.status_code == 200

    def test_analyst_cannot_archive(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, analyst_headers, migrated_engine, org_a)
        created = _create_scenario(
            client, analyst_headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        ).json()

        resp = client.post(
            f"/api/v1/comparison-scenarios/{created['id']}/archive",
            json={"reason": "test"},
            headers=analyst_headers,
        )
        assert resp.status_code == 403

    def test_administrator_can_archive(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, analyst_headers, migrated_engine, org_a)
        created = _create_scenario(
            client, analyst_headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        ).json()

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        resp = client.post(
            f"/api/v1/comparison-scenarios/{created['id']}/archive",
            json={"reason": "test"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_archived"] is True


class TestAuditEvents:
    def test_create_writes_created_and_solved_events(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers, migrated_engine, org_a)
        created = _create_scenario(
            client, headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        ).json()

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                sqltext(
                    "SELECT event_type, count(*) FROM audit_events"
                    " WHERE entity_type = 'comparison_scenario' AND entity_id = :id"
                    " GROUP BY event_type"
                ),
                {"id": created["id"]},
            ).all()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("scenario.created") == 1
        assert counts.get("scenario.solved") == 1

    def test_rerun_writes_rerun_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers, migrated_engine, org_a)
        created = _create_scenario(
            client, headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        ).json()
        rerun = client.post(
            f"/api/v1/comparison-scenarios/{created['id']}/clone", json={}, headers=headers
        ).json()

        with migrated_engine.connect() as conn:
            count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events"
                    " WHERE entity_type = 'comparison_scenario' AND entity_id = :id"
                    " AND event_type = 'scenario.rerun'"
                ),
                {"id": rerun["id"]},
            ).scalar_one()
        assert count == 1

    def test_archive_writes_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_signature_case(client, headers, migrated_engine, org_a)
        created = _create_scenario(
            client, headers, ctx["rfq"]["id"], strategy="lowest_landed_cost"
        ).json()
        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        client.post(
            f"/api/v1/comparison-scenarios/{created['id']}/archive",
            json={"reason": "done"},
            headers=admin_headers,
        )

        with migrated_engine.connect() as conn:
            count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events"
                    " WHERE entity_type = 'comparison_scenario' AND entity_id = :id"
                    " AND event_type = 'scenario.archived'"
                ),
                {"id": created["id"]},
            ).scalar_one()
        assert count == 1
