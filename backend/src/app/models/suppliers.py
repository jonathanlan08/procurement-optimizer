"""Supplier master data: suppliers, supplier_contacts, supplier_performance_records
(docs/planning/02-erd.md §4).

Children reference their parent supplier with a composite foreign key
`(organization_id, supplier_id) -> suppliers(organization_id, id)` rather than a
plain `supplier_id` FK. This is isolation control #3 (docs/planning/00-decisions.md
§1.2): a cross-org reference is refused by Postgres itself, not merely rejected by
application code.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CHAR, Boolean, Date, ForeignKeyConstraint, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    ArchivableMixin,
    OrgOwnedBase,
    TimestampedMixin,
    VersionedMixin,
    org_identity_constraint,
)


class Supplier(TimestampedMixin, VersionedMixin, ArchivableMixin, OrgOwnedBase):
    """A vendor an organization may request quotes from.

    `code` uniqueness is enforced by a partial, case-insensitive index
    `(organization_id, lower(code)) WHERE archived_at IS NULL` created in
    migration 0002 (02-erd.md §8 `uq_suppliers_org_code_active`) — not
    represented here, matching how `organizations.slug` / `users.email`
    functional uniqueness lives only in migration 0001 (see identity.py).
    Archiving frees the code for reuse, same soft-delete convention as
    elsewhere in the schema.
    """

    __tablename__ = "suppliers"
    __table_args__ = (org_identity_constraint("suppliers"),)

    code: Mapped[str] = mapped_column(Text())
    name: Mapped[str] = mapped_column(Text())
    country_code: Mapped[str] = mapped_column(CHAR(2))
    supported_currencies: Mapped[list[str]] = mapped_column(ARRAY(CHAR(3)), default=list)
    standard_payment_terms: Mapped[str | None] = mapped_column(Text(), default=None)
    standard_incoterm: Mapped[str | None] = mapped_column(Text(), default=None)
    typical_lead_time_days: Mapped[int | None] = mapped_column(Integer(), default=None)
    capacity_units_per_month: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), default=None
    )
    default_moq: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)


class SupplierContact(TimestampedMixin, ArchivableMixin, OrgOwnedBase):
    __tablename__ = "supplier_contacts"
    __table_args__ = (
        org_identity_constraint("supplier_contacts"),
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
        ),
    )

    # part of the composite FK above, not a single-column ForeignKey
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    name: Mapped[str] = mapped_column(Text())
    email: Mapped[str | None] = mapped_column(Text(), default=None)
    phone: Mapped[str | None] = mapped_column(Text(), default=None)
    role_title: Mapped[str | None] = mapped_column(Text(), default=None)
    is_primary: Mapped[bool] = mapped_column(Boolean(), default=False)


class SupplierPerformanceRecord(OrgOwnedBase):
    """User-entered or seeded performance history for a supplier over a period.

    No archived_at/version in 02-erd.md §4: these are historical records, not a
    mutable aggregate with a lifecycle of their own.
    """

    __tablename__ = "supplier_performance_records"
    __table_args__ = (
        org_identity_constraint("supplier_performance_records"),
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
        ),
    )

    # part of the composite FK above, not a single-column ForeignKey
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    period_start: Mapped[date] = mapped_column(Date())
    period_end: Mapped[date] = mapped_column(Date())
    on_time_delivery_rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    defect_rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    quality_score: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    orders_count: Mapped[int] = mapped_column(Integer())
    source: Mapped[str] = mapped_column(Text())  # user_entered|synthetic_seed
    notes: Mapped[str | None] = mapped_column(Text(), default=None)
    recorded_at: Mapped[datetime] = mapped_column()


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in two FKs on the child tables (the plain org FK and the
# composite supplier FK), so `foreign_keys` disambiguates which constraint each
# relationship follows.
Supplier.organization = relationship("Organization", lazy="select")

SupplierContact.organization = relationship(
    "Organization", foreign_keys=[SupplierContact.organization_id], lazy="select"
)
SupplierContact.supplier = relationship(
    "Supplier",
    foreign_keys=[SupplierContact.organization_id, SupplierContact.supplier_id],
    lazy="select",
    # organization_id is legitimately written by both relationships (it is half
    # of the composite FK to suppliers as well as the plain FK to organizations)
    overlaps="organization",
)

SupplierPerformanceRecord.organization = relationship(
    "Organization", foreign_keys=[SupplierPerformanceRecord.organization_id], lazy="select"
)
SupplierPerformanceRecord.supplier = relationship(
    "Supplier",
    foreign_keys=[
        SupplierPerformanceRecord.organization_id,
        SupplierPerformanceRecord.supplier_id,
    ],
    lazy="select",
    overlaps="organization",
)

__all__ = [
    "Supplier",
    "SupplierContact",
    "SupplierPerformanceRecord",
]
