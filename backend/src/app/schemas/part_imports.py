"""Part import request/response schemas (docs/planning/03-api-contract.md
§4.5 `/part-imports`).

Not in this task's brief's own file-creation list, which named only the
migration/model/importing/service/api/test files — but every other router in
this codebase (`api/v1/parts.py`, `api/v1/suppliers.py`, `api/v1/supplier_
contacts.py`, `api/v1/supplier_performance.py`) pulls its Pydantic request/
response models from a dedicated `app/schemas/<resource>.py` module rather
than defining them inline; inlining schemas into `api/v1/part_imports.py`
instead would be the larger deviation from this codebase's own established
shape. See the top-level task report for the explicit call-out.

Wire conventions (§1.2): `GET /part-imports/{id}` uses cursor pagination
(`items` + `page: {limit, next_cursor, prev_cursor, has_more}`, §1.3) rather
than the offset pagination `parts`/`suppliers` use — the contract explicitly
calls this endpoint "full row-level preview, cursor-paginated", and up to
10,000 rows per batch is exactly the "high-volume list" cursor pagination is
documented as the default for. `prev_cursor` is always `null`: this is a
forward-only keyset implementation (next by `row_number`), a deliberate
scope decision — no endpoint in this codebase implements bidirectional
cursor paging yet to mirror, and nothing in this task's required test list
exercises backward paging.

`POST /part-imports`'s response shape is the contract's own explicit example
(§4.5: `{batch_id, rows_total, rows_valid, rows_invalid, rows_duplicate,
sample_rows[], errors[]}`) and intentionally does not match `GET
/part-imports/{id}`'s shape — different response models for the two
endpoints, per the contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.part_imports import PartImportBatch, PartImportRow


class PartImportRowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    row_number: int
    raw_values: dict[str, Any]
    normalized_values: dict[str, Any] | None
    disposition: str
    errors: list[dict[str, Any]]
    resulting_part_id: str | None

    @classmethod
    def from_model(cls, row: PartImportRow) -> PartImportRowResponse:
        return cls(
            row_number=row.row_number,
            raw_values=row.raw_values,
            normalized_values=row.normalized_values,
            disposition=row.disposition,
            errors=row.errors,
            resulting_part_id=(
                str(row.resulting_part_id) if row.resulting_part_id is not None else None
            ),
        )


class PartImportRowErrorResponse(BaseModel):
    row_number: int
    field: str | None
    issue: str


class PartImportPreviewResponse(BaseModel):
    """`201` response of `POST /part-imports` (§4.5's own explicit shape)."""

    batch_id: str
    rows_total: int
    rows_valid: int
    rows_invalid: int
    rows_duplicate: int
    sample_rows: list[PartImportRowResponse]
    errors: list[PartImportRowErrorResponse]

    @classmethod
    def from_batch(
        cls,
        batch: PartImportBatch,
        *,
        sample_rows: list[PartImportRow],
        errors: list[PartImportRowErrorResponse],
    ) -> PartImportPreviewResponse:
        return cls(
            batch_id=str(batch.id),
            rows_total=batch.rows_total,
            rows_valid=batch.rows_valid,
            rows_invalid=batch.rows_invalid,
            rows_duplicate=batch.rows_duplicate,
            sample_rows=[PartImportRowResponse.from_model(r) for r in sample_rows],
            errors=errors,
        )


class PartImportBatchSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    state: str
    source_filename: str
    format: str
    rows_total: int
    rows_valid: int
    rows_invalid: int
    rows_duplicate: int
    created_by_id: str
    created_at: datetime
    committed_at: datetime | None

    @classmethod
    def from_model(cls, batch: PartImportBatch) -> PartImportBatchSummaryResponse:
        return cls(
            id=str(batch.id),
            state=batch.state.value,
            source_filename=batch.source_filename,
            format=batch.format.value,
            rows_total=batch.rows_total,
            rows_valid=batch.rows_valid,
            rows_invalid=batch.rows_invalid,
            rows_duplicate=batch.rows_duplicate,
            created_by_id=str(batch.created_by_id),
            created_at=batch.created_at,
            committed_at=batch.committed_at,
        )


class PartImportPageInfo(BaseModel):
    limit: int
    next_cursor: str | None
    prev_cursor: str | None
    has_more: bool


class PartImportBatchDetailResponse(BaseModel):
    """`GET /part-imports/{id}`: batch summary fields plus a cursor-paginated
    page of rows (§4.5 "full row-level preview, cursor-paginated")."""

    id: str
    state: str
    source_filename: str
    format: str
    rows_total: int
    rows_valid: int
    rows_invalid: int
    rows_duplicate: int
    created_by_id: str
    created_at: datetime
    committed_at: datetime | None
    items: list[PartImportRowResponse]
    page: PartImportPageInfo

    @classmethod
    def from_batch(
        cls,
        batch: PartImportBatch,
        *,
        rows: list[PartImportRow],
        limit: int,
        next_cursor: str | None,
        has_more: bool,
    ) -> PartImportBatchDetailResponse:
        summary = PartImportBatchSummaryResponse.from_model(batch)
        return cls(
            id=summary.id,
            state=summary.state,
            source_filename=summary.source_filename,
            format=summary.format,
            rows_total=summary.rows_total,
            rows_valid=summary.rows_valid,
            rows_invalid=summary.rows_invalid,
            rows_duplicate=summary.rows_duplicate,
            created_by_id=summary.created_by_id,
            created_at=summary.created_at,
            committed_at=summary.committed_at,
            items=[PartImportRowResponse.from_model(r) for r in rows],
            page=PartImportPageInfo(
                limit=limit, next_cursor=next_cursor, prev_cursor=None, has_more=has_more
            ),
        )


class PartImportCommitResponse(BaseModel):
    """`200` response of `POST /part-imports/{id}/commit` (§4.5's own
    explicit shape: `{created, updated, skipped}`). `updated` is always `0`
    — see app/models/part_imports.py module docstring: this service never
    updates an existing part from an import row, only creates or skips."""

    created: int
    updated: int
    skipped: int


__all__ = [
    "PartImportBatchDetailResponse",
    "PartImportBatchSummaryResponse",
    "PartImportCommitResponse",
    "PartImportPageInfo",
    "PartImportPreviewResponse",
    "PartImportRowErrorResponse",
    "PartImportRowResponse",
]
