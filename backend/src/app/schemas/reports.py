"""Report request/response schemas (docs/planning/03-api-contract.md
§4.18, app/models/reports.py, app/services/report_service.py).

**Wire shape is FROZEN** - a frontend agent builds against this
concurrently (delegating task's own words). Every field name below is
copied verbatim from that task's "WIRE CONTRACT" section; nothing here is
free to rename.

`content_sha256` is surfaced as a lowercase hex string, not raw bytes - the
same `.hex()` convention `app/schemas/documents.py` already establishes for
`QuoteDocument.content_sha256` (both columns are `bytea`; see migration
0015's module docstring for why). `purged` is derived from `purged_at IS
NOT NULL`, not a stored column of its own - the wire contract asks for a
boolean, and `GeneratedReport` has no separate boolean flag to mirror
(`purged_at` alone is both the marker and the timestamp, per ERD §11).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.reports import GeneratedReport, ReportFormat, ReportType

# -- requests ----------------------------------------------------------


class ReportCreateRequest(BaseModel):
    """`POST /reports` body (03-api-contract.md §4.18:
    `{scenario_id, report_type, format}`, plus this build's own
    `parameters?` - see `app/services/report_service.py` module docstring
    for what each `report_type` reads from `parameters`)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    scenario_id: uuid.UUID
    report_type: ReportType
    format: ReportFormat
    parameters: dict[str, Any] = Field(default_factory=dict)


# -- responses -----------------------------------------------------------


class ReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scenario_id: str
    report_type: str
    format: str
    state: str
    size_bytes: int
    content_sha256: str | None
    parameters: dict[str, Any]
    calculation_version: str
    generated_by_id: str
    generated_at: datetime
    expires_at: datetime
    purged: bool
    error_message: str | None

    @classmethod
    def from_model(cls, report: GeneratedReport) -> ReportResponse:
        return cls(
            id=str(report.id),
            scenario_id=str(report.scenario_id),
            report_type=report.report_type.value,
            format=report.format.value,
            state=report.state.value,
            size_bytes=report.size_bytes,
            content_sha256=(
                report.content_sha256.hex() if report.content_sha256 is not None else None
            ),
            parameters=report.parameters,
            calculation_version=report.calculation_version,
            generated_by_id=str(report.generated_by_id),
            generated_at=report.generated_at,
            expires_at=report.expires_at,
            purged=report.purged_at is not None,
            error_message=report.error_message,
        )


class ReportListResponse(BaseModel):
    items: list[ReportResponse]
    page: dict[str, Any]


__all__ = ["ReportCreateRequest", "ReportListResponse", "ReportResponse"]
