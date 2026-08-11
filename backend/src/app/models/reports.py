"""generated_reports (docs/planning/02-erd.md GENERATED_REPORTS box,
docs/planning/02-erd.md §11 purge rules, docs/planning/03-api-contract.md
§4.18). See migration 0015's own module docstring for the full
ERD/task-paraphrase reconciliation (`content_sha256` as `bytea` matching
`app/models/documents.py`'s `QuoteDocument.content_sha256`; `storage_key`/
`content_sha256` nullable for `pending`/`failed` rows and nulled together
on purge; `error_message` and `purged_at` as genuine additions beyond the
ERD box's own literal columns).

No `TimestampedMixin`/`VersionedMixin`/`ArchivableMixin` — the ERD box lists
none of `created_at`/`updated_at`/`version`/`archived_at` for this table,
and a `GeneratedReport` row is never edited after creation in this build:
`generated_at` is its own creation timestamp, and the only two later
mutations a row can ever undergo (a purge job nulling `storage_key`/
`content_sha256` and setting `purged_at`) are ERD §11's own named
exception to "business data is never hard-deleted" business-as-usual, not
a mutable aggregate in the `ComparisonScenario`/`Rfq` sense — there is no
purge route in this build's scope (see `app/services/report_service.py`
module docstring), so that mutation path is exercised only by tests
writing `purged_at` directly, mirroring how `test_briefs_api.py`-style
fixtures already reach into the DB for setup outside the HTTP surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, ForeignKeyConstraint, LargeBinary, Text
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import OrgOwnedBase, org_identity_constraint


class ReportType(StrEnum):
    SUPPLIER_COMPARISON = "supplier_comparison"
    CFO_RECOMMENDATION = "cfo_recommendation"
    NEGOTIATION_BRIEF = "negotiation_brief"
    SCENARIO_SUMMARY = "scenario_summary"
    AUDIT_HISTORY = "audit_history"


REPORT_TYPE_ENUM = SaEnum(
    ReportType,
    name="report_type_enum",
    values_callable=lambda e: [m.value for m in e],
)


class ReportFormat(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"
    PDF = "pdf"


REPORT_FORMAT_ENUM = SaEnum(
    ReportFormat,
    name="report_format_enum",
    values_callable=lambda e: [m.value for m in e],
)


class ReportState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


REPORT_STATE_ENUM = SaEnum(
    ReportState,
    name="report_state_enum",
    values_callable=lambda e: [m.value for m in e],
)


class GeneratedReport(OrgOwnedBase):
    """One rendered export artifact, grounded in one `ComparisonScenario`.
    See module docstring for the mixin-omission rationale and migration
    0015's docstring for the column-level ERD reconciliation."""

    __tablename__ = "generated_reports"
    __table_args__ = (
        org_identity_constraint("generated_reports"),
        # composite org FK: the scenario this report is grounded in.
        ForeignKeyConstraint(
            ["organization_id", "scenario_id"],
            ["comparison_scenarios.organization_id", "comparison_scenarios.id"],
            ondelete="RESTRICT",
            name="fk_generated_reports_organization_id_scenario_id",
        ),
    )

    # part of the composite FK above, not a single-column ForeignKey
    scenario_id: Mapped[uuid.UUID] = mapped_column()
    report_type: Mapped[ReportType] = mapped_column(REPORT_TYPE_ENUM)
    format: Mapped[ReportFormat] = mapped_column(REPORT_FORMAT_ENUM)
    # nullable: nulled on purge, and never set for pending/failed rows.
    storage_key: Mapped[str | None] = mapped_column(Text(), default=None)
    # bytea, matching app.models.documents.QuoteDocument.content_sha256
    # (migration 0015 module docstring); nullable for the same reason as
    # storage_key.
    content_sha256: Mapped[bytes | None] = mapped_column(LargeBinary(), default=None)
    size_bytes: Mapped[int] = mapped_column(BigInteger(), default=0)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB(), default=dict)
    # the scenario's OWN calculation_version, copied at generation time —
    # every figure in the report traces back to that calculation run.
    calculation_version: Mapped[str] = mapped_column(Text())
    state: Mapped[ReportState] = mapped_column(REPORT_STATE_ENUM, default=ReportState.PENDING)
    generated_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    generated_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()
    # ERD §11 purge marker (migration 0015 module docstring).
    purged_at: Mapped[datetime | None] = mapped_column(default=None)
    # set only when state is FAILED (app/services/report_service.py).
    error_message: Mapped[str | None] = mapped_column(Text(), default=None)


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in two FKs here (the plain org FK plus the composite scenario
# FK), so `foreign_keys` disambiguates which constraint each relationship
# follows, and `overlaps` silences SQLAlchemy's shared-column warning — the
# same pattern as briefs.py/scenarios.py/quotes.py.
GeneratedReport.organization = relationship(
    "Organization", foreign_keys=[GeneratedReport.organization_id], lazy="select"
)
GeneratedReport.scenario = relationship(
    "ComparisonScenario",
    foreign_keys=[GeneratedReport.organization_id, GeneratedReport.scenario_id],
    lazy="select",
    overlaps="organization",
)
GeneratedReport.generated_by = relationship(
    "User", foreign_keys=[GeneratedReport.generated_by_id], lazy="select"
)


__all__ = [
    "REPORT_FORMAT_ENUM",
    "REPORT_STATE_ENUM",
    "REPORT_TYPE_ENUM",
    "GeneratedReport",
    "ReportFormat",
    "ReportState",
    "ReportType",
]
