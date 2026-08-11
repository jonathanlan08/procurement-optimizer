"""comparison_scenarios, scenario_results, allocation_results
(docs/planning/02-erd.md §7).

See app/models/scenarios.py module docstring for the full ERD/task
reconciliation: `comparison_scenarios` follows the ERD's literal five
granular snapshot columns (constraints_snapshot/assumptions_snapshot/
fx_snapshot/quote_snapshot_refs/weights_snapshot) rather than the
delegating task's consolidated `inputs_snapshot` paraphrase, plus an
ERD-literal `scoring_configuration_id` composite FK the task never
mentioned; `scenario_results` is one row per scenario holding the whole
serialized `app.domain.scoring.contracts.ScoringResult` as JSONB (not one
row per supplier as the ERD's literal box implies), because the task's own
four-column paraphrase maps directly onto that FROZEN domain contract;
`allocation_results` mirrors `app.domain.optimization.contracts.
AllocationResult` field-for-field (also FROZEN, explicitly "the shape you
persist" per this task's own instructions), including `status` wrapping
`AllocationStatus` directly (4 members) rather than the ERD's 5-member
`solver_status_enum`.

Both `scenario_results` and `allocation_results` are immutable: no
`updated_at`/`version`/`archived_at` columns at all, and `UNIQUE
(organization_id, scenario_id)` encodes "at most one result row per
scenario" (recomputing creates a new `comparison_scenarios` row instead,
02-erd.md §10).

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE comparison_strategy_enum AS ENUM"
        " ('lowest_unit_price','lowest_landed_cost','fastest_delivery',"
        "'lowest_risk','balanced','custom')"
    )
    op.execute(
        "CREATE TYPE scenario_state_enum AS ENUM"
        " ('draft','running','complete','failed')"
    )
    op.execute(
        "CREATE TYPE allocation_status_enum AS ENUM"
        " ('optimal','feasible','infeasible','error')"
    )

    # -- comparison_scenarios ------------------------------------------------
    op.create_table(
        "comparison_scenarios",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rfq_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "strategy",
            pg.ENUM(
                "lowest_unit_price",
                "lowest_landed_cost",
                "fastest_delivery",
                "lowest_risk",
                "balanced",
                "custom",
                name="comparison_strategy_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("scoring_configuration_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "constraints_snapshot",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "assumptions_snapshot",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "fx_snapshot", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "quote_snapshot_refs",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "weights_snapshot",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("calculation_version", sa.Text(), nullable=False),
        sa.Column("solver_version", sa.Text(), nullable=False),
        sa.Column(
            "state",
            pg.ENUM(
                "draft",
                "running",
                "complete",
                "failed",
                name="scenario_state_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "created_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
            "organization_id", "id", name="uq_comparison_scenarios_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_comparison_scenarios_organization_id_rfq_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "scoring_configuration_id"],
            ["scoring_configurations.organization_id", "scoring_configurations.id"],
            ondelete="RESTRICT",
            # shortened from the naming convention's full form (would exceed
            # Postgres's 63-char identifier limit).
            name="fk_comparison_scenarios_org_scoring_configuration_id",
        ),
    )
    op.create_index(
        "ix_comparison_scenarios_organization_id",
        "comparison_scenarios",
        ["organization_id"],
    )
    op.create_index(
        # 02-erd.md §9 ix_scen_rfq: scenario history for one RFQ, newest first.
        "ix_comparison_scenarios_rfq",
        "comparison_scenarios",
        ["organization_id", "rfq_id", sa.text("created_at DESC")],
    )

    # -- scenario_results -----------------------------------------------
    op.create_table(
        "scenario_results",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scenario_id", pg.UUID(as_uuid=True), nullable=False),
        # the serialized ScoringResult (scores incl. reasons, weights_used,
        # cohort_size, notes) minus scoring_version (own column below).
        sa.Column("scoring_output", pg.JSONB(), nullable=False),
        sa.Column("calculation_version", sa.Text(), nullable=False),
        sa.Column("scoring_version", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "id", name="uq_scenario_results_org_identity"),
        sa.ForeignKeyConstraint(
            ["organization_id", "scenario_id"],
            ["comparison_scenarios.organization_id", "comparison_scenarios.id"],
            ondelete="RESTRICT",
            name="fk_scenario_results_organization_id_scenario_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "scenario_id", name="uq_scenario_results_org_scenario"
        ),
    )
    op.create_index(
        "ix_scenario_results_organization_id", "scenario_results", ["organization_id"]
    )

    # -- allocation_results -----------------------------------------------
    op.create_table(
        "allocation_results",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scenario_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            pg.ENUM(
                "optimal",
                "feasible",
                "infeasible",
                "error",
                name="allocation_status_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "allocations", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("expected_total_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("supplier_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "binding_constraints",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("infeasibility", pg.JSONB(), nullable=True),
        sa.Column(
            "rejected_alternatives",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("stats", pg.JSONB(), nullable=True),
        sa.Column("model_hash", sa.Text(), nullable=True),
        sa.Column("optimization_version", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("solved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_allocation_results_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "scenario_id"],
            ["comparison_scenarios.organization_id", "comparison_scenarios.id"],
            ondelete="RESTRICT",
            name="fk_allocation_results_organization_id_scenario_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "scenario_id", name="uq_allocation_results_org_scenario"
        ),
        sa.CheckConstraint(
            "expected_total_cost IS NULL OR expected_total_cost >= 0",
            name="ck_allocation_results_expected_total_cost_nonneg",
        ),
        sa.CheckConstraint(
            "supplier_count >= 0", name="ck_allocation_results_supplier_count_nonneg"
        ),
        sa.CheckConstraint(
            "(stats IS NULL) = (model_hash IS NULL)",
            name="ck_allocation_results_model_hash_stats_paired",
        ),
    )
    op.create_index(
        "ix_allocation_results_organization_id", "allocation_results", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_table("allocation_results")
    op.drop_table("scenario_results")
    op.drop_table("comparison_scenarios")
    op.execute("DROP TYPE allocation_status_enum")
    op.execute("DROP TYPE scenario_state_enum")
    op.execute("DROP TYPE comparison_strategy_enum")
