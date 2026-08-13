"""RFQ repository - org-scoped data access (docs/planning/02-erd.md §5,
app/models/rfqs.py).

Extends OrgScopedRepository, so every query is filtered by organization_id
before any other predicate (isolation control #2).

Mirrors `BomRepository`'s split between "the aggregate itself" and "child rows
fetched with their own explicit queries" - `RfqLine`, `RfqSupplier`, and
`RfqStatusHistory` all carry a one-directional `.rfq` relationship (see
app/models/rfqs.py: no `back_populates`/backref on the `Rfq` side), so lines,
supplier invitations, and status-history rows are queried here directly
rather than via an eager-loaded collection attribute that does not exist.

Default listing hides archived RFQs (`archived_at IS NULL`) unless
`include_archived=True` is passed - the filter list
(03-api-contract.md §4.8: "filters status[], q, due_before, due_after") does
not name `include_archived` for RFQs specifically, but §1.4's blanket rule
("include_archived=true required to see soft-deleted rows") is general and
`Rfq` mixes in `ArchivableMixin` exactly like every other archivable
resource in this codebase, so the same behavior is applied here rather than
carved out as an exception.

**`search()` returns `(Rfq, line_count)` pairs, not bare `Rfq` rows**
(2026-08 product-audit remediation, P2: "Lines column always displays a
dash because the summary API omits the count"). `line_count` is joined on
via `_line_count_subquery()` - one `GROUP BY rfq_id` query producing a
`(rfq_id, line_count)` row per RFQ that has any lines, outer-joined onto the
paged RFQ rows with `COALESCE(..., 0)` for RFQs with none - so the list
endpoint gets every row's line count from the *same* query that fetches the
page, not one extra `count(*)` per row (`BomRepository.
_latest_version_subquery` is the closest existing precedent for "a
`GROUP BY` subquery joined onto the main listing query" in this codebase,
though that one filters rows rather than annotating them with a count).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Select, func, select

from app.models.base import OrgOwnedBase
from app.models.rfqs import Rfq, RfqLine, RfqStatus, RfqStatusHistory, RfqSupplier
from app.repositories.base import OrgIsolationViolation, OrgScopedRepository


class RfqRepository(OrgScopedRepository[Rfq]):
    model = Rfq

    # -- lookups ---------------------------------------------------------

    def get_by_internal_reference(self, internal_reference: str) -> Rfq | None:
        """Case-insensitive lookup among ACTIVE (non-archived) RFQs only.

        Mirrors the partial unique index `uq_rfqs_org_ref_active` (migration
        0007: unique on `lower(internal_reference)` WHERE `archived_at IS
        NULL`). Archiving an RFQ frees its reference for reuse.
        """
        stmt = self._base_query().where(
            func.lower(Rfq.internal_reference) == internal_reference.strip().lower(),
            Rfq.archived_at.is_(None),
        )
        return self.session.execute(stmt).scalar_one_or_none()

    # -- listing -----------------------------------------------------------

    def _line_count_subquery(self) -> Any:
        """One row per `rfq_id` in this org that has at least one line,
        carrying that RFQ's line count. Mirrors `BomRepository.
        _latest_version_subquery`'s shape (a `GROUP BY` subquery meant to be
        joined onto a listing query), but counts rather than filters."""
        return (
            select(
                RfqLine.rfq_id.label("rfq_id"),
                func.count().label("line_count"),
            )
            .where(RfqLine.organization_id == self.organization_id)
            .group_by(RfqLine.rfq_id)
            .subquery()
        )

    def search(
        self,
        *,
        statuses: list[RfqStatus] | None = None,
        q: str | None = None,
        due_before: date | None = None,
        due_after: date | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[tuple[Rfq, int]]:
        """Returns `(Rfq, line_count)` pairs - see module docstring. A
        single query (outer join onto a `GROUP BY` subquery), not
        `list_page()` + a per-row follow-up count, so this stays O(1)
        round trips regardless of page size."""
        clauses = self._filters(
            statuses=statuses,
            q=q,
            due_before=due_before,
            due_after=due_after,
            include_archived=include_archived,
        )
        counts = self._line_count_subquery()
        stmt = (
            self._base_query()
            .where(*clauses)
            .add_columns(func.coalesce(counts.c.line_count, 0))
            .outerjoin(counts, counts.c.rfq_id == Rfq.id)
            .order_by(Rfq.created_at.desc())
            .limit(min(limit, 200))
            .offset(offset)
        )
        rows = self.session.execute(stmt).all()
        return [(row[0], int(row[1])) for row in rows]

    def count_matching(
        self,
        *,
        statuses: list[RfqStatus] | None = None,
        q: str | None = None,
        due_before: date | None = None,
        due_after: date | None = None,
        include_archived: bool = False,
    ) -> int:
        clauses = self._filters(
            statuses=statuses,
            q=q,
            due_before=due_before,
            due_after=due_after,
            include_archived=include_archived,
        )
        return self.count(*clauses)

    def _filters(
        self,
        *,
        statuses: list[RfqStatus] | None,
        q: str | None,
        due_before: date | None,
        due_after: date | None,
        include_archived: bool,
    ) -> list[Any]:
        clauses: list[Any] = []
        if not include_archived:
            clauses.append(Rfq.archived_at.is_(None))
        if statuses:
            clauses.append(Rfq.status.in_(statuses))
        if q:
            term = f"%{q.strip()}%"
            clauses.append(
                (Rfq.name.ilike(term)) | (Rfq.internal_reference.ilike(term))
            )
        if due_before is not None:
            clauses.append(Rfq.due_date <= due_before)
        if due_after is not None:
            clauses.append(Rfq.due_date >= due_after)
        return clauses

    # -- lines ---------------------------------------------------------

    def list_lines(self, rfq_id: uuid.UUID) -> list[RfqLine]:
        stmt = (
            self._select(RfqLine)
            .where(RfqLine.rfq_id == rfq_id)
            .order_by(RfqLine.line_number)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_line(self, rfq_id: uuid.UUID, line_id: uuid.UUID) -> RfqLine | None:
        stmt = self._select(RfqLine).where(RfqLine.id == line_id, RfqLine.rfq_id == rfq_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def max_line_number(self, rfq_id: uuid.UUID) -> int:
        stmt = self._select_scalar(func.max(RfqLine.line_number)).where(
            RfqLine.rfq_id == rfq_id
        )
        value = self.session.execute(stmt).scalar_one()
        return int(value) if value is not None else 0

    def add_line(self, line: RfqLine) -> RfqLine:
        self._guard_org(line)
        self.session.add(line)
        return line

    def delete_line(self, line: RfqLine) -> None:
        self.session.delete(line)

    def count_lines(self, rfq_id: uuid.UUID) -> int:
        stmt = self._select_scalar(func.count()).select_from(RfqLine).where(
            RfqLine.rfq_id == rfq_id, RfqLine.organization_id == self.organization_id
        )
        return int(self.session.execute(stmt).scalar_one())

    # -- suppliers -----------------------------------------------------

    def list_suppliers(self, rfq_id: uuid.UUID) -> list[RfqSupplier]:
        stmt = (
            self._select(RfqSupplier)
            .where(RfqSupplier.rfq_id == rfq_id)
            .order_by(RfqSupplier.invited_at)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_supplier(
        self, rfq_id: uuid.UUID, rfq_supplier_id: uuid.UUID
    ) -> RfqSupplier | None:
        stmt = self._select(RfqSupplier).where(
            RfqSupplier.id == rfq_supplier_id, RfqSupplier.rfq_id == rfq_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_supplier_by_supplier_id(
        self, rfq_id: uuid.UUID, supplier_id: uuid.UUID
    ) -> RfqSupplier | None:
        """Existing invitation row for this (rfq, supplier) pair, if any -
        mirrors `uq_rfq_suppliers_org_rfq_supplier` so the service can raise
        a clean 409 before ever reaching the database constraint."""
        stmt = self._select(RfqSupplier).where(
            RfqSupplier.rfq_id == rfq_id, RfqSupplier.supplier_id == supplier_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def add_supplier(self, rfq_supplier: RfqSupplier) -> RfqSupplier:
        self._guard_org(rfq_supplier)
        self.session.add(rfq_supplier)
        return rfq_supplier

    def count_invited_suppliers(self, rfq_id: uuid.UUID) -> int:
        """Suppliers currently invited (not excluded)."""
        stmt = self._select_scalar(func.count()).select_from(RfqSupplier).where(
            RfqSupplier.rfq_id == rfq_id,
            RfqSupplier.organization_id == self.organization_id,
            RfqSupplier.excluded_at.is_(None),
        )
        return int(self.session.execute(stmt).scalar_one())

    # -- status history --------------------------------------------------

    def add_status_history(self, entry: RfqStatusHistory) -> RfqStatusHistory:
        self._guard_org(entry)
        self.session.add(entry)
        return entry

    def list_status_history(self, rfq_id: uuid.UUID) -> list[RfqStatusHistory]:
        stmt = (
            self._select(RfqStatusHistory)
            .where(RfqStatusHistory.rfq_id == rfq_id)
            .order_by(RfqStatusHistory.occurred_at)
        )
        return list(self.session.execute(stmt).scalars().all())

    # -- internals -------------------------------------------------------

    def _select(self, model: type[OrgOwnedBase]) -> Select[Any]:
        """Org-scoped `select(model)` for the child tables above: none of
        them are `ModelT` (the repository is generic over `Rfq`), so
        `_base_query()` cannot be reused directly."""
        return select(model).where(model.organization_id == self.organization_id)

    def _select_scalar(self, expr: Any) -> Select[Any]:
        return select(expr)

    def _guard_org(self, entity: OrgOwnedBase) -> None:
        if entity.organization_id != self.organization_id:
            raise OrgIsolationViolation(
                f"attempted to add {type(entity).__name__} with organization_id="
                f"{entity.organization_id!r} through a repository scoped to "
                f"{self.organization_id}"
            )


__all__ = ["RfqRepository"]
