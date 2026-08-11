"""Document repository — org-scoped data access for `quote_documents`
(docs/planning/02-erd.md §6, app/models/documents.py).

Not on the delegating task's own "NEW files" list (which named
`providers/storage/*`, `services/document_service.py`, `api/v1/documents.py`,
and the two test files) — but every other service in this codebase
(`QuoteService`, `RfqService`, `PartService`, ...) delegates its org-scoped
queries to a dedicated `app/repositories/<resource>.py` module rather than
running `select(...)` directly against the session, and `OrgScopedRepository`
is this codebase's only sanctioned way to filter by `organization_id`
(isolation control #2, `app/repositories/base.py`'s own docstring). Inlining
raw queries into `DocumentService` instead would be the larger deviation from
this codebase's established shape — the same reasoning
`app/schemas/part_imports.py`'s module docstring already gives for an
analogous not-explicitly-listed-but-necessary file.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.models.documents import DocumentState, QuoteDocument
from app.repositories.base import OrgScopedRepository


class DocumentRepository(OrgScopedRepository[QuoteDocument]):
    model = QuoteDocument

    def get_by_sha256(self, sha256: bytes) -> QuoteDocument | None:
        """Org-scoped duplicate-upload lookup
        (`uq_quote_documents_org_sha256`, app/models/documents.py)."""
        stmt = self._base_query().where(QuoteDocument.content_sha256 == sha256)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_documents(
        self,
        *,
        rfq_id: uuid.UUID | None = None,
        supplier_id: uuid.UUID | None = None,
        state: DocumentState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[QuoteDocument]:
        where: list[Any] = []
        if rfq_id is not None:
            where.append(QuoteDocument.rfq_id == rfq_id)
        if supplier_id is not None:
            where.append(QuoteDocument.supplier_id == supplier_id)
        if state is not None:
            where.append(QuoteDocument.state == state)
        stmt = (
            self._base_query()
            .where(*where)
            .order_by(QuoteDocument.uploaded_at.desc())
            .limit(min(limit, 200))
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())


__all__ = ["DocumentRepository"]
