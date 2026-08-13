"""BOM repository — org-scoped data access, copy-on-write aware
(docs/planning/02-erd.md §5 / §10, app/models/boms.py module docstring).

Extends OrgScopedRepository, so every query is filtered by organization_id
before any other predicate (isolation control #2).

Listing defaults to "latest version per root_bom_id" (03-api-contract.md
§4.7: `GET /boms | latest version per root_bom_id by default; ?all_versions=
true`). "Latest" means highest `version_number` for that root, computed with
a `GROUP BY root_bom_id` subquery and joined back onto the full row — not
"latest non-archived version": if the current head happens to be archived
and the caller has not passed `include_archived=true`, that root simply does
not appear in the page, the same way an archived `Part`/`Supplier` simply
does not appear in their default list. There is no fallback to an older,
non-archived version of the same root — that would silently show the caller
something other than the actual current state of the BOM.

`BillOfMaterialLine` has no ORM relationship back to `BillOfMaterials` (see
app/models/boms.py: the relationships defined there are all one-directional,
`BillOfMaterialLine.bom`, with no `back_populates`/backref on the
`BillOfMaterials` side) — this task's edit allowlist does not include
models/boms.py, so lines are fetched here with their own explicit queries
("line helpers" per this task's brief) rather than via eager-loading a
relationship attribute that does not exist.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select

from app.models.boms import BillOfMaterialLine, BillOfMaterials
from app.repositories.base import OrgIsolationViolation, OrgScopedRepository


class BomRepository(OrgScopedRepository[BillOfMaterials]):
    model = BillOfMaterials

    # -- listing -------------------------------------------------------

    def _latest_version_subquery(self) -> Any:
        """One row per `root_bom_id` in this org, carrying that root's
        highest `version_number`."""
        return (
            select(
                BillOfMaterials.root_bom_id.label("root_bom_id"),
                func.max(BillOfMaterials.version_number).label("max_version"),
            )
            .where(BillOfMaterials.organization_id == self.organization_id)
            .group_by(BillOfMaterials.root_bom_id)
            .subquery()
        )

    def _scoped_statement(
        self, *, all_versions: bool, include_archived: bool
    ) -> Select[tuple[BillOfMaterials]]:
        stmt = self._base_query()
        if not all_versions:
            latest = self._latest_version_subquery()
            stmt = stmt.join(
                latest,
                (BillOfMaterials.root_bom_id == latest.c.root_bom_id)
                & (BillOfMaterials.version_number == latest.c.max_version),
            )
        if not include_archived:
            stmt = stmt.where(BillOfMaterials.archived_at.is_(None))
        return stmt

    def search(
        self,
        *,
        q: str | None = None,
        all_versions: bool = False,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BillOfMaterials]:
        stmt = self._scoped_statement(all_versions=all_versions, include_archived=include_archived)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    BillOfMaterials.name.ilike(pattern),
                    BillOfMaterials.product_name.ilike(pattern),
                )
            )
        stmt = (
            stmt.order_by(BillOfMaterials.root_bom_id, BillOfMaterials.version_number.desc())
            .limit(min(limit, 200))
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_matching(
        self,
        *,
        q: str | None = None,
        all_versions: bool = False,
        include_archived: bool = False,
    ) -> int:
        stmt = self._scoped_statement(all_versions=all_versions, include_archived=include_archived)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    BillOfMaterials.name.ilike(pattern),
                    BillOfMaterials.product_name.ilike(pattern),
                )
            )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        return int(self.session.execute(count_stmt).scalar_one())

    # -- version chain ---------------------------------------------------

    def get_version_chain(self, root_bom_id: uuid.UUID) -> list[BillOfMaterials]:
        """Every version of one BOM, ascending by `version_number` (v1
        first). Org-scoped like every other query here; an empty list means
        either no such root exists or it belongs to another organization —
        callers translate that to a 404, never a 403 (§1.1)."""
        stmt = (
            self._base_query()
            .where(BillOfMaterials.root_bom_id == root_bom_id)
            .order_by(BillOfMaterials.version_number)
        )
        return list(self.session.execute(stmt).scalars().all())

    def is_latest_version(self, bom: BillOfMaterials) -> bool:
        """True when `bom` is the current head of its root's version chain —
        the only version a new version may be forked from (BomService.new_version)."""
        stmt = select(func.max(BillOfMaterials.version_number)).where(
            BillOfMaterials.organization_id == self.organization_id,
            BillOfMaterials.root_bom_id == bom.root_bom_id,
        )
        max_version = self.session.execute(stmt).scalar_one()
        return bool(bom.version_number == max_version)

    # -- lines -------------------------------------------------------------

    def list_lines(self, bom_id: uuid.UUID) -> list[BillOfMaterialLine]:
        stmt = (
            select(BillOfMaterialLine)
            .where(
                BillOfMaterialLine.organization_id == self.organization_id,
                BillOfMaterialLine.bom_id == bom_id,
            )
            .order_by(BillOfMaterialLine.line_number)
        )
        return list(self.session.execute(stmt).scalars().all())

    def add_line(self, line: BillOfMaterialLine) -> BillOfMaterialLine:
        if line.organization_id != self.organization_id:
            raise OrgIsolationViolation(
                f"attempted to add BillOfMaterialLine with organization_id="
                f"{line.organization_id!r} through a repository scoped to "
                f"{self.organization_id}"
            )
        self.session.add(line)
        return line


__all__ = ["BomRepository"]
