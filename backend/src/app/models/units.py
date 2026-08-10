"""Unit catalogue: unit_definitions, unit_conversions (docs/planning/02-erd.md §4).

`unit_definitions.organization_id` is nullable by explicit ERD annotation ("null
= global catalog"): NULL rows are the shared standard catalogue (each, pack,
box, tray, reel, kilogram, gram, pound, ounce, meter, centimeter, millimeter,
foot, inch — seeded once by `app.seed.units_catalog.seed_unit_catalog`,
docs/planning/09-task-decomposition.md task 2.5 "global unit catalogue seed");
a non-null organization_id is a unit an organization defined for itself. This
is a deliberate, narrow deviation from the OrgOwnedBase convention (every
other business table in this codebase carries organization_id NOT NULL —
backend/src/app/models/base.py) — it is the one point in the schema where
SPEC §10's "global standard catalogue PLUS user-defined units per
organization" requires a shared row that no single organization owns.

Because the parent's organization_id can be NULL, `UnitConversion.from_unit_id`
/`to_unit_id` are plain (non-composite) foreign keys to `unit_definitions.id`
— the composite org-guard FK used everywhere else
(``FOREIGN KEY (organization_id, x_id) REFERENCES parent (organization_id, id)``,
02-erd.md §1) can never match a NULL parent column, so it is structurally
impossible for this one parent/child pair. See `UnitConversion`'s docstring
for the accepted isolation gap this leaves.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, Numeric, Text
from sqlalchemy import Enum as SaEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, OrgOwnedBase, PkMixin, org_identity_constraint


class UnitDimension(StrEnum):
    COUNT = "count"
    MASS = "mass"
    LENGTH = "length"


DIMENSION_ENUM = SaEnum(
    UnitDimension,
    name="dimension_enum",
    values_callable=lambda e: [m.value for m in e],
)


class UnitDefinition(PkMixin, Base):
    """A unit of measure: a global standard-catalogue entry, or an org's own.

    No timestamps/version/archived_at: 02-erd.md §4 lists none for this table
    — treated as near-immutable reference data, the same judgement call
    already made for `SupplierPerformanceRecord`
    (backend/src/app/models/suppliers.py).

    `to_canonical_factor` is nullable: `count`-dimension containers (pack,
    box, tray, reel) have no universal ratio to `each` — that ratio is a
    property of the part, not the word "reel"
    (docs/planning/05-calculation-methodology.md §5), and is recorded
    per-part (or as an org default) in `UnitConversion` instead. `each`, and
    every `mass`/`length` unit, always has a defined factor.
    """

    __tablename__ = "unit_definitions"
    __table_args__ = (org_identity_constraint("unit_definitions"),)

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), default=None, index=True
    )
    code: Mapped[str] = mapped_column(Text())
    display_name: Mapped[str] = mapped_column(Text())
    dimension: Mapped[UnitDimension] = mapped_column(DIMENSION_ENUM)
    to_canonical_factor: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), default=None)
    is_user_defined: Mapped[bool] = mapped_column(Boolean(), default=False)


class UnitConversion(OrgOwnedBase):
    """An explicit, org-owned conversion assumption between two units.

    Always org-scoped (organization_id NOT NULL, via OrgOwnedBase) even when
    it references the global catalogue: 02-erd.md does not mark this table's
    organization_id nullable, and the "global" mass/length factors (kg<->lb,
    m<->ft) live on `UnitDefinition.to_canonical_factor` itself, not as rows
    here (docs/planning/05-calculation-methodology.md §5) — every row in this
    table is a real assumption someone recorded (an org default or a
    part-specific pack size), so it always has an owning organization and a
    creator. `assumption_note` is required and non-blank (enforced by a CHECK
    in migration 0003): SPEC §10 requires conversion assumptions to be shown
    explicitly, never implicit.

    Known isolation gap: `from_unit_id`/`to_unit_id` are plain FKs to
    `unit_definitions.id` (see module docstring for why the composite guard
    cannot apply here), so a row could in principle reference another
    organization's *private* user-defined unit rather than only the shared
    global catalogue. No code path creates such a row today and the shipped
    catalogue/fixtures never do either; closing this gap needs a trigger or a
    same-org-or-null check at the service layer — a follow-up in the same
    spirit as the accepted gaps already tracked in docs/planning/02-erd.md §12.

    `part_id` has no foreign key yet: `parts` is created by a later migration
    (docs/planning/09-task-decomposition.md task 2.6); the column exists now
    so 02-erd.md's shape is complete, and the FK lands with that migration.
    """

    __tablename__ = "unit_conversions"
    __table_args__ = (org_identity_constraint("unit_conversions"),)

    from_unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    to_unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    # nullable: part-specific pack size (02-erd.md §4); no FK until `parts` exists
    part_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    factor: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    assumption_note: Mapped[str] = mapped_column(Text())
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime]


# UOW insert-ordering relationships (see identity.py comment).
UnitDefinition.organization = relationship("Organization", lazy="select")
UnitConversion.organization = relationship("Organization", lazy="select")
UnitConversion.from_unit = relationship(
    "UnitDefinition", foreign_keys=[UnitConversion.from_unit_id], lazy="select"
)
UnitConversion.to_unit = relationship(
    "UnitDefinition", foreign_keys=[UnitConversion.to_unit_id], lazy="select"
)
UnitConversion.created_by = relationship(
    "User", foreign_keys=[UnitConversion.created_by_id], lazy="select"
)


__all__ = [
    "DIMENSION_ENUM",
    "UnitConversion",
    "UnitDefinition",
    "UnitDimension",
]
