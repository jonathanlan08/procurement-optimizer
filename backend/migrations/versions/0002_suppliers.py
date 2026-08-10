"""suppliers, supplier_contacts, supplier_performance_records

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("country_code", sa.CHAR(2), nullable=False),
        sa.Column(
            "supported_currencies",
            pg.ARRAY(sa.CHAR(3)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("standard_payment_terms", sa.Text(), nullable=True),
        sa.Column("standard_incoterm", sa.Text(), nullable=True),
        sa.Column("typical_lead_time_days", sa.Integer(), nullable=True),
        sa.Column("capacity_units_per_month", sa.Numeric(18, 6), nullable=True),
        sa.Column("default_moq", sa.Numeric(18, 6), nullable=True),
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
        sa.UniqueConstraint("organization_id", "id", name="uq_suppliers_org_identity"),
        # array-of-CHAR(3) form of ck_currency_iso (02-erd.md §8); Postgres CHECK
        # constraints forbid subqueries, so this validates every element with a
        # single regex over the array joined with no separator rather than
        # EXISTS(SELECT ... FROM unnest(...)).
        sa.CheckConstraint(
            "array_to_string(supported_currencies, '') ~ '^([A-Z]{3})*$'",
            name="ck_suppliers_supported_currencies_iso",
        ),
        sa.CheckConstraint(
            "typical_lead_time_days IS NULL OR typical_lead_time_days >= 0",
            name="ck_suppliers_typical_lead_time_days_nonneg",
        ),
        sa.CheckConstraint(
            "capacity_units_per_month IS NULL OR capacity_units_per_month >= 0",
            name="ck_suppliers_capacity_units_per_month_nonneg",
        ),
        sa.CheckConstraint(
            "default_moq IS NULL OR default_moq >= 0",
            name="ck_suppliers_default_moq_nonneg",
        ),
    )
    op.create_index("ix_suppliers_organization_id", "suppliers", ["organization_id"])
    op.create_index(
        # 02-erd.md §9: list views filter to non-archived suppliers
        "ix_suppliers_org_active",
        "suppliers",
        ["organization_id"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_index(
        # 02-erd.md §8 uq_suppliers_org_code_active: unique per org while active,
        # case-insensitive (citext in the ERD; functional index here, same
        # no-extension approach migration 0001 uses for slug/email).
        "uq_suppliers_org_code_active",
        "suppliers",
        ["organization_id", sa.text("lower(code)")],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "supplier_contacts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("supplier_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("role_title", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
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
            "organization_id", "id", name="uq_supplier_contacts_org_identity"
        ),
        # composite org FK (00-decisions.md §1.2 / 02-erd.md §1): cross-org
        # (organization_id, supplier_id) pairs are refused by Postgres itself.
        sa.ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_supplier_contacts_organization_id_supplier_id",
        ),
    )
    op.create_index(
        "ix_supplier_contacts_organization_id", "supplier_contacts", ["organization_id"]
    )

    op.create_table(
        "supplier_performance_records",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("supplier_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("on_time_delivery_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("defect_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("quality_score", sa.Numeric(9, 6), nullable=False),
        sa.Column("orders_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_supplier_performance_records_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_supplier_performance_records_organization_id_supplier_id",
        ),
        # rates are decimal strings 0-1 (03-api-contract.md §4.4)
        sa.CheckConstraint(
            "on_time_delivery_rate >= 0 AND on_time_delivery_rate <= 1",
            name="ck_supplier_performance_records_on_time_delivery_rate_range",
        ),
        sa.CheckConstraint(
            "defect_rate >= 0 AND defect_rate <= 1",
            name="ck_supplier_performance_records_defect_rate_range",
        ),
        sa.CheckConstraint(
            "quality_score >= 0",
            name="ck_supplier_performance_records_quality_score_nonneg",
        ),
        sa.CheckConstraint(
            "orders_count >= 0",
            name="ck_supplier_performance_records_orders_count_nonneg",
        ),
    )
    op.create_index(
        "ix_supplier_performance_records_organization_id",
        "supplier_performance_records",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_table("supplier_performance_records")
    op.drop_table("supplier_contacts")
    op.drop_table("suppliers")
