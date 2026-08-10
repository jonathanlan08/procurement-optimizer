"""Migration and schema-guarantee tests: up on empty DB, audit append-only,
citext case-insensitivity, membership partial unique."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError


def _mk_org(db, org_id) -> None:
    db.execute(
        text(
            "INSERT INTO organizations (id, slug, name, base_currency, is_demo,"
            " version, created_at, updated_at)"
            " VALUES (:id, :slug, 'Test Org', 'USD', false, 1, :now, :now)"
        ),
        {"id": org_id, "slug": f"org-{org_id}", "now": datetime.now(UTC)},
    )


class TestSchemaGuarantees:
    def test_migrations_reached_head(self, db) -> None:
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_dir = Path(__file__).resolve().parent.parent.parent
        script = ScriptDirectory.from_config(Config(str(backend_dir / "alembic.ini")))
        expected_head = script.get_current_head()
        version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == expected_head

    def test_audit_events_are_append_only(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id)
        event_id = make_uuid.new_id()
        db.execute(
            text(
                "INSERT INTO audit_events (id, organization_id, event_type, entity_type,"
                " occurred_at) VALUES (:id, :org, 'test.create', 'test', :now)"
            ),
            {"id": event_id, "org": org_id, "now": datetime.now(UTC)},
        )
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(
                text("UPDATE audit_events SET event_type = 'tampered' WHERE id = :id"),
                {"id": event_id},
            )

    def test_audit_events_delete_blocked(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id)
        event_id = make_uuid.new_id()
        db.execute(
            text(
                "INSERT INTO audit_events (id, organization_id, event_type, entity_type,"
                " occurred_at) VALUES (:id, :org, 'test.create', 'test', :now)"
            ),
            {"id": event_id, "org": org_id, "now": datetime.now(UTC)},
        )
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": event_id})

    def test_user_email_is_case_insensitive_unique(self, db, make_uuid) -> None:
        now = datetime.now(UTC)
        db.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, is_active,"
                " failed_login_count, created_at, updated_at)"
                " VALUES (:id, 'Analyst@Example.com', 'x', 'A', true, 0, :now, :now)"
            ),
            {"id": make_uuid.new_id(), "now": now},
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, full_name, is_active,"
                    " failed_login_count, created_at, updated_at)"
                    " VALUES (:id, 'analyst@example.com', 'x', 'B', true, 0, :now, :now)"
                ),
                {"id": make_uuid.new_id(), "now": now},
            )

    def test_one_live_membership_per_user_per_org(self, db, make_uuid) -> None:
        now = datetime.now(UTC)
        org_id, user_id = make_uuid.new_id(), make_uuid.new_id()
        _mk_org(db, org_id)
        db.execute(
            text(
                "INSERT INTO users (id, email, password_hash, full_name, is_active,"
                " failed_login_count, created_at, updated_at)"
                " VALUES (:id, :email, 'x', 'A', true, 0, :now, :now)"
            ),
            {"id": user_id, "email": f"u-{user_id}@example.com", "now": now},
        )

        def add_membership(revoked: bool) -> None:
            db.execute(
                text(
                    "INSERT INTO organization_memberships (id, organization_id, user_id,"
                    " role, revoked_at, version, created_at, updated_at)"
                    " VALUES (:id, :org, :user, 'analyst', :revoked, 1, :now, :now)"
                ),
                {
                    "id": make_uuid.new_id(),
                    "org": org_id,
                    "user": user_id,
                    "revoked": now if revoked else None,
                    "now": now,
                },
            )

        add_membership(revoked=True)  # historical membership is fine
        add_membership(revoked=False)  # one live membership is fine
        with pytest.raises(IntegrityError):
            add_membership(revoked=False)  # second live one violates partial unique

    def test_transaction_rollback_leaves_no_rows(self, db) -> None:
        # the db fixture rolls back around every test; verify isolation works by
        # observing that prior tests' orgs are absent
        count = db.execute(
            text("SELECT count(*) FROM organizations WHERE name = 'Test Org'")
        ).scalar_one()
        assert count == 0
