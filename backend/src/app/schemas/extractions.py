"""Extraction request/response schemas (docs/planning/03-api-contract.md
§4.10 `/quote-documents/{id}/extraction-runs`, app/models/documents.py).

Not on the delegating task's own "NEW files" list — the same
not-listed-but-necessary situation `app/schemas/documents.py`'s own module
docstring already documents for an earlier phase.

**JSON field names follow §4.10's own literal annotation for
`GET /extraction-runs/{id}`** (`state, provider, simulated, schema_version,
overall_confidence, fields_requiring_review, error`), not the model's column
names verbatim — `provider` (not `provider_name`) and `simulated` (not
`is_simulated`) mirror the contract's own words; `error` is a single nested
`{code, message}` object (or `null`) rather than two flat columns, matching
this codebase's general error-envelope shape (`app/core/errors.py`).
`injection_suspected` is added beyond the contract's terse annotation per
this task's own explicit instruction (its OBJECTIVE literally lists it among
the fields `GET /extraction-runs/{id}` must surface) — computed from
`ExtractionRun.raw_response["injection_scan"]`, the run-level place
`ExtractionService.start_run` persists the canary scanner's verdict (there is
no dedicated column for it; see that service's module docstring).

**Money/decimal fields are JSON strings** (§1.2): `overall_confidence`,
`confidence`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.documents import ExtractionField, ExtractionRun

# -- runs --------------------------------------------------------------


class ExtractionRunErrorInfo(BaseModel):
    code: str
    message: str


class ExtractionRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    run_number: int
    state: str
    provider: str
    provider_model: str | None
    simulated: bool
    schema_version: str
    prompt_version: str
    overall_confidence: str | None
    fields_requiring_review: int | None
    injection_suspected: bool
    error: ExtractionRunErrorInfo | None
    started_at: datetime
    finished_at: datetime | None
    superseded_at: datetime | None

    @classmethod
    def from_model(cls, run: ExtractionRun) -> ExtractionRunResponse:
        error = None
        if run.error_code is not None:
            error = ExtractionRunErrorInfo(code=run.error_code, message=run.error_message or "")
        injection_suspected = False
        if isinstance(run.raw_response, dict):
            scan = run.raw_response.get("injection_scan")
            if isinstance(scan, dict):
                injection_suspected = bool(scan.get("suspected", False))
        return cls(
            id=str(run.id),
            document_id=str(run.document_id),
            run_number=run.run_number,
            state=run.state.value,
            provider=run.provider_name,
            provider_model=run.provider_model,
            simulated=run.simulated,
            schema_version=run.schema_version,
            prompt_version=run.prompt_version,
            overall_confidence=(
                str(run.overall_confidence) if run.overall_confidence is not None else None
            ),
            fields_requiring_review=run.fields_requiring_review,
            injection_suspected=injection_suspected,
            error=error,
            started_at=run.started_at,
            finished_at=run.finished_at,
            superseded_at=run.superseded_at,
        )


class ExtractionRunListResponse(BaseModel):
    items: list[ExtractionRunResponse]


# -- fields --------------------------------------------------------------


class ExtractionFieldResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    extraction_run_id: str
    field_path: str
    target_entity: str
    target_line_index: int | None
    field_name: str
    raw_value: str | None
    normalized_value: str | None
    value_type: str
    confidence: str
    band: str
    requires_confirmation: bool
    is_confirmed: bool
    confirmed_by_id: str | None
    confirmed_at: datetime | None
    source_page: int | None
    source_bbox: dict[str, Any] | None
    injection_flagged: bool

    @classmethod
    def from_model(cls, field: ExtractionField) -> ExtractionFieldResponse:
        return cls(
            id=str(field.id),
            extraction_run_id=str(field.extraction_run_id),
            field_path=field.field_path,
            target_entity=field.target_entity,
            target_line_index=field.target_line_index,
            field_name=field.field_name,
            raw_value=field.raw_value,
            normalized_value=field.normalized_value,
            value_type=field.value_type,
            confidence=str(field.confidence),
            band=field.band.value,
            requires_confirmation=field.requires_confirmation,
            is_confirmed=field.is_confirmed,
            confirmed_by_id=(str(field.confirmed_by_id) if field.confirmed_by_id else None),
            confirmed_at=field.confirmed_at,
            source_page=field.source_page,
            source_bbox=field.source_bbox,
            injection_flagged=field.injection_flagged,
        )


class ExtractionFieldListResponse(BaseModel):
    items: list[ExtractionFieldResponse]


class ExtractionFieldPatchRequest(BaseModel):
    """`PATCH /extraction-runs/{id}/fields/{fid}` body, §4.10's own literal
    shape. `is_confirmed` has no `?` in the contract's annotation (unlike
    `normalized_value`/`reason`) and is therefore required here, not
    optional. See `services/extraction_service.py` for how the two
    task-brief operations (`confirm_field`/`correct_field`) are dispatched
    from this one combined body."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    normalized_value: str | None = None
    is_confirmed: bool
    reason: str | None = Field(default=None, max_length=1000)


# -- materialize -----------------------------------------------------------


class ExtractionMaterializeRequest(BaseModel):
    """`POST /extraction-runs/{id}/confirm` body. Not spelled out in
    §4.10's own terse annotation, but required per this task's explicit
    instruction: the RFQ comes from the document, but nothing in the
    extraction payload can name *which invited supplier* issued the quote
    with the reliability a foreign key demands, so the reviewer supplies it
    explicitly when materializing (see `services/extraction_service.py`'s
    module docstring)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    supplier_id: uuid.UUID


__all__ = [
    "ExtractionFieldListResponse",
    "ExtractionFieldPatchRequest",
    "ExtractionFieldResponse",
    "ExtractionMaterializeRequest",
    "ExtractionRunErrorInfo",
    "ExtractionRunListResponse",
    "ExtractionRunResponse",
]
