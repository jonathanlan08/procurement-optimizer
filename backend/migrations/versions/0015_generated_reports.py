"""generated_reports (docs/planning/02-erd.md GENERATED_REPORTS box,
docs/planning/02-erd.md §11 purge rules, docs/planning/03-api-contract.md
§4.18).

Columns follow the ERD box literally, plus the §11 purge columns the box
itself doesn't spell out but the prose right below it requires: "Hard
delete exists only for: expired sessions, expired `generated_reports`
artifacts (row kept, blob purged, `storage_key` nulled with `purged_at`
set) ...". `storage_key` is therefore NULLABLE (not the ERD box's own
un-annotated look, which would suggest NOT NULL) so a purge can null it
without deleting the row, and `purged_at` (nullable, absent from the box
entirely) is added to record when that happened. `error_message` is
likewise a genuine addition beyond the box: `report_state_enum` includes
`failed` (ERD-literal), and a `failed` row needs somewhere to record why
(see `app/services/report_service.py` module docstring: a renderer
exception is persisted as a `failed` row with `error_message` set, still
`201`, rather than a `500` — this column is where that message lives).

**`content_sha256` is `bytea`, not text hex** — the ERD box's own literal
type. The delegating task's own prose additionally asked this to match
"how document sha256 is stored elsewhere", pointing at
`app/models/documents.py`; that file's `QuoteDocument.content_sha256` is
itself `LargeBinary()` (bytea) with the hex string only ever produced at
the wire boundary (`.hex()` in `app/schemas/documents.py`), matching what
the ERD box says for `GENERATED_REPORTS` too — so both instructions agree
once the actual file is read, and this migration follows both: bytea,
storing the raw digest bytes, never a hex string, in the database.

`size_bytes` is `NOT NULL DEFAULT 0` (0 for a `pending`/`failed` row that
never produced bytes), not nullable, since the ERD box's own inline
comment has no "nullable" annotation for it (contrast `storage_key`'s
explicit nullability need above). `content_sha256` IS nullable for the
same `pending`/`failed` reason despite carrying no such annotation either
— a hash of bytes that were never produced cannot be stored, and the ERD
box predates this build's synchronous-generation-with-a-failed-state
shape (`report_service.py` module docstring "Deviation 1").

`expires_at` is always populated at generation time by this build (a flat
`REPORT_RETENTION_DAYS = 90` constant in `report_service.py` — the ERD/SPEC
name the concept but never state a duration, so this is a documented
assumption, not a literal requirement) and is therefore `NOT NULL`.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_REPORT_TYPES = (
    "supplier_comparison",
    "cfo_recommendation",
    "negotiation_brief",
    "scenario_summary",
    "audit_history",
)
_REPORT_FORMATS = ("csv", "xlsx", "pdf")
_REPORT_STATES = ("pending", "ready", "failed")


def upgrade() -> None:
    op.execute(
        "CREATE TYPE report_type_enum AS ENUM ("
        + ",".join(f"'{v}'" for v in _REPORT_TYPES)
        + ")"
    )
    op.execute(
        "CREATE TYPE report_format_enum AS ENUM ("
        + ",".join(f"'{v}'" for v in _REPORT_FORMATS)
        + ")"
    )
    op.execute(
        "CREATE TYPE report_state_enum AS ENUM ("
        + ",".join(f"'{v}'" for v in _REPORT_STATES)
        + ")"
    )

    op.create_table(
        "generated_reports",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scenario_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "report_type",
            pg.ENUM(*_REPORT_TYPES, name="report_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "format",
            pg.ENUM(*_REPORT_FORMATS, name="report_format_enum", create_type=False),
            nullable=False,
        ),
        # nullable: nulled on purge (module docstring; 02-erd.md §11).
        sa.Column("storage_key", sa.Text(), nullable=True),
        # bytea, nullable: no bytes exist yet for pending/failed rows
        # (module docstring).
        sa.Column("content_sha256", sa.LargeBinary(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "parameters", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("calculation_version", sa.Text(), nullable=False),
        sa.Column(
            "state",
            pg.ENUM(*_REPORT_STATES, name="report_state_enum", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "generated_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        # ERD §11 purge marker: set alongside nulling storage_key.
        sa.Column("purged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # failed-state explanation (module docstring).
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("organization_id", "id", name="uq_generated_reports_org_identity"),
        sa.ForeignKeyConstraint(
            ["organization_id", "scenario_id"],
            ["comparison_scenarios.organization_id", "comparison_scenarios.id"],
            ondelete="RESTRICT",
            name="fk_generated_reports_organization_id_scenario_id",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_generated_reports_size_bytes_nonneg"),
        # storage_key and content_sha256 are set together (on ready) and
        # nulled together (on purge, or when generation never produced
        # bytes at all) — never one without the other.
        sa.CheckConstraint(
            "(storage_key IS NULL) = (content_sha256 IS NULL)",
            name="ck_generated_reports_storage_key_sha256_paired",
        ),
    )
    op.create_index(
        "ix_generated_reports_organization_id", "generated_reports", ["organization_id"]
    )
    op.create_index(
        "ix_reports_org_time",
        "generated_reports",
        ["organization_id", sa.text("generated_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("generated_reports")
    op.execute("DROP TYPE report_state_enum")
    op.execute("DROP TYPE report_format_enum")
    op.execute("DROP TYPE report_type_enum")
