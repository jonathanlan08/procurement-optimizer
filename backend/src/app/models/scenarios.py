"""comparison_scenarios, scenario_results, allocation_results
(docs/planning/02-erd.md §7; docs/planning/02-erd.md §10 snapshot/
reproducibility strategy; docs/SPEC.md §Scenario comparison).

This is a schema-only phase: no routes/services. The delegating task's own
column brief for this file explicitly instructs "Follow the ERD's literal
columns where they differ from this paraphrase" - the OPPOSITE default from
the precedent set in `rfqs.py`/`quotes.py`/`analysis.py` (where the
delegating task's own narrower/differently-shaped field list wins over the
ERD's literal box). That reversal is honored deliberately and unevenly
below, resolved column-by-column against two things the task's paraphrase
does NOT override: (a) the ERD's own literal box for
`COMPARISON_SCENARIOS`/`SCENARIO_RESULTS`/`ALLOCATION_RESULTS` (02-erd.md
§7), and (b) the two FROZEN, principal-owned domain contracts this table
set exists to persist - `app.domain.optimization.contracts.AllocationResult`
(explicitly called out in this task's own READ FIRST list as "the shape you
persist") and `app.domain.scoring.contracts.ScoringResult` (same
"PRINCIPAL-OWNED"/frozen-dataclass status, discovered while reading
`app/domain/scoring/scorer.py`). Every deviation is called out below as a
decision, per this codebase's standing convention.

**1. `ComparisonScenario` follows the ERD's literal, granular snapshot
columns**, not the task paraphrase's single consolidated `inputs_snapshot`
JSONB. The ERD box (02-erd.md §7) gives five separate JSONB columns -
`constraints_snapshot`, `assumptions_snapshot`, `fx_snapshot`,
`quote_snapshot_refs`, `weights_snapshot` - each independently inspectable,
which is strictly more useful for the "historical results reproducible
after assumptions change" requirement (SPEC §Scenario comparison) than one
opaque blob, and is exactly the case the closing "follow the ERD's literal
columns" instruction reads as targeting. `quote_snapshot_refs`' JSONB shape
is documented here to also carry the landed-cost result id used per quote
line (the task paraphrase's "quote ids + landed-cost result ids") - the ERD
box's own inline comment ("quote ids + revisions") predates
`landed_cost_results` existing in this codebase as a queryable table
(`analysis.py`, migration 0011) and could not have anticipated it; folding
it into this JSONB's shape, rather than adding a new column, is also the
only structurally available option, since `landed_cost_results` is an
existing file this task is not permitted to touch and does not itself carry
a `scenario_id` FK (see `analysis.py` module docstring point 1).

**2. `ComparisonScenario.scoring_configuration_id` is a genuine ERD-literal
addition** the task's paraphrase never mentions at all (not a conflict to
resolve in either direction, just a gap the ERD fills): 02-erd.md §7's own
relationship diagram draws `SCORING_CONFIGURATIONS ||--o{
COMPARISON_SCENARIOS : uses`, and `scoring_configurations` already exists in
this codebase (`analysis.py`). Nullable - the `lowest_unit_price` /
`lowest_landed_cost` / `fastest_delivery` / `lowest_risk` strategies are
single-criterion sorts that need no weighted scoring configuration at all;
only `balanced`/`custom` genuinely require one.

**3. `ComparisonScenario.strategy` uses the task's own fuller enum member
spelling** (`lowest_unit_price`, `lowest_landed_cost`, `fastest_delivery`,
`lowest_risk`, `balanced`, `custom`), not the ERD box's abbreviated inline
comment (`lowest_unit_price|lowest_landed|fastest|lowest_risk|balanced|
custom`). Four of six members are already verbatim-identical between the
two; the other two are strict, self-documenting expansions of the same
concept (SPEC §Scenario comparison's own prose: "lowest quoted unit price,
lowest total landed cost, fastest delivery, lowest supply risk, balanced,
user-configured"), not a contradictory rename - taken as the task's
own explicit, freshly-reconciled spelling of "per ERD/SPEC," the same
"verbatim task spelling wins, documented as intentional" precedent
`rfqs.py`/`quotes.py` already establish for renamed columns.

**4. `ComparisonScenario.state`, not `status`.** The task's paraphrase says
"status if ERD has one"; the ERD's literal column for this concept is
spelled `state` (`scenario_state_enum state "draft|running|complete|
failed"`), so the literal ERD name is used verbatim.

**5. `ComparisonScenario.notes` is a genuine addition beyond the ERD box**,
which has no `notes` column for `COMPARISON_SCENARIOS` at all - kept as
requested by the task's own explicit field list, the same "additive, not a
reshaping of anything the ERD already specifies" reasoning `rfqs.py`
documents for `RfqSupplier.notes`.

**6. `ComparisonScenario` timestamps/version/archivable** are the task's
own explicit ask ("timestamps, version, archivable") and are consistent
with, not contradicted by, the ERD: 02-erd.md §1's global convention
mandates `created_at`/`updated_at` on every *mutable* table and `version`
on every mutable *aggregate* regardless of whether an individual ERD box
spells out `updated_at` (the same gap `rfqs.py`'s module docstring already
notes and resolves the same way for `RFQS`), and §11 explicitly lists
"scenarios" among the entities that are never hard-deleted, which is
exactly what `archived_at`/`archived_by_id`/`archive_reason` implement. A
`ComparisonScenario` transitions `draft -> running -> complete|failed`, so
it is mutable in the same sense `Rfq`/`Quote` are.

**7. `ScenarioResult` is ONE row per `ComparisonScenario`, not one row per
supplier/quote as the ERD's literal `SCENARIO_RESULTS` box implies** (that
box carries per-row `supplier_id`, `quote_id`, `total_landed_cost`,
`effective_unit_cost`, `currency`, `weighted_score`, `criterion_scores`,
`completeness`, `missing_data`, `warnings`, `rank`). This is the one place
the task's paraphrase is followed over the ERD's literal shape, because the
paraphrase's four columns ("scoring output JSONB (ranked scores incl.
reasons), calculation_version, scoring_version, computed_at") map exactly
onto `app.domain.scoring.contracts.ScoringResult` - a FROZEN,
principal-owned dataclass whose `scores: tuple[SupplierScore, ...]` field
*is* "ranked scores incl. reasons" (`SupplierScore.criterion_scores` already
carries a human-auditable `reason` string per criterion) and whose
`scoring_version` field is the literal name the task paraphrase also uses.
Persisting the whole `ScoringResult` (`scores` + `weights_used` +
`cohort_size` + `notes`, minus `scoring_version` which gets its own column)
as one JSONB column keeps each `ScenarioResult` row independently
interpretable without joining back to `ComparisonScenario.weights_snapshot`.
`calculation_version` records which landed-cost calculation version fed the
`total_landed_cost`/`effective_unit_cost` figures that were scored (the ERD
box has no timestamp or version column for `SCENARIO_RESULTS` at all; both
`calculation_version` and `computed_at` are additions the task's paraphrase
supplies and the ERD is simply silent on, not a case of "differs from").

**8. `AllocationResultRecord` mirrors `app.domain.optimization.contracts.
AllocationResult` field-for-field** (this task's own READ FIRST instruction
calls that dataclass "the shape you persist - FROZEN"), which is also
exactly what the task's own paraphrase for this table already describes
column-by-column. Deviations from the ERD's literal `ALLOCATION_RESULTS`
box, each because the FROZEN dataclass shape wins over the ERD box where
the two differ:
   - `status` (task's literal name), typed by wrapping
     `app.domain.optimization.contracts.AllocationStatus` directly - the
     same "domain enum is the single source of truth, the DB type just
     mirrors it" pattern `analysis.py`'s `RESULT_COMPLETENESS_ENUM`/
     `LANDED_COST_COMPONENT_ENUM` and `documents.py`'s
     `CONFIDENCE_BAND_ENUM` already establish. `AllocationStatus` has four
     members (`optimal|feasible|infeasible|error`); the ERD's literal
     `solver_status_enum` has five (`optimal|feasible|infeasible|
     solver_error|timeout`) and would let a persisted row express a status
     the domain contract cannot produce - the FROZEN contract's member set
     wins.
   - `expected_total_cost` (task's literal name, `NUMERIC(18,6)` nullable
     exactly as instructed), not the ERD's `objective_total_cost` - matches
     `AllocationResult.expected_total_cost` and its docstring's own
     "the EXACT Decimal recomputation ... never the solver's scaled
     objective" framing, which the ERD's name does not convey.
   - `solved_at` (task's literal name), not the ERD's `computed_at`.
   - `optimization_version` is its own top-level column (mirrors
     `AllocationResult.optimization_version`, always known - it identifies
     the deployed contract/solver-model version - even when `stats` itself
     is `None`), separate from the `stats` JSONB blob, exactly as the task
     paraphrase lists it ("solver stats JSONB incl. model_hash,
     optimization_version").
   - `model_hash` is promoted to its own top-level `text` column *in
     addition to* appearing inside the serialized `stats` JSONB (which is
     the verbatim serialization of `SolverStats`, whose own `model_hash`
     field does not go away) - reconciling the task paraphrase ("solver
     stats JSONB incl. model_hash", nested) with the ERD's literal box
     (`text model_hash`, a standalone column) rather than picking one
     over the other, since a standalone column is genuinely useful for a
     future model-hash dedup/cache lookup and costs nothing to keep
     alongside the JSONB. `model_hash` is nullable, paired with `stats`
     (`ck_allocation_results_model_hash_stats_paired`): `SolverStats`
     (and therefore `model_hash`) does not exist for an `ERROR` result
     that failed before a model was ever built.
   - `error_message` is a genuine addition beyond the task's own explicit
     column list for this table, needed to persist
     `AllocationResult.error_message: str | None` losslessly - since this
     task's READ FIRST instruction calls the whole dataclass "the shape you
     persist," omitting the one field the paraphrase's bullet list happens
     not to enumerate would silently drop data the FROZEN contract carries.
   - `supplier_count` is likewise a genuine addition for the same reason -
     `AllocationResult.supplier_count: int` (not optional on the frozen
     dataclass) has no ERD-box or task-paraphrase counterpart at all.
   - The ERD's `char3 currency` column is deliberately NOT carried over:
     `AllocationResult` has no currency field at all (the solved objective
     is denominated in whatever `ComparisonScenario`'s owning RFQ's
     `base_currency` already is), and the FROZEN dataclass shape is the
     tighter, load-bearing source of truth for what actually gets computed
     and persisted here.
   - The ERD's `solver_seed` is NOT modeled as a column: per
     `contracts.py`'s own module docstring, determinism fixes
     `random_seed=0` as a constant solver configuration value, not data
     that varies per result - there is nothing to persist per-row. Noted
     here explicitly as an accepted, deliberate ERD gap, in the spirit of
     02-erd.md §12's own "spec gaps" list.

**Cascades (02-erd.md §11).** `comparison_scenarios -> scenario_results`
and `comparison_scenarios -> allocation_results` are NOT on the ERD's
explicit CASCADE whitelist (only four pairs are: `quote_lines ->
quote_price_breaks`, `extraction_runs -> extraction_fields`,
`landed_cost_results -> landed_cost_components`, `part_import_batches ->
part_import_rows`), so both are `RESTRICT` by the "everything else is
RESTRICT" default - the same letter-of-the-whitelist discipline
`quotes.py`/`analysis.py` already apply.

**Immutability (02-erd.md §10).** `scenario_results` and
`allocation_results` rows are never updated: recomputing a scenario's
results creates a NEW `ComparisonScenario` row entirely ("historical
results reproducible after assumptions change" is satisfied by the
snapshot on `comparison_scenarios`, not by mutating a result row). Both
tables therefore carry no `TimestampedMixin`/`VersionedMixin`/
`ArchivableMixin` at all - no `updated_at`, no `version`, no `archived_at` -
which is also how the schema test asserts immutability (columns absent,
not merely unused). `UNIQUE (organization_id, scenario_id)` on both tables
encodes "at most one result row per scenario" directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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

from app.domain.optimization.contracts import AllocationStatus
from app.models.base import (
    ArchivableMixin,
    OrgOwnedBase,
    TimestampedMixin,
    VersionedMixin,
    org_identity_constraint,
)


class ComparisonStrategy(StrEnum):
    """Module docstring point 3: fuller spelling of the ERD's abbreviated
    `strategy_enum` members, per the task's own explicit "per ERD/SPEC"
    reconciliation."""

    LOWEST_UNIT_PRICE = "lowest_unit_price"
    LOWEST_LANDED_COST = "lowest_landed_cost"
    FASTEST_DELIVERY = "fastest_delivery"
    LOWEST_RISK = "lowest_risk"
    BALANCED = "balanced"
    CUSTOM = "custom"


COMPARISON_STRATEGY_ENUM = SaEnum(
    ComparisonStrategy,
    name="comparison_strategy_enum",
    values_callable=lambda e: [m.value for m in e],
)


class ScenarioState(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


SCENARIO_STATE_ENUM = SaEnum(
    ScenarioState,
    name="scenario_state_enum",
    values_callable=lambda e: [m.value for m in e],
)

# Wraps app.domain.optimization.contracts.AllocationStatus directly (module
# docstring point 8): the DB type's members can never drift from the FROZEN
# domain enum's, the same pattern analysis.py/documents.py already
# establish for their own domain-backed enums.
ALLOCATION_STATUS_ENUM = SaEnum(
    AllocationStatus,
    name="allocation_status_enum",
    values_callable=lambda e: [m.value for m in e],
)


class ComparisonScenario(TimestampedMixin, VersionedMixin, ArchivableMixin, OrgOwnedBase):
    """One saved, reproducible what-if comparison over one RFQ's quotes. See
    module docstring points 1-6 for the ERD-literal snapshot columns,
    the `scoring_configuration_id` addition, the `strategy` spelling, the
    `state` naming, the `notes` addition, and the timestamps/version/
    archivable rationale."""

    __tablename__ = "comparison_scenarios"
    __table_args__ = (
        org_identity_constraint("comparison_scenarios"),
        # composite org FK: the RFQ this scenario analyzes.
        ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_comparison_scenarios_organization_id_rfq_id",
        ),
        # composite org FK, nullable (module docstring point 2): not every
        # strategy needs a weighted scoring configuration.
        ForeignKeyConstraint(
            ["organization_id", "scoring_configuration_id"],
            ["scoring_configurations.organization_id", "scoring_configurations.id"],
            ondelete="RESTRICT",
            # shortened from the naming convention's full form (organization_id
            # + scoring_configuration_id would exceed Postgres's 63-char
            # identifier limit).
            name="fk_comparison_scenarios_org_scoring_configuration_id",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    rfq_id: Mapped[uuid.UUID] = mapped_column()
    name: Mapped[str] = mapped_column(Text())
    # module docstring point 3
    strategy: Mapped[ComparisonStrategy] = mapped_column(COMPARISON_STRATEGY_ENUM)
    # part of composite FK #2 above, not a single-column ForeignKey; nullable
    scoring_configuration_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    # ERD-literal granular snapshot columns (module docstring point 1), the
    # FULL reproducibility snapshot per 02-erd.md §10.
    constraints_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB(), default=dict)
    assumptions_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB(), default=dict)
    fx_snapshot: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    # shape includes landed-cost result ids per quote line, not just quote
    # ids + revisions - module docstring point 1.
    quote_snapshot_refs: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    weights_snapshot: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    calculation_version: Mapped[str] = mapped_column(Text())
    solver_version: Mapped[str] = mapped_column(Text())
    # module docstring point 4: ERD's literal "state", not "status".
    state: Mapped[ScenarioState] = mapped_column(SCENARIO_STATE_ENUM, default=ScenarioState.DRAFT)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    completed_at: Mapped[datetime | None] = mapped_column(default=None)
    # module docstring point 5: addition beyond the ERD box.
    notes: Mapped[str | None] = mapped_column(Text(), default=None)


class ScenarioResult(OrgOwnedBase):
    """The immutable scoring outcome of one `ComparisonScenario` - one row
    per scenario, not one row per supplier (module docstring point 7): the
    whole `app.domain.scoring.contracts.ScoringResult` (ranked
    `SupplierScore`s incl. per-criterion reasons, weights used, cohort size,
    notes) is persisted as one JSONB column.

    No timestamp/version/archive mixins: immutable by convention (module
    docstring "Immutability").
    """

    __tablename__ = "scenario_results"
    __table_args__ = (
        org_identity_constraint("scenario_results"),
        # composite org FK: the scenario this result belongs to. RESTRICT
        # per 02-erd.md §11's whitelist (module docstring "Cascades").
        ForeignKeyConstraint(
            ["organization_id", "scenario_id"],
            ["comparison_scenarios.organization_id", "comparison_scenarios.id"],
            ondelete="RESTRICT",
            name="fk_scenario_results_organization_id_scenario_id",
        ),
        # at most one result row per scenario (module docstring
        # "Immutability"): re-scoring creates a new ComparisonScenario.
        UniqueConstraint(
            "organization_id", "scenario_id", name="uq_scenario_results_org_scenario"
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    scenario_id: Mapped[uuid.UUID] = mapped_column()
    # the serialized ScoringResult (scores incl. reasons, weights_used,
    # cohort_size, notes) minus scoring_version, which gets its own column
    # below - module docstring point 7.
    scoring_output: Mapped[dict[str, Any]] = mapped_column(JSONB())
    calculation_version: Mapped[str] = mapped_column(Text())
    scoring_version: Mapped[str] = mapped_column(Text())
    computed_at: Mapped[datetime] = mapped_column()


class AllocationResultRecord(OrgOwnedBase):
    """The immutable optimizer outcome of one `ComparisonScenario`. Mirrors
    `app.domain.optimization.contracts.AllocationResult` field-for-field -
    module docstring point 8 for every naming/shape decision and the
    additions (`error_message`, `supplier_count`) needed to persist that
    FROZEN dataclass losslessly.

    No timestamp/version/archive mixins: immutable by convention (module
    docstring "Immutability").
    """

    __tablename__ = "allocation_results"
    __table_args__ = (
        org_identity_constraint("allocation_results"),
        # composite org FK: the scenario this result belongs to. RESTRICT
        # per 02-erd.md §11's whitelist (module docstring "Cascades").
        ForeignKeyConstraint(
            ["organization_id", "scenario_id"],
            ["comparison_scenarios.organization_id", "comparison_scenarios.id"],
            ondelete="RESTRICT",
            name="fk_allocation_results_organization_id_scenario_id",
        ),
        # at most one result row per scenario (module docstring
        # "Immutability"): re-solving creates a new ComparisonScenario.
        UniqueConstraint(
            "organization_id", "scenario_id", name="uq_allocation_results_org_scenario"
        ),
        CheckConstraint(
            "expected_total_cost IS NULL OR expected_total_cost >= 0",
            name="ck_allocation_results_expected_total_cost_nonneg",
        ),
        CheckConstraint(
            "supplier_count >= 0", name="ck_allocation_results_supplier_count_nonneg"
        ),
        # SolverStats (and therefore model_hash) does not exist for an
        # ERROR result that failed before a model was ever built - module
        # docstring point 8.
        CheckConstraint(
            "(stats IS NULL) = (model_hash IS NULL)",
            name="ck_allocation_results_model_hash_stats_paired",
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    scenario_id: Mapped[uuid.UUID] = mapped_column()
    # wraps AllocationStatus directly (module docstring point 8).
    status: Mapped[AllocationStatus] = mapped_column(ALLOCATION_STATUS_ENUM)
    # AllocationEntry-shaped dicts; empty unless status is OPTIMAL/FEASIBLE.
    allocations: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    # exact Decimal recompute, never the solver's scaled objective; None
    # unless solved.
    expected_total_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    supplier_count: Mapped[int] = mapped_column(Integer(), default=0)
    binding_constraints: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    infeasibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    rejected_alternatives: Mapped[list[Any]] = mapped_column(JSONB(), default=list)
    # serialized SolverStats (status_raw, deterministic_time, model_hash,
    # num_variables, num_constraints); None only when status is ERROR before
    # a model was ever built.
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    # standalone copy of stats->model_hash (module docstring point 8).
    model_hash: Mapped[str | None] = mapped_column(Text(), default=None)
    optimization_version: Mapped[str] = mapped_column(Text())
    error_message: Mapped[str | None] = mapped_column(Text(), default=None)
    solved_at: Mapped[datetime] = mapped_column()


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in several FKs on most mappers here (the plain org FK plus one
# or more composite FKs each), so `foreign_keys` disambiguates which
# constraint each relationship follows, and `overlaps` silences SQLAlchemy's
# warning about the shared organization_id column between them - the same
# pattern as rfqs.py/quotes.py/analysis.py.
ComparisonScenario.organization = relationship(
    "Organization", foreign_keys=[ComparisonScenario.organization_id], lazy="select"
)
ComparisonScenario.rfq = relationship(
    "Rfq",
    foreign_keys=[ComparisonScenario.organization_id, ComparisonScenario.rfq_id],
    lazy="select",
    overlaps="organization",
)
ComparisonScenario.scoring_configuration = relationship(
    "ScoringConfiguration",
    foreign_keys=[
        ComparisonScenario.organization_id,
        ComparisonScenario.scoring_configuration_id,
    ],
    lazy="select",
    overlaps="organization,rfq",
)
ComparisonScenario.created_by = relationship(
    "User", foreign_keys=[ComparisonScenario.created_by_id], lazy="select"
)

ScenarioResult.organization = relationship(
    "Organization", foreign_keys=[ScenarioResult.organization_id], lazy="select"
)
ScenarioResult.scenario = relationship(
    "ComparisonScenario",
    foreign_keys=[ScenarioResult.organization_id, ScenarioResult.scenario_id],
    lazy="select",
    overlaps="organization",
)

AllocationResultRecord.organization = relationship(
    "Organization", foreign_keys=[AllocationResultRecord.organization_id], lazy="select"
)
AllocationResultRecord.scenario = relationship(
    "ComparisonScenario",
    foreign_keys=[AllocationResultRecord.organization_id, AllocationResultRecord.scenario_id],
    lazy="select",
    overlaps="organization",
)


__all__ = [
    "ALLOCATION_STATUS_ENUM",
    "COMPARISON_STRATEGY_ENUM",
    "SCENARIO_STATE_ENUM",
    "AllocationResultRecord",
    "ComparisonScenario",
    "ComparisonStrategy",
    "ScenarioResult",
    "ScenarioState",
]
