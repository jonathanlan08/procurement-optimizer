"""Negotiation-brief API integration tests (docs/planning/03-api-contract.md
§4.17, app/services/brief_service.py, docs/SPEC.md §Negotiation brief).

Fixture pattern mirrors test_scenarios_api.py: a committed org + one user
per role, built directly against the migrated database, driven through the
HTTP API. `_setup_two_supplier_case` reuses that file's own "Acme
Low-Price"/"Beta Premium" signature shape (same unit prices/shipping costs,
same 500-unit quantity) so a `lowest_landed_cost` scenario reliably ranks
Beta ahead of Acme on landed cost — Acme is the brief's SUBJECT supplier in
most tests below, Beta its (only, therefore BEST) alternative, which makes
`price_target`/`stretch_target` deterministically checkable against the
scenario's own reported `TOTAL_LANDED_COST` figure (divided by the known
500-unit quantity) without re-deriving the whole landed-cost calculator by
hand — the same "assert the relationship/formula, not a hand-verified
figure" discipline test_scenarios_api.py's own module docstring documents
for itself.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session as SaSession

from app.core.config import Environment, Settings
from app.core.errors import ValidationAppError
from app.core.security import hash_password
from app.main import create_app
from app.models.identity import Role

ORIGIN = "http://localhost:5173"
PASSWORD = "correct-horse-battery-brief"

ASSUMPTIONS: dict[str, Any] = {
    "quality_risk_rate": "0.02",
    "delay_risk_per_day": "0",
    "promised_lead_time_days": "0",
    "required_lead_time_days": "0",
    "annual_rate": "0.08",
    "baseline_terms_days": "30",
    "tariff_rate": "0.30",
    "assume_missing_costs_zero": True,
}

_DECIMAL_TOKEN_RE = re.compile(r"-?\d+\.\d+")

_SPEC_SECTION_KEYS = frozenset(
    {
        "procurement_objective",
        "supplier_position",
        "quoted_unit_price",
        "effective_unit_cost",
        "landed_cost_comparison",
        "per_line_comparison",
        "price_target",
        "stretch_target",
        "walk_away_threshold",
        "volume_leverage",
        "payment_terms_opportunity",
        "lead_time_concerns",
        "quality_concerns",
        "spec_concerns",
        "alternative_suppliers",
        "batna",
        "recommended_concessions",
        "questions_for_clarification",
        "draft_supplier_email",
    }
)

_VALID_PROVENANCE = frozenset(
    {"supplier_provided", "user_assumption", "calculated", "ai_narrative", "missing"}
)


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
    from app.models.identity import Organization, OrganizationMembership, User

    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    with SaSession(migrated_engine) as s:
        org = Organization(
            id=uuid.uuid4(),
            slug=f"brief-org-{suffix}",
            name=f"Brief Org {suffix}",
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
            "name": "Brief test bracket",
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
    include_terms: bool = True,
    include_optional_line_fields: bool = True,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "matched_rfq_line_id": rfq["lines"][0]["id"],
        "description": "Brief test line",
        "quantity": quantity,
        "unit_definition_id": unit_id,
        "unit_price": unit_price,
        "shipping_cost": shipping_cost,
    }
    if include_optional_line_fields:
        line["moq"] = "1.000000"
        line["lead_time_days"] = 14
        line["country_of_origin"] = "US"
    body: dict[str, Any] = {
        "supplier_id": supplier_id,
        "quote_date": "2026-08-01",
        "currency": "USD",
        "lines": [line],
    }
    if include_terms:
        body["terms"] = {"payment_terms": "Net 30", "payment_terms_days": 30}
    resp = client.post(f"/api/v1/rfqs/{rfq['id']}/quotes", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _setup_two_supplier_case(
    client: TestClient, headers: dict[str, str], migrated_engine: Engine, org: dict[str, Any]
) -> dict[str, Any]:
    """Acme Low-Price (unit_price 8.00, shipping 3000 -> high landed cost)
    vs. Beta Premium (unit_price 11.00, shipping 100 -> low landed cost),
    500 units — same signature case test_scenarios_api.py's own
    `_setup_signature_case` builds, so Beta always ends up the lower-landed-
    cost alternative to Acme."""
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
        include_terms=False, include_optional_line_fields=False,
    )
    quote_b = _create_quote(
        client, headers, rfq, supplier_b["id"], unit_id,
        quantity="500.000000", unit_price="11.00000000", shipping_cost="100.000000",
    )
    return {
        "rfq": rfq, "supplier_a": supplier_a, "supplier_b": supplier_b,
        "quote_a": quote_a, "quote_b": quote_b,
    }


def _create_and_complete_scenario(
    client: TestClient,
    headers: dict[str, str],
    rfq_id: str,
    *,
    strategy: str = "lowest_landed_cost",
) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/rfqs/{rfq_id}/comparison-scenarios",
        json={
            "name": f"Scenario ({strategy})",
            "strategy": strategy,
            "assumptions": ASSUMPTIONS,
            "constraints": {},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "complete"
    return body  # type: ignore[no-any-return]


def _generate_brief(
    client: TestClient,
    headers: dict[str, str],
    scenario_id: str,
    supplier_id: str,
    *,
    objective: str | None = None,
    targets: dict[str, Any] | None = None,
) -> Any:
    body: dict[str, Any] = {"supplier_id": supplier_id}
    if objective is not None:
        body["objective"] = objective
    if targets is not None:
        body["targets"] = targets
    return client.post(
        f"/api/v1/comparison-scenarios/{scenario_id}/negotiation-briefs",
        json=body,
        headers=headers,
    )


def _expected_unit_price(total: str, quantity: str) -> str:
    """Mirrors app.core.money.quantize_unit_price/to_wire (8dp, ROUND_HALF_EVEN)."""
    value = (Decimal(total) / Decimal(quantity)).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
    )
    return format(value, "f")


def _total_landed_cost_raw_value(scenario_body: dict[str, Any], supplier_name: str) -> str:
    for score in scenario_body["scoring_result"]["scores"]:
        if score["supplier_name"] == supplier_name:
            for cs in score["criterion_scores"]:
                if cs["criterion"] == "total_landed_cost":
                    return cs["raw_value"]  # type: ignore[no-any-return]
    raise AssertionError(f"no total_landed_cost criterion score for {supplier_name!r}")


# ---------------------------------------------------------------------------


class TestGenerateBrief:
    def test_all_spec_sections_present_with_valid_provenance(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])

        resp = _generate_brief(client, headers, scenario["id"], ctx["supplier_a"]["id"])
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert set(body["sections"].keys()) == _SPEC_SECTION_KEYS
        for key, section in body["sections"].items():
            assert section["provenance"] in _VALID_PROVENANCE, (key, section)
            assert isinstance(section["text"], str) and section["text"], key

        assert body["state"] == "draft"
        assert body["requires_review"] is True
        assert body["simulated"] is True
        assert body["narrative_is_generated"] is False
        assert body["narrative_provider"] == "template"

    def test_price_target_stretch_walk_away_have_disclosed_formulas(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])

        resp = _generate_brief(client, headers, scenario["id"], ctx["supplier_a"]["id"])
        assert resp.status_code == 201, resp.text
        body = resp.json()

        beta_total_landed = _total_landed_cost_raw_value(scenario, "Beta Premium")
        expected_price_target = _expected_unit_price(beta_total_landed, "500.000000")
        assert body["price_target"] == expected_price_target

        expected_stretch = format(
            (Decimal(expected_price_target) * Decimal("0.97")).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
            ),
            "f",
        )
        assert body["stretch_target"] == expected_stretch

        acme_total_landed = _total_landed_cost_raw_value(scenario, "Acme Low-Price")
        expected_walk_away = _expected_unit_price(acme_total_landed, "500.000000")
        assert body["walk_away_threshold"] == expected_walk_away

        for key in ("price_target", "stretch_target", "walk_away_threshold"):
            assert "CALCULATED" in body["sections"][key]["text"]
            assert body["sections"][key]["provenance"] == "calculated"

        # recommended concessions should flag the price gap (Acme is priced
        # above the calculated target)
        assert "price target" in body["sections"]["recommended_concessions"]["text"].lower()

    def test_cheapest_supplier_gets_no_target_instead_of_price_increase_invite(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Regression (2026-08 external review P1): for the supplier who is
        ALREADY the cheapest on the line, every alternative benchmark is more
        expensive — the old blended math emitted that higher figure as the
        'price target', and the draft email invited a price increase. The
        guardrail must emit NO target with a disclosed explanation instead."""
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])

        # Beta Premium is the LOWER-landed-cost supplier in this fixture.
        resp = _generate_brief(client, headers, scenario["id"], ctx["supplier_b"]["id"])
        assert resp.status_code == 201, resp.text
        body = resp.json()

        assert body["price_target"] is None
        assert body["stretch_target"] is None
        section = body["sections"]["price_target"]
        assert section["provenance"] == "calculated"
        assert "invite a price increase" in section["text"]
        assert "closer to" not in body["sections"]["draft_supplier_email"]["text"]

    def test_email_price_ask_suppressed_when_target_exceeds_quoted_price(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Regression (2026-08 external review P1): Acme quoted 8.00 bare;
        the landed-basis target (Beta's same-line landed unit cost, ~11.x) is
        a valid brief figure but is ABOVE the number Acme wrote on the quote —
        the draft email must NOT ask Acme to move 'closer to' it."""
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])

        resp = _generate_brief(client, headers, scenario["id"], ctx["supplier_a"]["id"])
        assert resp.status_code == 201, resp.text
        body = resp.json()

        # the brief itself still carries the same-line landed target...
        assert body["price_target"] is not None
        assert Decimal(body["price_target"]) > Decimal("8.00")
        # ...but the email contains no price ask (concessions carry the gap)
        assert "closer to" not in body["sections"]["draft_supplier_email"]["text"]
        assert "price target" in body["sections"]["recommended_concessions"]["text"].lower()

    def test_per_line_comparison_section_lists_each_allocated_line(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])

        resp = _generate_brief(client, headers, scenario["id"], ctx["supplier_a"]["id"])
        assert resp.status_code == 201, resp.text
        section = resp.json()["sections"]["per_line_comparison"]
        assert section["provenance"] == "calculated"
        rows = section["data"]["lines"]
        assert len(rows) == 1
        assert rows[0]["best_alternative_supplier"] == "Beta Premium"
        assert rows[0]["landed_unit_cost"] is not None
        assert rows[0]["best_alternative_landed_unit_cost"] is not None

    def test_missing_quote_fields_appear_under_questions(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])

        # Acme's quote was created with include_terms=False and
        # include_optional_line_fields=False (moq/lead_time/country_of_origin
        # all missing, plus no quote_terms row at all).
        resp = _generate_brief(client, headers, scenario["id"], ctx["supplier_a"]["id"])
        assert resp.status_code == 201, resp.text
        questions_text = resp.json()["sections"]["questions_for_clarification"]["text"].lower()

        assert "minimum order quantity" in questions_text
        assert "lead time" in questions_text
        assert "country of origin" in questions_text
        assert "payment terms" in questions_text

    def test_draft_email_contains_no_unexplained_numbers(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])

        resp = _generate_brief(client, headers, scenario["id"], ctx["supplier_a"]["id"])
        assert resp.status_code == 201, resp.text
        body = resp.json()

        email_body = body["draft_email_body"]
        tokens = _DECIMAL_TOKEN_RE.findall(email_body)
        assert tokens, "expected the draft email to reference at least one figure"

        sections_without_email = {
            k: v for k, v in body["sections"].items() if k != "draft_supplier_email"
        }
        haystack = json.dumps(sections_without_email) + json.dumps(
            {
                "price_target": body["price_target"],
                "stretch_target": body["stretch_target"],
                "walk_away_threshold": body["walk_away_threshold"],
            }
        )
        for token in tokens:
            assert token in haystack, f"draft email contains unexplained number {token!r}"

    def test_no_send_endpoint_exists_anywhere(self) -> None:
        settings = Settings(
            environment=Environment.TEST, database_url="postgresql+psycopg://x/x"
        )
        app = create_app(settings)
        schema = app.openapi()
        offending = [p for p in schema["paths"] if "email" in p.lower() or "send" in p.lower()]
        assert offending == [], f"found a send/email route: {offending}"


class TestValidation:
    def test_supplier_not_scored_on_scenario_returns_422(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])
        unrelated_supplier = _create_supplier(client, headers, name="Unrelated Co")

        resp = _generate_brief(
            client, headers, scenario["id"], unrelated_supplier["id"]
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"


class TestNumericCrossCheckGuard:
    def test_poisoned_provider_trips_the_cross_check(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        """Injects a poisoned `AiNarrativeProvider` directly via
        `BriefService`'s own constructor DI (the same seam
        `build_narrative_provider` uses at the API layer) — the mechanical
        guard `app.services.brief_service.numeric_cross_check` must reject a
        fabricated figure a narrative provider slips into rendered text,
        per SPEC's "AI must not invent... savings, concessions..."."""
        from app.core.clock import SystemClock
        from app.core.ids import RandomIdGenerator
        from app.providers.narrative.template import TemplateNarrativeProvider
        from app.services.audit import AuditRecorder
        from app.services.brief_service import BriefService

        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])

        class _PoisonedProvider:
            name = "poisoned-test-provider"
            is_generated = False

            def render_sections(self, brief_facts: dict[str, Any]) -> dict[str, str]:
                rendered = dict(TemplateNarrativeProvider().render_sections(brief_facts))
                rendered["recommended_concessions"] = (
                    rendered["recommended_concessions"]
                    + " Independent market research suggests a typical discount of "
                    "42.5000000 percent."
                )
                return rendered

        analyst_user_id = uuid.UUID(org_a["users"][Role.ANALYST.value]["user_id"])
        org_id = uuid.UUID(org_a["org_id"])

        with SaSession(migrated_engine) as session:
            audit = AuditRecorder(
                session,
                SystemClock(),
                RandomIdGenerator(),
                organization_id=org_id,
                actor_user_id=analyst_user_id,
            )
            service = BriefService(
                session,
                org_id,
                audit,
                SystemClock(),
                RandomIdGenerator(),
                provider=_PoisonedProvider(),
            )
            with pytest.raises(ValidationAppError) as exc_info:
                service.generate(
                    uuid.UUID(scenario["id"]),
                    uuid.UUID(ctx["supplier_a"]["id"]),
                    actor_id=analyst_user_id,
                )
            session.rollback()

        assert "does not appear anywhere in the underlying brief facts" in str(exc_info.value)


class TestReviewWorkflow:
    def test_review_transitions_state_and_clears_requires_review(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])
        brief = _generate_brief(
            client, headers, scenario["id"], ctx["supplier_a"]["id"]
        ).json()
        assert brief["requires_review"] is True

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        forbidden = client.post(
            f"/api/v1/negotiation-briefs/{brief['id']}/review",
            json={"approved": True, "reviewer_notes": "looks fine"},
            headers=viewer_headers,
        )
        assert forbidden.status_code == 403, forbidden.text

        # the viewer login above overwrote the shared TestClient's session
        # cookie; re-authenticate as analyst before continuing.
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        resp = client.post(
            f"/api/v1/negotiation-briefs/{brief['id']}/review",
            json={"approved": True, "reviewer_notes": "looks fine"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        reviewed = resp.json()
        assert reviewed["state"] == "human_reviewed"
        assert reviewed["requires_review"] is False
        assert reviewed["reviewed_by_id"] is not None
        assert reviewed["reviewed_at"] is not None

        again = client.post(
            f"/api/v1/negotiation-briefs/{brief['id']}/review",
            json={"approved": True},
            headers=headers,
        )
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "conflict_state"

    def test_get_reflects_review_state(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])
        brief = _generate_brief(
            client, headers, scenario["id"], ctx["supplier_a"]["id"]
        ).json()

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        fetched = client.get(f"/api/v1/negotiation-briefs/{brief['id']}", headers=viewer_headers)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["requires_review"] is True


class TestArchive:
    def test_archive_requires_administrator(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])
        brief = _generate_brief(
            client, headers, scenario["id"], ctx["supplier_a"]["id"]
        ).json()

        analyst_forbidden = client.post(
            f"/api/v1/negotiation-briefs/{brief['id']}/archive",
            json={"reason": "no longer needed"},
            headers=headers,
        )
        assert analyst_forbidden.status_code == 403, analyst_forbidden.text

        admin_headers = _headers(_login_as(client, org_a, Role.ADMINISTRATOR))
        resp = client.post(
            f"/api/v1/negotiation-briefs/{brief['id']}/archive",
            json={"reason": "no longer needed"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_archived"] is True


class TestRoleGating:
    def test_viewer_cannot_generate(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        analyst_headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, analyst_headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, analyst_headers, ctx["rfq"]["id"])

        viewer_headers = _headers(_login_as(client, org_a, Role.VIEWER))
        resp = _generate_brief(client, viewer_headers, scenario["id"], ctx["supplier_a"]["id"])
        assert resp.status_code == 403, resp.text


class TestCrossOrgIsolation:
    def test_org_b_cannot_generate_against_org_a_scenario(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers_a, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers_a, ctx["rfq"]["id"])

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = _generate_brief(client, headers_b, scenario["id"], ctx["supplier_a"]["id"])
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_org_b_cannot_read_org_a_brief(
        self,
        client: TestClient,
        org_a: dict[str, Any],
        org_b: dict[str, Any],
        migrated_engine: Engine,
    ) -> None:
        headers_a = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers_a, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers_a, ctx["rfq"]["id"])
        brief = _generate_brief(
            client, headers_a, scenario["id"], ctx["supplier_a"]["id"]
        ).json()

        headers_b = _headers(_login_as(client, org_b, Role.ANALYST))
        resp = client.get(f"/api/v1/negotiation-briefs/{brief['id']}", headers=headers_b)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


class TestAuditEvents:
    def test_generate_writes_brief_generated_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])
        brief = _generate_brief(
            client, headers, scenario["id"], ctx["supplier_a"]["id"]
        ).json()

        with migrated_engine.connect() as conn:
            count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events"
                    " WHERE entity_type = 'negotiation_brief' AND entity_id = :id"
                    " AND event_type = 'brief.generated'"
                ),
                {"id": brief["id"]},
            ).scalar_one()
        assert count == 1

    def test_review_writes_brief_reviewed_event(
        self, client: TestClient, org_a: dict[str, Any], migrated_engine: Engine
    ) -> None:
        headers = _headers(_login_as(client, org_a, Role.ANALYST))
        ctx = _setup_two_supplier_case(client, headers, migrated_engine, org_a)
        scenario = _create_and_complete_scenario(client, headers, ctx["rfq"]["id"])
        brief = _generate_brief(
            client, headers, scenario["id"], ctx["supplier_a"]["id"]
        ).json()
        client.post(
            f"/api/v1/negotiation-briefs/{brief['id']}/review",
            json={"approved": True},
            headers=headers,
        )

        with migrated_engine.connect() as conn:
            count = conn.execute(
                sqltext(
                    "SELECT count(*) FROM audit_events"
                    " WHERE entity_type = 'negotiation_brief' AND entity_id = :id"
                    " AND event_type = 'brief.reviewed'"
                ),
                {"id": brief["id"]},
            ).scalar_one()
        assert count == 1
