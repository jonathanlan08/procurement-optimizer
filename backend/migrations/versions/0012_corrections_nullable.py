"""quote_corrections.quote_id nullable + extraction_run_id (part-matching
phase).

See app/models/documents.py module docstring point 8a for the full
reasoning: this closes a genuine conflict `services/extraction_service.py`'s
own module docstring previously flagged — 04-document-pipeline.md's pipeline
places Stage 10 (Corrections) *before* Stage 11 (Materialize), so a
correction can legitimately happen while only an `ExtractionRun`/
`ExtractionField` exist and no `Quote` row has been built yet, but
`quote_id` was originally `NOT NULL` on this table, so `correct_field` could
not write a `QuoteCorrection` row in that case at all (only the audit event
recorded the edit).

Two changes, both additive/loosening (no data loss on upgrade):

1. `quote_corrections.quote_id` — `DROP NOT NULL`. Nullable, same `MATCH
   SIMPLE` NULL-exempts-the-row pattern the existing `extraction_field_id`
   composite FK on this same table already uses (migration 0010).
2. `quote_corrections.extraction_run_id` — new nullable composite org FK
   column to `extraction_runs`, since the model did not already have it
   (checked `app/models/documents.py` first, per this task's own
   instruction). Gives *every* correction, pre- or post-materialization, a
   durable, directly queryable link back to the run that produced the field
   it corrects — previously the only place that link existed at all was the
   `extraction.materialized` audit event's own `after_state`
   (`ExtractionRepository.find_materialized_quote_id`).

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("quote_corrections", "quote_id", nullable=True)

    op.add_column(
        "quote_corrections",
        sa.Column("extraction_run_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_quote_corrections_organization_id_extraction_run_id",
        "quote_corrections",
        "extraction_runs",
        ["organization_id", "extraction_run_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_quote_corrections_organization_id_extraction_run_id",
        "quote_corrections",
        type_="foreignkey",
    )
    op.drop_column("quote_corrections", "extraction_run_id")

    op.alter_column("quote_corrections", "quote_id", nullable=False)
