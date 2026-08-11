"""landed_cost_results, landed_cost_components, scoring_configurations
(docs/planning/02-erd.md §7).

See app/models/analysis.py module docstring for the deliberate ERD/task
deviations: `landed_cost_results` follows the delegating task's own narrower
column list (no `scenario_id`/`source_currency`/`exchange_rate_id`/
`manual_overrides` — the ERD's fuller box — since no `comparison_scenarios`
table exists yet in this phase and the task's own `inputs_snapshot` JSONB is
explicitly specified to already carry "fx/unit normalization provenance",
making those columns redundant for now); `landed_cost_components.provenance`
is a plain `text` column per the task's literal spelling (the ERD calls the
same concept `data_source`); `scoring_configurations` uses the task's literal
`weights` JSONB column (not the ERD's `criteria` + `weight_sum`) and
`created_by_id` (matching the FK-naming convention used by every other
`created_by`-shaped column in this codebase — `rfqs.created_by_id`,
`bills_of_materials.created_by_id`, etc. — over the task prose's unsuffixed
"created_by").

Both `landed_cost_result_completeness_enum` and
`landed_cost_component_enum` wrap `app.domain.values.Completeness` /
`app.domain.landed_cost.contracts.CostComponent` directly (see
app/models/analysis.py), the same "domain enum is the single source of
truth, the DB type just mirrors it" pattern already established by
`app/models/documents.py`'s `CONFIDENCE_BAND_ENUM` wrapping
`app.domain.confidence.ConfidenceBand`.

`landed_cost_results` rows are immutable — recalculation always inserts a new
row; "latest wins" is a query-time concern (`ix_landed_cost_results_line_latest`
below exists for exactly that lookup), not a DB constraint.

`scoring_configurations` uniqueness ("unique active name per org") is a
functional, case-insensitive partial index, the same no-`citext`-extension
adaptation `rfqs.internal_reference` and `parts.internal_part_number` already
use (02-erd.md §7's own box has no explicit uniqueness note for this column,
but "unique active name per org" is this task's own explicit requirement).

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE landed_cost_result_completeness_enum AS ENUM"
        " ('complete','assumption_dependent','incomplete')"
    )
    op.execute(
        "CREATE TYPE landed_cost_component_enum AS ENUM"
        " ('extended_material','allocated_fixed','logistics','import',"
        "'quality_risk','delay_risk','financing')"
    )

    # -- landed_cost_results ------------------------------------------------
    op.create_table(
        "landed_cost_results",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quote_line_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("accepted_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("total_landed_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("effective_unit_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column(
            "completeness",
            pg.ENUM(
                "complete",
                "assumption_dependent",
                "incomplete",
                name="landed_cost_result_completeness_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("calculation_version", sa.Text(), nullable=False),
        # the full assembled LandedCostInput incl. FX/unit normalization
        # provenance (this task's own explicit column brief).
        sa.Column("inputs_snapshot", pg.JSONB(), nullable=False),
        sa.Column(
            "missing_inputs", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "assumptions", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("calculated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "calculated_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_landed_cost_results_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_line_id"],
            ["quote_lines.organization_id", "quote_lines.id"],
            ondelete="RESTRICT",
            name="fk_landed_cost_results_organization_id_quote_line_id",
        ),
        sa.CheckConstraint(
            "accepted_quantity > 0", name="ck_landed_cost_results_accepted_quantity_pos"
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_landed_cost_results_currency_iso"
        ),
    )
    op.create_index(
        "ix_landed_cost_results_organization_id",
        "landed_cost_results",
        ["organization_id"],
    )
    op.create_index(
        # "latest wins per line" lookup: newest row first for a given line.
        "ix_landed_cost_results_line_latest",
        "landed_cost_results",
        ["organization_id", "quote_line_id", sa.text("calculated_at DESC")],
    )

    # -- landed_cost_components ----------------------------------------
    op.create_table(
        "landed_cost_components",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("landed_cost_result_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "component",
            pg.ENUM(
                "extended_material",
                "allocated_fixed",
                "logistics",
                "import",
                "quality_risk",
                "delay_risk",
                "financing",
                name="landed_cost_component_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("inputs", pg.JSONB(), nullable=False),
        # task's literal column name; ERD calls the same concept "data_source"
        # (see module docstring).
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("is_assumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_missing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_landed_cost_components_org_identity"
        ),
        # CASCADE: 02-erd.md §11's explicit whitelist — a component cannot
        # exist without the result it breaks down.
        sa.ForeignKeyConstraint(
            ["organization_id", "landed_cost_result_id"],
            ["landed_cost_results.organization_id", "landed_cost_results.id"],
            ondelete="CASCADE",
            name="fk_landed_cost_components_organization_id_landed_cost_result_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "landed_cost_result_id",
            "component",
            name="uq_landed_cost_components_org_result_component",
        ),
    )
    op.create_index(
        "ix_landed_cost_components_organization_id",
        "landed_cost_components",
        ["organization_id"],
    )
    op.create_index(
        "ix_landed_cost_components_result",
        "landed_cost_components",
        ["organization_id", "landed_cost_result_id"],
    )

    # -- scoring_configurations ------------------------------------------
    op.create_table(
        "scoring_configurations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # list of CriterionSpec-shaped objects (task's literal column name;
        # ERD's box calls the same concept "criteria" + a separate
        # "weight_sum" column — see module docstring).
        sa.Column("weights", pg.JSONB(), nullable=False),
        sa.Column("is_sample", sa.Boolean(), nullable=False, server_default=sa.false()),
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
            "organization_id", "id", name="uq_scoring_configurations_org_identity"
        ),
    )
    op.create_index(
        "ix_scoring_configurations_organization_id",
        "scoring_configurations",
        ["organization_id"],
    )
    op.create_index(
        # "unique active name per org" (this task's explicit requirement);
        # functional + partial, same no-citext-extension adaptation as
        # uq_rfqs_org_ref_active (migration 0007) / uq_parts_org_ipn_active
        # (migration 0004).
        "uq_scoring_configurations_org_name_active",
        "scoring_configurations",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("scoring_configurations")
    op.drop_table("landed_cost_components")
    op.drop_table("landed_cost_results")
    op.execute("DROP TYPE landed_cost_component_enum")
    op.execute("DROP TYPE landed_cost_result_completeness_enum")
