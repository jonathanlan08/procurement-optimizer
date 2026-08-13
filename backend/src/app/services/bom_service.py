"""BOM service — org-scoped business logic for copy-on-write BOM
versioning (docs/planning/02-erd.md §5/§10, app/models/boms.py module
docstring, docs/planning/03-api-contract.md §4.7).

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception. Every mutation
writes exactly one audit event in the same transaction as the data change it
describes.

**Status-transition design (contract gap, filled here — see this task's
brief: "if genuinely undefined: draft on create, activate endpoint promotes
draft→active and old active→superseded; document it").**

03-api-contract.md §4.7 lists `PATCH /boms/{id}` ("metadata only while
draft; line edits on an active BOM require a new version") but never
actually specifies *how* a BOM moves from `draft` to `active` in the first
place, nor when the predecessor it replaces becomes `superseded` — RFQs get
a whole status-transition table (§4.8) and BOMs do not. `app/models/boms.py`
(`BomStatus`) confirms the four states (`draft|active|superseded|archived`)
exist but is likewise silent on the transition rules. This task's brief
explicitly asks for a `POST /boms/{id}/activate` route to fill that gap
(not present in the contract's literal route table, the same kind of
contract-silent-but-reasonable addition already made for
`POST /parts/{id}/unarchive` in `api/v1/parts.py` — see that module's
docstring for the identical judgement call). The design landed on:

1. `create()` — always produces `version_number=1`, `status=draft`,
   `previous_version_id=None`.
2. `new_version()` — copy-on-write fork. May only be called on the
   *current head* of a root's version chain (`BomRepository.
   is_latest_version`) and only while that head is `draft` or `active`
   (never `superseded` — that state, by definition, is not a head — or
   `archived` — a deliberately retired BOM should not silently grow a new
   descendant). The new row is always `version_number = head.version_number
   + 1`, `previous_version_id = head.id`, `status = draft`. The predecessor
   is **not** touched yet — it keeps whatever status it had (`draft` or
   `active`) until the new version is explicitly activated. This means two
   drafts can legally exist back-to-back in a chain (iterating before ever
   publishing), which the contract's silence does not forbid.
3. `activate()` — the only way a BOM version becomes `active`. Requires
   `status == draft` (activating an already-active/superseded/archived
   version is a `409 conflict_state` — there is nothing to promote). If the
   version has a predecessor (`previous_version_id is not None`), that
   predecessor is moved to `superseded` in the same transaction (unless it
   was independently `archived`, which is left alone — an operator's
   explicit archive decision is not silently overwritten by an unrelated
   activation). This is exactly this task's brief: "marks the old version
   superseded when the new one is activated."
4. `archive()` — mirrors `PartService.archive`: sets `archived_at`/
   `archived_by_id`/`archive_reason` (ArchivableMixin) **and**
   `status = archived` (BOMs, unlike `Part`/`Supplier`, encode "archived"
   as its own enum member as well as via the soft-delete columns — both are
   kept in sync). Works on any non-archived version, not only the head:
   the contract does not restrict which version may be archived, and
   retiring a specific bad version without touching the rest of the chain
   is a reasonable operation to allow.

Audit events are exactly the four this task's brief names —
`bom.created`, `bom.version_created`, `bom.activated`, `bom.archived` —
and no more: activation's side effect on the predecessor (marking it
`superseded`) is folded into the `bom.activated` event's `explanation`
rather than becoming its own `bom.superseded` event, since the brief's
audit list does not include one and the mutation happens in the same
transaction as (and only as a consequence of) the activation.

**Concurrent versioning race.** `uq_bills_of_materials_org_root_version`
(migration 0005) is the DB-level guard the model docstring calls "the
concurrency guard for 'two analysts create the next version at once' — the
loser's INSERT simply fails the unique constraint." `new_version()` catches
that `IntegrityError` and reports it as `ConflictStateError` (409
`conflict_state`), not `ConflictDuplicateError`: the loser did not attempt
to create an intentional duplicate the way a repeated supplier code would
be — the BOM's head moved out from under it mid-request, which is a state
race, not a business-rule duplicate. There is no `If-Match`/`version`
counter on `BillOfMaterials` to make this a `ConflictVersionError` either
(the model docstring is explicit: "No optimistic-lock `version` column on
`BillOfMaterials`" — the copy-on-write chain itself is the concurrency
control), so `conflict_state` is the closest fit in the error table (§3:
"illegal state transition").
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import ConflictStateError, ErrorDetail, NotFoundError, ValidationAppError
from app.core.ids import IdGenerator
from app.core.money import quantize_qty
from app.models.boms import BillOfMaterialLine, BillOfMaterials, BomStatus
from app.models.units import UnitDefinition
from app.repositories.bom_repository import BomRepository
from app.repositories.part_repository import PartRepository
from app.schemas.boms import BomCreate, BomLineCreate, BomVersionCreate
from app.services.audit import AuditRecorder

# Module-level aliases, not inline `list[...]` annotations on methods below:
# BomService defines a method named `list` (mirrors PartService), which (per
# `from __future__ import annotations`) shadows the builtin `list` for any
# bare `list[...]` written later in the same class body — mypy then reads it
# as "BomService.list used as a type" instead of the builtin generic (see
# app/services/part_service.py for the identical precedent/comment).
_BomWithLines = tuple[BillOfMaterials, list[BillOfMaterialLine]]
_BomPage = tuple[list[BillOfMaterials], int]
_VersionChain = list[BillOfMaterials]


def _state(bom: BillOfMaterials, lines: list[BillOfMaterialLine]) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "root_bom_id": str(bom.root_bom_id),
        "version_number": bom.version_number,
        "previous_version_id": (
            str(bom.previous_version_id) if bom.previous_version_id is not None else None
        ),
        "name": bom.name,
        "product_name": bom.product_name,
        "status": bom.status.value,
        "notes": bom.notes,
        "is_archived": bom.is_archived,
        "archived_at": (bom.archived_at.isoformat() if bom.archived_at else None),
        "archive_reason": bom.archive_reason,
        "lines": [
            {
                "line_number": line.line_number,
                "part_id": str(line.part_id),
                "quantity_per_assembly": str(line.quantity_per_assembly),
                "unit_definition_id": str(line.unit_definition_id),
                "is_optional": line.is_optional,
                "substitute_part_id": (
                    str(line.substitute_part_id) if line.substitute_part_id is not None else None
                ),
                "notes": line.notes,
            }
            for line in lines
        ],
    }


class BomService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._repo = BomRepository(session, organization_id)
        self._part_repo = PartRepository(session, organization_id)

    # -- unit_definition_id cross-check (mirrors PartService) --------------

    def _validate_unit_definition(self, unit_definition_id: uuid.UUID, *, field: str) -> None:
        unit = self._db.execute(
            select(UnitDefinition).where(UnitDefinition.id == unit_definition_id)
        ).scalar_one_or_none()
        if unit is None:
            raise ValidationAppError(
                "unit_definition_id does not reference an existing unit.",
                details=[ErrorDetail(field=field, issue="does not reference an existing unit")],
            )
        if unit.organization_id is not None and unit.organization_id != self._organization_id:
            raise ValidationAppError(
                "unit_definition_id references another organization's private unit.",
                details=[
                    ErrorDetail(
                        field=field, issue="references another organization's private unit"
                    )
                ],
            )

    # -- line construction ---------------------------------------------

    def _build_line(
        self, index: int, body: BomLineCreate, bom_id: uuid.UUID
    ) -> BillOfMaterialLine:
        # in-org existence check, never 403 (§1.1): absent or other-org part
        # id is indistinguishable and always 404.
        part = self._part_repo.get(body.part_id)
        if part is None:
            raise NotFoundError("Part")

        if body.unit_definition_id is not None:
            self._validate_unit_definition(
                body.unit_definition_id, field=f"lines[{index}].unit_definition_id"
            )
            unit_definition_id = body.unit_definition_id
        else:
            # default: the part's own unit (already validated when the part
            # itself was created/updated, so no re-check needed here).
            unit_definition_id = part.unit_definition_id

        if body.substitute_part_id is not None:
            if body.substitute_part_id == body.part_id:
                raise ValidationAppError(
                    f"lines[{index}].substitute_part_id cannot equal part_id.",
                    details=[
                        ErrorDetail(
                            field=f"lines[{index}].substitute_part_id",
                            issue="cannot equal part_id",
                        )
                    ],
                )
            if self._part_repo.get(body.substitute_part_id) is None:
                raise NotFoundError("Part")

        return BillOfMaterialLine(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            bom_id=bom_id,
            line_number=index + 1,
            part_id=body.part_id,
            quantity_per_assembly=quantize_qty(body.quantity_per_assembly),
            unit_definition_id=unit_definition_id,
            is_optional=body.is_optional,
            substitute_part_id=body.substitute_part_id,
            notes=body.notes,
        )

    # -- reads ---------------------------------------------------------

    def get(self, bom_id: uuid.UUID) -> _BomWithLines:
        bom = self._repo.get_or_raise(bom_id)
        return bom, self._repo.list_lines(bom.id)

    def list(
        self,
        *,
        q: str | None = None,
        all_versions: bool = False,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> _BomPage:
        items = self._repo.search(
            q=q,
            all_versions=all_versions,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        total = self._repo.count_matching(
            q=q,
            all_versions=all_versions, include_archived=include_archived
        )
        return items, total

    def version_chain(self, bom_id: uuid.UUID) -> _VersionChain:
        """Every version of the root chain that `bom_id` belongs to
        (ascending). `bom_id` may be any version's id, not only the root's
        own — the root's v1 row happens to share its id with `root_bom_id`,
        so passing either resolves to the same chain."""
        head = self._repo.get_or_raise(bom_id)
        return self._repo.get_version_chain(head.root_bom_id)

    # -- mutations ---------------------------------------------------------

    def create(self, body: BomCreate, *, actor_id: uuid.UUID) -> _BomWithLines:
        now = self._clock.now()
        bom_id = self._ids.new_id()
        bom = BillOfMaterials(
            id=bom_id,
            organization_id=self._organization_id,
            root_bom_id=bom_id,  # self-referential: v1 is its own root
            version_number=1,
            previous_version_id=None,
            name=body.name,
            product_name=body.product_name,
            status=BomStatus.DRAFT,
            notes=body.notes,
            created_by_id=actor_id,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(bom)
        # flush the BOM row before building lines: bill_of_material_lines'
        # composite FK targets (organization_id, bom_id) on this same row,
        # and the row must actually exist (not merely be pending in the
        # session) before that reference is meaningful.
        self._db.flush()

        lines = [self._build_line(i, ln, bom_id) for i, ln in enumerate(body.lines)]
        for line in lines:
            self._repo.add_line(line)
        self._db.flush()

        self._audit.record(
            "bom.created",
            entity_type="bom",
            entity_id=bom.id,
            after_state=_state(bom, lines),
            explanation=f"BOM '{bom.name}' created",
        )
        return bom, lines

    def new_version(
        self, bom_id: uuid.UUID, body: BomVersionCreate, *, actor_id: uuid.UUID
    ) -> _BomWithLines:
        head = self._repo.get_or_raise(bom_id)
        if head.status not in (BomStatus.DRAFT, BomStatus.ACTIVE):
            raise ConflictStateError(
                "A new version can only be created from a draft or active BOM version."
            )
        if not self._repo.is_latest_version(head):
            raise ConflictStateError(
                "This is not the latest version of this BOM; fetch the current "
                "head and create the next version from it."
            )

        head_lines = self._repo.list_lines(head.id)
        before = _state(head, head_lines)

        now = self._clock.now()
        new_id = self._ids.new_id()
        new_bom = BillOfMaterials(
            id=new_id,
            organization_id=self._organization_id,
            root_bom_id=head.root_bom_id,
            version_number=head.version_number + 1,
            previous_version_id=head.id,
            name=body.name if body.name is not None else head.name,
            product_name=(
                body.product_name if body.product_name is not None else head.product_name
            ),
            status=BomStatus.DRAFT,
            notes=body.notes if body.notes is not None else head.notes,
            created_by_id=actor_id,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(new_bom)
        try:
            self._db.flush()
        except IntegrityError as exc:
            # uq_bills_of_materials_org_root_version: another version was
            # forked from this same head concurrently (see module docstring
            # "Concurrent versioning race").
            raise ConflictStateError(
                "Another version was created from this BOM concurrently; reload and retry."
            ) from exc

        lines = [self._build_line(i, ln, new_id) for i, ln in enumerate(body.lines)]
        for line in lines:
            self._repo.add_line(line)
        self._db.flush()

        self._audit.record(
            "bom.version_created",
            entity_type="bom",
            entity_id=new_bom.id,
            before_state=before,
            after_state=_state(new_bom, lines),
            explanation=(
                f"BOM '{new_bom.name}' version {new_bom.version_number} created "
                f"from version {head.version_number}"
            ),
        )
        return new_bom, lines

    def activate(self, bom_id: uuid.UUID) -> _BomWithLines:
        bom = self._repo.get_or_raise(bom_id)
        if bom.status != BomStatus.DRAFT:
            raise ConflictStateError("Only a draft BOM version can be activated.")

        lines = self._repo.list_lines(bom.id)
        before = _state(bom, lines)
        now = self._clock.now()
        bom.status = BomStatus.ACTIVE
        bom.updated_at = now

        previous: BillOfMaterials | None = None
        if bom.previous_version_id is not None:
            previous = self._repo.get_or_raise(bom.previous_version_id)
            if previous.status != BomStatus.ARCHIVED:
                previous.status = BomStatus.SUPERSEDED
                previous.updated_at = now

        self._db.flush()

        explanation = f"BOM '{bom.name}' version {bom.version_number} activated"
        if previous is not None:
            explanation += f"; version {previous.version_number} marked superseded"

        self._audit.record(
            "bom.activated",
            entity_type="bom",
            entity_id=bom.id,
            before_state=before,
            after_state=_state(bom, lines),
            explanation=explanation,
        )
        return bom, lines

    def archive(self, bom_id: uuid.UUID, *, reason: str, actor_id: uuid.UUID) -> _BomWithLines:
        bom = self._repo.get_or_raise(bom_id)
        if bom.is_archived:
            raise ConflictStateError("BOM is already archived.")

        lines = self._repo.list_lines(bom.id)
        before = _state(bom, lines)
        now = self._clock.now()
        bom.archived_at = now
        bom.archived_by_id = actor_id
        bom.archive_reason = reason
        bom.status = BomStatus.ARCHIVED
        bom.updated_at = now
        self._db.flush()

        self._audit.record(
            "bom.archived",
            entity_type="bom",
            entity_id=bom.id,
            before_state=before,
            after_state=_state(bom, lines),
            explanation=f"BOM '{bom.name}' version {bom.version_number} archived: {reason}",
        )
        return bom, lines


__all__ = ["BomService"]
