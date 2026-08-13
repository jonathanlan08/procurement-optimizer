"""bills_of_materials, bill_of_material_lines

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

bom_status_enum = pg.ENUM(
    "draft", "active", "superseded", "archived", name="bom_status_enum", create_type=False
)


def upgrade() -> None:
    op.execute(
        "CREATE TYPE bom_status_enum AS ENUM ('draft','active','superseded','archived')"
    )

    op.create_table(
        "bills_of_materials",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # stable across every version of one BOM; self-referential (the first
        # version's row points at its own id - see app/models/boms.py).
        sa.Column("root_bom_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False, server_default="1"),
        # NULL only for the first version of a BOM.
        sa.Column("previous_version_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("product_name", sa.Text(), nullable=False),
        sa.Column("status", bom_status_enum, nullable=False, server_default="draft"),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_bills_of_materials_org_identity"
        ),
        # composite org FKs (00-decisions.md §1.2 / 02-erd.md §1): self-
        # referential, both back onto this same table.
        sa.ForeignKeyConstraint(
            ["organization_id", "root_bom_id"],
            ["bills_of_materials.organization_id", "bills_of_materials.id"],
            ondelete="RESTRICT",
            name="fk_bills_of_materials_organization_id_root_bom_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "previous_version_id"],
            ["bills_of_materials.organization_id", "bills_of_materials.id"],
            ondelete="RESTRICT",
            name="fk_bills_of_materials_organization_id_previous_version_id",
        ),
        # 02-erd.md §10: copy-on-write concurrency guard - a second writer
        # racing to create the same next version fails this constraint.
        sa.UniqueConstraint(
            "organization_id",
            "root_bom_id",
            "version_number",
            name="uq_bills_of_materials_org_root_version",
        ),
        sa.CheckConstraint(
            "version_number > 0", name="ck_bills_of_materials_version_number_pos"
        ),
        sa.CheckConstraint(
            "(version_number = 1) = (previous_version_id IS NULL)",
            name="ck_bills_of_materials_first_version_no_previous",
        ),
    )
    op.create_index(
        "ix_bills_of_materials_organization_id", "bills_of_materials", ["organization_id"]
    )
    op.create_index(
        "ix_bills_of_materials_root_bom_id", "bills_of_materials", ["root_bom_id"]
    )
    op.create_index(
        "ix_bills_of_materials_previous_version_id",
        "bills_of_materials",
        ["previous_version_id"],
        postgresql_where=sa.text("previous_version_id IS NOT NULL"),
    )

    op.create_table(
        "bill_of_material_lines",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("bom_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("part_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity_per_assembly", sa.Numeric(18, 6), nullable=False),
        sa.Column(
            # plain FK - unit_definitions.organization_id is nullable (global
            # catalogue), so the composite org-guard FK cannot apply here
            # (see Part.unit_definition_id, migration 0004).
            "unit_definition_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("unit_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("is_optional", sa.Boolean(), nullable=False, server_default=sa.false()),
        # nullable: an approved substitute for this line's part (SPEC §4
        # "optional substitutes") - see app/models/boms.py module docstring.
        sa.Column("substitute_part_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_bill_of_material_lines_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "bom_id"],
            ["bills_of_materials.organization_id", "bills_of_materials.id"],
            ondelete="RESTRICT",
            name="fk_bill_of_material_lines_organization_id_bom_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_bill_of_material_lines_organization_id_part_id",
        ),
        # nullable composite org FK; NULL is exempt from the constraint
        # (MATCH SIMPLE default), same as part_alternatives.alternative_part_id
        # (migration 0004).
        sa.ForeignKeyConstraint(
            ["organization_id", "substitute_part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_bill_of_material_lines_organization_id_substitute_part_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "bom_id",
            "line_number",
            name="uq_bill_of_material_lines_org_bom_line_number",
        ),
        sa.CheckConstraint(
            "quantity_per_assembly > 0",
            name="ck_bill_of_material_lines_quantity_per_assembly_pos",
        ),
        sa.CheckConstraint(
            "substitute_part_id IS NULL OR substitute_part_id <> part_id",
            name="ck_bill_of_material_lines_substitute_not_self",
        ),
    )
    op.create_index(
        "ix_bill_of_material_lines_organization_id",
        "bill_of_material_lines",
        ["organization_id"],
    )
    op.create_index(
        "ix_bill_of_material_lines_bom_id", "bill_of_material_lines", ["bom_id"]
    )
    op.create_index(
        "ix_bill_of_material_lines_part_id", "bill_of_material_lines", ["part_id"]
    )
    op.create_index(
        "ix_bill_of_material_lines_substitute_part_id",
        "bill_of_material_lines",
        ["substitute_part_id"],
        postgresql_where=sa.text("substitute_part_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("bill_of_material_lines")
    op.drop_table("bills_of_materials")
    op.execute("DROP TYPE bom_status_enum")
