"""RFQs: rfqs, rfq_lines, rfq_suppliers, rfq_status_history
(docs/planning/02-erd.md §5).

**Status machine (principal's ruling, authoritative).** `rfq_status_enum` is
draft|open|under_review|awarded|closed|archived. The only allowed
transitions are draft->open, open->under_review, under_review->awarded,
under_review->open (reopen), awarded->closed, draft->archived,
closed->archived - nothing else, including no self-transitions. This is
captured as *data*, not scattered logic, in `ALLOWED_RFQ_TRANSITIONS` below,
so services and tests share one source of truth (02-erd.md §8: "State
machines are enforced in the service layer with an explicit transition
table; a DB `CHECK` cannot express 'from -> to'"). Every transition is
expected to write an `RfqStatusHistory` row (from_status, to_status,
actor_user_id, note, occurred_at) - enforced by the service layer in a
later phase, not by a DB trigger, the same reasoning `BillOfMaterials`'
copy-on-write chain already established (boms.py) for its own state
transition (draft -> active -> superseded).

**Deliberate ERD deviations.** The delegating task's explicit field
enumeration for this schema-only phase is narrower and, in three places,
differently-shaped than 02-erd.md §5's field boxes. Each is treated as an
intentional re-scoping, not an oversight, and is called out here so it reads
as a decision:

1. `RfqStatusHistory` renames three ERD columns: `reason` -> `note`,
   `changed_by_id` -> `actor_user_id`, `changed_at` -> `occurred_at`. The
   renames come verbatim from the status-transition ruling text
   ("Every transition writes an rfq_status_history row (from_status,
   to_status, actor_user_id, note, occurred_at)"), stated twice in the task,
   so treated as intentional rather than a paraphrase.
2. `requested_payment_terms` and `requested_incoterm` live on `Rfq` -
   principal's correction, matching the ERD. `Rfq` still does not carry
   `assembly_quantity` (present on
   `RFQS` in the ERD): the delegating task's field list for `Rfq` omits all
   three, and is followed verbatim rather than padded back out from the ERD
   box.
3. `RfqLine` does not carry `required_by_date` or `target_unit_price` (both
   present on `RFQ_LINES` in the ERD), for the same reason - omitted from
   the delegating task's field list for `RfqLine`.
4. `RfqSupplier` drops the ERD's `invite_status_enum status` column and
   `responded_at` entirely. Invitation/exclusion state is instead carried by
   two nullable timestamps (`invited_at`, always set at creation;
   `excluded_at`, set only if excluded) plus `exclusion_reason` - the
   delegating task spells this out explicitly ("invited_at, excluded_at +
   exclusion_reason (invitation/exclusion states)"). A `notes` column is
   added (not present in the ERD box at all).

None of these deviations touch the status-transition ruling itself, which is
implemented exactly as specified above.

Every child table reaches its parent(s) with the composite org-guard FK
`(organization_id, x_id) -> parent(organization_id, id)` (02-erd.md §1), the
same pattern used throughout `boms.py`/`suppliers.py`/`parts.py`: a
cross-org reference is refused by Postgres itself, not merely by application
code.

`Rfq.due_date`/`Rfq.requested_delivery_date` are plain `DATE` columns (no
timezone) per 00-decisions.md §4 #25's "organization-local business date"
ruling - a calendar date, not an instant. `due_date` is NOT NULL (the RFQ
response deadline is core to what an RFQ *is*); `requested_delivery_date` is
nullable - the "requested" wording in both the ERD and SPEC §5 signals an
aspirational ask, not a hard requirement at creation time.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    CURRENCY_CHAR,
    ArchivableMixin,
    OrgOwnedBase,
    TimestampedMixin,
    VersionedMixin,
    org_identity_constraint,
)


class RfqStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    UNDER_REVIEW = "under_review"
    AWARDED = "awarded"
    CLOSED = "closed"
    ARCHIVED = "archived"


RFQ_STATUS_ENUM = SaEnum(
    RfqStatus,
    name="rfq_status_enum",
    values_callable=lambda e: [m.value for m in e],
)

# Principal's status-transition ruling - the single source of truth for the
# RFQ state machine. Services and tests both import this dict rather than
# re-encoding the transition rules. Every RfqStatus is a key (including the
# terminal ARCHIVED, mapped to an empty frozenset) so lookups never need a
# fallback default.
ALLOWED_RFQ_TRANSITIONS: dict[RfqStatus, frozenset[RfqStatus]] = {
    RfqStatus.DRAFT: frozenset({RfqStatus.OPEN, RfqStatus.ARCHIVED}),
    RfqStatus.OPEN: frozenset({RfqStatus.UNDER_REVIEW}),
    RfqStatus.UNDER_REVIEW: frozenset({RfqStatus.AWARDED, RfqStatus.OPEN}),
    RfqStatus.AWARDED: frozenset({RfqStatus.CLOSED}),
    RfqStatus.CLOSED: frozenset({RfqStatus.ARCHIVED}),
    RfqStatus.ARCHIVED: frozenset(),
}


class Rfq(TimestampedMixin, VersionedMixin, ArchivableMixin, OrgOwnedBase):
    """A request for quotation an organization sends to suppliers.

    `internal_reference` uniqueness is enforced by a partial,
    case-insensitive index `(organization_id, lower(internal_reference))
    WHERE archived_at IS NULL` created in the migration (02-erd.md §8
    `uq_rfqs_org_ref_active`) - the same convention as `Supplier.code` /
    `Part.internal_part_number`. Archiving frees the reference for reuse.
    """

    __tablename__ = "rfqs"
    __table_args__ = (
        org_identity_constraint("rfqs"),
        # composite org FK: an RFQ may be exploded from a specific BOM
        # version (02-erd.md §5 "source of"). Nullable - a manually built
        # RFQ with no BOM origin is exempt from the constraint (Postgres
        # default MATCH SIMPLE), the same pattern as
        # BillOfMaterialLine.substitute_part_id (boms.py).
        ForeignKeyConstraint(
            ["organization_id", "source_bom_id"],
            ["bills_of_materials.organization_id", "bills_of_materials.id"],
            ondelete="RESTRICT",
            name="fk_rfqs_organization_id_source_bom_id",
        ),
        CheckConstraint("base_currency ~ '^[A-Z]{3}$'", name="ck_rfqs_base_currency_iso"),
    )

    name: Mapped[str] = mapped_column(Text())
    # citext UK in 02-erd.md §5; no citext extension (00-decisions.md §7) -
    # case-insensitive uniqueness is a functional partial index in the
    # migration instead, same as Part.internal_part_number / Supplier.code.
    internal_reference: Mapped[str] = mapped_column(Text())
    status: Mapped[RfqStatus] = mapped_column(RFQ_STATUS_ENUM, default=RfqStatus.DRAFT)
    base_currency: Mapped[str] = mapped_column(CURRENCY_CHAR)
    # principal's correction: requested commercial terms are RFQ-level
    requested_payment_terms: Mapped[str | None] = mapped_column(Text(), default=None)
    requested_incoterm: Mapped[str | None] = mapped_column(Text(), default=None)
    due_date: Mapped[date] = mapped_column(Date())
    requested_delivery_date: Mapped[date | None] = mapped_column(Date(), default=None)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)
    # part of composite FK above, not a single-column ForeignKey; nullable
    source_bom_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class RfqLine(OrgOwnedBase):
    """One requested line item of one RFQ: a part, a quantity, and the
    specification/commercial terms requested for it.

    No timestamp/version/archive mixins: 02-erd.md §5 lists none for
    `RFQ_LINES`, the same judgement call already made for
    `BillOfMaterialLine` (boms.py) and `SupplierPerformanceRecord`
    (suppliers.py).
    """

    __tablename__ = "rfq_lines"
    __table_args__ = (
        org_identity_constraint("rfq_lines"),
        # composite org FK: the RFQ this line belongs to.
        ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_rfq_lines_organization_id_rfq_id",
        ),
        # composite org FK: the requested part.
        ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_rfq_lines_organization_id_part_id",
        ),
        UniqueConstraint(
            "organization_id",
            "rfq_id",
            "line_number",
            name="uq_rfq_lines_org_rfq_line_number",
        ),
        # ERD §8 ck_rfq_lines_required_qty_pos.
        CheckConstraint("required_quantity > 0", name="ck_rfq_lines_required_qty_pos"),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    rfq_id: Mapped[uuid.UUID] = mapped_column()
    line_number: Mapped[int] = mapped_column(Integer())
    # part of composite FK #2 above, not a single-column ForeignKey
    part_id: Mapped[uuid.UUID] = mapped_column()
    required_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    # plain FK, not composite - unit_definitions.organization_id is nullable
    # (global catalogue), same reasoning as Part.unit_definition_id (parts.py).
    unit_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    required_specifications: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    # module docstring point #2: lives here, not on Rfq, per the delegating
    # task's explicit field placement.
    notes: Mapped[str | None] = mapped_column(Text(), default=None)


class RfqSupplier(OrgOwnedBase):
    """One supplier invited to (or excluded from) one RFQ.

    No timestamp/version/archive mixins: 02-erd.md §5 lists none for
    `RFQ_SUPPLIERS`. Invitation/exclusion state is carried by
    `invited_at`/`excluded_at` rather than an enum `status` column - module
    docstring point #4.
    """

    __tablename__ = "rfq_suppliers"
    __table_args__ = (
        org_identity_constraint("rfq_suppliers"),
        # composite org FK: the RFQ this invitation belongs to.
        ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_rfq_suppliers_organization_id_rfq_id",
        ),
        # composite org FK: the invited supplier.
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_rfq_suppliers_organization_id_supplier_id",
        ),
        UniqueConstraint(
            "organization_id",
            "rfq_id",
            "supplier_id",
            name="uq_rfq_suppliers_org_rfq_supplier",
        ),
        # pairing check, same shape as ck_parts_target_price_currency_paired
        # (parts.py): an exclusion reason without an exclusion timestamp (or
        # vice versa) is a data error, not a valid state.
        CheckConstraint(
            "(excluded_at IS NULL) = (exclusion_reason IS NULL)",
            name="ck_rfq_suppliers_exclusion_reason_paired",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    rfq_id: Mapped[uuid.UUID] = mapped_column()
    # part of composite FK #2 above, not a single-column ForeignKey
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    invited_at: Mapped[datetime] = mapped_column()
    excluded_at: Mapped[datetime | None] = mapped_column(default=None)
    exclusion_reason: Mapped[str | None] = mapped_column(Text(), default=None)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)


class RfqStatusHistory(OrgOwnedBase):
    """An append-only log of one RFQ's status transitions
    (`ALLOWED_RFQ_TRANSITIONS` above is the authority on which transitions
    are legal; this table is where every transition that actually happened
    is recorded).

    No timestamp/version/archive mixins beyond `occurred_at` itself: rows
    here are never updated, so `TimestampedMixin`'s `updated_at` would be
    meaningless. Unlike `audit_events` (audit.py) this is *not* the
    append-only-with-a-DB-trigger table - 02-erd.md §3 reserves that
    enforcement for `audit_events` specifically; this table is
    append-friendly by convention (the service layer never issues an
    UPDATE/DELETE against it) but carries no trigger, per the delegating
    task's explicit instruction.
    """

    __tablename__ = "rfq_status_history"
    __table_args__ = (
        org_identity_constraint("rfq_status_history"),
        # composite org FK: the RFQ this history row belongs to.
        ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_rfq_status_history_organization_id_rfq_id",
        ),
        # defense in depth: ALLOWED_RFQ_TRANSITIONS already guarantees no
        # entry is a self-transition (see the property test), but a status
        # history row recording "from X to X" is a data error regardless of
        # which layer would have produced it.
        CheckConstraint(
            "from_status <> to_status", name="ck_rfq_status_history_no_self_transition"
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    rfq_id: Mapped[uuid.UUID] = mapped_column()
    from_status: Mapped[RfqStatus] = mapped_column(RFQ_STATUS_ENUM)
    to_status: Mapped[RfqStatus] = mapped_column(RFQ_STATUS_ENUM)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    note: Mapped[str | None] = mapped_column(Text(), default=None)
    occurred_at: Mapped[datetime] = mapped_column()


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in several FKs on most mappers here (the plain org FK plus one
# or two composite FKs each), so `foreign_keys` disambiguates which
# constraint each relationship follows, and `overlaps` silences SQLAlchemy's
# warning about the shared organization_id column between them - the same
# pattern as BillOfMaterials/BillOfMaterialLine (boms.py).
Rfq.organization = relationship(
    "Organization", foreign_keys=[Rfq.organization_id], lazy="select"
)
Rfq.created_by = relationship("User", foreign_keys=[Rfq.created_by_id], lazy="select")
Rfq.source_bom = relationship(
    "BillOfMaterials",
    foreign_keys=[Rfq.organization_id, Rfq.source_bom_id],
    lazy="select",
    overlaps="organization",
)

RfqLine.organization = relationship(
    "Organization", foreign_keys=[RfqLine.organization_id], lazy="select"
)
RfqLine.rfq = relationship(
    "Rfq",
    foreign_keys=[RfqLine.organization_id, RfqLine.rfq_id],
    lazy="select",
    overlaps="organization",
)
RfqLine.part = relationship(
    "Part",
    foreign_keys=[RfqLine.organization_id, RfqLine.part_id],
    lazy="select",
    overlaps="organization,rfq",
)
RfqLine.unit_definition = relationship(
    "UnitDefinition", foreign_keys=[RfqLine.unit_definition_id], lazy="select"
)

RfqSupplier.organization = relationship(
    "Organization", foreign_keys=[RfqSupplier.organization_id], lazy="select"
)
RfqSupplier.rfq = relationship(
    "Rfq",
    foreign_keys=[RfqSupplier.organization_id, RfqSupplier.rfq_id],
    lazy="select",
    overlaps="organization",
)
RfqSupplier.supplier = relationship(
    "Supplier",
    foreign_keys=[RfqSupplier.organization_id, RfqSupplier.supplier_id],
    lazy="select",
    overlaps="organization,rfq",
)

RfqStatusHistory.organization = relationship(
    "Organization", foreign_keys=[RfqStatusHistory.organization_id], lazy="select"
)
RfqStatusHistory.rfq = relationship(
    "Rfq",
    foreign_keys=[RfqStatusHistory.organization_id, RfqStatusHistory.rfq_id],
    lazy="select",
    overlaps="organization",
)
RfqStatusHistory.actor = relationship(
    "User", foreign_keys=[RfqStatusHistory.actor_user_id], lazy="select"
)

__all__ = [
    "ALLOWED_RFQ_TRANSITIONS",
    "RFQ_STATUS_ENUM",
    "Rfq",
    "RfqLine",
    "RfqStatus",
    "RfqStatusHistory",
    "RfqSupplier",
]
