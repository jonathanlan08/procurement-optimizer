"""Parts master data: parts, part_alternatives (docs/planning/02-erd.md §4).

`PartAlternative` references its parent part with a composite foreign key
`(organization_id, part_id) -> parts(organization_id, id)`, the same isolation
control as `SupplierContact` (backend/src/app/models/suppliers.py). It
additionally carries a *second* composite foreign key on the same pattern for
`alternative_part_id` - an approved alternative is itself a part in the same
organization's catalogue. `alternative_part_id` is nullable (02-erd.md §4:
"nullable if external"): when the alternative is a part this organization has
never catalogued internally, `alternative_mpn` records its manufacturer part
number instead, and the composite FK is simply not checked for that row
(Postgres default MATCH SIMPLE: a NULL member of a composite FK exempts the
row from the constraint).

`Part.unit_definition_id` is a **plain** (non-composite) foreign key to
`unit_definitions.id`, not the composite org-guard pattern used elsewhere.
This mirrors the comment on `UnitConversion.from_unit_id`/`to_unit_id`
(backend/src/app/models/units.py): `unit_definitions.organization_id` is
nullable (NULL = shared global catalogue), so the composite FK
`(organization_id, unit_definition_id) -> unit_definitions(organization_id, id)`
can never match a global row - it is structurally impossible to use the
composite guard against a parent whose own organization_id may be NULL.

`Part.normalized_key` is a `GENERATED ALWAYS ... STORED` column. 02-erd.md §4
describes it only as "generated, for text matching" without naming a source
expression, and 02-erd.md §9/§12 point the normalized-text/fuzzy strategy at
`pg_trgm`, which this project does not use at all
(docs/planning/00-decisions.md §7; migration 0001's "No extensions by
design" comment). Two decisions fill that gap; neither is dictated verbatim
by the ERD, so both are recorded here:

1. **Source column: `internal_part_number`.** docs/planning/04-document-
   pipeline.md §10 lists `normalized_text` as strategy 4, an *equality*
   match on `normalized_key` - distinct from strategy 5's fuzzy
   `rapidfuzz.token_set_ratio` over description + MPN, which already covers
   free-text. An equality match after aggressive normalization is only
   useful against a short identifier a supplier is likely to reproduce
   near-verbatim but with different punctuation/casing/spacing (e.g.
   "ACME-100 Rev.B" vs "ACME100REVB") - that is what `internal_part_number`
   is, not a human-written description that is almost never
   byte-identical even after normalization. `internal_part_number` is also
   exactly the field strategy 1 (exact match) targets, so strategy 4 reads
   naturally as its normalization-tolerant fallback.
2. **Index, not extension.** 02-erd.md §9 specifies
   `USING gin (normalized_key gin_trgm_ops)` for fuzzy candidate retrieval;
   migration 0004 creates a plain btree index instead (no `pg_trgm`
   available). This fully supports the *equality* strategy above; it does
   not support trigram similarity search. Fuzzy retrieval (strategy 5)
   stays entirely application-side (`rapidfuzz`) over an explicit candidate
   set, which is how 04-document-pipeline.md §10 already describes it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Computed, ForeignKey, ForeignKeyConstraint, Numeric, Text
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


class Part(TimestampedMixin, VersionedMixin, ArchivableMixin, OrgOwnedBase):
    """A catalogue item an organization requests quotes against and builds
    BOMs from.

    `internal_part_number` uniqueness is enforced by a partial,
    case-insensitive index `(organization_id, lower(internal_part_number))
    WHERE archived_at IS NULL` created in migration 0004 (02-erd.md §8
    `uq_parts_org_ipn_active`) - the same convention as `Supplier.code`
    (backend/src/app/models/suppliers.py). Archiving frees the number for
    reuse.
    """

    __tablename__ = "parts"
    __table_args__ = (org_identity_constraint("parts"),)

    internal_part_number: Mapped[str] = mapped_column(Text())
    manufacturer_part_number: Mapped[str | None] = mapped_column(Text(), default=None)
    manufacturer: Mapped[str | None] = mapped_column(Text(), default=None)
    name: Mapped[str] = mapped_column(Text())
    description: Mapped[str | None] = mapped_column(Text(), default=None)
    category: Mapped[str | None] = mapped_column(Text(), default=None)
    # plain FK, not composite - see module docstring (global catalogue units
    # have organization_id IS NULL, same reasoning as UnitConversion).
    unit_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    required_specifications: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    target_price_currency: Mapped[str | None] = mapped_column(CURRENCY_CHAR, default=None)
    # GENERATED ALWAYS AS ... STORED in migration 0004 (see module docstring
    # for the source-column and indexing decisions the ERD leaves open).
    # `Computed` here mirrors that server-side definition so the ORM never
    # attempts to write a value for it (Postgres rejects any client-supplied
    # value for a GENERATED ALWAYS column) and reads the server-computed
    # value back via RETURNING after insert.
    normalized_key: Mapped[str] = mapped_column(
        Text(),
        Computed(
            "regexp_replace(lower(internal_part_number), '[^a-z0-9]', '', 'g')",
            persisted=True,
        ),
    )
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)


class PartAlternative(OrgOwnedBase):
    """An approved (or conditional/rejected) alternative for a part.

    No `created_at`/`updated_at`/`version`/`archived_at`: 02-erd.md §4 lists
    none of these for this table - the same judgement call already made for
    `SupplierPerformanceRecord` (backend/src/app/models/suppliers.py); this
    is a decision record with its own `approved_at`, not a mutable aggregate
    with a lifecycle of its own.
    """

    __tablename__ = "part_alternatives"
    __table_args__ = (
        org_identity_constraint("part_alternatives"),
        # composite org FK #1: the part this row is an alternative *for*
        ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_part_alternatives_organization_id_part_id",
        ),
        # composite org FK #2: the alternative part itself, when internal.
        # Nullable - see module docstring - so Postgres MATCH SIMPLE skips
        # this constraint entirely for external (alternative_mpn-only) rows.
        ForeignKeyConstraint(
            ["organization_id", "alternative_part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_part_alternatives_organization_id_alternative_part_id",
        ),
    )

    # part of the composite FKs above, not single-column ForeignKeys
    part_id: Mapped[uuid.UUID] = mapped_column()
    # nullable: "nullable if external" (02-erd.md §4) - see module docstring
    alternative_part_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    alternative_mpn: Mapped[str | None] = mapped_column(Text(), default=None)
    approval_status: Mapped[str] = mapped_column(Text())  # approved|conditional|rejected
    rationale: Mapped[str | None] = mapped_column(Text(), default=None)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    approved_at: Mapped[datetime | None] = mapped_column(default=None)


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in three FKs on PartAlternative (the plain org FK and two
# composite parts FKs), so `foreign_keys` disambiguates which constraint each
# relationship follows, and `overlaps` silences SQLAlchemy's warning about the
# shared organization_id column between them.
Part.organization = relationship("Organization", lazy="select")
Part.unit_definition = relationship(
    "UnitDefinition", foreign_keys=[Part.unit_definition_id], lazy="select"
)

PartAlternative.organization = relationship(
    "Organization", foreign_keys=[PartAlternative.organization_id], lazy="select"
)
PartAlternative.part = relationship(
    "Part",
    foreign_keys=[PartAlternative.organization_id, PartAlternative.part_id],
    lazy="select",
    overlaps="organization",
)
PartAlternative.alternative_part = relationship(
    "Part",
    foreign_keys=[PartAlternative.organization_id, PartAlternative.alternative_part_id],
    lazy="select",
    overlaps="organization,part",
)
PartAlternative.approved_by = relationship(
    "User", foreign_keys=[PartAlternative.approved_by_id], lazy="select"
)

__all__ = [
    "Part",
    "PartAlternative",
]
