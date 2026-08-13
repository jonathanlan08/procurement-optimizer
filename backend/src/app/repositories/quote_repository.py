"""Quote repository - org-scoped data access (docs/planning/02-erd.md §6,
app/models/quotes.py).

Extends OrgScopedRepository, so every query is filtered by organization_id
before any other predicate (isolation control #2).

Mirrors RfqRepository's split between "the aggregate itself" (`Quote`, bound
as `ModelT`) and its child rows (`QuoteLine`, `QuotePriceBreak`,
`QuoteTerms`), fetched with their own explicit org-scoped queries via the
same `_select`/`_guard_org` internals - none of the four models here carry a
`back_populates`/backref collection on their parent (see app/models/quotes.py:
every relationship defined there is one-directional, forward-only), so there
is nothing to eager-load through.

Default listing (`list_by_rfq`) excludes superseded quotes unless
`include_superseded=True` is passed (this task's brief: "list by rfq
(exclude superseded by default)"). The manual-supersession chain
(app/models/quotes.py module docstring point 2) means an RFQ can accumulate
several superseded revisions per supplier over time, and a client asking
"what quotes are currently live against this RFQ" should not have to filter
those out itself.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, select

from app.models.base import OrgOwnedBase
from app.models.quotes import Quote, QuoteLine, QuotePriceBreak, QuoteStatus, QuoteTerms
from app.repositories.base import OrgIsolationViolation, OrgScopedRepository


class QuoteRepository(OrgScopedRepository[Quote]):
    model = Quote

    # -- quotes ------------------------------------------------------------

    def list_by_rfq(
        self, rfq_id: uuid.UUID, *, include_superseded: bool = False
    ) -> list[Quote]:
        stmt = self._base_query().where(Quote.rfq_id == rfq_id)
        if not include_superseded:
            stmt = stmt.where(Quote.status != QuoteStatus.SUPERSEDED)
        stmt = stmt.order_by(Quote.created_at.desc())
        return list(self.session.execute(stmt).scalars().all())

    # -- lines ---------------------------------------------------------

    def list_lines(self, quote_id: uuid.UUID) -> list[QuoteLine]:
        stmt = (
            self._select(QuoteLine)
            .where(QuoteLine.quote_id == quote_id)
            .order_by(QuoteLine.line_number)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_line(self, quote_id: uuid.UUID, line_id: uuid.UUID) -> QuoteLine | None:
        stmt = self._select(QuoteLine).where(
            QuoteLine.id == line_id, QuoteLine.quote_id == quote_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def add_line(self, line: QuoteLine) -> QuoteLine:
        self._guard_org(line)
        self.session.add(line)
        return line

    def count_lines(self, quote_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(QuoteLine).where(
            QuoteLine.quote_id == quote_id, QuoteLine.organization_id == self.organization_id
        )
        return int(self.session.execute(stmt).scalar_one())

    # -- price breaks --------------------------------------------------

    def list_price_breaks(self, quote_line_id: uuid.UUID) -> list[QuotePriceBreak]:
        stmt = (
            self._select(QuotePriceBreak)
            .where(QuotePriceBreak.quote_line_id == quote_line_id)
            .order_by(QuotePriceBreak.min_quantity)
        )
        return list(self.session.execute(stmt).scalars().all())

    def add_price_break(self, price_break: QuotePriceBreak) -> QuotePriceBreak:
        self._guard_org(price_break)
        self.session.add(price_break)
        return price_break

    # -- terms -----------------------------------------------------------

    def get_terms(self, quote_id: uuid.UUID) -> QuoteTerms | None:
        stmt = self._select(QuoteTerms).where(QuoteTerms.quote_id == quote_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def add_terms(self, terms: QuoteTerms) -> QuoteTerms:
        self._guard_org(terms)
        self.session.add(terms)
        return terms

    # -- internals -------------------------------------------------------

    def _select(self, model: type[OrgOwnedBase]) -> Select[Any]:
        """Org-scoped `select(model)` for the child tables above: none of
        them are `ModelT` (the repository is generic over `Quote`), so
        `_base_query()` cannot be reused directly - same pattern as
        RfqRepository._select."""
        return select(model).where(model.organization_id == self.organization_id)

    def _guard_org(self, entity: OrgOwnedBase) -> None:
        if entity.organization_id != self.organization_id:
            raise OrgIsolationViolation(
                f"attempted to add {type(entity).__name__} with organization_id="
                f"{entity.organization_id!r} through a repository scoped to "
                f"{self.organization_id}"
            )


__all__ = ["QuoteRepository"]
