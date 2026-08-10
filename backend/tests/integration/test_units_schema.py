"""Units schema tests: migration coverage, factor CHECK constraints,
global-vs-org code uniqueness, and seed catalogue idempotency
(docs/planning/02-erd.md §4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.clock import FrozenClock
from app.seed.units_catalog import STANDARD_UNIT_CATALOG, seed_unit_catalog


def _mk_org(db, org_id, slug) -> None:
    db.execute(
        text(
            "INSERT INTO organizations (id, slug, name, base_currency, is_demo,"
            " version, created_at, updated_at)"
            " VALUES (:id, :slug, 'Test Org', 'USD', false, 1, :now, :now)"
        ),
        {"id": org_id, "slug": slug, "now": datetime.now(UTC)},
    )


def _mk_user(db, user_id, email) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO users (id, email, password_hash, full_name, is_active,"
            " created_at, updated_at)"
            " VALUES (:id, :email, 'x', 'Test User', true, :now, :now)"
        ),
        {"id": user_id, "email": email, "now": now},
    )


def _mk_unit_definition(
    db, unit_id, organization_id, code, dimension="mass", factor="1"
) -> None:
    db.execute(
        text(
            "INSERT INTO unit_definitions (id, organization_id, code, display_name,"
            " dimension, to_canonical_factor, is_user_defined)"
            " VALUES (:id, :org, :code, :code, CAST(:dimension AS dimension_enum),"
            " :factor, false)"
        ),
        {
            "id": unit_id,
            "org": organization_id,
            "code": code,
            "dimension": dimension,
            "factor": factor,
        },
    )


def _mk_unit_conversion(
    db,
    conversion_id,
    organization_id,
    from_unit_id,
    to_unit_id,
    created_by_id,
    part_id=None,
    factor="1",
    assumption_note="1 reel = 5000 each",
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO unit_conversions (id, organization_id, from_unit_id,"
            " to_unit_id, part_id, factor, assumption_note, created_by_id, created_at)"
            " VALUES (:id, :org, :from_unit, :to_unit, :part, :factor, :note, :creator, :now)"
        ),
        {
            "id": conversion_id,
            "org": organization_id,
            "from_unit": from_unit_id,
            "to_unit": to_unit_id,
            "part": part_id,
            "factor": factor,
            "note": assumption_note,
            "creator": created_by_id,
            "now": now,
        },
    )


class TestUnitsSchema:
    def test_migration_creates_unit_tables(self, db) -> None:
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public'"
                    " AND table_name IN ('unit_definitions', 'unit_conversions')"
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {"unit_definitions", "unit_conversions"}

    def test_to_canonical_factor_check_rejects_zero(self, db, make_uuid) -> None:
        with pytest.raises(IntegrityError):
            _mk_unit_definition(db, make_uuid.new_id(), None, "bad-unit", factor="0")

    def test_to_canonical_factor_check_rejects_negative(self, db, make_uuid) -> None:
        with pytest.raises(IntegrityError):
            _mk_unit_definition(db, make_uuid.new_id(), None, "bad-unit", factor="-1")

    def test_to_canonical_factor_allows_null(self, db, make_uuid) -> None:
        # pack/box/tray/reel-style entries: no universal factor
        unit_id = make_uuid.new_id()
        db.execute(
            text(
                "INSERT INTO unit_definitions (id, organization_id, code,"
                " display_name, dimension, to_canonical_factor, is_user_defined)"
                " VALUES (:id, NULL, 'pack', 'Pack', 'count'::dimension_enum, NULL, false)"
            ),
            {"id": unit_id},
        )
        count = db.execute(
            text("SELECT count(*) FROM unit_definitions WHERE id = :id"), {"id": unit_id}
        ).scalar_one()
        assert count == 1

    def test_global_code_unique_case_insensitive(self, db, make_uuid) -> None:
        _mk_unit_definition(db, make_uuid.new_id(), None, "kg", dimension="mass", factor="1")
        with pytest.raises(IntegrityError):
            _mk_unit_definition(db, make_uuid.new_id(), None, "KG", dimension="mass", factor="1")

    def test_org_code_unique_per_org_reusable_across_orgs(self, db, make_uuid) -> None:
        org_a, org_b = make_uuid.new_id(), make_uuid.new_id()
        _mk_org(db, org_a, f"org-a-{org_a}")
        _mk_org(db, org_b, f"org-b-{org_b}")

        _mk_unit_definition(db, make_uuid.new_id(), org_a, "spool", dimension="count", factor="1")
        # same code, different org: allowed
        _mk_unit_definition(db, make_uuid.new_id(), org_b, "spool", dimension="count", factor="1")

        with pytest.raises(IntegrityError):
            _mk_unit_definition(
                db, make_uuid.new_id(), org_a, "spool", dimension="count", factor="1"
            )

    def test_unit_conversions_factor_check_rejects_zero(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        from_unit = make_uuid.new_id()
        to_unit = make_uuid.new_id()
        _mk_unit_definition(db, from_unit, None, "reel-zero-a", dimension="count", factor="1")
        _mk_unit_definition(db, to_unit, None, "each-zero-a", dimension="count", factor="1")

        with pytest.raises(IntegrityError):
            _mk_unit_conversion(
                db, make_uuid.new_id(), org_id, from_unit, to_unit, user_id, factor="0"
            )

    def test_unit_conversions_factor_check_rejects_negative(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        from_unit = make_uuid.new_id()
        to_unit = make_uuid.new_id()
        _mk_unit_definition(db, from_unit, None, "reel-neg-a", dimension="count", factor="1")
        _mk_unit_definition(db, to_unit, None, "each-neg-a", dimension="count", factor="1")

        with pytest.raises(IntegrityError):
            _mk_unit_conversion(
                db, make_uuid.new_id(), org_id, from_unit, to_unit, user_id, factor="-5"
            )

    def test_unit_conversions_rejects_same_unit_both_sides(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id, None, "reel-same", dimension="count", factor="1")

        with pytest.raises(IntegrityError):
            _mk_unit_conversion(db, make_uuid.new_id(), org_id, unit_id, unit_id, user_id)

    def test_unit_conversions_rejects_blank_assumption_note(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        from_unit = make_uuid.new_id()
        to_unit = make_uuid.new_id()
        _mk_unit_definition(db, from_unit, None, "reel-blank", dimension="count", factor="1")
        _mk_unit_definition(db, to_unit, None, "each-blank", dimension="count", factor="1")

        with pytest.raises(IntegrityError):
            _mk_unit_conversion(
                db, make_uuid.new_id(), org_id, from_unit, to_unit, user_id, assumption_note="   "
            )

    def test_unit_conversions_org_default_unique_per_unit_pair(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        from_unit = make_uuid.new_id()
        to_unit = make_uuid.new_id()
        _mk_unit_definition(db, from_unit, None, "reel-dup", dimension="count", factor="1")
        _mk_unit_definition(db, to_unit, None, "each-dup", dimension="count", factor="1")

        _mk_unit_conversion(db, make_uuid.new_id(), org_id, from_unit, to_unit, user_id)
        with pytest.raises(IntegrityError):
            _mk_unit_conversion(db, make_uuid.new_id(), org_id, from_unit, to_unit, user_id)

    def test_seed_unit_catalog_idempotent(self, db, make_uuid) -> None:
        clock = FrozenClock(datetime.now(UTC))
        organization_id = make_uuid.new_id()

        first = seed_unit_catalog(db, organization_id, clock, make_uuid)
        count_after_first = db.execute(
            text("SELECT count(*) FROM unit_definitions WHERE organization_id IS NULL")
        ).scalar_one()
        assert count_after_first == len(STANDARD_UNIT_CATALOG)
        assert len(first) == len(STANDARD_UNIT_CATALOG)

        second = seed_unit_catalog(db, organization_id, clock, make_uuid)
        count_after_second = db.execute(
            text("SELECT count(*) FROM unit_definitions WHERE organization_id IS NULL")
        ).scalar_one()
        assert count_after_second == count_after_first
        assert len(second) == len(first)

    def test_seed_unit_catalog_pound_to_kg_factor_exact(self, db, make_uuid) -> None:
        clock = FrozenClock(datetime.now(UTC))
        organization_id = make_uuid.new_id()

        rows = seed_unit_catalog(db, organization_id, clock, make_uuid)
        pound = next(row for row in rows if row.code == "lb")
        assert pound.to_canonical_factor == Decimal("0.45359237")
