"""Part service - org-scoped business logic for part CRUD and alternatives.

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception. Every mutation
writes exactly one audit event in the same transaction as the data change it
describes, so an audit trail that diverges from the data is impossible.

Mirrors supplier_service.py + supplier_contact_service.py for the two halves
of this file: part CRUD/archive follows supplier_service.py; alternatives
(add/remove/list) follow the supplier-contact sub-resource pattern, with the
parent part required to exist in-org before any alternative operation.
"""

from __future__ import annotations

import uuid
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
from app.core.money import quantize_unit_price
from app.models.parts import Part, PartAlternative
from app.models.units import UnitDefinition
from app.repositories.part_repository import PartAlternativeRepository, PartRepository
from app.schemas.parts import PartAlternativeCreate, PartCreate, PartUpdate
from app.services.audit import AuditRecorder

# Module-level aliases, not inline `list[...]` annotations on `list_alternatives`
# below: PartService defines a method named `list`, which (per `from __future__
# import annotations`) shadows the builtin `list` for any bare `list[...]`
# written later in the same class body - mypy then reads it as "PartService.list
# used as a type" instead of the builtin generic. Aliasing at module scope,
# where `list` is unambiguously the builtin, sidesteps the collision.
_PartPage = tuple[list[Part], int]
_PartAlternativePage = tuple[list[PartAlternative], int]

# fields settable via PartUpdate that map 1:1 onto Part columns
_UPDATABLE_FIELDS = (
    "internal_part_number",
    "manufacturer_part_number",
    "manufacturer",
    "name",
    "description",
    "category",
    "unit_definition_id",
    "required_specifications",
    "target_price",
    "target_price_currency",
    "is_active",
)


def _duplicate_part_number_error(internal_part_number: str) -> ConflictDuplicateError:
    return ConflictDuplicateError(
        f"Part number '{internal_part_number}' is already in use by an active part "
        "in this organization."
    )


def _duplicate_alternative_error() -> ConflictDuplicateError:
    return ConflictDuplicateError(
        "This part already has an alternative recorded for that part/manufacturer part number."
    )


def _state(part: Part) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "internal_part_number": part.internal_part_number,
        "manufacturer_part_number": part.manufacturer_part_number,
        "manufacturer": part.manufacturer,
        "name": part.name,
        "description": part.description,
        "category": part.category,
        "unit_definition_id": str(part.unit_definition_id),
        "required_specifications": part.required_specifications,
        "target_price": (str(part.target_price) if part.target_price is not None else None),
        "target_price_currency": part.target_price_currency,
        "is_active": part.is_active,
        "version": part.version,
        "archived_at": (part.archived_at.isoformat() if part.archived_at else None),
        "archive_reason": part.archive_reason,
    }


def _alt_state(alt: PartAlternative) -> dict[str, Any]:
    return {
        "part_id": str(alt.part_id),
        "alternative_part_id": (
            str(alt.alternative_part_id) if alt.alternative_part_id is not None else None
        ),
        "alternative_mpn": alt.alternative_mpn,
        "approval_status": alt.approval_status,
        "rationale": alt.rationale,
        "approved_by_id": (str(alt.approved_by_id) if alt.approved_by_id is not None else None),
        "approved_at": (alt.approved_at.isoformat() if alt.approved_at else None),
    }


class PartService:
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
        self._repo = PartRepository(session, organization_id)
        self._alt_repo = PartAlternativeRepository(session, organization_id)

    # -- unit_definition_id cross-check -----------------------------------

    def _validate_unit_definition(self, unit_definition_id: uuid.UUID) -> None:
        """unit_definition_id must reference the global catalogue (organization_id
        IS NULL) or a unit owned by this organization - never another
        organization's private unit. Plain FK only (see app.models.parts.Part
        docstring: the composite org-guard FK cannot apply against a nullable
        parent organization_id), so this is enforced here, in the service.
        """
        unit = self._db.execute(
            select(UnitDefinition).where(UnitDefinition.id == unit_definition_id)
        ).scalar_one_or_none()
        if unit is None:
            raise ValidationAppError(
                "unit_definition_id does not reference an existing unit.",
                details=[
                    ErrorDetail(
                        field="unit_definition_id",
                        issue="does not reference an existing unit",
                    )
                ],
            )
        if unit.organization_id is not None and unit.organization_id != self._organization_id:
            raise ValidationAppError(
                "unit_definition_id references another organization's private unit.",
                details=[
                    ErrorDetail(
                        field="unit_definition_id",
                        issue="references another organization's private unit",
                    )
                ],
            )

    # -- part CRUD ----------------------------------------------------------

    def get(self, part_id: uuid.UUID) -> Part:
        return self._repo.get_or_raise(part_id)

    def list(
        self,
        *,
        q: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> _PartPage:
        items = self._repo.search(
            q=q,
            category=category,
            is_active=is_active,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        total = self._repo.count_matching(
            q=q, category=category, is_active=is_active, include_archived=include_archived
        )
        return items, total

    def create(self, body: PartCreate) -> Part:
        self._validate_unit_definition(body.unit_definition_id)
        if self._repo.get_by_internal_part_number(body.internal_part_number) is not None:
            raise _duplicate_part_number_error(body.internal_part_number)

        now = self._clock.now()
        part = Part(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            internal_part_number=body.internal_part_number,
            manufacturer_part_number=body.manufacturer_part_number,
            manufacturer=body.manufacturer,
            name=body.name,
            description=body.description,
            category=body.category,
            unit_definition_id=body.unit_definition_id,
            required_specifications=body.required_specifications,
            target_price=(
                quantize_unit_price(body.target_price) if body.target_price is not None else None
            ),
            target_price_currency=body.target_price_currency,
            is_active=body.is_active,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(part)
        try:
            self._db.flush()
        except IntegrityError as exc:
            raise _duplicate_part_number_error(body.internal_part_number) from exc

        self._audit.record(
            "part.created",
            entity_type="part",
            entity_id=part.id,
            after_state=_state(part),
            explanation=f"Part '{part.internal_part_number}' created",
        )
        return part

    def update(self, part_id: uuid.UUID, body: PartUpdate, *, if_match_version: int) -> Part:
        part = self._repo.get_or_raise(part_id)
        if part.version != if_match_version:
            raise ConflictVersionError()

        before = _state(part)
        changes = body.model_dump(exclude_unset=True)

        if (
            "internal_part_number" in changes
            and changes["internal_part_number"].lower() != part.internal_part_number.lower()
        ):
            existing = self._repo.get_by_internal_part_number(changes["internal_part_number"])
            if existing is not None and existing.id != part.id:
                raise _duplicate_part_number_error(changes["internal_part_number"])

        if "unit_definition_id" in changes:
            self._validate_unit_definition(changes["unit_definition_id"])

        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            value = changes[field]
            if field == "target_price" and body.target_price is not None:
                # `body.target_price`, not `changes["target_price"]`:
                # `model_dump()` runs the price type's `PlainSerializer`
                # unconditionally (`when_used` defaults to "always"), so
                # `changes` holds the *wire* string form - `quantize_unit_price()`
                # needs the parsed `Decimal` from the model attribute.
                value = quantize_unit_price(body.target_price)
            setattr(part, field, value)

        part.version += 1
        part.updated_at = self._clock.now()

        try:
            self._db.flush()
        except IntegrityError as exc:
            raise _duplicate_part_number_error(
                changes.get("internal_part_number", part.internal_part_number)
            ) from exc

        self._audit.record(
            "part.updated",
            entity_type="part",
            entity_id=part.id,
            before_state=before,
            after_state=_state(part),
            explanation=f"Part '{part.internal_part_number}' updated",
        )
        return part

    def archive(self, part_id: uuid.UUID, *, reason: str, actor_id: uuid.UUID) -> Part:
        part = self._repo.get_or_raise(part_id)
        if part.is_archived:
            raise ConflictStateError("Part is already archived.")

        before = _state(part)
        now = self._clock.now()
        part.archived_at = now
        part.archived_by_id = actor_id
        part.archive_reason = reason
        part.updated_at = now
        part.version += 1
        self._db.flush()

        self._audit.record(
            "part.archived",
            entity_type="part",
            entity_id=part.id,
            before_state=before,
            after_state=_state(part),
            explanation=f"Part '{part.internal_part_number}' archived: {reason}",
        )
        return part

    def unarchive(self, part_id: uuid.UUID, *, reason: str) -> Part:
        part = self._repo.get_or_raise(part_id)
        if not part.is_archived:
            raise ConflictStateError("Part is not archived.")

        before = _state(part)
        now = self._clock.now()
        part.archived_at = None
        part.archived_by_id = None
        part.archive_reason = None
        part.updated_at = now
        part.version += 1
        self._db.flush()

        self._audit.record(
            "part.unarchived",
            entity_type="part",
            entity_id=part.id,
            before_state=before,
            after_state=_state(part),
            explanation=f"Part '{part.internal_part_number}' unarchived: {reason}",
        )
        return part

    # -- alternatives ---------------------------------------------------

    def _require_part(self, part_id: uuid.UUID) -> Part:
        """404s for an absent or cross-org part (never 403)."""
        return self._repo.get_or_raise(part_id)

    def list_alternatives(
        self, part_id: uuid.UUID, *, limit: int = 50, offset: int = 0
    ) -> _PartAlternativePage:
        self._require_part(part_id)
        items = self._alt_repo.list_for_part(part_id, limit=limit, offset=offset)
        total = self._alt_repo.count_for_part(part_id)
        return items, total

    def add_alternative(
        self, part_id: uuid.UUID, body: PartAlternativeCreate, *, actor_id: uuid.UUID
    ) -> PartAlternative:
        self._require_part(part_id)

        if body.alternative_part_id is not None:
            if body.alternative_part_id == part_id:
                raise ValidationAppError(
                    "A part cannot be recorded as its own alternative.",
                    details=[
                        ErrorDetail(
                            field="alternative_part_id", issue="cannot equal the part's own id"
                        )
                    ],
                )
            # internal: must resolve in-org via the repository, else 404
            # (never 403 - cross-org existence must not leak, §1.1).
            if self._repo.get(body.alternative_part_id) is None:
                raise NotFoundError("Part")
            if (
                self._alt_repo.get_by_part_and_alternative(part_id, body.alternative_part_id)
                is not None
            ):
                raise _duplicate_alternative_error()

        now = self._clock.now()
        alt = PartAlternative(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            part_id=part_id,
            alternative_part_id=body.alternative_part_id,
            alternative_mpn=body.alternative_mpn,
            approval_status=body.approval_status,
            rationale=body.rationale,
            approved_by_id=actor_id,
            approved_at=now,
        )
        self._alt_repo.add(alt)
        try:
            self._db.flush()
        except IntegrityError as exc:
            raise _duplicate_alternative_error() from exc

        self._audit.record(
            "part.alternative_added",
            entity_type="part_alternative",
            entity_id=alt.id,
            after_state=_alt_state(alt),
            explanation=f"Alternative recorded for part {part_id}",
        )
        return alt

    def remove_alternative(self, part_id: uuid.UUID, alternative_id: uuid.UUID) -> None:
        self._require_part(part_id)
        alt = self._alt_repo.get_for_part(part_id, alternative_id)
        if alt is None:
            raise NotFoundError("PartAlternative")

        before = _alt_state(alt)
        self._db.delete(alt)
        self._db.flush()

        self._audit.record(
            "part.alternative_removed",
            entity_type="part_alternative",
            entity_id=alternative_id,
            before_state=before,
            explanation=f"Alternative removed from part {part_id}",
        )


__all__ = ["PartService"]
