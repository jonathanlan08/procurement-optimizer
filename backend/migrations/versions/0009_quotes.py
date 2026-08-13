"""quotes, quote_lines, quote_price_breaks, quote_terms

See app/models/quotes.py module docstring for the deliberate ERD deviations
(superseded_by_id direction, source rename/enum promotion, the added
quote_lines.part_id FK, production_capacity rename, and the resolution of the
quote_terms vs. quote_lines tax/tariff/duty/customs placement question in
favor of the ERD) and for the price-break no-overlap constraint adaptation
(btree_gist is forbidden in this project; UNIQUE (organization_id,
quote_line_id, min_quantity) stands in for it, with the true partial-range
overlap case deferred to an application-level validator).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE quote_status_enum AS ENUM"
        " ('draft','in_review','confirmed','superseded','rejected')"
    )
    op.execute("CREATE TYPE quote_source_enum AS ENUM ('manual','extracted')")
    op.execute(
        "CREATE TYPE match_status_enum AS ENUM ('unmatched','auto','confirmed','rejected')"
    )

    op.create_table(
        "quotes",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rfq_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_number", sa.Text(), nullable=True),
        sa.Column("quote_date", sa.Date(), nullable=False),
        sa.Column("expiration_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column(
            "status",
            pg.ENUM(
                "draft",
                "in_review",
                "confirmed",
                "superseded",
                "rejected",
                name="quote_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "source",
            pg.ENUM(
                "manual", "extracted", name="quote_source_enum", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        # nullable: only set once a manual revision supersedes this quote
        # (app/models/quotes.py module docstring point 2 - forward-pointing).
        sa.Column("superseded_by_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "archived_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_quotes_org_identity"),
        sa.ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_quotes_organization_id_rfq_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_quotes_organization_id_supplier_id",
        ),
        # composite org FK, self-referential, nullable: only checked when
        # superseded_by_id is non-null (MATCH SIMPLE).
        sa.ForeignKeyConstraint(
            ["organization_id", "superseded_by_id"],
            ["quotes.organization_id", "quotes.id"],
            ondelete="RESTRICT",
            name="fk_quotes_organization_id_superseded_by_id",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_quotes_currency_iso"),
        sa.CheckConstraint(
            "superseded_by_id IS NULL OR superseded_by_id <> id",
            name="ck_quotes_superseded_by_not_self",
        ),
    )
    op.create_index("ix_quotes_organization_id", "quotes", ["organization_id"])
    op.create_index("ix_quotes_rfq_id", "quotes", ["rfq_id"])
    op.create_index("ix_quotes_supplier_id", "quotes", ["supplier_id"])
    op.create_index(
        "ix_quotes_superseded_by_id",
        "quotes",
        ["superseded_by_id"],
        postgresql_where=sa.text("superseded_by_id IS NOT NULL"),
    )

    op.create_table(
        "quote_lines",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quote_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("quoted_part_number", sa.Text(), nullable=True),
        sa.Column("quoted_mpn", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column(
            # plain FK - unit_definitions.organization_id is nullable (global
            # catalogue); see migration 0007's rfq_lines.unit_definition_id.
            "unit_definition_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("unit_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("raw_unit_of_measure", sa.Text(), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("moq", sa.Numeric(18, 6), nullable=True),
        sa.Column("tooling_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("setup_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("packaging_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("shipping_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("insurance_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("other_fixed_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("tariff_amount", sa.Numeric(18, 6), nullable=True),
        sa.Column("duty_amount", sa.Numeric(18, 6), nullable=True),
        sa.Column("customs_fee", sa.Numeric(18, 6), nullable=True),
        sa.Column("tax_amount", sa.Numeric(18, 6), nullable=True),
        sa.Column("country_of_origin", sa.CHAR(2), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        # app/models/quotes.py module docstring point 5: renamed from ERD's
        # capacity_units.
        sa.Column("production_capacity", sa.Numeric(18, 6), nullable=True),
        sa.Column("matched_rfq_line_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "match_status",
            pg.ENUM(
                "unmatched",
                "auto",
                "confirmed",
                "rejected",
                name="match_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="unmatched",
        ),
        # module docstring point 4: an addition beyond the ERD's QUOTE_LINES
        # box, per the delegating task.
        sa.Column("part_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("organization_id", "id", name="uq_quote_lines_org_identity"),
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_id"],
            ["quotes.organization_id", "quotes.id"],
            ondelete="RESTRICT",
            name="fk_quote_lines_organization_id_quote_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "matched_rfq_line_id"],
            ["rfq_lines.organization_id", "rfq_lines.id"],
            ondelete="RESTRICT",
            name="fk_quote_lines_organization_id_matched_rfq_line_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_quote_lines_organization_id_part_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "quote_id",
            "line_number",
            name="uq_quote_lines_org_quote_line_number",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_quote_lines_quantity_pos"),
        sa.CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0", name="ck_quote_lines_unit_price_nonneg"
        ),
        sa.CheckConstraint("moq IS NULL OR moq > 0", name="ck_quote_lines_moq_pos"),
        sa.CheckConstraint(
            "production_capacity IS NULL OR production_capacity > 0",
            name="ck_quote_lines_production_capacity_pos",
        ),
        sa.CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0",
            name="ck_quote_lines_lead_time_days_nonneg",
        ),
        sa.CheckConstraint(
            "tooling_cost IS NULL OR tooling_cost >= 0",
            name="ck_quote_lines_tooling_cost_nonneg",
        ),
        sa.CheckConstraint(
            "setup_cost IS NULL OR setup_cost >= 0", name="ck_quote_lines_setup_cost_nonneg"
        ),
        sa.CheckConstraint(
            "packaging_cost IS NULL OR packaging_cost >= 0",
            name="ck_quote_lines_packaging_cost_nonneg",
        ),
        sa.CheckConstraint(
            "shipping_cost IS NULL OR shipping_cost >= 0",
            name="ck_quote_lines_shipping_cost_nonneg",
        ),
        sa.CheckConstraint(
            "insurance_cost IS NULL OR insurance_cost >= 0",
            name="ck_quote_lines_insurance_cost_nonneg",
        ),
        sa.CheckConstraint(
            "other_fixed_cost IS NULL OR other_fixed_cost >= 0",
            name="ck_quote_lines_other_fixed_cost_nonneg",
        ),
        sa.CheckConstraint(
            "tariff_amount IS NULL OR tariff_amount >= 0",
            name="ck_quote_lines_tariff_amount_nonneg",
        ),
        sa.CheckConstraint(
            "duty_amount IS NULL OR duty_amount >= 0", name="ck_quote_lines_duty_amount_nonneg"
        ),
        sa.CheckConstraint(
            "customs_fee IS NULL OR customs_fee >= 0", name="ck_quote_lines_customs_fee_nonneg"
        ),
        sa.CheckConstraint(
            "tax_amount IS NULL OR tax_amount >= 0", name="ck_quote_lines_tax_amount_nonneg"
        ),
    )
    op.create_index("ix_quote_lines_organization_id", "quote_lines", ["organization_id"])
    op.create_index(
        # 02-erd.md §9 ix_ql_quote: comparison joins, ordered by line number.
        "ix_ql_quote",
        "quote_lines",
        ["organization_id", "quote_id", "line_number"],
    )
    op.create_index(
        # 02-erd.md §9 ix_ql_match: comparison joins via the matched RFQ line.
        "ix_ql_match",
        "quote_lines",
        ["organization_id", "matched_rfq_line_id"],
    )
    op.create_index(
        "ix_quote_lines_part_id",
        "quote_lines",
        ["organization_id", "part_id"],
        postgresql_where=sa.text("part_id IS NOT NULL"),
    )

    op.create_table(
        "quote_price_breaks",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quote_line_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("min_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("max_quantity", sa.Numeric(18, 6), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("setup_fee", sa.Numeric(18, 6), nullable=True),
        sa.Column("tier_index", sa.SmallInteger(), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_quote_price_breaks_org_identity"
        ),
        # CASCADE: 02-erd.md §11's explicit whitelist - a price break cannot
        # exist without its quote line.
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_line_id"],
            ["quote_lines.organization_id", "quote_lines.id"],
            ondelete="CASCADE",
            name="fk_quote_price_breaks_organization_id_quote_line_id",
        ),
        sa.CheckConstraint(
            "min_quantity > 0 AND (max_quantity IS NULL OR max_quantity >= min_quantity)",
            name="ck_quote_price_breaks_range",
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name="ck_quote_price_breaks_unit_price_nonneg"
        ),
        sa.CheckConstraint(
            "setup_fee IS NULL OR setup_fee >= 0",
            name="ck_quote_price_breaks_setup_fee_nonneg",
        ),
        # 02-erd.md §8 uq_price_break_min, org-scoped. Also stands in for the
        # forbidden btree_gist EXCLUDE no-overlap constraint - see the
        # migration module docstring and app/models/quotes.py.
        sa.UniqueConstraint(
            "organization_id",
            "quote_line_id",
            "min_quantity",
            name="uq_quote_price_breaks_org_line_min_quantity",
        ),
    )
    op.create_index(
        "ix_quote_price_breaks_organization_id",
        "quote_price_breaks",
        ["organization_id"],
    )
    op.create_index(
        # 02-erd.md §9 ix_qpb_line: tier selection.
        "ix_qpb_line",
        "quote_price_breaks",
        ["quote_line_id", "min_quantity"],
    )

    op.create_table(
        "quote_terms",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quote_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_terms", sa.Text(), nullable=True),
        sa.Column("payment_terms_days", sa.Integer(), nullable=True),
        sa.Column("incoterm", sa.Text(), nullable=True),
        sa.Column("shipping_terms", sa.Text(), nullable=True),
        sa.Column("warranty_terms", sa.Text(), nullable=True),
        sa.Column("validity_days", sa.Integer(), nullable=True),
        sa.Column("exceptions", sa.Text(), nullable=True),
        sa.Column("exclusions", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("organization_id", "id", name="uq_quote_terms_org_identity"),
        # RESTRICT, not CASCADE - see migration module docstring: the ERD's
        # cascade whitelist does not include quotes -> quote_terms.
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_id"],
            ["quotes.organization_id", "quotes.id"],
            ondelete="RESTRICT",
            name="fk_quote_terms_organization_id_quote_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "quote_id", name="uq_quote_terms_org_quote"
        ),
        sa.CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days >= 0",
            name="ck_quote_terms_payment_terms_days_nonneg",
        ),
        sa.CheckConstraint(
            "validity_days IS NULL OR validity_days >= 0",
            name="ck_quote_terms_validity_days_nonneg",
        ),
    )
    op.create_index("ix_quote_terms_organization_id", "quote_terms", ["organization_id"])


def downgrade() -> None:
    op.drop_table("quote_terms")
    op.drop_table("quote_price_breaks")
    op.drop_table("quote_lines")
    op.drop_table("quotes")
    op.execute("DROP TYPE match_status_enum")
    op.execute("DROP TYPE quote_source_enum")
    op.execute("DROP TYPE quote_status_enum")
