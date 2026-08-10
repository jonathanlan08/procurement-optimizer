"""Bills of materials: bills_of_materials, bill_of_material_lines
(docs/planning/02-erd.md §5).

**Copy-on-write versioning (ERD §5 / §10).** A BOM is never edited in place
once lines exist. `root_bom_id` is stable across every version of one BOM
(the first version's row sets `root_bom_id` to its own `id` — a standard
self-referential-FK-to-self pattern; Postgres checks the FK after the row
exists, at end of statement, so this is not a chicken-and-egg problem).
Every subsequent edit creates a brand-new `bills_of_materials` row with
`version_number + 1` and `previous_version_id` pointing at the row it
supersedes; the prior row's `status` moves to `superseded` by the service
layer (§8: state machines are enforced there, not by a DB trigger).
`UNIQUE (organization_id, root_bom_id, version_number)` (ERD §10) is both a
data-integrity rule and the concurrency guard for "two analysts create the
next version at once" — the loser's INSERT simply fails the unique
constraint. `root_bom_id` and `previous_version_id` are composite org FKs
back onto this same table, the same `(organization_id, x_id) REFERENCES
parent (organization_id, id)` guard used everywhere else
(docs/planning/00-decisions.md §1.2).

**No optimistic-lock `version` column on `BillOfMaterials`.** ERD §5 lists
`integer version` for `RFQS` in the same diagram, and 02-erd.md §4 /
migration 0004 give `PARTS` and `SUPPLIERS` one too (via `VersionedMixin`),
but the `BILLS_OF_MATERIALS` field list conspicuously does not include it.
That omission is treated as deliberate here, not an oversight: the
copy-on-write chain above already *is* the concurrency control for the one
mutation that matters (creating the next version), so a second, independent
optimistic-lock counter on top of `version_number` would just be a second
mechanism for the same job. `TimestampedMixin` (`created_at`/`updated_at`)
and `ArchivableMixin` are still applied despite the ERD diagram showing only
`created_at`/`archived_at`, following the same precedent already set by
`Part`/`Supplier` (backend/src/app/models/parts.py, suppliers.py): §1's
blanket "created_at/updated_at on every mutable table" and "archived_at +
archived_by_id + archive_reason" soft-delete convention apply regardless of
how compact an individual ERD entity box is — `PARTS`' own box does not list
`created_at` at all, yet migration 0004 creates it.

**`BillOfMaterialLine` carries no timestamp/version/archive mixins at all**,
the same judgement call already made for `PartAlternative`
(backend/src/app/models/parts.py): 02-erd.md §5 lists no such columns for
`BILL_OF_MATERIAL_LINES`, and copy-on-write makes lines immutable snapshots
of one BOM version — there is nothing to update in place or archive
individually; a changed line means a whole new BOM version with a whole new
set of lines.

`BillOfMaterialLine.bom_id` and `.part_id` reference their parents with the
composite org-guard FK (`(organization_id, bom_id) -> bills_of_materials
(organization_id, id)`, `(organization_id, part_id) -> parts
(organization_id, id)`) — a cross-org reference is refused by Postgres
itself, not merely by application code. Both use `ondelete="RESTRICT"`:
02-erd.md §11's cascade whitelist (quote_lines→quote_price_breaks,
extraction_runs→extraction_fields, landed_cost_results→
landed_cost_components, part_import_batches→part_import_rows) does not
include this pair, so it falls to the `RESTRICT` default like everything
else.

`BillOfMaterialLine.substitute_part_id` follows exactly the
`PartAlternative.alternative_part_id` pattern (parts.py): a nullable
composite org FK to `parts`, exempted from the constraint entirely when NULL
(Postgres default `MATCH SIMPLE`). A `CHECK (substitute_part_id IS NULL OR
substitute_part_id <> part_id)` is added by the same reasoning as
`part_alternatives`' `ck_part_alternatives_no_self_reference` — not spelled
out in 02-erd.md §8's "selected, non-exhaustive" catalogue, but the same
shape of bug it exists to prevent there.

`BillOfMaterialLine.unit_definition_id` is a **plain** (non-composite)
foreign key to `unit_definitions.id`, for the same reason as
`Part.unit_definition_id` (parts.py module docstring):
`unit_definitions.organization_id` is nullable (NULL = shared global
catalogue), so the composite org-guard FK can never match a global row.

`line_number` is unique per BOM (`UNIQUE (organization_id, bom_id,
line_number)`) per SPEC §4's implicit ordering of "component lines".
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SaEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    ArchivableMixin,
    OrgOwnedBase,
    TimestampedMixin,
    org_identity_constraint,
)


class BomStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


BOM_STATUS_ENUM = SaEnum(
    BomStatus,
    name="bom_status_enum",
    values_callable=lambda e: [m.value for m in e],
)


class BillOfMaterials(TimestampedMixin, ArchivableMixin, OrgOwnedBase):
    """One version of a product's bill of materials. See module docstring for
    the copy-on-write versioning design and the deliberate absence of
    `VersionedMixin`."""

    __tablename__ = "bills_of_materials"
    __table_args__ = (
        org_identity_constraint("bills_of_materials"),
        # composite org FK #1: stable id shared by every version of one BOM.
        # Self-referential — the first version's row points at itself.
        ForeignKeyConstraint(
            ["organization_id", "root_bom_id"],
            ["bills_of_materials.organization_id", "bills_of_materials.id"],
            ondelete="RESTRICT",
            name="fk_bills_of_materials_organization_id_root_bom_id",
        ),
        # composite org FK #2: the version this row supersedes, if any.
        ForeignKeyConstraint(
            ["organization_id", "previous_version_id"],
            ["bills_of_materials.organization_id", "bills_of_materials.id"],
            ondelete="RESTRICT",
            name="fk_bills_of_materials_organization_id_previous_version_id",
        ),
        # ERD §10: the copy-on-write concurrency guard.
        UniqueConstraint(
            "organization_id",
            "root_bom_id",
            "version_number",
            name="uq_bills_of_materials_org_root_version",
        ),
        CheckConstraint("version_number > 0", name="ck_bills_of_materials_version_number_pos"),
        # pairing check, same shape as ck_parts_target_price_currency_paired
        # (parts.py): the first version of a BOM has no predecessor, and only
        # the first version has none.
        CheckConstraint(
            "(version_number = 1) = (previous_version_id IS NULL)",
            name="ck_bills_of_materials_first_version_no_previous",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    root_bom_id: Mapped[uuid.UUID] = mapped_column()
    version_number: Mapped[int] = mapped_column(Integer(), default=1)
    # part of composite FK #2 above, not a single-column ForeignKey; NULL
    # only for the first version of a BOM (see the paired CHECK above)
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    name: Mapped[str] = mapped_column(Text())
    product_name: Mapped[str] = mapped_column(Text())
    status: Mapped[BomStatus] = mapped_column(BOM_STATUS_ENUM, default=BomStatus.DRAFT)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class BillOfMaterialLine(OrgOwnedBase):
    """One required (or optional, substitutable) component line of one BOM
    version. No timestamp/version/archive columns — see module docstring:
    lines are immutable snapshots under copy-on-write."""

    __tablename__ = "bill_of_material_lines"
    __table_args__ = (
        org_identity_constraint("bill_of_material_lines"),
        # composite org FK: the BOM version this line belongs to.
        ForeignKeyConstraint(
            ["organization_id", "bom_id"],
            ["bills_of_materials.organization_id", "bills_of_materials.id"],
            ondelete="RESTRICT",
            name="fk_bill_of_material_lines_organization_id_bom_id",
        ),
        # composite org FK: the required part.
        ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_bill_of_material_lines_organization_id_part_id",
        ),
        # composite org FK, nullable: an approved substitute for this line's
        # part. NULL is exempt from the constraint (MATCH SIMPLE), same as
        # PartAlternative.alternative_part_id (parts.py).
        ForeignKeyConstraint(
            ["organization_id", "substitute_part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_bill_of_material_lines_organization_id_substitute_part_id",
        ),
        UniqueConstraint(
            "organization_id",
            "bom_id",
            "line_number",
            name="uq_bill_of_material_lines_org_bom_line_number",
        ),
        CheckConstraint(
            "quantity_per_assembly > 0",
            name="ck_bill_of_material_lines_quantity_per_assembly_pos",
        ),
        CheckConstraint(
            "substitute_part_id IS NULL OR substitute_part_id <> part_id",
            name="ck_bill_of_material_lines_substitute_not_self",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    bom_id: Mapped[uuid.UUID] = mapped_column()
    line_number: Mapped[int] = mapped_column(Integer())
    # part of composite FK #2 above, not a single-column ForeignKey
    part_id: Mapped[uuid.UUID] = mapped_column()
    quantity_per_assembly: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    # plain FK, not composite — see module docstring (global catalogue units
    # have organization_id IS NULL, same reasoning as Part.unit_definition_id).
    unit_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    is_optional: Mapped[bool] = mapped_column(Boolean(), default=False)
    # part of composite FK #3 above, not a single-column ForeignKey
    substitute_part_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in several FKs on both mappers here (the plain org FK plus two
# or three composite FKs each), so `foreign_keys` disambiguates which
# constraint each relationship follows, and `overlaps` silences SQLAlchemy's
# warning about the shared organization_id column between them — the same
# pattern as PartAlternative (parts.py).
BillOfMaterials.organization = relationship(
    "Organization", foreign_keys=[BillOfMaterials.organization_id], lazy="select"
)
BillOfMaterials.created_by = relationship(
    "User", foreign_keys=[BillOfMaterials.created_by_id], lazy="select"
)
BillOfMaterials.root_bom = relationship(
    "BillOfMaterials",
    foreign_keys=[BillOfMaterials.organization_id, BillOfMaterials.root_bom_id],
    remote_side=[BillOfMaterials.organization_id, BillOfMaterials.id],
    lazy="select",
    overlaps="organization,created_by",
)
BillOfMaterials.previous_version = relationship(
    "BillOfMaterials",
    foreign_keys=[BillOfMaterials.organization_id, BillOfMaterials.previous_version_id],
    remote_side=[BillOfMaterials.organization_id, BillOfMaterials.id],
    lazy="select",
    overlaps="organization,created_by,root_bom",
)

BillOfMaterialLine.organization = relationship(
    "Organization", foreign_keys=[BillOfMaterialLine.organization_id], lazy="select"
)
BillOfMaterialLine.bom = relationship(
    "BillOfMaterials",
    foreign_keys=[BillOfMaterialLine.organization_id, BillOfMaterialLine.bom_id],
    lazy="select",
    overlaps="organization",
)
BillOfMaterialLine.part = relationship(
    "Part",
    foreign_keys=[BillOfMaterialLine.organization_id, BillOfMaterialLine.part_id],
    lazy="select",
    overlaps="organization,bom",
)
BillOfMaterialLine.substitute_part = relationship(
    "Part",
    foreign_keys=[BillOfMaterialLine.organization_id, BillOfMaterialLine.substitute_part_id],
    lazy="select",
    overlaps="organization,bom,part",
)
BillOfMaterialLine.unit_definition = relationship(
    "UnitDefinition",
    foreign_keys=[BillOfMaterialLine.unit_definition_id],
    lazy="select",
)

__all__ = [
    "BOM_STATUS_ENUM",
    "BillOfMaterialLine",
    "BillOfMaterials",
    "BomStatus",
]
