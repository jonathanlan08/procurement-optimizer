"""Extraction repository - org-scoped data access for `extraction_runs`,
`extraction_fields`, `document_pages`, and `quote_corrections`
(docs/planning/02-erd.md §6, app/models/documents.py).

Not on the delegating task's own "NEW files" list (which names
`services/extraction_service.py`, `api/v1/extractions.py`, and the
integration test file) - the same not-listed-but-necessary situation
`app/repositories/document_repository.py`'s own module docstring already
documents: every other service in this codebase delegates its org-scoped
queries to a dedicated `app/repositories/<resource>.py` module rather than
running `select(...)` directly against the session, and `OrgScopedRepository`
is this codebase's only sanctioned way to filter by `organization_id`
(isolation control #2). `document_repository.py` itself is FROZEN for this
task (an existing file this change may not modify), so a second, purpose-built
repository lands here rather than growing that one.

**Two repository classes in one file.** `ExtractionRepository` is bound to
`ExtractionRun` (the aggregate `ExtractionField`/`QuoteCorrection` rows hang
off of); `DocumentPageRepository` is bound to `DocumentPage` - a
document-scoped, not run-scoped, table (module docstring of
`app/models/documents.py`: "the audit record of exactly what text the
extraction provider saw"). Co-located rather than split into a third file,
the same "not separately listed, so co-locate" judgement
`app/repositories/part_repository.py`'s own docstring already documents for
`PartAlternativeRepository`.

**`find_materialized_quote_id`** exists to solve a real gap: neither
`ExtractionRun` nor `Quote` carries a stored forward/back link between a run
and the quote `ExtractionService.materialize()` built from it (`Quote` has no
`source_extraction_run_id` column at all - see `app/models/quotes.py`'s own
module docstring point 1, "meaningless before the extraction pipeline
exists" - and this phase does not add one; the frozen model is not modified).
The one durable, already-existing place that link is recorded is the
`extraction.materialized` audit event's own `after_state` (which
`ExtractionService` writes with `extraction_run_id` alongside the quote it
describes) - so this method reads it back from there. Used by
`ExtractionService.correct_field` to decide whether a post-materialization
correction can also produce a `QuoteCorrection` row (`quote_id` is `NOT NULL`
on that table; see `services/extraction_service.py`'s own module docstring
for the full reasoning).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.core.errors import NotFoundError
from app.domain.confidence import ConfidenceBand
from app.models.audit import AuditEvent
from app.models.base import OrgOwnedBase
from app.models.documents import DocumentPage, ExtractionField, ExtractionRun, QuoteCorrection
from app.repositories.base import OrgScopedRepository


class ExtractionRepository(OrgScopedRepository[ExtractionRun]):
    model = ExtractionRun

    # -- runs --------------------------------------------------------------

    def list_runs_for_document(self, document_id: uuid.UUID) -> list[ExtractionRun]:
        stmt = (
            self._base_query()
            .where(ExtractionRun.document_id == document_id)
            .order_by(ExtractionRun.run_number.desc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def next_run_number(self, document_id: uuid.UUID) -> int:
        existing = self.list_runs_for_document(document_id)
        return max((r.run_number for r in existing), default=0) + 1

    # -- fields --------------------------------------------------------------

    def add_field(self, field: ExtractionField) -> ExtractionField:
        self._guard_org(field)
        self.session.add(field)
        return field

    def list_fields(
        self,
        run_id: uuid.UUID,
        *,
        requires_confirmation: bool | None = None,
        band: ConfidenceBand | None = None,
        is_confirmed: bool | None = None,
    ) -> list[ExtractionField]:
        where: list[Any] = [ExtractionField.extraction_run_id == run_id]
        if requires_confirmation is not None:
            where.append(ExtractionField.requires_confirmation == requires_confirmation)
        if band is not None:
            where.append(ExtractionField.band == band)
        if is_confirmed is not None:
            where.append(ExtractionField.is_confirmed == is_confirmed)
        stmt = self._select(ExtractionField).where(*where).order_by(ExtractionField.field_path)
        return list(self.session.execute(stmt).scalars().all())

    def get_field(self, field_id: uuid.UUID) -> ExtractionField | None:
        stmt = self._select(ExtractionField).where(ExtractionField.id == field_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def get_field_or_raise(self, field_id: uuid.UUID) -> ExtractionField:
        field = self.get_field(field_id)
        if field is None:
            raise NotFoundError("ExtractionField")
        return field

    # -- corrections -----------------------------------------------------

    def add_correction(self, correction: QuoteCorrection) -> QuoteCorrection:
        self._guard_org(correction)
        self.session.add(correction)
        return correction

    def find_materialized_quote_id(self, run_id: uuid.UUID) -> uuid.UUID | None:
        """See module docstring. Reads the `extraction.materialized` audit
        trail (there is no other durable run -> quote link in this schema)
        for the most recent event whose `after_state["extraction_run_id"]`
        matches `run_id`, and returns that event's `entity_id` (the quote).
        Filtered in Python, not via a JSONB operator, to stay driver-agnostic
        and simple at the modest audit-event volumes this v0.1 build expects."""
        stmt = (
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == self.organization_id,
                AuditEvent.event_type == "extraction.materialized",
            )
            .order_by(AuditEvent.occurred_at.desc())
        )
        for event in self.session.execute(stmt).scalars().all():
            after = event.after_state or {}
            if after.get("extraction_run_id") == str(run_id):
                return event.entity_id
        return None

    # -- internals -------------------------------------------------------

    def _select(self, model: type[OrgOwnedBase]) -> Any:
        """Org-scoped `select(model)` for the child tables above: none of
        them are `ModelT` (the repository is generic over `ExtractionRun`),
        so `_base_query()` cannot be reused directly - same pattern as
        `QuoteRepository._select`/`RfqRepository._select`."""
        return select(model).where(model.organization_id == self.organization_id)

    def _guard_org(self, entity: OrgOwnedBase) -> None:
        if entity.organization_id != self.organization_id:
            from app.repositories.base import OrgIsolationViolation

            raise OrgIsolationViolation(
                f"attempted to add {type(entity).__name__} with organization_id="
                f"{entity.organization_id!r} through a repository scoped to "
                f"{self.organization_id}"
            )


class DocumentPageRepository(OrgScopedRepository[DocumentPage]):
    model = DocumentPage

    def add_page(self, page: DocumentPage) -> DocumentPage:
        self._guard_org(page)
        self.session.add(page)
        return page

    def list_pages(self, document_id: uuid.UUID) -> list[DocumentPage]:
        stmt = (
            self._base_query()
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
        )
        return list(self.session.execute(stmt).scalars().all())

    def _guard_org(self, entity: OrgOwnedBase) -> None:
        if entity.organization_id != self.organization_id:
            from app.repositories.base import OrgIsolationViolation

            raise OrgIsolationViolation(
                f"attempted to add {type(entity).__name__} with organization_id="
                f"{entity.organization_id!r} through a repository scoped to "
                f"{self.organization_id}"
            )


__all__ = ["DocumentPageRepository", "ExtractionRepository"]
