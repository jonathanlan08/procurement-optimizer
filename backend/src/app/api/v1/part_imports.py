"""Part import routes (docs/planning/03-api-contract.md §4.5). Sync `def` by
policy, mirroring api/v1/parts.py.

Route naming follows the contract exactly rather than this task's brief:
`POST /part-imports/{id}/cancel`, not `/discard` — §4.5's own route table
says `cancel`, and the resulting state is `rolled_back`
(app.models.part_imports.PartImportState), not "discarded"; the brief used
different words for the same route, and the contract wins per instructions.

Security (this task's brief, non-negotiable):
- File size is checked with a **streamed** read against `settings.
  max_upload_bytes`, aborting with `413` the moment the running total is
  exceeded, before the whole file is ever handed to the parser
  (`_read_upload_within_limit`).
- The file format is decided from the filename extension only (`.csv` /
  `.xlsx`) — `415` for anything else, including a missing filename. Content-
  Type headers are not trusted for this decision: browsers and HTTP clients
  disagree wildly on what MIME type a `.csv`/`.xlsx` upload carries, while
  the extension is what `app.importing.part_import_parser.parse_csv`/
  `parse_xlsx` are each hard-coded to handle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.api.deps import (
    AuditDep,
    ClockDep,
    DbDep,
    IdsDep,
    Principal,
    PrincipalDep,
    SettingsDep,
    require_role,
)
from app.core.errors import PayloadTooLargeError, UnsupportedMediaTypeError
from app.models.identity import Role
from app.models.part_imports import PartImportFormat
from app.schemas.part_imports import (
    PartImportBatchDetailResponse,
    PartImportBatchSummaryResponse,
    PartImportCommitResponse,
    PartImportPreviewResponse,
    PartImportRowErrorResponse,
)
from app.services.part_import_service import PartImportService

router = APIRouter(prefix="/part-imports", tags=["part-imports"])

_EXTENSION_FORMAT: dict[str, PartImportFormat] = {
    ".csv": PartImportFormat.CSV,
    ".xlsx": PartImportFormat.XLSX,
}
_READ_CHUNK_BYTES = 1024 * 1024
_SAMPLE_ROWS_LIMIT = 20
_PREVIEW_ERRORS_LIMIT = 100


def get_part_import_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> PartImportService:
    return PartImportService(db, principal.organization_id, audit, clock, ids)


PartImportServiceDep = Annotated[PartImportService, Depends(get_part_import_service)]


def _determine_format(filename: str | None) -> PartImportFormat:
    if not filename:
        raise UnsupportedMediaTypeError("A filename with a .csv or .xlsx extension is required.")
    suffix = Path(filename).suffix.lower()
    fmt = _EXTENSION_FORMAT.get(suffix)
    if fmt is None:
        raise UnsupportedMediaTypeError(
            f"Unsupported file type {suffix or filename!r}. Only .csv and .xlsx are accepted."
        )
    return fmt


def _read_upload_within_limit(file: UploadFile, max_bytes: int) -> bytes:
    """Chunked size check bounding what THIS handler holds in memory — not
    the first line of defense (Starlette spools multipart file parts to disk
    before the handler runs); oversized declared bodies are rejected
    pre-routing by `BodySizeLimitMiddleware`, and chunked bodies need the
    reverse-proxy cap documented in DEPLOYMENT.md §8."""
    buf = bytearray()
    while True:
        chunk = file.file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise PayloadTooLargeError(
                f"Uploaded file exceeds the maximum allowed size of {max_bytes} bytes."
            )
    return bytes(buf)


@router.post("", status_code=201)
def create_part_import(
    service: PartImportServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> PartImportPreviewResponse:
    fmt = _determine_format(file.filename)
    data = _read_upload_within_limit(file, settings.max_upload_bytes)
    batch = service.preview(
        file_bytes=data,
        filename=file.filename or "upload",
        fmt=fmt,
        actor_id=principal.user.id,
    )
    sample_rows, _next_cursor, _has_more = service.list_rows(batch.id, limit=_SAMPLE_ROWS_LIMIT)
    errors = [
        PartImportRowErrorResponse(row_number=e.row_number, field=e.field, issue=e.issue)
        for e in service.list_errors(batch.id, limit=_PREVIEW_ERRORS_LIMIT)
    ]
    return PartImportPreviewResponse.from_batch(batch, sample_rows=sample_rows, errors=errors)


@router.get("/{batch_id}")
def get_part_import(
    batch_id: UUID,
    service: PartImportServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> PartImportBatchDetailResponse:
    batch = service.get(batch_id)
    rows, next_cursor, has_more = service.list_rows(batch_id, limit=limit, cursor=cursor)
    return PartImportBatchDetailResponse.from_batch(
        batch, rows=rows, limit=limit, next_cursor=next_cursor, has_more=has_more
    )


@router.post("/{batch_id}/commit")
def commit_part_import(
    batch_id: UUID,
    service: PartImportServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> PartImportCommitResponse:
    result = service.commit(batch_id)
    return PartImportCommitResponse(
        created=result.created, updated=result.updated, skipped=result.skipped
    )


@router.post("/{batch_id}/cancel")
def cancel_part_import(
    batch_id: UUID,
    service: PartImportServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> PartImportBatchSummaryResponse:
    batch = service.cancel(batch_id)
    return PartImportBatchSummaryResponse.from_model(batch)


__all__ = ["router"]
