"""Seed the synthetic demonstration organization (idempotent).

v0.1 scope grows phase by phase; currently: the demo org and three users
(owner / analyst / viewer). ALL DATA IS SYNTHETIC.

Usage:
    export PO_DATABASE_URL=...   # from scripts/dev_db.py
    uv run python scripts/seed_demo.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SaSession

from app.core.config import load_settings
from app.core.security import hash_password
from app.models.identity import Organization, OrganizationMembership, Role, User

DEMO_SLUG = "meridian-fab"
DEMO_ORG_NAME = "Meridian Fabrication Works (Demo)"

# Synthetic demo credentials — intentionally public, documented in the README.
DEMO_USERS = [
    ("demo-owner@meridianfab.example", "Morgan Reyes", Role.OWNER, "demo-owner-2026"),
    ("demo-analyst@meridianfab.example", "Ada Chen", Role.ANALYST, "demo-analyst-2026"),
    ("demo-viewer@meridianfab.example", "Sam Okafor", Role.VIEWER, "demo-viewer-2026"),
]


def seed() -> None:
    settings = load_settings()
    engine = create_engine(settings.database_url)
    now = datetime.now(UTC)

    with SaSession(engine) as s:
        org = s.execute(
            select(Organization).where(Organization.slug == DEMO_SLUG)
        ).scalar_one_or_none()
        if org is None:
            org = Organization(
                id=uuid.uuid4(),
                slug=DEMO_SLUG,
                name=DEMO_ORG_NAME,
                base_currency="USD",
                is_demo=True,
                created_at=now,
                updated_at=now,
            )
            s.add(org)
            print(f"created demo organization {DEMO_SLUG}")

        for email, full_name, role, password in DEMO_USERS:
            user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if user is None:
                user = User(
                    id=uuid.uuid4(),
                    email=email,
                    password_hash=hash_password(password),
                    full_name=full_name,
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
                print(f"created {role.value}: {email}")

        s.commit()
    engine.dispose()
    print("seed complete")


if __name__ == "__main__":
    seed()
