"""rfqs, rfq_lines, rfq_suppliers, rfq_status_history

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE rfq_status_enum AS ENUM"
        " ('draft','open','under_review','awarded','closed','archived')"
    )

    op.create_table(
        "rfqs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("internal_reference", sa.Text(), nullable=False),
        sa.Column(
            "status",
            pg.ENUM(
                "draft",
                "open",
                "under_review",
                "awarded",
                "closed",
                "archived",
                name="rfq_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("base_currency", sa.CHAR(3), nullable=False),
        sa.Column("requested_payment_terms", sa.Text(), nullable=True),
        sa.Column("requested_incoterm", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("requested_delivery_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        # nullable: an RFQ may be exploded from a specific BOM version, or
        # built manually with no BOM origin (see app/models/rfqs.py).
        sa.Column("source_bom_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
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
        sa.UniqueConstraint("organization_id", "id", name="uq_rfqs_org_identity"),
        # composite org FK (00-decisions.md §1.2 / 02-erd.md §1): a cross-org
        # (organization_id, source_bom_id) pair is refused by Postgres
        # itself. Only checked when source_bom_id is non-null (MATCH SIMPLE).
        sa.ForeignKeyConstraint(
            ["organization_id", "source_bom_id"],
            ["bills_of_materials.organization_id", "bills_of_materials.id"],
            ondelete="RESTRICT",
            name="fk_rfqs_organization_id_source_bom_id",
        ),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$'", name="ck_rfqs_base_currency_iso"
        ),
    )
    op.create_index("ix_rfqs_organization_id", "rfqs", ["organization_id"])
    op.create_index(
        "ix_rfqs_source_bom_id",
        "rfqs",
        ["source_bom_id"],
        postgresql_where=sa.text("source_bom_id IS NOT NULL"),
    )
    op.create_index(
        # 02-erd.md §8 uq_rfqs_org_ref_active: unique per org while active,
        # case-insensitive (citext in the ERD; functional index here, same
        # no-extension approach as uq_parts_org_ipn_active, migration 0004).
        "uq_rfqs_org_ref_active",
        "rfqs",
        ["organization_id", sa.text("lower(internal_reference)")],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "rfq_lines",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rfq_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("part_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("required_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column(
            # plain FK - unit_definitions.organization_id is nullable (global
            # catalogue), so the composite org-guard FK cannot apply here
            # (see Part.unit_definition_id, migration 0004).
            "unit_definition_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("unit_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("required_specifications", pg.JSONB(), nullable=True),
        # app/models/rfqs.py module docstring point #2: lives here, not on
        # rfqs, per the delegating task's explicit field placement.
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("organization_id", "id", name="uq_rfq_lines_org_identity"),
        sa.ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_rfq_lines_organization_id_rfq_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_rfq_lines_organization_id_part_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "rfq_id",
            "line_number",
            name="uq_rfq_lines_org_rfq_line_number",
        ),
        sa.CheckConstraint(
            "required_quantity > 0", name="ck_rfq_lines_required_qty_pos"
        ),
    )
    op.create_index("ix_rfq_lines_organization_id", "rfq_lines", ["organization_id"])
    op.create_index(
        # 02-erd.md §9 ix_rfq_lines_rfq: RFQ detail view - every line of one
        # RFQ, in line-number order.
        "ix_rfq_lines_rfq",
        "rfq_lines",
        ["organization_id", "rfq_id", "line_number"],
    )
    op.create_index("ix_rfq_lines_part_id", "rfq_lines", ["part_id"])

    op.create_table(
        "rfq_suppliers",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rfq_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("invited_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("excluded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("exclusion_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_rfq_suppliers_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_rfq_suppliers_organization_id_rfq_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_rfq_suppliers_organization_id_supplier_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "rfq_id",
            "supplier_id",
            name="uq_rfq_suppliers_org_rfq_supplier",
        ),
        sa.CheckConstraint(
            "(excluded_at IS NULL) = (exclusion_reason IS NULL)",
            name="ck_rfq_suppliers_exclusion_reason_paired",
        ),
    )
    op.create_index(
        "ix_rfq_suppliers_organization_id", "rfq_suppliers", ["organization_id"]
    )
    op.create_index("ix_rfq_suppliers_rfq_id", "rfq_suppliers", ["rfq_id"])
    op.create_index("ix_rfq_suppliers_supplier_id", "rfq_suppliers", ["supplier_id"])

    op.create_table(
        "rfq_status_history",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rfq_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "from_status",
            pg.ENUM(
                "draft",
                "open",
                "under_review",
                "awarded",
                "closed",
                "archived",
                name="rfq_status_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "to_status",
            pg.ENUM(
                "draft",
                "open",
                "under_review",
                "awarded",
                "closed",
                "archived",
                name="rfq_status_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_rfq_status_history_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_rfq_status_history_organization_id_rfq_id",
        ),
        sa.CheckConstraint(
            "from_status <> to_status",
            name="ck_rfq_status_history_no_self_transition",
        ),
    )
    op.create_index(
        "ix_rfq_status_history_organization_id",
        "rfq_status_history",
        ["organization_id"],
    )
    op.create_index(
        "ix_rfq_status_history_rfq_id", "rfq_status_history", ["rfq_id"]
    )


def downgrade() -> None:
    op.drop_table("rfq_status_history")
    op.drop_table("rfq_suppliers")
    op.drop_table("rfq_lines")
    op.drop_table("rfqs")
    op.execute("DROP TYPE rfq_status_enum")
