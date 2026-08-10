"""Suppliers schema tests: migration coverage, composite-FK org isolation,
per-org code uniqueness, currency check constraint (docs/planning/02-erd.md §4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _mk_org(db, org_id, slug) -> None:
    db.execute(
        text(
            "INSERT INTO organizations (id, slug, name, base_currency, is_demo,"
            " version, created_at, updated_at)"
            " VALUES (:id, :slug, 'Test Org', 'USD', false, 1, :now, :now)"
        ),
        {"id": org_id, "slug": slug, "now": datetime.now(UTC)},
    )


def _mk_supplier(db, supplier_id, org_id, code, currencies=None) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO suppliers (id, organization_id, code, name, country_code,"
            " supported_currencies, is_active, version, created_at, updated_at)"
            " VALUES (:id, :org, :code, 'Test Supplier', 'US', :currencies, true, 1,"
            " :now, :now)"
        ),
        {
            "id": supplier_id,
            "org": org_id,
            "code": code,
            "currencies": currencies if currencies is not None else ["USD"],
            "now": now,
        },
    )


def _mk_contact(db, contact_id, org_id, supplier_id, name="Contact") -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO supplier_contacts (id, organization_id, supplier_id, name,"
            " is_primary, created_at, updated_at)"
            " VALUES (:id, :org, :supplier, :name, false, :now, :now)"
        ),
        {"id": contact_id, "org": org_id, "supplier": supplier_id, "name": name, "now": now},
    )


class TestSuppliersSchema:
    def test_migration_creates_supplier_tables(self, db) -> None:
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('suppliers', 'supplier_contacts', 'supplier_performance_records')"
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {
            "suppliers",
            "supplier_contacts",
            "supplier_performance_records",
        }

    def test_composite_fk_rejects_cross_org_contact(self, db, make_uuid) -> None:
        """The critical test: a contact whose organization_id differs from its
        supplier's organization_id must be refused at the database layer by the
        composite FK, not merely by application logic."""
        org_a, org_b = make_uuid.new_id(), make_uuid.new_id()
        _mk_org(db, org_a, f"org-a-{org_a}")
        _mk_org(db, org_b, f"org-b-{org_b}")
        supplier_id = make_uuid.new_id()
        _mk_supplier(db, supplier_id, org_a, "SUP-A")

        with pytest.raises(IntegrityError):
            _mk_contact(db, make_uuid.new_id(), org_b, supplier_id)

    def test_composite_fk_accepts_same_org_contact(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        supplier_id = make_uuid.new_id()
        _mk_supplier(db, supplier_id, org_id, "SUP-1")

        contact_id = make_uuid.new_id()
        _mk_contact(db, contact_id, org_id, supplier_id)

        count = db.execute(
            text("SELECT count(*) FROM supplier_contacts WHERE id = :id"), {"id": contact_id}
        ).scalar_one()
        assert count == 1

    def test_supplier_code_unique_per_org_reusable_across_orgs(self, db, make_uuid) -> None:
        org_a, org_b = make_uuid.new_id(), make_uuid.new_id()
        _mk_org(db, org_a, f"org-a-{org_a}")
        _mk_org(db, org_b, f"org-b-{org_b}")

        _mk_supplier(db, make_uuid.new_id(), org_a, "DUP-CODE")
        # same code, different org: allowed
        _mk_supplier(db, make_uuid.new_id(), org_b, "DUP-CODE")

        with pytest.raises(IntegrityError):
            _mk_supplier(db, make_uuid.new_id(), org_a, "DUP-CODE")

    def test_currency_check_rejects_lowercase_codes(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")

        with pytest.raises(IntegrityError):
            _mk_supplier(db, make_uuid.new_id(), org_id, "SUP-LC", currencies=["usd"])
