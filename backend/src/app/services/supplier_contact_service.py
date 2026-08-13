"""Supplier contact service - org-scoped business logic for the
`/suppliers/{id}/contacts` sub-resource (docs/planning/03-api-contract.md §4.4).

Mirrors supplier_service.py: services never commit (the request-scoped
`get_db` dependency commits on success and rolls back on any raised
exception), and every mutation writes exactly one audit event in the same
transaction as the data change it describes.

The parent supplier must exist in the caller's organization for every
operation here - `SupplierRepository.get_or_raise` 404s on both a genuinely
absent supplier and a same-id-different-org supplier, so a contact can never
be listed, created, read, updated, or archived under a supplier that belongs
to another organization.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import ConflictStateError, NotFoundError
from app.core.ids import IdGenerator
from app.models.suppliers import SupplierContact
from app.repositories.supplier_contact_repository import SupplierContactRepository
from app.repositories.supplier_repository import SupplierRepository
from app.schemas.supplier_contacts import SupplierContactCreate, SupplierContactUpdate
from app.services.audit import AuditRecorder

# fields settable via SupplierContactUpdate that map 1:1 onto SupplierContact columns
_UPDATABLE_FIELDS = ("name", "email", "phone", "role_title", "is_primary")


def _state(contact: SupplierContact) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "supplier_id": str(contact.supplier_id),
        "name": contact.name,
        "email": contact.email,
        "phone": contact.phone,
        "role_title": contact.role_title,
        "is_primary": contact.is_primary,
        "archived_at": (contact.archived_at.isoformat() if contact.archived_at else None),
        "archive_reason": contact.archive_reason,
    }


class SupplierContactService:
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
        self._suppliers = SupplierRepository(session, organization_id)
        self._repo = SupplierContactRepository(session, organization_id)

    def _require_supplier(self, supplier_id: uuid.UUID) -> None:
        """404s for an absent or cross-org supplier (never 403)."""
        self._suppliers.get_or_raise(supplier_id)

    def list(
        self,
        supplier_id: uuid.UUID,
        *,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SupplierContact], int]:
        self._require_supplier(supplier_id)
        items = self._repo.list_for_supplier(
            supplier_id, include_archived=include_archived, limit=limit, offset=offset
        )
        total = self._repo.count_for_supplier(supplier_id, include_archived=include_archived)
        return items, total

    def get(self, supplier_id: uuid.UUID, contact_id: uuid.UUID) -> SupplierContact:
        self._require_supplier(supplier_id)
        contact = self._repo.get_for_supplier(supplier_id, contact_id)
        if contact is None:
            raise NotFoundError("SupplierContact")
        return contact

    def create(
        self, supplier_id: uuid.UUID, body: SupplierContactCreate
    ) -> SupplierContact:
        self._require_supplier(supplier_id)

        now = self._clock.now()
        contact = SupplierContact(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            supplier_id=supplier_id,
            name=body.name,
            email=body.email,
            phone=body.phone,
            role_title=body.role_title,
            is_primary=body.is_primary,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(contact)
        self._db.flush()

        self._audit.record(
            "supplier_contact.created",
            entity_type="supplier_contact",
            entity_id=contact.id,
            after_state=_state(contact),
            explanation=f"Contact '{contact.name}' added to supplier {supplier_id}",
        )
        return contact

    def update(
        self, supplier_id: uuid.UUID, contact_id: uuid.UUID, body: SupplierContactUpdate
    ) -> SupplierContact:
        contact = self.get(supplier_id, contact_id)
        before = _state(contact)
        changes = body.model_dump(exclude_unset=True)

        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            setattr(contact, field, changes[field])

        contact.updated_at = self._clock.now()
        self._db.flush()

        self._audit.record(
            "supplier_contact.updated",
            entity_type="supplier_contact",
            entity_id=contact.id,
            before_state=before,
            after_state=_state(contact),
            explanation=f"Contact '{contact.name}' updated",
        )
        return contact

    def archive(
        self, supplier_id: uuid.UUID, contact_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> SupplierContact:
        """DELETE = archive (§4.4): soft delete, never a hard delete."""
        contact = self.get(supplier_id, contact_id)
        if contact.is_archived:
            raise ConflictStateError("Supplier contact is already archived.")

        before = _state(contact)
        now = self._clock.now()
        contact.archived_at = now
        contact.archived_by_id = actor_id
        contact.updated_at = now
        self._db.flush()

        self._audit.record(
            "supplier_contact.archived",
            entity_type="supplier_contact",
            entity_id=contact.id,
            before_state=before,
            after_state=_state(contact),
            explanation=f"Contact '{contact.name}' archived",
        )
        return contact


__all__ = ["SupplierContactService"]
