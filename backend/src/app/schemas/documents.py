"""Document request/response schemas (docs/planning/03-api-contract.md §4.9
`/quote-documents`, app/models/documents.py).

Not on the delegating task's own "NEW files" list - the same not-listed-but-
necessary situation `app/schemas/part_imports.py`'s own module docstring
already documents: every other router in this codebase pulls its Pydantic
request/response models from a dedicated `app/schemas/<resource>.py` module
rather than defining them inline, and inlining schemas into
`api/v1/documents.py` instead would be the larger deviation from this
codebase's established shape.

**`DocumentResponse` never includes `storage_key`.** §4.9's own route table:
`GET /quote-documents/{id}` is "metadata + page summaries; **never** a
storage URL." Page summaries themselves (`document_pages` rows) belong to
Stage 4 (text/table acquisition), out of the Stage 1-3 scope
(services/document_service.py's own module docstring) - omitted here for the
same reason `.../pages/{n}/preview` has no route in `api/v1/documents.py`.

**No `ETag`/`If-Match` on `DocumentResponse`.** Unlike `Quote`/`Rfq`/`Bom`,
`QuoteDocument` does not mix in `VersionedMixin` (app/models/documents.py's
own module docstring point 3: "gets `ArchivableMixin` but not
`TimestampedMixin` or `VersionedMixin`") - there is no `version` column to
carry as a concurrency token.

**`content_sha256` is surfaced as a lowercase hex string**, not raw bytes -
JSON has no binary type, and hex is the natural, greppable representation for
a value whose entire purpose (duplicate detection) is being compared by eye
or copy-pasted into a support ticket.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.documents import QuoteDocument


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rfq_id: str
    supplier_id: str | None
    original_filename: str
    detected_mime: str | None
    declared_mime: str
    size_bytes: int
    content_sha256: str | None
    state: str
    quarantine_reason: str | None
    page_count: int | None
    uploaded_by_id: str
    uploaded_at: datetime
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None

    @classmethod
    def from_model(cls, document: QuoteDocument) -> DocumentResponse:
        return cls(
            id=str(document.id),
            rfq_id=str(document.rfq_id),
            supplier_id=(str(document.supplier_id) if document.supplier_id else None),
            original_filename=document.original_filename,
            detected_mime=document.detected_mime,
            declared_mime=document.declared_mime,
            size_bytes=document.size_bytes,
            content_sha256=(document.content_sha256.hex() if document.content_sha256 else None),
            state=document.state.value,
            quarantine_reason=document.quarantine_reason,
            page_count=document.page_count,
            uploaded_by_id=str(document.uploaded_by_id),
            uploaded_at=document.uploaded_at,
            is_archived=document.is_archived,
            archived_at=document.archived_at,
            archive_reason=document.archive_reason,
        )


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]


class DocumentArchiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


__all__ = ["DocumentArchiveRequest", "DocumentListResponse", "DocumentResponse"]
