"""parts, part_alternatives

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "parts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("internal_part_number", sa.Text(), nullable=False),
        sa.Column("manufacturer_part_number", sa.Text(), nullable=True),
        sa.Column("manufacturer", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column(
            # plain FK - unit_definitions.organization_id is nullable (global
            # catalogue), so the composite org-guard FK cannot apply here
            # (see UnitConversion.from_unit_id/to_unit_id, migration 0003,
            # and Part's docstring in app/models/parts.py).
            "unit_definition_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("unit_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("required_specifications", pg.JSONB(), nullable=True),
        sa.Column("target_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("target_price_currency", sa.CHAR(3), nullable=True),
        sa.Column(
            # No pg_trgm / citext extension (00-decisions.md §7): normalized
            # from internal_part_number with lower() + regexp_replace()
            # instead of the ERD's implied citext/trigram approach. See
            # Part's docstring in app/models/parts.py for the source-column
            # and indexing decisions this fills in.
            "normalized_key",
            sa.Text(),
            sa.Computed(
                "regexp_replace(lower(internal_part_number), '[^a-z0-9]', '', 'g')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.UniqueConstraint("organization_id", "id", name="uq_parts_org_identity"),
        # §8 ck_*_amount_nonneg / ck_currency_iso patterns, applied to the
        # part's own target price (independent of any RFQ/quote currency).
        sa.CheckConstraint(
            "target_price IS NULL OR target_price >= 0",
            name="ck_parts_target_price_nonneg",
        ),
        sa.CheckConstraint(
            "target_price_currency IS NULL OR target_price_currency ~ '^[A-Z]{3}$'",
            name="ck_parts_target_price_currency_iso",
        ),
        # §1 "every amount column is accompanied by a currency column" -
        # enforced as pairing rather than mere convention: a price without a
        # currency (or vice versa) is a data error, not a valid "unset" state.
        sa.CheckConstraint(
            "(target_price IS NULL) = (target_price_currency IS NULL)",
            name="ck_parts_target_price_currency_paired",
        ),
    )
    op.create_index("ix_parts_organization_id", "parts", ["organization_id"])
    op.create_index(
        # 02-erd.md §8 uq_parts_org_ipn_active: unique per org while active,
        # case-insensitive (citext in the ERD; functional index here, same
        # no-extension approach as uq_suppliers_org_code_active, migration
        # 0002). Archiving frees the number for reuse.
        "uq_parts_org_ipn_active",
        "parts",
        ["organization_id", sa.text("lower(internal_part_number)")],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_index(
        # 02-erd.md §9 ix_parts_mpn: exact-match lookup support for part
        # matching strategy 2 (docs/planning/04-document-pipeline.md §10).
        "ix_parts_mpn",
        "parts",
        ["organization_id", "manufacturer_part_number"],
    )
    op.create_index(
        # 02-erd.md §9 specifies `USING gin (normalized_key gin_trgm_ops)`
        # for fuzzy candidate retrieval via pg_trgm. This project uses no
        # Postgres extensions (00-decisions.md §7), so this is a plain
        # btree index instead - it supports the *equality* normalized-text
        # strategy fully (04-document-pipeline.md §10 strategy 4); fuzzy
        # candidate retrieval (strategy 5) stays application-side
        # (rapidfuzz), not a DB similarity index. Named without the "_trgm"
        # suffix used in the ERD since it is no longer a trigram index.
        "ix_parts_normalized_key",
        "parts",
        ["normalized_key"],
    )

    op.create_table(
        "part_alternatives",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("part_id", pg.UUID(as_uuid=True), nullable=False),
        # nullable: "nullable if external" (02-erd.md §4) - see
        # app/models/parts.py PartAlternative docstring.
        sa.Column("alternative_part_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("alternative_mpn", sa.Text(), nullable=True),
        sa.Column("approval_status", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "approved_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_part_alternatives_org_identity"
        ),
        # composite org FKs (00-decisions.md §1.2 / 02-erd.md §1): cross-org
        # (organization_id, part_id) and (organization_id, alternative_part_id)
        # pairs are refused by Postgres itself. The second FK is only checked
        # when alternative_part_id is non-null (MATCH SIMPLE default).
        sa.ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_part_alternatives_organization_id_part_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "alternative_part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_part_alternatives_organization_id_alternative_part_id",
        ),
        sa.CheckConstraint(
            "alternative_part_id IS NULL OR part_id <> alternative_part_id",
            name="ck_part_alternatives_no_self_reference",
        ),
        # unique pair per org; alternative_part_id IS NULL rows (external
        # alternatives, distinguished by alternative_mpn) are exempt by
        # ordinary SQL NULL-distinctness, same as any other nullable-column
        # unique constraint.
        sa.UniqueConstraint(
            "organization_id",
            "part_id",
            "alternative_part_id",
            name="uq_part_alternatives_org_part_alternative",
        ),
    )
    op.create_index(
        "ix_part_alternatives_organization_id", "part_alternatives", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_table("part_alternatives")
    op.drop_table("parts")
