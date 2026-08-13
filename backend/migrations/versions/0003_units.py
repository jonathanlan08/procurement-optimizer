"""unit_definitions (global catalogue + org-defined units), unit_conversions

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

dimension_enum = pg.ENUM(
    "count", "mass", "length", name="dimension_enum", create_type=False
)


def upgrade() -> None:
    op.execute("CREATE TYPE dimension_enum AS ENUM ('count','mass','length')")

    op.create_table(
        "unit_definitions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            # nullable: NULL = shared global standard catalogue (02-erd.md §4
            # "null = global catalog"); non-null = an org's own unit.
            nullable=True,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("dimension", dimension_enum, nullable=False),
        sa.Column("to_canonical_factor", sa.Numeric(24, 12), nullable=True),
        sa.Column("is_user_defined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("organization_id", "id", name="uq_unit_definitions_org_identity"),
        # nullable: count-dimension containers (pack/box/tray/reel) have no
        # universal ratio to `each` - see UnitDefinition docstring.
        sa.CheckConstraint(
            "to_canonical_factor IS NULL OR to_canonical_factor > 0",
            name="ck_unit_definitions_to_canonical_factor_pos",
        ),
    )
    op.create_index(
        "ix_unit_definitions_organization_id", "unit_definitions", ["organization_id"]
    )
    op.create_index(
        # Global catalogue codes must be unique among themselves. Postgres
        # UNIQUE treats NULLs as distinct from one another, so a plain
        # (organization_id, lower(code)) constraint would silently allow
        # duplicate global codes (organization_id IS NULL on every one of
        # them) - this partial index closes that gap explicitly.
        "uq_unit_definitions_global_code_lower",
        "unit_definitions",
        [sa.text("lower(code)")],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )
    op.create_index(
        # An org's own units are unique per org, same functional-lower()
        # pattern as uq_suppliers_org_code_active (migration 0002).
        "uq_unit_definitions_org_code_lower",
        "unit_definitions",
        ["organization_id", sa.text("lower(code)")],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )

    op.create_table(
        "unit_conversions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "from_unit_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("unit_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "to_unit_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("unit_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # nullable: part-specific pack size (02-erd.md §4). No FK yet - `parts`
        # is created by a later migration (09-task-decomposition.md task 2.6).
        sa.Column("part_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("factor", sa.Numeric(24, 12), nullable=False),
        sa.Column("assumption_note", sa.Text(), nullable=False),
        sa.Column(
            "created_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_unit_conversions_org_identity"),
        sa.CheckConstraint("factor > 0", name="ck_unit_conversions_factor_pos"),
        sa.CheckConstraint(
            "from_unit_id <> to_unit_id", name="ck_unit_conversions_distinct_units"
        ),
        # SPEC §10 "show conversion assumptions explicitly" - enforced, not
        # merely conventional.
        sa.CheckConstraint(
            "btrim(assumption_note) <> ''",
            name="ck_unit_conversions_assumption_note_not_blank",
        ),
    )
    op.create_index(
        "ix_unit_conversions_organization_id", "unit_conversions", ["organization_id"]
    )
    op.create_index("ix_unit_conversions_from_unit_id", "unit_conversions", ["from_unit_id"])
    op.create_index("ix_unit_conversions_to_unit_id", "unit_conversions", ["to_unit_id"])
    op.create_index(
        "ix_unit_conversions_part_id",
        "unit_conversions",
        ["part_id"],
        postgresql_where=sa.text("part_id IS NOT NULL"),
    )
    op.create_index(
        # a part-specific override is unique per (org, unit pair, part)
        "uq_unit_conversions_org_units_part",
        "unit_conversions",
        ["organization_id", "from_unit_id", "to_unit_id", "part_id"],
        unique=True,
        postgresql_where=sa.text("part_id IS NOT NULL"),
    )
    op.create_index(
        # an org-default (no part_id) conversion is unique per (org, unit pair)
        "uq_unit_conversions_org_units_default",
        "unit_conversions",
        ["organization_id", "from_unit_id", "to_unit_id"],
        unique=True,
        postgresql_where=sa.text("part_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("unit_conversions")
    op.drop_table("unit_definitions")
    op.execute("DROP TYPE dimension_enum")
