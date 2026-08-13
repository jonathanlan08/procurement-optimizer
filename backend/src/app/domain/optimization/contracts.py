"""Order-allocation optimization contracts - PRINCIPAL-OWNED.

Methodology: docs/planning/06-optimization-methodology.md, rulings in
00-decisions.md §1.1 (determinism) and §2/§4 (cost-basis concentration,
per-quote-line capacity, lead-time eligibility handled pre-solve).

Model (CP-SAT, integers):
- decision variables: alloc[line, offer] (integer units, scaled by QTY),
  used[supplier] (bool), tier[line, offer, k] (bool, exactly-one per used
  offer - all-units price breaks make cost piecewise linear);
- objective: minimize Σ scaled landed unit cost * alloc (+ fixed costs via
  used/first-allocation booleans); money scaled by 10^4 into int64;
- constraints: demand per line (Σ alloc == required), capacity per offer,
  MOQ (alloc == 0 OR alloc >= moq), max supplier count, max concentration
  (COST basis: supplier spend <= cap * total spend), budget, locked
  allocations, exclusions.

Honesty rules (non-negotiable):
- solver status maps 1:1 onto AllocationStatus - FEASIBLE is never presented
  as OPTIMAL;
- the reported expected_total_cost is the EXACT Decimal recomputation of the
  chosen allocation through the landed-cost machinery, never the solver's
  scaled objective;
- infeasibility names the conflicting constraint groups (assumption cores)
  and, where found, a minimal numeric relaxation that restores feasibility;
- determinism: num_search_workers=1, random_seed=0, max_deterministic_time,
  inputs sorted before model build, model_hash stored with every result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

OPTIMIZATION_VERSION: Final[str] = "1.0.0"
MONEY_INT_SCALE: Final[int] = 10_000  # 10^4: Decimal money -> int64 with headroom


class AllocationStatus(StrEnum):
    OPTIMAL = "optimal"  # proven optimal within the deterministic budget
    FEASIBLE = "feasible"  # a valid allocation, optimality NOT proven
    INFEASIBLE = "infeasible"  # no allocation satisfies the constraints
    ERROR = "error"  # solver failure; message preserved, never hidden


@dataclass(frozen=True, slots=True)
class DemandLine:
    rfq_line_id: UUID
    part_label: str  # display only
    required_quantity: int  # integer units after normalization


@dataclass(frozen=True, slots=True)
class OfferTier:
    min_quantity: int
    max_quantity: int | None  # None = open-ended
    landed_unit_cost: Decimal  # per-unit landed cost at this tier (exact)


@dataclass(frozen=True, slots=True)
class Offer:
    """One supplier's quote line for one demand line, landed-cost-normalized."""

    quote_line_id: UUID
    rfq_line_id: UUID
    supplier_id: UUID
    supplier_label: str
    tiers: tuple[OfferTier, ...]  # >= 1, sorted by min_quantity
    moq: int | None
    capacity: int | None  # per-quote-line capacity (00-decisions.md §4 #11)
    fixed_cost: Decimal  # tooling/setup charged once if any allocation > 0
    incomplete_landed_cost: bool  # completeness != COMPLETE -> excluded unless overridden


@dataclass(frozen=True, slots=True)
class EligibilityExclusion:
    """Pre-solve exclusion with a human reason (lead time, spec, user, incomplete)."""

    quote_line_id: UUID
    supplier_label: str
    reason: str


@dataclass(frozen=True, slots=True)
class AllocationConstraints:
    max_supplier_count: int | None = None
    max_concentration: Decimal | None = None  # cost-basis fraction (0, 1]
    budget_limit: Decimal | None = None
    locked_allocations: tuple[LockedAllocation, ...] = field(default_factory=tuple)
    excluded_supplier_ids: tuple[UUID, ...] = field(default_factory=tuple)
    allow_incomplete_offers: bool = False  # user override, recorded


@dataclass(frozen=True, slots=True)
class LockedAllocation:
    rfq_line_id: UUID
    supplier_id: UUID
    quantity: int


@dataclass(frozen=True, slots=True)
class AllocationProblem:
    lines: tuple[DemandLine, ...]
    offers: tuple[Offer, ...]
    constraints: AllocationConstraints
    pre_excluded: tuple[EligibilityExclusion, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AllocationEntry:
    rfq_line_id: UUID
    quote_line_id: UUID
    supplier_id: UUID
    supplier_label: str
    quantity: int
    tier_applied: OfferTier  # recalculated for the ALLOCATED quantity
    line_cost: Decimal  # exact Decimal recomputation


@dataclass(frozen=True, slots=True)
class BindingConstraint:
    name: str  # e.g. "capacity", "max_concentration"
    detail: str  # human sentence with values


@dataclass(frozen=True, slots=True)
class InfeasibilityExplanation:
    conflicting_groups: tuple[str, ...]  # named constraint groups in conflict
    detail: str  # human explanation
    minimal_relaxation: str | None  # e.g. "raising budget_limit to 9,140.00 restores feasibility"


@dataclass(frozen=True, slots=True)
class SolverStats:
    status_raw: str  # CP-SAT status name, verbatim
    deterministic_time: float
    model_hash: str  # sha256 over the canonical serialized model inputs
    num_variables: int
    num_constraints: int
    # 06-optimization-methodology.md §7.4 disclosure (2026-08 calculation
    # audit F1): the objective sees costs rounded half-even at 4 dp, so two
    # offers differing by < 10^-4/unit are indistinguishable to the solver.
    # Conservative worst-case bound on how much cheaper a passed-over
    # alternative could be in exact space:
    #   2 x 5e-5 x (total required units + number of nonzero fixed costs).
    # None only on pre-solve/error paths where no objective was built.
    max_scaling_error: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AllocationResult:
    status: AllocationStatus
    allocations: tuple[AllocationEntry, ...]  # empty unless OPTIMAL/FEASIBLE
    expected_total_cost: Decimal | None  # exact recompute; None unless solved
    supplier_count: int
    binding_constraints: tuple[BindingConstraint, ...]
    infeasibility: InfeasibilityExplanation | None
    rejected_alternatives: tuple[str, ...]  # human sentences, e.g. next-best single-supplier
    stats: SolverStats | None
    error_message: str | None = None
    optimization_version: str = OPTIMIZATION_VERSION
