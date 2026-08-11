"""Quote-document routes (docs/planning/03-api-contract.md §4.9
`/quote-documents`). Sync `def` by policy, mirroring every other router in
this codebase.

**Two routers, one module** — the same shape `api/v1/quotes.py` already
establishes and documents. The delegating task's own item 3 paraphrases the
route table as four flat `/quote-documents...` paths, but §4.9's *actual*
route table nests create/list under the parent RFQ
(`POST`/`GET /rfqs/{id}/quote-documents`) while `GET /quote-documents/{id}`,
`GET /quote-documents/{id}/content`, and `POST /quote-documents/{id}/archive`
address the document directly — followed literally here per "follow the
contract's actual paths if they differ" (this task's own instruction), the
identical resolution `api/v1/quotes.py`'s module docstring already documents
for the same task-prose-vs-contract-table gap. `rfq_documents_router` and
`router` are both exported and both mounted in `app/main.py`.

`.../pages/{n}/preview` (§4.9's fifth row) has no route here — it renders a
page image from Stage 4 (text/table acquisition) output, which
`services/document_service.py` does not implement (Stage 1-3 scope only; see
that module's own docstring).

Security, per this task's brief:
- File size is checked with a **streamed** read against
  `settings.max_upload_bytes`, aborting with `413` before the whole file is
  ever buffered — the same `_read_upload_within_limit` technique
  `api/v1/part_imports.py` already uses, duplicated here rather than
  imported (that module exports only `router`).
- `FileValidationError` raised by `DocumentService.upload` (via
  `app.ingestion.file_validation.validate_upload`) is mapped to an HTTP
  error **in this route**, per the task's own explicit instruction: any
  message that names the size limit maps to `413 payload_too_large`
  (defense in depth — the streamed read above already catches most
  oversized uploads before `upload()` is ever called); everything else
  (magic-byte/extension/declared-type mismatches, an empty file) maps to
  `415 unsupported_media_type`.
- `GET .../content` streams via `StreamingResponse` with
  `Content-Disposition: attachment` and the document's own sanitized
  `original_filename` (never the client-supplied raw name, never a storage
  key). `X-Content-Type-Options: nosniff` is already set globally by
  `SecurityHeadersMiddleware` (`app/api/middleware.py`) and is not repeated
  here.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse

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
from app.core.errors import (
    AppError,
    ErrorDetail,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationAppError,
)
from app.ingestion.file_validation import FileValidationError
from app.models.documents import DocumentState
from app.models.identity import Role
from app.providers.storage import build_storage_provider
from app.schemas.documents import DocumentArchiveRequest, DocumentListResponse, DocumentResponse
from app.services.document_service import DocumentService

rfq_documents_router = APIRouter(prefix="/rfqs/{rfq_id}/quote-documents", tags=["documents"])
router = APIRouter(prefix="/quote-documents", tags=["documents"])

_READ_CHUNK_BYTES = 1024 * 1024


def get_document_service(
    db: DbDep,
    principal: PrincipalDep,
    audit: AuditDep,
    clock: ClockDep,
    ids: IdsDep,
    settings: SettingsDep,
) -> DocumentService:
    # Built per request from Settings, not injected via api/deps.py
    # (principal-owned, off limits) — per this task's own instruction; see
    # providers/storage/__init__.py's module docstring.
    storage = build_storage_provider(settings)
    return DocumentService(
        db,
        principal.organization_id,
        audit,
        clock,
        ids,
        storage=storage,
        max_upload_bytes=settings.max_upload_bytes,
    )


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


def _read_upload_within_limit(file: UploadFile, max_bytes: int) -> bytes:
    """Chunked size check bounding what THIS handler holds in memory. It is
    NOT the first line of defense: FastAPI resolves `UploadFile` by parsing
    the multipart body before the handler runs, and Starlette spools file
    parts to a temp file with no size limit of its own — so oversized
    DECLARED bodies are rejected pre-routing by `BodySizeLimitMiddleware`
    (app/api/middleware.py), and a chunked body without Content-Length must
    be capped by the reverse proxy (DEPLOYMENT.md §8). This check remains
    authoritative for the exact per-file limit."""
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


def _map_file_validation_error(exc: FileValidationError) -> AppError:
    if "size limit" in str(exc):
        return PayloadTooLargeError(str(exc))
    return UnsupportedMediaTypeError(str(exc))


def _parse_optional_supplier_id(raw: str | None) -> UUID | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ValidationAppError(
            "supplier_id must be a valid UUID.",
            details=[
                ErrorDetail(field="supplier_id", issue="must be a valid UUID", value_hint=raw)
            ],
        ) from exc


# -- nested under the RFQ: list/create --------------------------------------


@rfq_documents_router.post("", status_code=201)
def upload_document(
    rfq_id: UUID,
    service: DocumentServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    supplier_id: Annotated[str | None, Form()] = None,
) -> DocumentResponse:
    parsed_supplier_id = _parse_optional_supplier_id(supplier_id)
    data = _read_upload_within_limit(file, settings.max_upload_bytes)
    try:
        document = service.upload(
            rfq_id=rfq_id,
            supplier_id=parsed_supplier_id,
            filename=file.filename or "upload",
            content_type=file.content_type,
            data=data,
            actor_id=principal.user.id,
        )
    except FileValidationError as exc:
        raise _map_file_validation_error(exc) from exc
    return DocumentResponse.from_model(document)


@rfq_documents_router.get("")
def list_rfq_documents(
    rfq_id: UUID,
    service: DocumentServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    # B008 suppressed below — ruff's immutable-type allowlist for the
    # "function call in a default" check doesn't recognize an enum/`UUID` as
    # immutable the way it does `str`/`bool`/`int`; `Query()` itself is a
    # side-effect-free, idempotent call either way (same reasoning
    # api/v1/rfqs.py's `list_rfqs` already documents for its own `status`/
    # `due_before`/`due_after` filters).
    state: DocumentState | None = Query(default=None),  # noqa: B008
    supplier_id: UUID | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DocumentListResponse:
    documents = service.list_documents(
        rfq_id=rfq_id, supplier_id=supplier_id, state=state, limit=limit, offset=offset
    )
    return DocumentListResponse(items=[DocumentResponse.from_model(d) for d in documents])


# -- addressed directly ------------------------------------------------


@router.get("/{document_id}")
def get_document(
    document_id: UUID,
    service: DocumentServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> DocumentResponse:
    document = service.get_document(document_id)
    return DocumentResponse.from_model(document)


def _content_disposition(filename: str) -> str:
    """RFC 6266 attachment header. Starlette encodes response headers as
    latin-1, so a sanitized-but-non-Latin-1 filename (e.g. a CJK supplier
    document name) placed directly in `filename="..."` raises
    `UnicodeEncodeError` and permanently 500s the download (2026-08 security
    audit, MEDIUM-3). Send an ASCII fallback plus the RFC 5987 `filename*`
    form, which every current browser prefers when present."""
    ascii_fallback = (
        filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    )
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


@router.get("/{document_id}/content")
def download_document_content(
    document_id: UUID,
    service: DocumentServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> StreamingResponse:
    data, content_type, safe_filename = service.download(document_id)
    headers = {"Content-Disposition": _content_disposition(safe_filename)}
    return StreamingResponse(iter([data]), media_type=content_type, headers=headers)


@router.post("/{document_id}/archive")
def archive_document(
    document_id: UUID,
    body: DocumentArchiveRequest,
    service: DocumentServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
) -> DocumentResponse:
    document = service.delete(document_id, reason=body.reason, actor_id=principal.user.id)
    return DocumentResponse.from_model(document)


__all__ = ["rfq_documents_router", "router"]
