"""Part import batches: part_import_batches, part_import_rows
(docs/planning/02-erd.md §4; SPEC §3 "CSV and XLSX import with: header
validation, row-level validation, duplicate detection, import preview,
transactional import, rollback after failure, import audit event").

Field names follow 02-erd.md's PART_IMPORT_BATCHES/PART_IMPORT_ROWS boxes
exactly (`state`, not `status`; `raw_values`/`normalized_values`, not "raw
payload"/"parsed payload"; `disposition`, not "row status") rather than this
task's own prose, which used different names for the same concepts - the ERD
is the more authoritative source for the schema shape.

**`PartImportBatch.format` is not in 02-erd.md's box.** Added anyway,
following the same precedent already recorded in `app/models/boms.py`'s
module docstring for `BillOfMaterials` carrying `TimestampedMixin`/
`ArchivableMixin` beyond what its own ERD box lists: the spec
explicitly calls for a `format csv|xlsx` column, and every table in this
schema already carries strictly more structure than its own compact ERD box
notation shows in at least one place (BOMs is the precedent; `Part` is
another - 02-erd.md §4's own `PARTS` box omits `created_at` entirely, yet
migration 0004 creates it, per §1's blanket "created_at/updated_at on every
mutable table" rule). Unlike the BOM case, this module does *not* add
`TimestampedMixin`/`ArchivableMixin`/`VersionedMixin`: 02-erd.md's
`PART_IMPORT_BATCHES` box lists `created_at` and `committed_at` and no other
timestamp, and reads as deliberately minimal rather than merely compact -
there is no `updated_at`-worthy field that mutates outside the two
transitions those two columns already date (`created_at` for the row's
insertion, `committed_at` for `commit`); the "when did this become
`rolled_back`" question is answered by the `part_import.cancelled` audit
event's `occurred_at` instead, the same way 02-erd.md leaves several other
less-central transitions to the audit trail rather than a dedicated column.
No `version` (optimistic-lock) column either: `state` itself is the
concurrency guard for the one mutation that matters (`commit`/`cancel`
require `state = 'previewing'`, exactly the same job a `version` column
would do), the same reasoning `app/models/boms.py` already gives for why
`BillOfMaterials` has no `version` despite `Part`/`Supplier` having one.

**`PartImportRow.disposition` implements a subset of 02-erd.md's own
documented value set.** The ERD box literally spells out `"create|update|
skip_duplicate|error"` for this column, but nothing in SPEC §3, the
brief, or docs/planning/03-api-contract.md §4.5 describes an *update*
policy for a part import (matching-on-internal-part-number-and-overwriting
existing fields is a real, separate product decision - which fields
overwrite? does version/optimistic-locking apply? is there a diff-preview?
- entirely unspecified). `PartImportRowDisposition` is a 3-value StrEnum
(`create`, `skip_duplicate`, `error`); `update` is documented but not
emitted by any code path here, the same "column stores a slightly wider
domain than the application currently uses" situation already accepted for
`PartAlternative.approval_status` (`app/models/parts.py`) - plain `Text()`,
no CHECK constraint, so adding `update` later is a pure application-layer
change with no migration required.

**`PartImportRow.batch_id` FK uses `ondelete="CASCADE"`**, the one
deliberate exception to this schema's `RESTRICT`-by-default rule
(`app/models/base.py`): 02-erd.md §11 names this exact pair -
"`part_import_batches`→`part_import_rows`" - in its explicit cascade
whitelist ("`ON DELETE CASCADE` only inside an aggregate where the child
cannot exist alone"), the same list that licenses `bill_of_material_lines`'s
FK to `bills_of_materials` (`app/models/boms.py`).

**`PartImportRow.resulting_part_id`** is a composite org-guard FK to
`parts` - `(organization_id, resulting_part_id) -> parts(organization_id,
id)` - following the same pattern as `BillOfMaterialLine.part_id`/
`PartAlternative.alternative_part_id`, not the plain, unqualified FK this
task's brief guessed at ("no FK per ERD if absent there"): 02-erd.md's own
`PART_IMPORT_ROWS` box marks the column `FK`, so the composite guard
applies exactly as everywhere else a row references `parts`. Nullable and
exempt from the constraint when NULL (`MATCH SIMPLE`, Postgres default) -
every row starts with no resulting part (preview only), and rows with
`disposition` `skip_duplicate`/`error` never gain one.

`row_number` is unique per batch (`UNIQUE (organization_id, batch_id,
row_number)`), the same shape as `bill_of_material_lines`'s `line_number`
uniqueness (`app/models/boms.py`) - 02-erd.md does not spell this out for
`PART_IMPORT_ROWS` but two rows claiming the same position within one
import is exactly the class of bug that constraint exists to catch
elsewhere in this schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import OrgOwnedBase, org_identity_constraint


class PartImportFormat(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"


PART_IMPORT_FORMAT_ENUM = SaEnum(
    PartImportFormat,
    name="part_import_format_enum",
    values_callable=lambda e: [m.value for m in e],
)


class PartImportState(StrEnum):
    PREVIEWING = "previewing"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


IMPORT_STATE_ENUM = SaEnum(
    PartImportState,
    name="import_state_enum",
    values_callable=lambda e: [m.value for m in e],
)


class PartImportRowDisposition(StrEnum):
    """Plain `Text()` column, not a DB enum - see module docstring for why
    the ERD's fourth value (`update`) is documented but unimplemented."""

    CREATE = "create"
    SKIP_DUPLICATE = "skip_duplicate"
    ERROR = "error"


class PartImportBatch(OrgOwnedBase):
    """One uploaded CSV/XLSX file: its parse/validation outcome and, once
    committed, the fact that it was. See module docstring for the deliberate
    absence of `TimestampedMixin`/`ArchivableMixin`/`VersionedMixin`."""

    __tablename__ = "part_import_batches"
    __table_args__ = (org_identity_constraint("part_import_batches"),)

    source_filename: Mapped[str] = mapped_column(Text())
    file_sha256: Mapped[bytes] = mapped_column(LargeBinary())
    # not in 02-erd.md's PART_IMPORT_BATCHES box - see module docstring
    format: Mapped[PartImportFormat] = mapped_column(PART_IMPORT_FORMAT_ENUM)
    state: Mapped[PartImportState] = mapped_column(
        IMPORT_STATE_ENUM, default=PartImportState.PREVIEWING
    )
    rows_total: Mapped[int] = mapped_column(Integer(), default=0)
    rows_valid: Mapped[int] = mapped_column(Integer(), default=0)
    rows_invalid: Mapped[int] = mapped_column(Integer(), default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer(), default=0)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime]
    committed_at: Mapped[datetime | None] = mapped_column(default=None)


class PartImportRow(OrgOwnedBase):
    """One data row of a batch: what was uploaded, what it normalized to,
    what (if anything) is wrong with it, and - after a successful commit -
    which `Part` it produced. See module docstring for the CASCADE FK to
    `part_import_batches`, the composite-org-guard FK to `parts`, and the
    3-value `disposition` domain."""

    __tablename__ = "part_import_rows"
    __table_args__ = (
        org_identity_constraint("part_import_rows"),
        ForeignKeyConstraint(
            ["organization_id", "batch_id"],
            ["part_import_batches.organization_id", "part_import_batches.id"],
            ondelete="CASCADE",
            name="fk_part_import_rows_organization_id_batch_id",
        ),
        # nullable composite org FK; NULL is exempt from the constraint
        # (MATCH SIMPLE default), same as part_alternatives.alternative_part_id
        # (app/models/parts.py) / bill_of_material_lines.substitute_part_id
        # (app/models/boms.py).
        ForeignKeyConstraint(
            ["organization_id", "resulting_part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_part_import_rows_organization_id_resulting_part_id",
        ),
        UniqueConstraint(
            "organization_id",
            "batch_id",
            "row_number",
            name="uq_part_import_rows_org_batch_row_number",
        ),
        CheckConstraint("row_number > 0", name="ck_part_import_rows_row_number_pos"),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    batch_id: Mapped[uuid.UUID] = mapped_column()
    row_number: Mapped[int] = mapped_column(Integer())
    raw_values: Mapped[dict[str, Any]] = mapped_column(JSONB())
    normalized_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    disposition: Mapped[str] = mapped_column(Text())
    errors: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    # part of composite FK #2 above, not a single-column ForeignKey
    resulting_part_id: Mapped[uuid.UUID | None] = mapped_column(default=None)


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in several FKs on PartImportRow, so `foreign_keys`
# disambiguates which constraint each relationship follows, and `overlaps`
# silences SQLAlchemy's warning about the shared organization_id column
# between them - the same pattern as BillOfMaterialLine (app/models/boms.py).
PartImportBatch.organization = relationship(
    "Organization", foreign_keys=[PartImportBatch.organization_id], lazy="select"
)
PartImportBatch.created_by = relationship(
    "User", foreign_keys=[PartImportBatch.created_by_id], lazy="select"
)

PartImportRow.organization = relationship(
    "Organization", foreign_keys=[PartImportRow.organization_id], lazy="select"
)
PartImportRow.batch = relationship(
    "PartImportBatch",
    foreign_keys=[PartImportRow.organization_id, PartImportRow.batch_id],
    lazy="select",
    overlaps="organization",
)
PartImportRow.resulting_part = relationship(
    "Part",
    foreign_keys=[PartImportRow.organization_id, PartImportRow.resulting_part_id],
    lazy="select",
    overlaps="organization,batch",
)

__all__ = [
    "IMPORT_STATE_ENUM",
    "PART_IMPORT_FORMAT_ENUM",
    "PartImportBatch",
    "PartImportFormat",
    "PartImportRow",
    "PartImportRowDisposition",
    "PartImportState",
]
