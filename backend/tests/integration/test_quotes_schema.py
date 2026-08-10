"""Quote schema tests: migration coverage, composite-FK org isolation on
quotes.rfq_id/supplier_id, quote_lines.quote_id/matched_rfq_line_id/part_id,
and quote_price_breaks.quote_line_id; quantity/unit_price/price-break CHECK
constraints; quote_price_breaks min_quantity uniqueness per line (the
adaptation standing in for the forbidden btree_gist EXCLUDE no-overlap
constraint — see app/models/quotes.py module docstring); quote_terms
one-per-quote; the superseded_by_id self-reference supersession chain; and
quote_status_enum/quote_source_enum/match_status_enum rejecting invalid
values (docs/planning/02-erd.md §6).

Note: 02-erd.md's constraint catalogue (§8) has no CHECK requiring
`expiration_date >= quote_date` anywhere, and 00-decisions.md §4 explicitly
rules that "expired quotes [are] usable with prominent warning" — so no such
constraint exists in the migration, and none is tested here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError


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
    # composite FK for quote_lines.unit_definition_id.
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
    db, rfq_id, org_id, created_by_id, internal_reference, due_date=date(2026, 9, 1)
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO rfqs (id, organization_id, name, internal_reference,"
            " status, base_currency, due_date, created_by_id,"
            " version, created_at, updated_at)"
            " VALUES (:id, :org, :ref, :ref, 'draft'::rfq_status_enum,"
            " 'USD', :due, :creator, 1, :now, :now)"
        ),
        {
            "id": rfq_id,
            "org": org_id,
            "ref": internal_reference,
            "due": due_date,
            "creator": created_by_id,
            "now": now,
        },
    )


def _mk_rfq_line(db, line_id, org_id, rfq_id, line_number, part_id, unit_id, quantity="1") -> None:
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


def _mk_quote(
    db,
    quote_id,
    org_id,
    rfq_id,
    supplier_id,
    quote_date=date(2026, 8, 1),
    currency="USD",
    status="draft",
    source="manual",
    superseded_by_id=None,
    quote_number=None,
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO quotes (id, organization_id, rfq_id, supplier_id, quote_number,"
            " quote_date, currency, status, source, superseded_by_id,"
            " version, created_at, updated_at)"
            " VALUES (:id, :org, :rfq, :supplier, :quote_number, :quote_date, :currency,"
            " CAST(:status AS quote_status_enum), CAST(:source AS quote_source_enum),"
            " :superseded_by, 1, :now, :now)"
        ),
        {
            "id": quote_id,
            "org": org_id,
            "rfq": rfq_id,
            "supplier": supplier_id,
            "quote_number": quote_number,
            "quote_date": quote_date,
            "currency": currency,
            "status": status,
            "source": source,
            "superseded_by": superseded_by_id,
            "now": now,
        },
    )


def _mk_quote_line(
    db,
    line_id,
    org_id,
    quote_id,
    line_number,
    unit_id,
    quantity="10",
    unit_price=None,
    moq=None,
    matched_rfq_line_id=None,
    part_id=None,
) -> None:
    db.execute(
        text(
            "INSERT INTO quote_lines (id, organization_id, quote_id, line_number,"
            " quantity, unit_definition_id, unit_price, moq, matched_rfq_line_id,"
            " match_status, part_id)"
            " VALUES (:id, :org, :quote, :line_no, :qty, :unit, :unit_price, :moq,"
            " :matched_rfq_line, 'unmatched'::match_status_enum, :part)"
        ),
        {
            "id": line_id,
            "org": org_id,
            "quote": quote_id,
            "line_no": line_number,
            "qty": quantity,
            "unit": unit_id,
            "unit_price": unit_price,
            "moq": moq,
            "matched_rfq_line": matched_rfq_line_id,
            "part": part_id,
        },
    )


def _mk_quote_price_break(
    db,
    break_id,
    org_id,
    quote_line_id,
    min_quantity,
    max_quantity=None,
    unit_price="10.00",
    setup_fee=None,
    tier_index=None,
) -> None:
    db.execute(
        text(
            "INSERT INTO quote_price_breaks (id, organization_id, quote_line_id,"
            " min_quantity, max_quantity, unit_price, setup_fee, tier_index)"
            " VALUES (:id, :org, :line, :min_qty, :max_qty, :unit_price, :setup_fee,"
            " :tier_index)"
        ),
        {
            "id": break_id,
            "org": org_id,
            "line": quote_line_id,
            "min_qty": min_quantity,
            "max_qty": max_quantity,
            "unit_price": unit_price,
            "setup_fee": setup_fee,
            "tier_index": tier_index,
        },
    )


def _mk_quote_terms(db, terms_id, org_id, quote_id, payment_terms=None) -> None:
    db.execute(
        text(
            "INSERT INTO quote_terms (id, organization_id, quote_id, payment_terms)"
            " VALUES (:id, :org, :quote, :payment_terms)"
        ),
        {"id": terms_id, "org": org_id, "quote": quote_id, "payment_terms": payment_terms},
    )


class _Fixture:
    """Bundles one org's worth of parent rows a quote needs, to keep test
    bodies focused on the behaviour under test. Generates and owns its own
    organization id/slug so call sites never juggle ids for a throwaway org."""

    def __init__(self, db, make_uuid, label="org") -> None:
        self.db = db
        self.make_uuid = make_uuid
        org_id = make_uuid.new_id()
        self.org_id = org_id
        _mk_org(db, org_id, f"{label}-{org_id}")
        self.user_id = make_uuid.new_id()
        _mk_user(db, self.user_id, f"user-{self.user_id}@example.test")
        self.unit_id = make_uuid.new_id()
        _mk_unit_definition(db, self.unit_id, code=f"each-{org_id}")
        self.part_id = make_uuid.new_id()
        _mk_part(db, self.part_id, org_id, self.unit_id, f"IPN-{org_id}")
        self.supplier_id = make_uuid.new_id()
        _mk_supplier(db, self.supplier_id, org_id, f"SUP-{org_id}")
        self.rfq_id = make_uuid.new_id()
        _mk_rfq(db, self.rfq_id, org_id, self.user_id, f"RFQ-{org_id}")
        self.rfq_line_id = make_uuid.new_id()
        _mk_rfq_line(db, self.rfq_line_id, org_id, self.rfq_id, 1, self.part_id, self.unit_id)

    def mk_quote(self, **kwargs):
        quote_id = self.make_uuid.new_id()
        _mk_quote(self.db, quote_id, self.org_id, self.rfq_id, self.supplier_id, **kwargs)
        return quote_id

    def mk_quote_line(self, quote_id, line_number=1, **kwargs):
        line_id = self.make_uuid.new_id()
        _mk_quote_line(self.db, line_id, self.org_id, quote_id, line_number, self.unit_id, **kwargs)
        return line_id


@pytest.fixture()
def fx(db, make_uuid):
    return _Fixture(db, make_uuid, label="org-a")


class TestQuotesSchema:
    def test_migration_creates_quote_tables(self, db) -> None:
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('quotes', 'quote_lines', 'quote_price_breaks', 'quote_terms')"
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {
            "quotes",
            "quote_lines",
            "quote_price_breaks",
            "quote_terms",
        }

    # -- composite FK org isolation -----------------------------------

    def test_composite_fk_rejects_cross_org_quote_to_rfq(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            # organization_id=org_a, supplier_id belongs to org_a (fine), but
            # rfq_id belongs to org_b.
            _mk_quote(
                db,
                make_uuid.new_id(),
                org_a.org_id,
                org_b.rfq_id,
                org_a.supplier_id,
            )

    def test_composite_fk_rejects_cross_org_quote_to_supplier(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            _mk_quote(
                db,
                make_uuid.new_id(),
                org_a.org_id,
                org_a.rfq_id,
                org_b.supplier_id,
            )

    def test_composite_fk_accepts_same_org_quote(self, fx, make_uuid) -> None:
        quote_id = fx.mk_quote()
        count = fx.db.execute(
            text("SELECT count(*) FROM quotes WHERE id = :id"), {"id": quote_id}
        ).scalar_one()
        assert count == 1

    def test_composite_fk_rejects_cross_org_quote_line_to_quote(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        quote_b = org_b.mk_quote()

        with pytest.raises(IntegrityError):
            # organization_id=org_a but quote_id belongs to org_b.
            _mk_quote_line(db, make_uuid.new_id(), org_a.org_id, quote_b, 1, org_a.unit_id)

    def test_composite_fk_rejects_cross_org_quote_line_to_part(self, fx, make_uuid) -> None:
        other = _Fixture(fx.db, make_uuid, label="org-b")
        quote_id = fx.mk_quote()

        with pytest.raises(IntegrityError):
            fx.mk_quote_line(quote_id, part_id=other.part_id)

    def test_composite_fk_rejects_cross_org_quote_line_to_matched_rfq_line(
        self, fx, make_uuid
    ) -> None:
        other = _Fixture(fx.db, make_uuid, label="org-b")
        quote_id = fx.mk_quote()

        with pytest.raises(IntegrityError):
            fx.mk_quote_line(quote_id, matched_rfq_line_id=other.rfq_line_id)

    def test_composite_fk_accepts_same_org_quote_line(self, fx) -> None:
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(
            quote_id, matched_rfq_line_id=fx.rfq_line_id, part_id=fx.part_id
        )
        count = fx.db.execute(
            text("SELECT count(*) FROM quote_lines WHERE id = :id"), {"id": line_id}
        ).scalar_one()
        assert count == 1

    def test_composite_fk_rejects_cross_org_price_break_to_line(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        quote_b = org_b.mk_quote()
        line_b = org_b.mk_quote_line(quote_b)

        with pytest.raises(IntegrityError):
            # organization_id=org_a but quote_line_id belongs to org_b.
            _mk_quote_price_break(db, make_uuid.new_id(), org_a.org_id, line_b, "1")

    def test_composite_fk_accepts_same_org_price_break(self, fx) -> None:
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id)
        break_id = fx.make_uuid.new_id()
        _mk_quote_price_break(fx.db, break_id, fx.org_id, line_id, "1", max_quantity="99")
        count = fx.db.execute(
            text("SELECT count(*) FROM quote_price_breaks WHERE id = :id"), {"id": break_id}
        ).scalar_one()
        assert count == 1

    def test_price_break_cascades_on_quote_line_delete(self, fx) -> None:
        """02-erd.md §11's explicit cascade whitelist: quote_lines ->
        quote_price_breaks is the one CASCADE pair among these four tables."""
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id)
        break_id = fx.make_uuid.new_id()
        _mk_quote_price_break(fx.db, break_id, fx.org_id, line_id, "1")

        fx.db.execute(text("DELETE FROM quote_lines WHERE id = :id"), {"id": line_id})

        count = fx.db.execute(
            text("SELECT count(*) FROM quote_price_breaks WHERE id = :id"), {"id": break_id}
        ).scalar_one()
        assert count == 0

    # -- CHECK constraints ----------------------------------------------

    def test_quantity_check_rejects_zero(self, fx) -> None:
        quote_id = fx.mk_quote()
        with pytest.raises(IntegrityError):
            fx.mk_quote_line(quote_id, quantity="0")

    def test_quantity_check_rejects_negative(self, fx) -> None:
        quote_id = fx.mk_quote()
        with pytest.raises(IntegrityError):
            fx.mk_quote_line(quote_id, quantity="-1")

    def test_negative_unit_price_rejected_on_quote_line(self, fx) -> None:
        quote_id = fx.mk_quote()
        with pytest.raises(IntegrityError):
            fx.mk_quote_line(quote_id, unit_price="-0.01")

    def test_null_unit_price_allowed_on_quote_line(self, fx) -> None:
        """Missing-field semantics (SPEC §7): NULL means the supplier did
        not state a unit price, not zero."""
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id, unit_price=None)
        unit_price = fx.db.execute(
            text("SELECT unit_price FROM quote_lines WHERE id = :id"), {"id": line_id}
        ).scalar_one()
        assert unit_price is None

    def test_zero_unit_price_allowed_on_quote_line(self, fx) -> None:
        """02-erd.md §8 ck_unit_price_nonneg: '0 is legal: free sample line'."""
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id, unit_price="0")
        unit_price = fx.db.execute(
            text("SELECT unit_price FROM quote_lines WHERE id = :id"), {"id": line_id}
        ).scalar_one()
        assert unit_price == 0

    def test_moq_check_rejects_zero(self, fx) -> None:
        quote_id = fx.mk_quote()
        with pytest.raises(IntegrityError):
            fx.mk_quote_line(quote_id, moq="0")

    def test_negative_unit_price_rejected_on_price_break(self, fx) -> None:
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id)
        with pytest.raises(IntegrityError):
            _mk_quote_price_break(
                fx.db, fx.make_uuid.new_id(), fx.org_id, line_id, "1", unit_price="-5.00"
            )

    def test_price_break_range_check_rejects_max_less_than_min(self, fx) -> None:
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id)
        with pytest.raises(IntegrityError):
            _mk_quote_price_break(
                fx.db,
                fx.make_uuid.new_id(),
                fx.org_id,
                line_id,
                min_quantity="100",
                max_quantity="50",
            )

    def test_price_break_range_check_rejects_zero_min_quantity(self, fx) -> None:
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id)
        with pytest.raises(IntegrityError):
            _mk_quote_price_break(fx.db, fx.make_uuid.new_id(), fx.org_id, line_id, "0")

    def test_price_break_open_ended_max_quantity_allowed(self, fx) -> None:
        """02-erd.md §6: 'numeric max_quantity 18,6 null=open ended'."""
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id)
        break_id = fx.make_uuid.new_id()
        _mk_quote_price_break(fx.db, break_id, fx.org_id, line_id, "1000", max_quantity=None)
        max_quantity = fx.db.execute(
            text("SELECT max_quantity FROM quote_price_breaks WHERE id = :id"),
            {"id": break_id},
        ).scalar_one()
        assert max_quantity is None

    def test_quote_currency_check_rejects_non_iso(self, fx) -> None:
        with pytest.raises(IntegrityError):
            _mk_quote(
                fx.db,
                fx.make_uuid.new_id(),
                fx.org_id,
                fx.rfq_id,
                fx.supplier_id,
                currency="usd",
            )

    # -- uniqueness -------------------------------------------------------

    def test_price_break_min_quantity_unique_per_line(self, fx) -> None:
        """02-erd.md §8 uq_price_break_min, and the adaptation standing in
        for the forbidden btree_gist no-overlap EXCLUDE constraint (see
        app/models/quotes.py module docstring): two tiers on the same line
        cannot claim the same starting quantity."""
        quote_id = fx.mk_quote()
        line_id = fx.mk_quote_line(quote_id)
        _mk_quote_price_break(fx.db, fx.make_uuid.new_id(), fx.org_id, line_id, "100")
        with pytest.raises(IntegrityError):
            _mk_quote_price_break(fx.db, fx.make_uuid.new_id(), fx.org_id, line_id, "100")

    def test_quote_line_number_unique_per_quote(self, fx) -> None:
        quote_id = fx.mk_quote()
        fx.mk_quote_line(quote_id, line_number=1)
        with pytest.raises(IntegrityError):
            fx.mk_quote_line(quote_id, line_number=1)

    def test_quote_terms_one_per_quote(self, fx) -> None:
        quote_id = fx.mk_quote()
        _mk_quote_terms(fx.db, fx.make_uuid.new_id(), fx.org_id, quote_id)
        with pytest.raises(IntegrityError):
            _mk_quote_terms(fx.db, fx.make_uuid.new_id(), fx.org_id, quote_id)

    def test_composite_fk_rejects_cross_org_quote_terms(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        quote_b = org_b.mk_quote()

        with pytest.raises(IntegrityError):
            _mk_quote_terms(db, make_uuid.new_id(), org_a.org_id, quote_b)

    # -- supersession chain -----------------------------------------------

    def test_supersession_chain_insertable(self, fx) -> None:
        """Manual revision supersession (00-decisions.md §4 #16):
        superseded_by_id is forward-pointing, old quote -> replacement quote
        (app/models/quotes.py module docstring point 2)."""
        old_quote_id = fx.mk_quote(status="draft")
        new_quote_id = fx.mk_quote(status="draft")

        fx.db.execute(
            text(
                "UPDATE quotes SET superseded_by_id = :new_id, status = 'superseded'"
                " WHERE id = :old_id"
            ),
            {"new_id": new_quote_id, "old_id": old_quote_id},
        )

        superseded_by = fx.db.execute(
            text("SELECT superseded_by_id FROM quotes WHERE id = :id"), {"id": old_quote_id}
        ).scalar_one()
        assert superseded_by == new_quote_id

    def test_superseded_by_rejects_self_reference(self, fx) -> None:
        quote_id = fx.mk_quote()
        with pytest.raises(IntegrityError):
            fx.db.execute(
                text("UPDATE quotes SET superseded_by_id = :id WHERE id = :id"),
                {"id": quote_id},
            )

    def test_superseded_by_composite_fk_rejects_cross_org(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        quote_a = org_a.mk_quote()
        quote_b = org_b.mk_quote()

        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE quotes SET superseded_by_id = :other WHERE id = :mine"),
                {"other": quote_b, "mine": quote_a},
            )

    # -- enums --------------------------------------------------------------

    def test_quote_status_enum_rejects_invalid_value(self, fx) -> None:
        with pytest.raises(DataError):
            _mk_quote(
                fx.db,
                fx.make_uuid.new_id(),
                fx.org_id,
                fx.rfq_id,
                fx.supplier_id,
                status="not_a_real_status",
            )

    def test_quote_source_enum_rejects_invalid_value(self, fx) -> None:
        with pytest.raises(DataError):
            _mk_quote(
                fx.db,
                fx.make_uuid.new_id(),
                fx.org_id,
                fx.rfq_id,
                fx.supplier_id,
                source="ai_hallucinated",
            )

    def test_match_status_enum_rejects_invalid_value(self, fx) -> None:
        quote_id = fx.mk_quote()
        with pytest.raises(DataError):
            fx.db.execute(
                text(
                    "INSERT INTO quote_lines (id, organization_id, quote_id, line_number,"
                    " quantity, unit_definition_id, match_status)"
                    " VALUES (:id, :org, :quote, 1, 1, :unit,"
                    " CAST('not_a_real_status' AS match_status_enum))"
                ),
                {
                    "id": fx.make_uuid.new_id(),
                    "org": fx.org_id,
                    "quote": quote_id,
                    "unit": fx.unit_id,
                },
            )
