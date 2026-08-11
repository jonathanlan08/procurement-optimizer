"""landed_cost_results, landed_cost_components, scoring_configurations
(docs/planning/02-erd.md §7).

**Deliberate ERD/task deviations**, called out here per this project's
convention of documenting every departure from the ERD's literal box rather
than silently narrowing or renaming:

1. **`LandedCostResult` follows this task's own narrower, explicit column
   list**, not the ERD's fuller `LANDED_COST_RESULTS` box. The ERD also
   carries `scenario_id` (nullable FK to `comparison_scenarios`),
   `source_currency`, `exchange_rate_id`, and `manual_overrides`; none of
   those are in this task's explicit brief, and `comparison_scenarios` does
   not exist yet in this codebase (a later phase), so `scenario_id` could not
   even be typed as a real FK today. The task's own `inputs_snapshot` JSONB
   is explicitly specified to capture "the full assembled input incl.
   fx/unit normalization provenance" — the functional replacement for
   `source_currency`/`exchange_rate_id`/`manual_overrides` in this schema-only
   phase (every FX rate id/value and unit-conversion note that produced the
   result is inside that snapshot, not broken out into its own columns).
2. **`LandedCostComponent.provenance` is a plain `text` column**, per the
   task's literal spelling ("provenance text"). The ERD calls the same
   concept `data_source`. Named per the task's explicit instruction.
3. **`ScoringConfiguration.weights` (JSONB), not the ERD's `criteria` +
   `weight_sum`.** The task's own column list is explicit: "weights JSONB
   (list of CriterionSpec-shaped objects)" — no separate `weight_sum`
   column; the sum is a validation-time computation
   (`ScoringConfigService`), not stored state (02-erd.md §8's own scoring
   note: "weights are stored raw ... normalized at calculation time, not at
   storage time, so the user's raw intent survives" — the same reasoning
   extends to not persisting a derived sum either).
4. **`ScoringConfiguration.created_by_id`, not the task prose's unsuffixed
   "created_by".** Every other `created_by`-shaped column in this codebase
   (`Rfq.created_by_id`, `BillOfMaterials.created_by_id`,
   `PartImportBatch.created_by_id`, ...) carries the `_id` suffix; treated as
   the task's own shorthand rather than a deliberate rename (unlike point 2
   above, which specifies an unambiguous, differently-shaped column), so the
   codebase-wide convention wins here.

Both enums below wrap a principal-owned domain enum directly rather than
re-declaring its members, so the DB type's members can never drift from the
domain module's — the same pattern `app/models/documents.py`'s
`CONFIDENCE_BAND_ENUM` already establishes for `app.domain.confidence.
ConfidenceBand`:
- `RESULT_COMPLETENESS_ENUM` wraps `app.domain.values.Completeness`.
- `LANDED_COST_COMPONENT_ENUM` wraps
  `app.domain.landed_cost.contracts.CostComponent` (all seven components).

**Immutability.** `landed_cost_results` rows are never updated: a
recalculation always inserts a new row (`LandedCostService.calculate_and_store`).
"Latest wins per line" is a query-time concern
(`ix_landed_cost_results_line_latest` exists for exactly that lookup), not a
DB constraint — there is nothing here preventing multiple rows per
`quote_line_id`, by design.

**Cascades (02-erd.md §11).** `landed_cost_results` -> `landed_cost_components`
is on the ERD's explicit CASCADE whitelist ("the child cannot exist alone");
`quote_lines` -> `landed_cost_results` is `RESTRICT` by the ERD's "everything
else is RESTRICT" default, the same letter-of-the-whitelist discipline
`app/models/quotes.py`/`app/models/boms.py`/`app/models/documents.py` already
apply.

**Uniqueness.** `scoring_configurations` enforces "unique active name per
org" via a functional, case-insensitive partial index created directly in
the migration (`uq_scoring_configurations_org_name_active`) — the same
no-`citext`-extension adaptation `Rfq.internal_reference` /
`Part.internal_part_number` already use; there is no SQLAlchemy
`__table_args__` entry for it here because a `lower(name)` expression index
cannot be expressed as a declarative `UniqueConstraint`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.landed_cost.contracts import CostComponent
from app.domain.values import Completeness
from app.models.base import (
    CURRENCY_CHAR,
    ArchivableMixin,
    OrgOwnedBase,
    TimestampedMixin,
    VersionedMixin,
    org_identity_constraint,
)

RESULT_COMPLETENESS_ENUM = SaEnum(
    Completeness,
    name="landed_cost_result_completeness_enum",
    values_callable=lambda e: [m.value for m in e],
)

LANDED_COST_COMPONENT_ENUM = SaEnum(
    CostComponent,
    name="landed_cost_component_enum",
    values_callable=lambda e: [m.value for m in e],
)


class LandedCostResult(OrgOwnedBase):
    """One immutable landed-cost calculation for one quote line. See module
    docstring point 1 for the narrower-than-ERD column set."""

    __tablename__ = "landed_cost_results"
    __table_args__ = (
        org_identity_constraint("landed_cost_results"),
        # composite org FK: the quote line this result was calculated for.
        ForeignKeyConstraint(
            ["organization_id", "quote_line_id"],
            ["quote_lines.organization_id", "quote_lines.id"],
            ondelete="RESTRICT",
            name="fk_landed_cost_results_organization_id_quote_line_id",
        ),
        CheckConstraint(
            "accepted_quantity > 0", name="ck_landed_cost_results_accepted_quantity_pos"
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_landed_cost_results_currency_iso"),
    )

    # part of composite FK above, not a single-column ForeignKey
    quote_line_id: Mapped[uuid.UUID] = mapped_column()
    accepted_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    currency: Mapped[str] = mapped_column(CURRENCY_CHAR)
    total_landed_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    effective_unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    completeness: Mapped[Completeness] = mapped_column(RESULT_COMPLETENESS_ENUM)
    calculation_version: Mapped[str] = mapped_column(Text())
    # the full assembled LandedCostInput incl. FX/unit normalization
    # provenance — see module docstring point 1.
    inputs_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB())
    missing_inputs: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    assumptions: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    calculated_at: Mapped[datetime] = mapped_column()
    calculated_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class LandedCostComponent(OrgOwnedBase):
    """One of the seven cost components (`CostComponent`) making up one
    `LandedCostResult`. See module docstring point 2 for the `provenance`
    column naming."""

    __tablename__ = "landed_cost_components"
    __table_args__ = (
        org_identity_constraint("landed_cost_components"),
        # composite org FK: the result this component belongs to. CASCADE
        # per 02-erd.md §11's explicit whitelist.
        ForeignKeyConstraint(
            ["organization_id", "landed_cost_result_id"],
            ["landed_cost_results.organization_id", "landed_cost_results.id"],
            ondelete="CASCADE",
            name="fk_landed_cost_components_organization_id_landed_cost_result_id",
        ),
        # defensive: LandedCostResult.components always has exactly one row
        # per CostComponent (contracts.py: "always all seven, stable order").
        UniqueConstraint(
            "organization_id",
            "landed_cost_result_id",
            "component",
            name="uq_landed_cost_components_org_result_component",
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    landed_cost_result_id: Mapped[uuid.UUID] = mapped_column()
    component: Mapped[CostComponent] = mapped_column(LANDED_COST_COMPONENT_ENUM)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    formula: Mapped[str] = mapped_column(Text())
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB())
    # module docstring point 2: task's literal spelling; ERD calls this
    # "data_source".
    provenance: Mapped[str] = mapped_column(Text())
    is_assumed: Mapped[bool] = mapped_column(Boolean(), default=False)
    is_missing: Mapped[bool] = mapped_column(Boolean(), default=False)


class ScoringConfiguration(TimestampedMixin, VersionedMixin, ArchivableMixin, OrgOwnedBase):
    """A named, org-owned set of vendor-scoring criterion weights. See module
    docstring points 3-4 for the `weights`/`created_by_id` naming and the
    "unique active name per org" functional index (migration-only, not
    expressible in `__table_args__`).
    """

    __tablename__ = "scoring_configurations"
    __table_args__ = (org_identity_constraint("scoring_configurations"),)

    name: Mapped[str] = mapped_column(Text())
    # list of CriterionSpec-shaped objects — module docstring point 3.
    weights: Mapped[list[Any]] = mapped_column(JSONB())
    is_sample: Mapped[bool] = mapped_column(Boolean(), default=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in a composite FK on LandedCostResult/LandedCostComponent
# alongside the plain org FK, so `foreign_keys` disambiguates which
# constraint each relationship follows, and `overlaps` silences SQLAlchemy's
# warning about the shared organization_id column — the same pattern as
# quotes.py/rfqs.py.
LandedCostResult.organization = relationship(
    "Organization", foreign_keys=[LandedCostResult.organization_id], lazy="select"
)
LandedCostResult.quote_line = relationship(
    "QuoteLine",
    foreign_keys=[LandedCostResult.organization_id, LandedCostResult.quote_line_id],
    lazy="select",
    overlaps="organization",
)
LandedCostResult.calculated_by = relationship(
    "User", foreign_keys=[LandedCostResult.calculated_by_id], lazy="select"
)

LandedCostComponent.organization = relationship(
    "Organization", foreign_keys=[LandedCostComponent.organization_id], lazy="select"
)
LandedCostComponent.result = relationship(
    "LandedCostResult",
    foreign_keys=[
        LandedCostComponent.organization_id,
        LandedCostComponent.landed_cost_result_id,
    ],
    lazy="select",
    overlaps="organization",
)

ScoringConfiguration.organization = relationship(
    "Organization", foreign_keys=[ScoringConfiguration.organization_id], lazy="select"
)
ScoringConfiguration.created_by = relationship(
    "User", foreign_keys=[ScoringConfiguration.created_by_id], lazy="select"
)


__all__ = [
    "LANDED_COST_COMPONENT_ENUM",
    "RESULT_COMPLETENESS_ENUM",
    "LandedCostComponent",
    "LandedCostResult",
    "ScoringConfiguration",
]
