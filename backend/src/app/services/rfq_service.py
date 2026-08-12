"""RFQ service — org-scoped business logic for RFQ CRUD, the status
workflow, invitations, and BOM explosion (docs/planning/03-api-contract.md
§4.8, app/models/rfqs.py module docstring — `ALLOWED_RFQ_TRANSITIONS` is the
single source of truth for the status machine, imported here rather than
re-encoded).

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception. Every mutation
writes exactly one audit event in the same transaction as the data change it
describes (this task's brief: `rfq.created/updated/line_added/line_updated/
line_removed/supplier_invited/supplier_excluded/supplier_reinstated/
status_changed`) — bulk operations (`add_lines`, `invite_suppliers`) write one
event *per row added*, not one event for the whole batch: each row is its own
audited fact, the same granularity `PartService.add_alternative` already
established for a single-row sub-resource create.

**Draft-only mutation gate with an administrator override.** 03-api-
contract.md §4.8's `PATCH /rfqs/{id}` row: "line edits blocked once `status !=
draft` unless `A`/`O` with `{override_reason}` (audited)". `_ensure_draft_or_
override` is the one gate both `update()` (RFQ metadata) and every line
mutation (`add_lines`/`update_line`/`remove_line`) share, so there is exactly
one place this rule is encoded, mirroring how `ALLOWED_RFQ_TRANSITIONS` is the
one place the status machine is encoded.

**BOM explosion.** `create()` with `body.source_bom_id` set resolves the BOM
in-org (404 if absent/cross-org — never 403, §1.1), requires `status ==
active` (else `409 conflict_state` — a draft or superseded BOM is not a
released bill of materials to quote against), then copies every line of that
BOM version into `rfq_lines` with `required_quantity = quantity_per_assembly
* assembly_quantity` (assembly_quantity defaults to `Decimal("1")` when
omitted, per this task's brief). The multiplication runs at full precision
inside `app.core.money.CALC_CONTEXT` (via `decimal.localcontext`, the same
mechanism `quantize()` itself uses internally) and is quantized once at
QTY_SCALE at the boundary, per the decimal policy in `app/core/money.py`'s
module docstring ("arithmetic runs at full precision inside a formula; each
result is quantized once at its boundary scale"). `BillOfMaterialLine.substitute_
part_id` has no equivalent column on `RfqLine` (see app/models/rfqs.py
module docstring point #3's field-list deviation from the ERD) and is
therefore simply dropped, not carried forward as a `notes` annotation or
otherwise — a deliberate consequence of that already-documented schema
choice, not a new one made here.

**Supplier sub-resource id.** Every method taking `rfq_supplier_id` addresses
the `RfqSupplier` join row's own id (see app/schemas/rfqs.py module
docstring) — never the invited `Supplier`'s id.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import (
    ConflictDuplicateError,
    ConflictStateError,
    ConflictVersionError,
    ErrorDetail,
    NotFoundError,
    ValidationAppError,
)
from app.core.ids import IdGenerator
from app.core.money import CALC_CONTEXT, quantize_qty
from app.models.boms import BillOfMaterialLine, BillOfMaterials, BomStatus
from app.models.identity import Role
from app.models.rfqs import (
    ALLOWED_RFQ_TRANSITIONS,
    Rfq,
    RfqLine,
    RfqStatus,
    RfqStatusHistory,
    RfqSupplier,
)
from app.models.units import UnitDefinition
from app.repositories.bom_repository import BomRepository
from app.repositories.part_repository import PartRepository
from app.repositories.rfq_repository import RfqRepository
from app.repositories.supplier_repository import SupplierRepository
from app.schemas.rfqs import (
    RfqCreate,
    RfqLineBulkCreate,
    RfqLineCreate,
    RfqLineUpdate,
    RfqStatusChangeRequest,
    RfqSupplierInviteRequest,
    RfqUpdate,
)
from app.services.audit import AuditRecorder

# Module-level aliases, not inline `list[...]` annotations anywhere in the
# class body below: RfqService defines a method named `list` (and `list_
# lines`/`list_suppliers`), which (per `from __future__ import annotations`)
# shadows the builtin `list` for any bare `list[...]` written in a method
# signature anywhere in the same class body — mypy then reads it as
# "RfqService.list used as a type" instead of the builtin generic. Same
# mypy-shadowing precedent as BomService/PartService (see their module
# docstrings/comments); every method signature below uses one of these
# aliases instead of a bare `list[...]`.
_RfqWithLines = tuple[Rfq, list[RfqLine]]
# (rfq, line_count) pairs — RfqRepository.search's own return shape, per its
# module docstring (2026-08 product-audit remediation, P2).
_RfqPage = tuple[list[tuple[Rfq, int]], int]
_RfqStatusFilter = list[RfqStatus] | None
_RfqLineList = list[RfqLine]
_RfqSupplierList = list[RfqSupplier]
_RfqStatusHistoryList = list[RfqStatusHistory]
_BomLineList = list[BillOfMaterialLine]

_ELEVATED_ROLES = frozenset({Role.ADMINISTRATOR, Role.OWNER})


def _duplicate_reference_error(internal_reference: str) -> ConflictDuplicateError:
    return ConflictDuplicateError(
        f"RFQ reference '{internal_reference}' is already in use by an active RFQ "
        "in this organization."
    )


def _rfq_state(rfq: Rfq, lines: list[RfqLine]) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "name": rfq.name,
        "internal_reference": rfq.internal_reference,
        "status": rfq.status.value,
        "base_currency": rfq.base_currency,
        "requested_payment_terms": rfq.requested_payment_terms,
        "requested_incoterm": rfq.requested_incoterm,
        "due_date": rfq.due_date.isoformat(),
        "requested_delivery_date": (
            rfq.requested_delivery_date.isoformat() if rfq.requested_delivery_date else None
        ),
        "notes": rfq.notes,
        "source_bom_id": (str(rfq.source_bom_id) if rfq.source_bom_id is not None else None),
        "version": rfq.version,
        "is_archived": rfq.is_archived,
        "archived_at": (rfq.archived_at.isoformat() if rfq.archived_at else None),
        "archive_reason": rfq.archive_reason,
        "lines": [_line_state(line) for line in lines],
    }


def _line_state(line: RfqLine) -> dict[str, Any]:
    return {
        "line_number": line.line_number,
        "part_id": str(line.part_id),
        "required_quantity": str(line.required_quantity),
        "unit_definition_id": str(line.unit_definition_id),
        "required_specifications": line.required_specifications,
        "notes": line.notes,
    }


def _supplier_state(rs: RfqSupplier) -> dict[str, Any]:
    return {
        "rfq_id": str(rs.rfq_id),
        "supplier_id": str(rs.supplier_id),
        "invited_at": rs.invited_at.isoformat(),
        "excluded_at": (rs.excluded_at.isoformat() if rs.excluded_at else None),
        "exclusion_reason": rs.exclusion_reason,
        "notes": rs.notes,
    }


class RfqService:
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
        self._repo = RfqRepository(session, organization_id)
        self._part_repo = PartRepository(session, organization_id)
        self._bom_repo = BomRepository(session, organization_id)
        self._supplier_repo = SupplierRepository(session, organization_id)

    # -- shared validation -------------------------------------------------

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

    def _ensure_draft_or_override(
        self, rfq: Rfq, *, actor_role: Role, override_reason: str | None
    ) -> str:
        """Enforces the §4.8 PATCH-row rule shared by RFQ metadata updates and
        every line mutation. Returns an explanation suffix (empty string when
        no override was needed) for the caller's audit `explanation`."""
        if rfq.status == RfqStatus.DRAFT:
            return ""
        if actor_role in _ELEVATED_ROLES and override_reason:
            return f" (override by {actor_role.value}: {override_reason})"
        raise ConflictStateError(
            "RFQ lines and metadata can only be edited while status is 'draft'; "
            "an administrator or owner may override by providing override_reason."
        )

    def _build_line(
        self, index: int, body: RfqLineCreate, rfq_id: uuid.UUID, line_number: int
    ) -> RfqLine:
        part = self._part_repo.get(body.part_id)
        if part is None:
            raise NotFoundError("Part")

        if body.unit_definition_id is not None:
            self._validate_unit_definition(
                body.unit_definition_id, field=f"lines[{index}].unit_definition_id"
            )
            unit_definition_id = body.unit_definition_id
        else:
            unit_definition_id = part.unit_definition_id

        return RfqLine(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            rfq_id=rfq_id,
            line_number=line_number,
            part_id=body.part_id,
            required_quantity=quantize_qty(body.required_quantity),
            unit_definition_id=unit_definition_id,
            required_specifications=body.required_specifications,
            notes=body.notes,
        )

    # -- reads ---------------------------------------------------------

    def get(self, rfq_id: uuid.UUID) -> _RfqWithLines:
        rfq = self._repo.get_or_raise(rfq_id)
        return rfq, self._repo.list_lines(rfq.id)

    def invited_supplier_count(self, rfq_id: uuid.UUID) -> int:
        return self._repo.count_invited_suppliers(rfq_id)

    def list(
        self,
        *,
        statuses: _RfqStatusFilter = None,
        q: str | None = None,
        due_before: date | None = None,
        due_after: date | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> _RfqPage:
        items = self._repo.search(
            statuses=statuses,
            q=q,
            due_before=due_before,
            due_after=due_after,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        total = self._repo.count_matching(
            statuses=statuses,
            q=q,
            due_before=due_before,
            due_after=due_after,
            include_archived=include_archived,
        )
        return items, total

    # -- create / update -------------------------------------------------

    def create(self, body: RfqCreate, *, actor_id: uuid.UUID) -> _RfqWithLines:
        if self._repo.get_by_internal_reference(body.internal_reference) is not None:
            raise _duplicate_reference_error(body.internal_reference)

        # Resolve and validate the source BOM (if any) *before* the Rfq row
        # is created: `Rfq.source_bom_id` is part of a composite org FK
        # (`fk_rfqs_organization_id_source_bom_id`), so inserting the row
        # with a cross-org `source_bom_id` already set would hit that
        # constraint at flush time and surface as a raw `IntegrityError`
        # instead of the clean `404`/`409` this method means to raise —
        # doing the BOM lookup first (via `BomRepository.get_or_raise`,
        # itself org-scoped) means the composite FK can never fire on a
        # value this service didn't already confirm is valid.
        bom_lines_source = None
        if body.source_bom_id is not None:
            bom = self._bom_repo.get_or_raise(body.source_bom_id)
            if bom.status != BomStatus.ACTIVE:
                raise ConflictStateError(
                    f"BOM '{bom.name}' is '{bom.status.value}', not 'active'; only "
                    "an active BOM version can be exploded into an RFQ."
                )
            bom_lines_source = (bom, self._bom_repo.list_lines(bom.id))

        now = self._clock.now()
        rfq_id = self._ids.new_id()
        rfq = Rfq(
            id=rfq_id,
            organization_id=self._organization_id,
            name=body.name,
            internal_reference=body.internal_reference,
            status=RfqStatus.DRAFT,
            base_currency=body.base_currency,
            requested_payment_terms=body.requested_payment_terms,
            requested_incoterm=body.requested_incoterm,
            due_date=body.due_date,
            requested_delivery_date=body.requested_delivery_date,
            notes=body.notes,
            source_bom_id=body.source_bom_id,
            created_by_id=actor_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(rfq)
        # flush before building lines: rfq_lines' composite FK targets
        # (organization_id, rfq_id) on this same row, which must actually
        # exist (not merely be pending in the session) first — same reasoning
        # as BomService.create.
        self._db.flush()

        explosion_note: str | None = None
        if bom_lines_source is not None:
            bom, bom_lines = bom_lines_source
            lines, explosion_note = self._build_exploded_lines(
                rfq_id, bom, bom_lines, body.assembly_quantity
            )
        else:
            lines = [
                self._build_line(i, ln, rfq_id, i + 1) for i, ln in enumerate(body.lines)
            ]
        for line in lines:
            self._repo.add_line(line)
        try:
            self._db.flush()
        except IntegrityError as exc:
            raise _duplicate_reference_error(body.internal_reference) from exc

        explanation = f"RFQ '{rfq.name}' ({rfq.internal_reference}) created"
        if explosion_note:
            explanation += f"; {explosion_note}"

        self._audit.record(
            "rfq.created",
            entity_type="rfq",
            entity_id=rfq.id,
            after_state=_rfq_state(rfq, lines),
            explanation=explanation,
        )
        return rfq, lines

    def _build_exploded_lines(
        self,
        rfq_id: uuid.UUID,
        bom: BillOfMaterials,
        bom_lines: _BomLineList,
        assembly_quantity: Decimal | None,
    ) -> tuple[_RfqLineList, str]:
        """Pure construction — the BOM itself has already been resolved and
        validated (`status == active`) by the caller (`create()`), before the
        `Rfq` row was ever flushed. Takes no DB-lookup action of its own."""
        qty = assembly_quantity if assembly_quantity is not None else Decimal("1")

        # Arithmetic runs at full precision inside CALC_CONTEXT, quantized
        # once at the QTY_SCALE boundary — the same "full precision then
        # quantize once" policy `quantize()` itself follows
        # (app/core/money.py module docstring), applied here to the one
        # multiplication this service needs.
        lines: list[RfqLine] = []
        with localcontext(CALC_CONTEXT):
            for i, bom_line in enumerate(bom_lines):
                required_quantity = quantize_qty(bom_line.quantity_per_assembly * qty)
                lines.append(
                    RfqLine(
                        id=self._ids.new_id(),
                        organization_id=self._organization_id,
                        rfq_id=rfq_id,
                        line_number=i + 1,
                        part_id=bom_line.part_id,
                        required_quantity=required_quantity,
                        unit_definition_id=bom_line.unit_definition_id,
                        required_specifications=None,
                        notes=bom_line.notes,
                    )
                )

        note = (
            f"exploded from BOM '{bom.name}' v{bom.version_number} "
            f"at assembly_quantity={qty}"
        )
        return lines, note

    def update(
        self,
        rfq_id: uuid.UUID,
        body: RfqUpdate,
        *,
        if_match_version: int,
        actor_role: Role,
    ) -> _RfqWithLines:
        rfq = self._repo.get_or_raise(rfq_id)
        if rfq.version != if_match_version:
            raise ConflictVersionError()

        override_suffix = self._ensure_draft_or_override(
            rfq, actor_role=actor_role, override_reason=body.override_reason
        )

        lines = self._repo.list_lines(rfq.id)
        before = _rfq_state(rfq, lines)
        changes = body.model_dump(exclude_unset=True, exclude={"override_reason"})

        if (
            "internal_reference" in changes
            and changes["internal_reference"].lower() != rfq.internal_reference.lower()
        ):
            existing = self._repo.get_by_internal_reference(changes["internal_reference"])
            if existing is not None and existing.id != rfq.id:
                raise _duplicate_reference_error(changes["internal_reference"])

        for field in (
            "name",
            "internal_reference",
            "base_currency",
            "due_date",
            "requested_delivery_date",
            "requested_payment_terms",
            "requested_incoterm",
            "notes",
        ):
            if field in changes:
                setattr(rfq, field, changes[field])

        rfq.version += 1
        rfq.updated_at = self._clock.now()

        try:
            self._db.flush()
        except IntegrityError as exc:
            raise _duplicate_reference_error(
                changes.get("internal_reference", rfq.internal_reference)
            ) from exc

        self._audit.record(
            "rfq.updated",
            entity_type="rfq",
            entity_id=rfq.id,
            before_state=before,
            after_state=_rfq_state(rfq, lines),
            explanation=f"RFQ '{rfq.name}' updated{override_suffix}",
        )
        return rfq, lines

    # -- status workflow ---------------------------------------------------

    def change_status(
        self, rfq_id: uuid.UUID, body: RfqStatusChangeRequest, *, actor_id: uuid.UUID
    ) -> _RfqWithLines:
        rfq = self._repo.get_or_raise(rfq_id)
        from_status = rfq.status
        to_status = body.to_status

        allowed = ALLOWED_RFQ_TRANSITIONS[from_status]
        if to_status not in allowed:
            allowed_names = (
                ", ".join(sorted(s.value for s in allowed)) if allowed else "none (terminal)"
            )
            raise ConflictStateError(
                f"Cannot transition RFQ from '{from_status.value}' to "
                f"'{to_status.value}'. Allowed target(s): {allowed_names}."
            )

        lines = self._repo.list_lines(rfq.id)
        before = _rfq_state(rfq, lines)

        now = self._clock.now()
        rfq.status = to_status
        rfq.updated_at = now
        rfq.version += 1
        if to_status == RfqStatus.ARCHIVED:
            rfq.archived_at = now
            rfq.archived_by_id = actor_id
            rfq.archive_reason = body.reason

        history = RfqStatusHistory(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            rfq_id=rfq.id,
            from_status=from_status,
            to_status=to_status,
            actor_user_id=actor_id,
            note=body.reason,
            occurred_at=now,
        )
        self._repo.add_status_history(history)
        self._db.flush()

        self._audit.record(
            "rfq.status_changed",
            entity_type="rfq",
            entity_id=rfq.id,
            before_state=before,
            after_state=_rfq_state(rfq, lines),
            explanation=(
                f"RFQ '{rfq.name}' status changed from '{from_status.value}' to "
                f"'{to_status.value}'" + (f": {body.reason}" if body.reason else "")
            ),
        )
        return rfq, lines

    def status_history(self, rfq_id: uuid.UUID) -> _RfqStatusHistoryList:
        self._repo.get_or_raise(rfq_id)
        return self._repo.list_status_history(rfq_id)

    # -- lines ---------------------------------------------------------

    def list_lines(self, rfq_id: uuid.UUID) -> _RfqLineList:
        self._repo.get_or_raise(rfq_id)
        return self._repo.list_lines(rfq_id)

    def add_lines(
        self, rfq_id: uuid.UUID, body: RfqLineBulkCreate, *, actor_role: Role
    ) -> _RfqLineList:
        rfq = self._repo.get_or_raise(rfq_id)
        override_suffix = self._ensure_draft_or_override(
            rfq, actor_role=actor_role, override_reason=body.override_reason
        )

        next_number = self._repo.max_line_number(rfq_id) + 1
        created: list[RfqLine] = []
        for i, line_body in enumerate(body.lines):
            line = self._build_line(i, line_body, rfq_id, next_number + i)
            self._repo.add_line(line)
            created.append(line)
        try:
            self._db.flush()
        except IntegrityError as exc:
            raise ConflictStateError(
                "Could not add RFQ line(s); the line numbering conflicted "
                "with a concurrent edit."
            ) from exc

        for line in created:
            self._audit.record(
                "rfq.line_added",
                entity_type="rfq_line",
                entity_id=line.id,
                after_state=_line_state(line),
                explanation=f"Line {line.line_number} added to RFQ {rfq_id}{override_suffix}",
            )
        return created

    def update_line(
        self,
        rfq_id: uuid.UUID,
        line_id: uuid.UUID,
        body: RfqLineUpdate,
        *,
        actor_role: Role,
    ) -> RfqLine:
        rfq = self._repo.get_or_raise(rfq_id)
        override_suffix = self._ensure_draft_or_override(
            rfq, actor_role=actor_role, override_reason=body.override_reason
        )
        line = self._repo.get_line(rfq_id, line_id)
        if line is None:
            raise NotFoundError("RfqLine")

        before = _line_state(line)
        changes = body.model_dump(exclude_unset=True, exclude={"override_reason"})

        if "part_id" in changes:
            part = self._part_repo.get(changes["part_id"])
            if part is None:
                raise NotFoundError("Part")
            line.part_id = changes["part_id"]

        if "unit_definition_id" in changes and changes["unit_definition_id"] is not None:
            self._validate_unit_definition(
                changes["unit_definition_id"], field="unit_definition_id"
            )
            line.unit_definition_id = changes["unit_definition_id"]

        if "required_quantity" in changes and body.required_quantity is not None:
            # `body.required_quantity` (real attribute access), not
            # `changes["required_quantity"]`: `model_dump()` runs
            # `QuantityString`'s `PlainSerializer` unconditionally (its
            # `when_used` defaults to "always", not "json"), so the dict
            # produced by `model_dump()` above already holds the *wire*
            # string form, not the parsed `Decimal` — `quantize_qty()` needs
            # the latter.
            line.required_quantity = quantize_qty(body.required_quantity)
        if "required_specifications" in changes:
            line.required_specifications = changes["required_specifications"]
        if "notes" in changes:
            line.notes = changes["notes"]

        self._db.flush()

        self._audit.record(
            "rfq.line_updated",
            entity_type="rfq_line",
            entity_id=line.id,
            before_state=before,
            after_state=_line_state(line),
            explanation=f"Line {line.line_number} of RFQ {rfq_id} updated{override_suffix}",
        )
        return line

    def remove_line(
        self,
        rfq_id: uuid.UUID,
        line_id: uuid.UUID,
        *,
        actor_role: Role,
        override_reason: str | None,
    ) -> None:
        rfq = self._repo.get_or_raise(rfq_id)
        override_suffix = self._ensure_draft_or_override(
            rfq, actor_role=actor_role, override_reason=override_reason
        )
        line = self._repo.get_line(rfq_id, line_id)
        if line is None:
            raise NotFoundError("RfqLine")

        before = _line_state(line)
        self._repo.delete_line(line)
        self._db.flush()

        self._audit.record(
            "rfq.line_removed",
            entity_type="rfq_line",
            entity_id=line_id,
            before_state=before,
            explanation=f"Line {before['line_number']} removed from RFQ {rfq_id}{override_suffix}",
        )

    # -- suppliers -----------------------------------------------------

    def list_suppliers(self, rfq_id: uuid.UUID) -> _RfqSupplierList:
        self._repo.get_or_raise(rfq_id)
        return self._repo.list_suppliers(rfq_id)

    def invite_suppliers(
        self, rfq_id: uuid.UUID, body: RfqSupplierInviteRequest
    ) -> _RfqSupplierList:
        rfq = self._repo.get_or_raise(rfq_id)
        if rfq.status not in (RfqStatus.DRAFT, RfqStatus.OPEN):
            raise ConflictStateError(
                f"Suppliers can only be invited while the RFQ is 'draft' or "
                f"'open' (current status: '{rfq.status.value}')."
            )

        now = self._clock.now()
        created: list[RfqSupplier] = []
        for supplier_id in body.supplier_ids:
            # in-org existence check, never 403 (§1.1): absent or other-org
            # supplier id is indistinguishable and always 404.
            if self._supplier_repo.get(supplier_id) is None:
                raise NotFoundError("Supplier")
            if self._repo.get_supplier_by_supplier_id(rfq_id, supplier_id) is not None:
                raise ConflictDuplicateError(
                    "This supplier has already been invited to (or excluded from) "
                    "this RFQ; use the reinstate route to un-exclude."
                )

            rs = RfqSupplier(
                id=self._ids.new_id(),
                organization_id=self._organization_id,
                rfq_id=rfq_id,
                supplier_id=supplier_id,
                invited_at=now,
            )
            self._repo.add_supplier(rs)
            created.append(rs)

        try:
            self._db.flush()
        except IntegrityError as exc:
            raise ConflictDuplicateError(
                "One or more suppliers were already invited to this RFQ."
            ) from exc

        for rs in created:
            self._audit.record(
                "rfq.supplier_invited",
                entity_type="rfq_supplier",
                entity_id=rs.id,
                after_state=_supplier_state(rs),
                explanation=f"Supplier {rs.supplier_id} invited to RFQ {rfq_id}",
            )
        return created

    def exclude_supplier(
        self,
        rfq_id: uuid.UUID,
        rfq_supplier_id: uuid.UUID,
        *,
        reason: str,
    ) -> RfqSupplier:
        self._repo.get_or_raise(rfq_id)
        rs = self._repo.get_supplier(rfq_id, rfq_supplier_id)
        if rs is None:
            raise NotFoundError("RfqSupplier")
        if rs.excluded_at is not None:
            raise ConflictStateError("Supplier is already excluded from this RFQ.")

        before = _supplier_state(rs)
        rs.excluded_at = self._clock.now()
        rs.exclusion_reason = reason
        self._db.flush()

        self._audit.record(
            "rfq.supplier_excluded",
            entity_type="rfq_supplier",
            entity_id=rs.id,
            before_state=before,
            after_state=_supplier_state(rs),
            explanation=f"Supplier {rs.supplier_id} excluded from RFQ {rfq_id}: {reason}",
        )
        return rs

    def reinstate_supplier(
        self, rfq_id: uuid.UUID, rfq_supplier_id: uuid.UUID
    ) -> RfqSupplier:
        self._repo.get_or_raise(rfq_id)
        rs = self._repo.get_supplier(rfq_id, rfq_supplier_id)
        if rs is None:
            raise NotFoundError("RfqSupplier")
        if rs.excluded_at is None:
            raise ConflictStateError("Supplier is not excluded from this RFQ.")

        before = _supplier_state(rs)
        rs.excluded_at = None
        rs.exclusion_reason = None
        self._db.flush()

        self._audit.record(
            "rfq.supplier_reinstated",
            entity_type="rfq_supplier",
            entity_id=rs.id,
            before_state=before,
            after_state=_supplier_state(rs),
            explanation=f"Supplier {rs.supplier_id} reinstated to RFQ {rfq_id}",
        )
        return rs


__all__ = ["RfqService"]
