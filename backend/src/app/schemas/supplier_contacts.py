"""Supplier contact request/response schemas
(docs/planning/03-api-contract.md §4.4).

Contacts have no `version` column (SupplierContact does not mix in
VersionedMixin - see app.models.suppliers), so unlike SupplierUpdate there is
no `If-Match` concurrency story here: §1.7 lists suppliers/parts/RFQs/quotes/
scoring configurations as the resources that carry ETag/If-Match, and contacts
are deliberately not among them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.suppliers import SupplierContact


class SupplierContactCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = Field(default=None)
    phone: str | None = Field(default=None, max_length=64)
    role_title: str | None = Field(default=None, max_length=200)
    is_primary: bool = False


class SupplierContactUpdate(BaseModel):
    """Partial update (PATCH): only fields present in the payload are applied."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: EmailStr | None = Field(default=None)
    phone: str | None = Field(default=None, max_length=64)
    role_title: str | None = Field(default=None, max_length=200)
    is_primary: bool | None = None


class SupplierContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    supplier_id: str
    name: str
    email: str | None
    phone: str | None
    role_title: str | None
    is_primary: bool
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, contact: SupplierContact) -> SupplierContactResponse:
        return cls(
            id=str(contact.id),
            supplier_id=str(contact.supplier_id),
            name=contact.name,
            email=contact.email,
            phone=contact.phone,
            role_title=contact.role_title,
            is_primary=contact.is_primary,
            is_archived=contact.is_archived,
            archived_at=contact.archived_at,
            archive_reason=contact.archive_reason,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )


class SupplierContactPageInfo(BaseModel):
    limit: int
    offset: int
    total: int


class SupplierContactListResponse(BaseModel):
    items: list[SupplierContactResponse]
    page: SupplierContactPageInfo


__all__ = [
    "SupplierContactCreate",
    "SupplierContactListResponse",
    "SupplierContactPageInfo",
    "SupplierContactResponse",
    "SupplierContactUpdate",
]
