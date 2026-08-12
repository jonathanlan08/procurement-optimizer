"""Supplier service — org-scoped business logic for supplier CRUD.

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception. Every mutation
writes exactly one audit event in the same transaction as the data change it
describes, so an audit trail that diverges from the data is impossible.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import ConflictDuplicateError, ConflictStateError, ConflictVersionError
from app.core.ids import IdGenerator
from app.core.money import quantize_qty
from app.models.suppliers import Supplier
from app.repositories.supplier_repository import SupplierRepository
from app.schemas.suppliers import SupplierCreate, SupplierUpdate
from app.services.audit import AuditRecorder

# fields settable via SupplierUpdate that map 1:1 onto Supplier columns
_UPDATABLE_FIELDS = (
    "code",
    "name",
    "country_code",
    "supported_currencies",
    "standard_payment_terms",
    "standard_incoterm",
    "typical_lead_time_days",
    "capacity_units_per_month",
    "default_moq",
    "is_active",
)
_QUANTIZED_FIELDS = frozenset({"capacity_units_per_month", "default_moq"})


def _duplicate_code_error(code: str) -> ConflictDuplicateError:
    return ConflictDuplicateError(
        f"Supplier code '{code}' is already in use by an active supplier in this organization."
    )


def _state(supplier: Supplier) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "code": supplier.code,
        "name": supplier.name,
        "country_code": supplier.country_code,
        "supported_currencies": list(supplier.supported_currencies),
        "standard_payment_terms": supplier.standard_payment_terms,
        "standard_incoterm": supplier.standard_incoterm,
        "typical_lead_time_days": supplier.typical_lead_time_days,
        "capacity_units_per_month": (
            str(supplier.capacity_units_per_month)
            if supplier.capacity_units_per_month is not None
            else None
        ),
        "default_moq": (str(supplier.default_moq) if supplier.default_moq is not None else None),
        "is_active": supplier.is_active,
        "version": supplier.version,
        "archived_at": (supplier.archived_at.isoformat() if supplier.archived_at else None),
        "archive_reason": supplier.archive_reason,
    }


class SupplierService:
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
        self._repo = SupplierRepository(session, organization_id)

    def get(self, supplier_id: uuid.UUID) -> Supplier:
        return self._repo.get_or_raise(supplier_id)

    def list(
        self,
        *,
        q: str | None = None,
        country: str | None = None,
        is_active: bool | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Supplier], int]:
        items = self._repo.search(
            q=q,
            country=country,
            is_active=is_active,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        total = self._repo.count_matching(
            q=q, country=country, is_active=is_active, include_archived=include_archived
        )
        return items, total

    def create(self, body: SupplierCreate) -> Supplier:
        if self._repo.get_by_code(body.code) is not None:
            raise _duplicate_code_error(body.code)

        now = self._clock.now()
        supplier = Supplier(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            code=body.code,
            name=body.name,
            country_code=body.country_code,
            supported_currencies=list(body.supported_currencies),
            standard_payment_terms=body.standard_payment_terms,
            standard_incoterm=body.standard_incoterm,
            typical_lead_time_days=body.typical_lead_time_days,
            capacity_units_per_month=(
                quantize_qty(body.capacity_units_per_month)
                if body.capacity_units_per_month is not None
                else None
            ),
            default_moq=(
                quantize_qty(body.default_moq) if body.default_moq is not None else None
            ),
            is_active=body.is_active,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(supplier)
        try:
            self._db.flush()
        except IntegrityError as exc:
            raise _duplicate_code_error(body.code) from exc

        self._audit.record(
            "supplier.created",
            entity_type="supplier",
            entity_id=supplier.id,
            after_state=_state(supplier),
            explanation=f"Supplier '{supplier.code}' created",
        )
        return supplier

    def update(
        self, supplier_id: uuid.UUID, body: SupplierUpdate, *, if_match_version: int
    ) -> Supplier:
        supplier = self._repo.get_or_raise(supplier_id)
        if supplier.version != if_match_version:
            raise ConflictVersionError()

        before = _state(supplier)
        changes = body.model_dump(exclude_unset=True)

        if "code" in changes and changes["code"].lower() != supplier.code.lower():
            existing = self._repo.get_by_code(changes["code"])
            if existing is not None and existing.id != supplier.id:
                raise _duplicate_code_error(changes["code"])

        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            value = changes[field]
            if field in _QUANTIZED_FIELDS and value is not None:
                # `getattr(body, field)`, not `changes[field]`: `model_dump()`
                # runs `DecimalString`'s `PlainSerializer` unconditionally (its
                # `when_used` defaults to "always", not "json"), so `changes`
                # holds the *wire* string form — `quantize_qty()` needs the
                # parsed `Decimal` from the model attribute.
                value = quantize_qty(getattr(body, field))
            setattr(supplier, field, value)

        supplier.version += 1
        supplier.updated_at = self._clock.now()

        try:
            self._db.flush()
        except IntegrityError as exc:
            raise _duplicate_code_error(changes.get("code", supplier.code)) from exc

        self._audit.record(
            "supplier.updated",
            entity_type="supplier",
            entity_id=supplier.id,
            before_state=before,
            after_state=_state(supplier),
            explanation=f"Supplier '{supplier.code}' updated",
        )
        return supplier

    def archive(self, supplier_id: uuid.UUID, *, reason: str, actor_id: uuid.UUID) -> Supplier:
        supplier = self._repo.get_or_raise(supplier_id)
        if supplier.is_archived:
            raise ConflictStateError("Supplier is already archived.")

        before = _state(supplier)
        now = self._clock.now()
        supplier.archived_at = now
        supplier.archived_by_id = actor_id
        supplier.archive_reason = reason
        supplier.updated_at = now
        supplier.version += 1
        self._db.flush()

        self._audit.record(
            "supplier.archived",
            entity_type="supplier",
            entity_id=supplier.id,
            before_state=before,
            after_state=_state(supplier),
            explanation=f"Supplier '{supplier.code}' archived: {reason}",
        )
        return supplier

    def unarchive(self, supplier_id: uuid.UUID, *, reason: str) -> Supplier:
        supplier = self._repo.get_or_raise(supplier_id)
        if not supplier.is_archived:
            raise ConflictStateError("Supplier is not archived.")

        before = _state(supplier)
        now = self._clock.now()
        supplier.archived_at = None
        supplier.archived_by_id = None
        supplier.archive_reason = None
        supplier.updated_at = now
        supplier.version += 1
        self._db.flush()

        self._audit.record(
            "supplier.unarchived",
            entity_type="supplier",
            entity_id=supplier.id,
            before_state=before,
            after_state=_state(supplier),
            explanation=f"Supplier '{supplier.code}' unarchived: {reason}",
        )
        return supplier


__all__ = ["SupplierService"]
