"""part_import_batches, part_import_rows

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE part_import_format_enum AS ENUM ('csv','xlsx')")
    op.execute(
        "CREATE TYPE import_state_enum AS ENUM"
        " ('previewing','committed','rolled_back','failed')"
    )

    op.create_table(
        "part_import_batches",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_filename", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.LargeBinary(), nullable=False),
        # not in 02-erd.md's PART_IMPORT_BATCHES box — see app/models/part_imports.py
        sa.Column(
            "format",
            pg.ENUM(
                "csv", "xlsx", name="part_import_format_enum", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            pg.ENUM(
                "previewing",
                "committed",
                "rolled_back",
                "failed",
                name="import_state_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="previewing",
        ),
        sa.Column("rows_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_valid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_invalid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_duplicate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("committed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_part_import_batches_org_identity"
        ),
        sa.CheckConstraint("rows_total >= 0", name="ck_part_import_batches_rows_total_nonneg"),
        sa.CheckConstraint("rows_valid >= 0", name="ck_part_import_batches_rows_valid_nonneg"),
        sa.CheckConstraint(
            "rows_invalid >= 0", name="ck_part_import_batches_rows_invalid_nonneg"
        ),
        sa.CheckConstraint(
            "rows_duplicate >= 0", name="ck_part_import_batches_rows_duplicate_nonneg"
        ),
    )
    op.create_index(
        "ix_part_import_batches_organization_id", "part_import_batches", ["organization_id"]
    )

    op.create_table(
        "part_import_rows",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("batch_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_values", pg.JSONB(), nullable=False),
        sa.Column("normalized_values", pg.JSONB(), nullable=True),
        # plain text, not a DB enum — see app/models/part_imports.py module
        # docstring (disposition implements a subset of 02-erd.md's own
        # documented value set: create|skip_duplicate|error, no `update`).
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("errors", pg.JSONB(), nullable=False, server_default="[]"),
        # nullable composite org FK to parts; see app/models/part_imports.py
        sa.Column("resulting_part_id", pg.UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_part_import_rows_org_identity"
        ),
        # 02-erd.md §11 explicit cascade whitelist entry: "part_import_batches
        # → part_import_rows" — the one deliberate CASCADE in this schema
        # rather than the RESTRICT-by-default everywhere else.
        sa.ForeignKeyConstraint(
            ["organization_id", "batch_id"],
            ["part_import_batches.organization_id", "part_import_batches.id"],
            ondelete="CASCADE",
            name="fk_part_import_rows_organization_id_batch_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "resulting_part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_part_import_rows_organization_id_resulting_part_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "batch_id",
            "row_number",
            name="uq_part_import_rows_org_batch_row_number",
        ),
        sa.CheckConstraint("row_number > 0", name="ck_part_import_rows_row_number_pos"),
    )
    op.create_index(
        "ix_part_import_rows_organization_id", "part_import_rows", ["organization_id"]
    )
    op.create_index("ix_part_import_rows_batch_id", "part_import_rows", ["batch_id"])
    op.create_index(
        "ix_part_import_rows_resulting_part_id",
        "part_import_rows",
        ["resulting_part_id"],
        postgresql_where=sa.text("resulting_part_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("part_import_rows")
    op.drop_table("part_import_batches")
    op.execute("DROP TYPE import_state_enum")
    op.execute("DROP TYPE part_import_format_enum")
