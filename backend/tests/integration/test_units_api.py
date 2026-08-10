"""GET /units: global catalogue + own-org units, never another org's."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as SaSession

from app.core.config import Environment, Settings
from app.core.security import hash_password
from app.main import create_app
from app.models.identity import Organization, OrganizationMembership, Role, User
from app.models.units import UnitDefinition, UnitDimension

ORIGIN = "http://localhost:5173"
PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="module")
def client(database_url: str, migrated_engine: Engine) -> Generator[TestClient, None, None]:
    settings = Settings(
        environment=Environment.TEST,
        database_url=database_url,
        allowed_origins=[ORIGIN],
        rate_limit_auth_per_minute=1000,
    )
    app = create_app(settings)
    with TestClient(app, base_url="http://testserver") as c:
        yield c
    app.state.engine.dispose()


def _org_with_analyst(migrated_engine: Engine) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(UTC)
    with SaSession(migrated_engine) as s:
        org = Organization(
            id=uuid.uuid4(),
            slug=f"units-{suffix}",
            name=f"Units Org {suffix}",
            base_currency="USD",
            is_demo=False,
            created_at=now,
            updated_at=now,
        )
        email = f"analyst-{suffix}@example.com"
        user = User(
            id=uuid.uuid4(),
            email=email,
            password_hash=hash_password(PASSWORD),
            full_name="Units Analyst",
            created_at=now,
            updated_at=now,
        )
        s.add_all([org, user])
        s.add(
            OrganizationMembership(
                id=uuid.uuid4(),
                organization_id=org.id,
                user_id=user.id,
                role=Role.ANALYST,
                accepted_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
        return {"org_id": str(org.id), "email": email}


def _login(client: TestClient, email: str) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert resp.status_code == 200, resp.text


class TestUnitsListing:
    def test_lists_global_and_own_units_only(
        self, client: TestClient, migrated_engine: Engine
    ) -> None:
        org_a = _org_with_analyst(migrated_engine)
        org_b = _org_with_analyst(migrated_engine)
        suffix = uuid.uuid4().hex[:8]
        with SaSession(migrated_engine) as s:
            s.add(
                UnitDefinition(
                    id=uuid.uuid4(),
                    organization_id=uuid.UUID(org_a["org_id"]),
                    code=f"tray12-{suffix}",
                    display_name="Tray of 12",
                    is_user_defined=True,
                    dimension=UnitDimension.COUNT,
                )
            )
            s.add(
                UnitDefinition(
                    id=uuid.uuid4(),
                    organization_id=uuid.UUID(org_b["org_id"]),
                    code=f"reel5k-{suffix}",
                    display_name="Reel of 5000",
                    is_user_defined=True,
                    dimension=UnitDimension.COUNT,
                )
            )
            s.commit()

        _login(client, org_a["email"])
        body = client.get("/api/v1/units").json()
        codes = {u["code"] for u in body["items"]}
        assert f"tray12-{suffix}" in codes
        assert f"reel5k-{suffix}" not in codes  # org B's private unit is invisible

    def test_requires_authentication(self, client: TestClient) -> None:
        client.cookies.clear()
        assert client.get("/api/v1/units").status_code == 401
