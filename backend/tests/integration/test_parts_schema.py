"""Parts schema tests: migration coverage, composite-FK org isolation on both
part_id and alternative_part_id, self-reference CHECK, internal-part-number
uniqueness per org (including reuse after archive), normalized_key generation
(docs/planning/02-erd.md §4)."""

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


def _mk_unit_definition(db, unit_id, code="each") -> None:
    # organization_id NULL: global catalogue entry (units.py convention),
    # avoids needing a composite FK for parts.unit_definition_id.
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


def _mk_part_alternative(
    db, alt_id, org_id, part_id, alternative_part_id, approval_status="approved"
) -> None:
    db.execute(
        text(
            "INSERT INTO part_alternatives (id, organization_id, part_id,"
            " alternative_part_id, approval_status)"
            " VALUES (:id, :org, :part, :alt, :status)"
        ),
        {
            "id": alt_id,
            "org": org_id,
            "part": part_id,
            "alt": alternative_part_id,
            "status": approval_status,
        },
    )


class TestPartsSchema:
    def test_migration_creates_parts_tables(self, db) -> None:
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('parts', 'part_alternatives')"
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {"parts", "part_alternatives"}

    def test_composite_fk_rejects_cross_org_alternative(self, db, make_uuid) -> None:
        """The critical test: a part_alternatives row whose alternative_part_id
        points at a part owned by a *different* organization than the row's
        own organization_id must be refused at the database layer by the
        composite FK, not merely by application logic."""
        org_a, org_b = make_uuid.new_id(), make_uuid.new_id()
        _mk_org(db, org_a, f"org-a-{org_a}")
        _mk_org(db, org_b, f"org-b-{org_b}")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        part_a = make_uuid.new_id()
        _mk_part(db, part_a, org_a, unit_id, "IPN-A")
        part_b = make_uuid.new_id()
        _mk_part(db, part_b, org_b, unit_id, "IPN-B")

        with pytest.raises(IntegrityError):
            # organization_id=org_a, part_id=part_a (same org, fine), but
            # alternative_part_id=part_b belongs to org_b.
            _mk_part_alternative(db, make_uuid.new_id(), org_a, part_a, part_b)

    def test_composite_fk_accepts_same_org_alternative(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        part_a = make_uuid.new_id()
        _mk_part(db, part_a, org_id, unit_id, "IPN-1")
        part_b = make_uuid.new_id()
        _mk_part(db, part_b, org_id, unit_id, "IPN-2")

        alt_id = make_uuid.new_id()
        _mk_part_alternative(db, alt_id, org_id, part_a, part_b)

        count = db.execute(
            text("SELECT count(*) FROM part_alternatives WHERE id = :id"), {"id": alt_id}
        ).scalar_one()
        assert count == 1

    def test_alternative_part_id_null_allowed_for_external(self, db, make_uuid) -> None:
        """External alternatives (SPEC §3, 02-erd.md §4) identify themselves
        via alternative_mpn with no internal part row; the composite FK on
        alternative_part_id must not block a NULL value (MATCH SIMPLE)."""
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)
        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-EXT")

        alt_id = make_uuid.new_id()
        db.execute(
            text(
                "INSERT INTO part_alternatives (id, organization_id, part_id,"
                " alternative_part_id, alternative_mpn, approval_status)"
                " VALUES (:id, :org, :part, NULL, 'EXT-MPN-1', 'conditional')"
            ),
            {"id": alt_id, "org": org_id, "part": part_id},
        )
        count = db.execute(
            text("SELECT count(*) FROM part_alternatives WHERE id = :id"), {"id": alt_id}
        ).scalar_one()
        assert count == 1

    def test_self_reference_check_rejects_part_as_its_own_alternative(
        self, db, make_uuid
    ) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)
        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-SELF")

        with pytest.raises(IntegrityError):
            _mk_part_alternative(db, make_uuid.new_id(), org_id, part_id, part_id)

    def test_internal_part_number_unique_per_org_reusable_across_orgs(
        self, db, make_uuid
    ) -> None:
        org_a, org_b = make_uuid.new_id(), make_uuid.new_id()
        _mk_org(db, org_a, f"org-a-{org_a}")
        _mk_org(db, org_b, f"org-b-{org_b}")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        _mk_part(db, make_uuid.new_id(), org_a, unit_id, "DUP-IPN")
        # same internal_part_number, different org: allowed
        _mk_part(db, make_uuid.new_id(), org_b, unit_id, "DUP-IPN")

        with pytest.raises(IntegrityError):
            _mk_part(db, make_uuid.new_id(), org_a, unit_id, "DUP-IPN")

    def test_internal_part_number_case_insensitive_unique(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        _mk_part(db, make_uuid.new_id(), org_id, unit_id, "CASE-1")
        with pytest.raises(IntegrityError):
            _mk_part(db, make_uuid.new_id(), org_id, unit_id, "case-1")

    def test_internal_part_number_reusable_after_archive(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        first_id = make_uuid.new_id()
        _mk_part(db, first_id, org_id, unit_id, "REUSE-1")

        db.execute(
            text("UPDATE parts SET archived_at = :now WHERE id = :id"),
            {"now": datetime.now(UTC), "id": first_id},
        )

        # archived, so the same internal_part_number can be reused in the
        # same org (uq_parts_org_ipn_active is a partial index)
        second_id = make_uuid.new_id()
        _mk_part(db, second_id, org_id, unit_id, "REUSE-1")

        count = db.execute(
            text(
                "SELECT count(*) FROM parts WHERE organization_id = :org"
                " AND lower(internal_part_number) = 'reuse-1'"
            ),
            {"org": org_id},
        ).scalar_one()
        assert count == 2

    def test_normalized_key_strips_punctuation_and_lowercases(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "ACME-100 Rev.B")

        normalized_key = db.execute(
            text("SELECT normalized_key FROM parts WHERE id = :id"), {"id": part_id}
        ).scalar_one()
        assert normalized_key == "acme100revb"
