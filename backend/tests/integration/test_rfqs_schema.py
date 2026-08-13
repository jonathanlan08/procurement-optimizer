"""RFQ schema tests: migration coverage, composite-FK org isolation on
rfq_lines.part_id and rfq_suppliers.supplier_id, required_quantity CHECK,
line-number uniqueness per RFQ, supplier uniqueness per RFQ, rfq_status_enum
rejecting invalid values, and ALLOWED_RFQ_TRANSITIONS coverage/no-self-
transition properties (docs/planning/02-erd.md §5)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError

from app.models.rfqs import ALLOWED_RFQ_TRANSITIONS, RfqStatus


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
    # composite FK for rfq_lines.unit_definition_id.
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


def _mk_supplier(db, supplier_id, org_id, code) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO suppliers (id, organization_id, code, name, country_code,"
            " created_at, updated_at)"
            " VALUES (:id, :org, :code, :code, 'US', :now, :now)"
        ),
        {"id": supplier_id, "org": org_id, "code": code, "now": now},
    )


def _mk_rfq(
    db,
    rfq_id,
    org_id,
    created_by_id,
    internal_reference,
    name="Test RFQ",
    status="draft",
    due_date=date(2026, 9, 1),
    source_bom_id=None,
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO rfqs (id, organization_id, name, internal_reference,"
            " status, base_currency, due_date, source_bom_id, created_by_id,"
            " version, created_at, updated_at)"
            " VALUES (:id, :org, :name, :ref, CAST(:status AS rfq_status_enum),"
            " 'USD', :due, :source_bom, :creator, 1, :now, :now)"
        ),
        {
            "id": rfq_id,
            "org": org_id,
            "name": name,
            "ref": internal_reference,
            "status": status,
            "due": due_date,
            "source_bom": source_bom_id,
            "creator": created_by_id,
            "now": now,
        },
    )


def _mk_rfq_line(
    db,
    line_id,
    org_id,
    rfq_id,
    line_number,
    part_id,
    unit_id,
    quantity="1",
) -> None:
    db.execute(
        text(
            "INSERT INTO rfq_lines (id, organization_id, rfq_id, line_number,"
            " part_id, required_quantity, unit_definition_id)"
            " VALUES (:id, :org, :rfq, :line_no, :part, :qty, :unit)"
        ),
        {
            "id": line_id,
            "org": org_id,
            "rfq": rfq_id,
            "line_no": line_number,
            "part": part_id,
            "qty": quantity,
            "unit": unit_id,
        },
    )


def _mk_rfq_supplier(db, rfq_supplier_id, org_id, rfq_id, supplier_id) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO rfq_suppliers (id, organization_id, rfq_id, supplier_id,"
            " invited_at)"
            " VALUES (:id, :org, :rfq, :supplier, :now)"
        ),
        {
            "id": rfq_supplier_id,
            "org": org_id,
            "rfq": rfq_id,
            "supplier": supplier_id,
            "now": now,
        },
    )


class TestRfqsSchema:
    def test_migration_creates_rfq_tables(self, db) -> None:
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('rfqs', 'rfq_lines', 'rfq_suppliers', 'rfq_status_history')"
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {
            "rfqs",
            "rfq_lines",
            "rfq_suppliers",
            "rfq_status_history",
        }

    def test_composite_fk_rejects_cross_org_line_to_part(self, db, make_uuid) -> None:
        """The critical test: an rfq_lines row whose part_id points at a part
        owned by a *different* organization than the line's own
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

        rfq_a = make_uuid.new_id()
        _mk_rfq(db, rfq_a, org_a, user_id, "RFQ-A-1")

        with pytest.raises(IntegrityError):
            # organization_id=org_a, rfq_id=rfq_a (same org, fine), but
            # part_id=part_b belongs to org_b.
            _mk_rfq_line(db, make_uuid.new_id(), org_a, rfq_a, 1, part_b, unit_id)

    def test_composite_fk_accepts_same_org_line_to_part(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)

        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-1")
        rfq_id = make_uuid.new_id()
        _mk_rfq(db, rfq_id, org_id, user_id, "RFQ-1")

        line_id = make_uuid.new_id()
        _mk_rfq_line(db, line_id, org_id, rfq_id, 1, part_id, unit_id)

        count = db.execute(
            text("SELECT count(*) FROM rfq_lines WHERE id = :id"), {"id": line_id}
        ).scalar_one()
        assert count == 1

    def test_composite_fk_rejects_cross_org_rfq_supplier(self, db, make_uuid) -> None:
        """Same isolation guarantee as above, on rfq_suppliers.supplier_id:
        a supplier belonging to a different org than the invitation row's
        own organization_id must be refused by the composite FK."""
        org_a, org_b = make_uuid.new_id(), make_uuid.new_id()
        _mk_org(db, org_a, f"org-a-{org_a}")
        _mk_org(db, org_b, f"org-b-{org_b}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")

        rfq_a = make_uuid.new_id()
        _mk_rfq(db, rfq_a, org_a, user_id, "RFQ-A-2")

        supplier_b = make_uuid.new_id()
        _mk_supplier(db, supplier_b, org_b, "SUP-B")

        with pytest.raises(IntegrityError):
            _mk_rfq_supplier(db, make_uuid.new_id(), org_a, rfq_a, supplier_b)

    def test_composite_fk_accepts_same_org_rfq_supplier(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")

        rfq_id = make_uuid.new_id()
        _mk_rfq(db, rfq_id, org_id, user_id, "RFQ-2")
        supplier_id = make_uuid.new_id()
        _mk_supplier(db, supplier_id, org_id, "SUP-1")

        rfq_supplier_id = make_uuid.new_id()
        _mk_rfq_supplier(db, rfq_supplier_id, org_id, rfq_id, supplier_id)

        count = db.execute(
            text("SELECT count(*) FROM rfq_suppliers WHERE id = :id"),
            {"id": rfq_supplier_id},
        ).scalar_one()
        assert count == 1

    def test_required_quantity_check_rejects_zero(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)
        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-QTY-0")
        rfq_id = make_uuid.new_id()
        _mk_rfq(db, rfq_id, org_id, user_id, "RFQ-QTY-0")

        with pytest.raises(IntegrityError):
            _mk_rfq_line(
                db, make_uuid.new_id(), org_id, rfq_id, 1, part_id, unit_id, quantity="0"
            )

    def test_required_quantity_check_rejects_negative(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        unit_id = make_uuid.new_id()
        _mk_unit_definition(db, unit_id)
        part_id = make_uuid.new_id()
        _mk_part(db, part_id, org_id, unit_id, "IPN-QTY-NEG")
        rfq_id = make_uuid.new_id()
        _mk_rfq(db, rfq_id, org_id, user_id, "RFQ-QTY-NEG")

        with pytest.raises(IntegrityError):
            _mk_rfq_line(
                db, make_uuid.new_id(), org_id, rfq_id, 1, part_id, unit_id, quantity="-3"
            )

    def test_line_number_unique_per_rfq(self, db, make_uuid) -> None:
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
        rfq_id = make_uuid.new_id()
        _mk_rfq(db, rfq_id, org_id, user_id, "RFQ-LN")

        _mk_rfq_line(db, make_uuid.new_id(), org_id, rfq_id, 1, part_1, unit_id)
        with pytest.raises(IntegrityError):
            _mk_rfq_line(db, make_uuid.new_id(), org_id, rfq_id, 1, part_2, unit_id)

    def test_supplier_unique_per_rfq(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")
        rfq_id = make_uuid.new_id()
        _mk_rfq(db, rfq_id, org_id, user_id, "RFQ-SUP")
        supplier_id = make_uuid.new_id()
        _mk_supplier(db, supplier_id, org_id, "SUP-DUP")

        _mk_rfq_supplier(db, make_uuid.new_id(), org_id, rfq_id, supplier_id)
        with pytest.raises(IntegrityError):
            _mk_rfq_supplier(db, make_uuid.new_id(), org_id, rfq_id, supplier_id)

    def test_status_enum_rejects_invalid_value(self, db, make_uuid) -> None:
        org_id = make_uuid.new_id()
        _mk_org(db, org_id, f"org-{org_id}")
        user_id = make_uuid.new_id()
        _mk_user(db, user_id, f"user-{user_id}@example.test")

        with pytest.raises(DataError):
            _mk_rfq(
                db,
                make_uuid.new_id(),
                org_id,
                user_id,
                "RFQ-BAD-STATUS",
                status="not_a_real_status",
            )

    def test_allowed_transitions_covers_every_non_terminal_status(self) -> None:
        """Per the ruling, ARCHIVED is the only terminal status
        (nothing transitions out of it). Every other status must have at
        least one allowed outgoing transition."""
        non_terminal_statuses = set(RfqStatus) - {RfqStatus.ARCHIVED}
        for status in non_terminal_statuses:
            assert status in ALLOWED_RFQ_TRANSITIONS
            assert len(ALLOWED_RFQ_TRANSITIONS[status]) > 0, (
                f"{status} has no allowed outgoing transitions"
            )

    def test_allowed_transitions_terminal_status_has_no_outgoing_transitions(self) -> None:
        assert ALLOWED_RFQ_TRANSITIONS[RfqStatus.ARCHIVED] == frozenset()

    @given(status=st.sampled_from(list(RfqStatus)))
    def test_allowed_transitions_no_self_transition(self, status: RfqStatus) -> None:
        """Property test over the ALLOWED_RFQ_TRANSITIONS dict: for every
        status, its own value never appears among its allowed targets."""
        targets = ALLOWED_RFQ_TRANSITIONS.get(status, frozenset())
        assert status not in targets

    @given(
        pair=st.sampled_from(
            [
                (from_status, to_status)
                for from_status, targets in ALLOWED_RFQ_TRANSITIONS.items()
                for to_status in targets
            ]
        )
    )
    def test_allowed_transitions_every_pair_is_a_real_transition(
        self, pair: tuple[RfqStatus, RfqStatus]
    ) -> None:
        """Property test over the flattened (from, to) pairs actually present
        in the dict: neither side is ever the same status."""
        from_status, to_status = pair
        assert from_status != to_status
