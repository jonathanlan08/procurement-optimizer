"""Document service - org-scoped upload/read/download/archive for
`quote_documents` (docs/planning/03-api-contract.md §4.9,
docs/planning/04-document-pipeline.md §§2-4, app/models/documents.py).

Services never commit: the request-scoped `get_db` dependency
(app/api/deps.py) commits on success and rolls back on any raised exception.

**Scope: Stages 1-3 of the pipeline only** (transport validation, content
validation, storage) - text/table acquisition, OCR, extraction, review,
correction, materialization, and part matching (Stages 4-13,
04-document-pipeline.md) are later phases and are not implemented here.
`GET /quote-documents/{id}/pages/{n}/preview` (§4.9's route table) belongs to
that later, rendering-capable phase and is deliberately not routed by this
task's `api/v1/documents.py`.

## Deliberate deviations / judgment calls

1. **`upload()`'s internal order is validate -> dedupe -> store -> create
   row -> audit**, not "create an `uploaded` row, then advance it through
   `validated`/`stored` in place." The delegating task's own prose lists
   the steps in exactly this order, and following it literally has a real
   correctness benefit over the alternative: if `StorageProvider.put()`
   raises (disk full, S3 unreachable, ...) *before* any `QuoteDocument` row
   exists, there is no metadata row left behind pointing at bytes that were
   never actually written. The row is created only once, already at its
   final state for this phase (`DocumentState.STORED`) - "transitions
   recorded honestly" (the task's own phrase) is read as "the persisted
   state must truthfully reflect how far the pipeline actually got," not as
   a requirement to literally persist three intermediate rows for a
   same-request, all-or-nothing sequence. No `quote_documents` row is ever
   created in `DocumentState.UPLOADED` or `.VALIDATED` by this module.
2. **A content-validation failure (`FileValidationError` from
   `app.ingestion.file_validation.validate_upload`) creates no row at all**
   and is left for `api/v1/documents.py` to map to a `413`/`415` HTTP error
   (per the task's own instruction, "implement mapping in the route").
   `app/models/documents.py`'s own module docstring notes `quarantined` is
   reachable from `uploaded`/`validated` *before* Stage 2/4 ever run - a
   persisted quarantine audit trail for rejected uploads is a real future
   need, but it is not part of the spec (no test in the
   own required list exercises it) and is left to the extraction-pipeline
   phase that will also own `app/domain/documents/transitions.py`.
3. **Duplicate content is `409 conflict_duplicate`, not the contract's
   `03-api-contract.md` §4.9 literal annotation of `201 {document,
   duplicate_of?}`.** That same document's own §3 error-code table lists
   `conflict_duplicate | 409 | uniqueness violation (duplicate supplier
   code, duplicate upload)` - i.e. the contract disagrees with itself
   between its terse per-route annotation and its own error table. This
   task's brief resolves that internal conflict explicitly and in detail
   ("sha256 dedupe per org (same hash -> 409 conflict_duplicate with
   existing document id in details)"), matching §3's error table over
   §4.9's shorthand; the existing document's id is surfaced as an
   `ErrorDetail(field="file", issue="duplicate_of", value_hint=...)`, the
   closest existing vocabulary to `duplicate_of` the error envelope has
   (`03-api-contract.md` §3).
4. **`delete()` is a soft archive**, matching `ArchivableMixin` (this model
   carries `archived_at`/`archived_by_id`/`archive_reason`, see
   app/models/documents.py's own module docstring point 3) and every other
   archivable resource in this codebase (`QuoteService.archive`,
   `SupplierService`, ...) never hard-deleting business data
   (`app/models/base.py`'s own module docstring: "soft delete via
   archived_at; business data is never hard-deleted"). Named `delete` here
   because that is the task brief's own method name; the route that calls
   it is `POST /quote-documents/{id}/archive` (the contract's literal route
   name, §4.9) - both describe the same soft-delete action. "State permits"
   is read as: not already archived (`409 conflict_state` otherwise) - the
   only two states this module ever produces are `stored` (terminal for
   this phase) and `archived`, so there is no other document-state gate to
   express yet.
5. **`storage.put()` is called with the *detected* mime type**
   (`app.ingestion.file_validation`'s sniffed `DocumentKind`), not the
   client's declared `Content-Type` header - `declared_mime` (the model's
   own untrusted, as-received field) is kept on the row for audit purposes,
   but only the sniffed, verified type is ever treated as authoritative
   (04-document-pipeline.md §3: "Detected type must match the declared
   extension" - the same "never trust the client's own label" posture
   extended to what gets handed to the storage backend).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import (
    ConflictDuplicateError,
    ConflictStateError,
    ErrorDetail,
    NotFoundError,
)
from app.core.ids import IdGenerator
from app.ingestion.file_validation import DocumentKind, validate_upload
from app.models.documents import DocumentState, QuoteDocument
from app.providers.storage.base import StorageProvider
from app.repositories.document_repository import DocumentRepository
from app.repositories.rfq_repository import RfqRepository
from app.repositories.supplier_repository import SupplierRepository
from app.services.audit import AuditRecorder

_MIME_OF_KIND: dict[DocumentKind, str] = {
    DocumentKind.PDF: "application/pdf",
    DocumentKind.PNG: "image/png",
    DocumentKind.JPEG: "image/jpeg",
    DocumentKind.CSV: "text/csv",
    DocumentKind.XLSX: ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


def _document_state(document: QuoteDocument) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "rfq_id": str(document.rfq_id),
        "supplier_id": (str(document.supplier_id) if document.supplier_id else None),
        "original_filename": document.original_filename,
        "detected_mime": document.detected_mime,
        "declared_mime": document.declared_mime,
        "size_bytes": document.size_bytes,
        "content_sha256": (document.content_sha256.hex() if document.content_sha256 else None),
        "state": document.state.value,
        "page_count": document.page_count,
        "is_archived": document.is_archived,
        "archived_at": (document.archived_at.isoformat() if document.archived_at else None),
        "archive_reason": document.archive_reason,
    }


class DocumentService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
        *,
        storage: StorageProvider,
        max_upload_bytes: int,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._storage = storage
        self._max_upload_bytes = max_upload_bytes
        self._repo = DocumentRepository(session, organization_id)
        self._rfq_repo = RfqRepository(session, organization_id)
        self._supplier_repo = SupplierRepository(session, organization_id)

    # -- upload --------------------------------------------------------

    def upload(
        self,
        *,
        rfq_id: uuid.UUID,
        supplier_id: uuid.UUID | None,
        filename: str,
        content_type: str | None,
        data: bytes,
        actor_id: uuid.UUID,
    ) -> QuoteDocument:
        self._rfq_repo.get_or_raise(rfq_id)
        if supplier_id is not None and self._supplier_repo.get(supplier_id) is None:
            raise NotFoundError("Supplier")

        # Stages 1-2: transport + content validation. Raises
        # FileValidationError (not an AppError) on failure, deliberately
        # left for api/v1/documents.py to map to 413/415 - see module
        # docstring point 2. No row is created below this line on failure.
        validated = validate_upload(
            original_filename=filename,
            content_type=content_type,
            head=data[:512],
            byte_size=len(data),
            max_bytes=self._max_upload_bytes,
        )

        sha256 = hashlib.sha256(data).digest()
        existing = self._repo.get_by_sha256(sha256)
        if existing is not None:
            raise ConflictDuplicateError(
                "A document with identical content has already been uploaded "
                "for this organization.",
                details=[
                    ErrorDetail(field="file", issue="duplicate_of", value_hint=str(existing.id))
                ],
            )

        mime = _MIME_OF_KIND[validated.kind]

        # Stage 3: store the immutable original before any metadata row
        # exists - module docstring point 1.
        self._storage.put(
            organization_id=self._organization_id,
            key=validated.storage_key,
            data=data,
            content_type=mime,
        )

        now = self._clock.now()
        document = QuoteDocument(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            rfq_id=rfq_id,
            supplier_id=supplier_id,
            original_filename=validated.safe_filename,
            storage_key=validated.storage_key,
            detected_mime=mime,
            declared_mime=content_type or mime,
            size_bytes=len(data),
            content_sha256=sha256,
            state=DocumentState.STORED,
            page_count=None,
            uploaded_by_id=actor_id,
            uploaded_at=now,
        )
        self._repo.add(document)
        try:
            self._db.flush()
        except IntegrityError as exc:
            # Belt-and-braces: a concurrent upload of identical bytes could
            # win the race between our pre-check above and this flush.
            raise ConflictDuplicateError(
                "A document with identical content has already been uploaded for this organization."
            ) from exc

        self._audit.record(
            "document.uploaded",
            entity_type="quote_document",
            entity_id=document.id,
            after_state=_document_state(document),
            explanation=(f"Document {document.original_filename!r} uploaded for RFQ {rfq_id}"),
        )
        return document

    # -- reads -----------------------------------------------------------

    def get_document(self, document_id: uuid.UUID) -> QuoteDocument:
        return self._repo.get_or_raise(document_id)

    def list_documents(
        self,
        *,
        rfq_id: uuid.UUID | None = None,
        supplier_id: uuid.UUID | None = None,
        state: DocumentState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[QuoteDocument]:
        if rfq_id is not None:
            self._rfq_repo.get_or_raise(rfq_id)
        if supplier_id is not None and self._supplier_repo.get(supplier_id) is None:
            raise NotFoundError("Supplier")
        return self._repo.list_documents(
            rfq_id=rfq_id,
            supplier_id=supplier_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    def download(self, document_id: uuid.UUID) -> tuple[bytes, str, str]:
        """Returns `(bytes, content_type, safe_filename)` for streaming.
        Never a storage URL (03-api-contract.md §4.9)."""
        document = self._repo.get_or_raise(document_id)
        data = self._storage.get(organization_id=self._organization_id, key=document.storage_key)
        content_type = document.detected_mime or document.declared_mime
        return data, content_type, document.original_filename

    # -- delete (soft archive; module docstring point 4) -----------------

    def delete(self, document_id: uuid.UUID, *, reason: str, actor_id: uuid.UUID) -> QuoteDocument:
        document = self._repo.get_or_raise(document_id)
        if document.is_archived:
            raise ConflictStateError("This document has already been archived.")

        before = _document_state(document)
        now = self._clock.now()
        document.archived_at = now
        document.archived_by_id = actor_id
        document.archive_reason = reason
        self._db.flush()

        self._audit.record(
            "document.archived",
            entity_type="quote_document",
            entity_id=document.id,
            before_state=before,
            after_state=_document_state(document),
            explanation=f"Document {document.id} archived: {reason}",
        )
        return document


__all__ = ["DocumentService"]
