"""Matching repository - org-scoped data access for `part_match_candidates`
(docs/planning/02-erd.md §6, app/models/documents.py).

Not on the delegating task's own "NEW files" list - the same
not-listed-but-necessary situation `app/repositories/extraction_repository.py`'s
own module docstring already documents for the identical class of gap: every
other service in this codebase delegates its org-scoped queries to a
dedicated `app/repositories/<resource>.py` module, and `PartMatchCandidate`
has no existing one.

`QuoteLineRepository` lives here too, for one narrow reason: every other
quote-line accessor in this codebase (`QuoteRepository.get_line`) requires
the parent `quote_id` up front, but `docs/planning/03-api-contract.md`
§4.12's literal routes address a quote line directly
(`POST/DELETE /quote-lines/{id}/match`, no `quote_id` in the path at all).
`QuoteRepository` is not on this task's edit allowlist, so rather than widen
its API, this file adds the one missing "by id alone" lookup as its own
tiny, non-duplicative repository bound to the same `QuoteLine` model -
`services/matching_service.py` reads `line.quote_id` off the fetched row to
reach everything else (`Quote`, its RFQ, its price breaks) through the
existing `QuoteRepository`/`RfqRepository`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete

from app.models.documents import PartMatchCandidate
from app.models.quotes import QuoteLine
from app.repositories.base import OrgScopedRepository


class MatchingRepository(OrgScopedRepository[PartMatchCandidate]):
    model = PartMatchCandidate

    def list_for_quote_line(self, quote_line_id: uuid.UUID) -> list[PartMatchCandidate]:
        stmt = (
            self._base_query()
            .where(PartMatchCandidate.quote_line_id == quote_line_id)
            .order_by(PartMatchCandidate.rank)
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_for_quote_lines(
        self, quote_line_ids: Sequence[uuid.UUID]
    ) -> list[PartMatchCandidate]:
        if not quote_line_ids:
            return []
        stmt = (
            self._base_query()
            .where(PartMatchCandidate.quote_line_id.in_(quote_line_ids))
            .order_by(PartMatchCandidate.quote_line_id, PartMatchCandidate.rank)
        )
        return list(self.session.execute(stmt).scalars().all())

    def delete_unconfirmed_for_line(self, quote_line_id: uuid.UUID) -> None:
        """Regeneration replaces the unconfirmed candidate set for a line
        (04-document-pipeline.md §10: "a confirmed match is sticky; re-
        running matching does not overwrite human_confirmed rows") - deletes
        every row for this line with `human_confirmed = False`, leaving any
        already-confirmed (including auto-confirmed strategy-1) rows
        untouched."""
        stmt = delete(PartMatchCandidate).where(
            PartMatchCandidate.organization_id == self.organization_id,
            PartMatchCandidate.quote_line_id == quote_line_id,
            PartMatchCandidate.human_confirmed.is_(False),
        )
        self.session.execute(stmt)


class QuoteLineRepository(OrgScopedRepository[QuoteLine]):
    """See module docstring: the one "by id alone" lookup
    `QuoteRepository.get_line` does not offer - `OrgScopedRepository.get`/
    `.get_or_raise` already do exactly this for any `ModelT`, so no override
    is needed; this class exists purely to bind that base behavior to
    `QuoteLine`."""

    model = QuoteLine


__all__ = ["MatchingRepository", "QuoteLineRepository"]
