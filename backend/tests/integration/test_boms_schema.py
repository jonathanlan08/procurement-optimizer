"""BOM schema tests: migration coverage, composite-FK org isolation on
bill_of_material_lines.part_id, quantity CHECK, line-number uniqueness per
BOM, and the copy-on-write version chain (docs/planning/02-erd.md §5, §10)."""

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


def _mk_unit_definition(db, unit_id, code="each") -> None:
    # organization_id NULL: global catalogue entry, avoids needing a
    # composite FK for bill_of_material_lines.unit_definition_id.
    db.execute(
        text(
            "INSERT INTO unit_definitions (id, organization_id, code, display_name,"
            " dimension, to_canonical_factor, is_user_defined)"
            " VALUES (:id, NULL, :code, :code, 'count'::dimension_enum, 1, false)"
        ),
        {"id": unit_id, "code": code},
    )


def _mk_part(db, part_id, org_id, unit_id, internal_part_number, name="Test Part") -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO parts (id, organization_id, internal_part_number, name,"
            " unit_definition_id, is_active, version, created_at, updated_at)"
            " VALUES (:id, :org, :ipn, :name, :unit, true, 1, :now, :now)"
        ),
        {
            "id": part_id,
            "org": org_id,
            "ipn": internal_part_number,
            "name": name,
            "unit": unit_id,
            "now": now,
        },
    )


def _mk_bom(
    db,
    bom_id,
    org_id,
    root_bom_id,
    created_by_id,
    version_number=1,
    previous_version_id=None,
    name="Widget BOM",
    product_name="Widget",
    status="draft",
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO bills_of_materials (id, organization_id, root_bom_id,"
            " version_number, previous_version_id, name, product_name, status,"
            " created_by_id, created_at, updated_at)"
            " VALUES (:id, :org, :root, :version, :previous, :name, :product,"
            " CAST(:status AS bom_status_enum), :creator, :now, :now)"
        ),
        {
            "id": bom_id,
            "org": org_id,
            "root": root_bom_id,
            "version": version_number,
            "previous": previous_version_id,
            "name": name,
            "product": product_name,
            "status": status,
            "creator": created_by_id,
            "now": now,
        },
    )


def _mk_bom_line(
    db,
    line_id,
    org_id,
    bom_id,
    line_number,
    part_id,
    unit_id,
    quantity="1",
    substitute_part_id=None,
) -> None:
    db.execute(
        text(
            "INSERT INTO bill_of_material_lines (id, organization_id, bom_id,"
            " line_number, part_id, quantity_per_assembly, unit_definition_id,"
            " is_optional, substitute_part_id)"
            " VALUES (:id, :org, :bom, :line_no, :part, :qty, :unit, false, :sub)"
        ),
        {
            "id": line_id,
            "org": org_id,
            "bom": bom_id,
            "line_no": line_number,
            "part": part_id,
            "qty": quantity,
            "unit": unit_id,
            "sub": substitute_part_id,
        },
    )


class TestBomsSchema:
    def test_migration_creates_bom_tables(self, db) -> None:
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('bills_of_materials', 'bill_of_material_lines')"
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {"bills_of_materials", "bill_of_material_lines"}

    def test_composite_fk_rejects_cross_org_line_to_part(self, db, make_uuid) -> None:
        """The critical test: a bill_of_material_lines row whose part_id points
        at a part owned by a *different* organization than the line's own
        organization_id must be refused at the database layer by the
        composite FK, not merely by application logic."""
        org_a, org_b = make_uuid.new_id(), make_uuid.new_id()
        _mk_org(db, org_a, f"org-a-{org_a}")
        _mk_org(db, org_b, f"org-b-{org_b}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        part_b = make_uuid.new_id()
        _mk_part(db, part_b, org_b, unit_id, "IPN-B")

        bom_a = make_uuid.new_id()
        _mk_bom(db, bom_a, org_a, bom_a, user_id)

        with pytest.raises(IntegrityError):
            # organization_id=org_a, bom_id=bom_a (same org, fine), but
            # part_id=part_b belongs to org_b.
            _mk_bom_line(db, make_uuid.new_id(), org_a, bom_a, 1, part_b, unit_id)

    def test_composite_fk_accepts_same_org_line_to_part(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-1")
        bom_id = make_uuid.new_id()
        _mk_bom(db, bom_id, org_id, bom_id, user_id)

        line_id = make_uuid.new_id()
        _mk_bom_line(db, line_id, org_id, bom_id, 1, part_id, unit_id)

        count = db.execute(
            text("SELECT count(*) FROM bill_of_material_lines WHERE id = :id"), {"id": line_id}
        ).scalar_one()
        assert count == 1

    def test_quantity_check_rejects_zero(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)
        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-QTY-0")
        bom_id = make_uuid.new_id()
        _mk_bom(db, bom_id, org_id, bom_id, user_id)

        with pytest.raises(IntegrityError):
            _mk_bom_line(db, make_uuid.new_id(), org_id, bom_id, 1, part_id, unit_id, quantity="0")

    def test_quantity_check_rejects_negative(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)
        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-QTY-NEG")
        bom_id = make_uuid.new_id()
        _mk_bom(db, bom_id, org_id, bom_id, user_id)

        with pytest.raises(IntegrityError):
            _mk_bom_line(
                db, make_uuid.new_id(), org_id, bom_id, 1, part_id, unit_id, quantity="-3"
            )

    def test_line_number_unique_per_bom(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)
        part_1 = make_uuid.new_id()
        _mk_part(db, part_1, org_id, unit_id, "IPN-LN-1")
        part_2 = make_uuid.new_id()
        _mk_part(db, part_2, org_id, unit_id, "IPN-LN-2")
        bom_id = make_uuid.new_id()
        _mk_bom(db, bom_id, org_id, bom_id, user_id)

        _mk_bom_line(db, make_uuid.new_id(), org_id, bom_id, 1, part_1, unit_id)
        with pytest.raises(IntegrityError):
            _mk_bom_line(db, make_uuid.new_id(), org_id, bom_id, 1, part_2, unit_id)

    def test_substitute_part_self_reference_rejected(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)
        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-SELF-SUB")
        bom_id = make_uuid.new_id()
        _mk_bom(db, bom_id, org_id, bom_id, user_id)

        with pytest.raises(IntegrityError):
            _mk_bom_line(
                db,
                make_uuid.new_id(),
                org_id,
                bom_id,
                1,
                part_id,
                unit_id,
                substitute_part_id=part_id,
            )

    def test_supersedes_chain_v1_then_v2(self, db, make_uuid) -> None:
        """Copy-on-write versioning (02-erd.md §10): insert v1 (root_bom_id
        points at itself), then insert v2 with previous_version_id=v1 and the
        same root_bom_id, version_number + 1."""
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")

        v1_id = make_uuid.new_id()
        _mk_bom(db, v1_id, org_id, v1_id, user_id, version_number=1, status="active")

        v2_id = make_uuid.new_id()
        _mk_bom(
            db,
            v2_id,
            org_id,
            v1_id,
            user_id,
            version_number=2,
            previous_version_id=v1_id,
            status="active",
        )

        # v1 is now superseded by the service layer / caller
        db.execute(
            text(
                "UPDATE bills_of_materials SET status = 'superseded'::bom_status_enum"
                " WHERE id = :id"
            ),
            {"id": v1_id},
        )

        rows = db.execute(
            text(
                "SELECT id, version_number, previous_version_id, status"
                " FROM bills_of_materials WHERE root_bom_id = :root ORDER BY version_number"
            ),
            {"root": v1_id},
        ).all()
        assert [r.version_number for r in rows] == [1, 2]
        assert rows[0].status == "superseded"
        assert rows[1].previous_version_id == v1_id
        assert rows[1].status == "active"

    def test_root_version_unique_rejects_duplicate_version_number(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")

        v1_id = make_uuid.new_id()
        _mk_bom(db, v1_id, org_id, v1_id, user_id, version_number=1)

        with pytest.raises(IntegrityError):
            # a second v2 racing for the same root_bom_id + version_number
            _mk_bom(
                db,
                make_uuid.new_id(),
                org_id,
                v1_id,
                user_id,
                version_number=1,
                previous_version_id=None,
            )

    def test_first_version_check_rejects_previous_id_on_version_one(self, db, make_uuid) -> None:
        """The paired CHECK - not the composite FK - is what this test
        targets: previous_version_id references a real, same-org row (so the
        FK is satisfied), yet version_number=1 must still have
        previous_version_id NULL."""
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")

        other_root_id = make_uuid.new_id()
        _mk_bom(db, other_root_id, org_id, other_root_id, user_id, version_number=1)

        bom_id = make_uuid.new_id()
        with pytest.raises(IntegrityError):
            _mk_bom(
                db,
                bom_id,
                org_id,
                bom_id,
                user_id,
                version_number=1,
                previous_version_id=other_root_id,
            )
