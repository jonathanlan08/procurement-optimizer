"""exchange_rates (docs/planning/02-erd.md EXCHANGE_RATES, SPEC §9)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "exchange_rates",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("base_currency", sa.CHAR(3), nullable=False),
        sa.Column("quote_currency", sa.CHAR(3), nullable=False),
        sa.Column("rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        # "manual" in v0.1 (only manual overrides are ever persisted; see
        # app/models/fx.py module docstring); "fixture"/a provider name are
        # reserved for a future live-provider refresh job.
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "is_manual_override", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_exchange_rates_org_identity"),
        # 02-erd.md §8
        sa.CheckConstraint("rate > 0", name="ck_exchange_rates_rate_pos"),
        sa.CheckConstraint(
            "base_currency <> quote_currency", name="ck_exchange_rates_distinct"
        ),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$'", name="ck_exchange_rates_base_currency_iso"
        ),
        sa.CheckConstraint(
            "quote_currency ~ '^[A-Z]{3}$'", name="ck_exchange_rates_quote_currency_iso"
        ),
        # SPEC §9: every rate preserves "manual override, override reason" —
        # a flagged override with no reason is a data error, not a valid
        # "unset" state (same pairing-check shape as
        # ck_parts_target_price_currency_paired, migration 0004).
        sa.CheckConstraint(
            "NOT is_manual_override OR btrim(override_reason) <> ''",
            name="ck_exchange_rates_override_reason_required",
        ),
    )
    op.create_index("ix_exchange_rates_organization_id", "exchange_rates", ["organization_id"])
    op.create_index(
        # as-of lookup (02-erd.md §9 ix_fx_lookup): greatest effective_date
        # <= as_of for an exact (org, base, quote) pair.
        "ix_fx_lookup",
        "exchange_rates",
        ["organization_id", "base_currency", "quote_currency", sa.text("effective_date DESC")],
    )
    op.create_index(
        # 02-erd.md §8 uq_exchange_rate_natural. In v0.1 `source` is always
        # "manual" (see column comment above), so this is effectively "one
        # override per (org, pair, date)" today, and stays correct if a
        # future provider-refresh job starts persisting non-manual rows too.
        "uq_exchange_rate_natural",
        "exchange_rates",
        ["organization_id", "base_currency", "quote_currency", "effective_date", "source"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("exchange_rates")
