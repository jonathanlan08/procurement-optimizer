"""negotiation_briefs (docs/planning/02-erd.md §7).

See app/models/briefs.py module docstring for the full ERD/task
reconciliation: `sections` (ERD-literal name, not the delegating task's
`content` paraphrase) is the structured brief — one JSONB object keyed by
SPEC section, each value carrying its own `provenance` label
(`supplier_provided|user_assumption|calculated|ai_narrative|missing`, SPEC
§Negotiation brief); `simulated` (ERD-literal name, not the task's
`narrative_is_generated` paraphrase) is the negated, public-facing form of
`AiNarrativeProvider.is_generated` (`app/providers/narrative/base.py`);
`state` is the ERD's literal 3-member `brief_state_enum`
(`draft|human_reviewed|approved`), though only `draft`/`human_reviewed` are
reachable through this build's routes (`approved` is a documented,
unreachable-in-v0.1 ERD member, the same "FROZEN enum member set kept even
when not fully reachable" precedent `scenarios.py`'s `AllocationStatus`
wrapping already establishes).

`created_by_id`/timestamps/`version`/`archived_at`+`archived_by_id`+
`archive_reason` are not in the ERD §7 box's own abbreviated per-table
listing, but are added per 02-erd.md §1's global convention
("created_at/updated_at on every MUTABLE table", "version ... on mutable
AGGREGATES") and §11's explicit "never hard-delete ... briefs" — the same
gap-filling `comparison_scenarios` (migration 0013) already documents for
itself. A brief mutates (`draft -> human_reviewed`) via `POST .../review`,
so it is mutable in the same sense `ComparisonScenario`/`Rfq`/`Quote` are.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE brief_state_enum AS ENUM ('draft','human_reviewed','approved')"
    )

    op.create_table(
        "negotiation_briefs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scenario_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", pg.UUID(as_uuid=True), nullable=False),
        # the structured brief: one object per SPEC content item, each
        # carrying its own {provenance, text, data} — see module docstring.
        sa.Column("sections", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("price_target", sa.Numeric(18, 8), nullable=True),
        sa.Column("stretch_target", sa.Numeric(18, 8), nullable=True),
        sa.Column("walk_away_threshold", sa.Numeric(18, 8), nullable=True),
        sa.Column("draft_email_subject", sa.Text(), nullable=False),
        sa.Column("draft_email_body", sa.Text(), nullable=False),
        sa.Column("narrative_provider", sa.Text(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "state",
            pg.ENUM(
                "draft",
                "human_reviewed",
                "approved",
                name="brief_state_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "reviewed_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "archived_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_negotiation_briefs_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "scenario_id"],
            ["comparison_scenarios.organization_id", "comparison_scenarios.id"],
            ondelete="RESTRICT",
            name="fk_negotiation_briefs_organization_id_scenario_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_negotiation_briefs_organization_id_supplier_id",
        ),
        sa.CheckConstraint(
            "price_target IS NULL OR price_target >= 0",
            name="ck_negotiation_briefs_price_target_nonneg",
        ),
        sa.CheckConstraint(
            "stretch_target IS NULL OR stretch_target >= 0",
            name="ck_negotiation_briefs_stretch_target_nonneg",
        ),
        sa.CheckConstraint(
            "walk_away_threshold IS NULL OR walk_away_threshold >= 0",
            name="ck_negotiation_briefs_walk_away_threshold_nonneg",
        ),
    )
    op.create_index(
        "ix_negotiation_briefs_organization_id", "negotiation_briefs", ["organization_id"]
    )
    op.create_index(
        # history for one scenario, newest first — mirrors ix_scen_rfq
        # (02-erd.md §9) and supports "list briefs for a scenario/RFQ" (the
        # latter joins through comparison_scenarios.rfq_id).
        "ix_negotiation_briefs_scenario",
        "negotiation_briefs",
        ["organization_id", "scenario_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("negotiation_briefs")
    op.execute("DROP TYPE brief_state_enum")
