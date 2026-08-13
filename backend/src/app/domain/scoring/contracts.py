"""Vendor-scoring contracts - PRINCIPAL-OWNED.

SPEC §Vendor comparison engine, methodology in 05 §7, rulings in
00-decisions.md §2 (#5: missing criterion values renormalize weights; missing
is shown, never imputed).

Guarantees the implementation must uphold:
- deterministic and reproducible WITHOUT an LLM: same inputs -> same scores,
  bit-for-bit, forever within a scoring_version;
- every criterion score carries a reason string a human can audit;
- direction is explicit per criterion (higher_is_better / lower_is_better);
- zero-weight criteria are evaluated for display but contribute nothing;
- suppliers with a missing criterion value get that criterion's weight
  renormalized away FOR THEM (documented per supplier), never a fabricated 0;
- excluded suppliers appear in results as excluded-with-reason, unscored;
- normalization is min-max across the compared cohort; equal values across
  the cohort score 1.0 for everyone (nobody is penalized for a tie);
- outliers are NOT clipped in v0.1 (documented; clipping would hide real
  price outliers, which is exactly what this product must not do).

Weights are fractions summing to ~1; the scorer renormalizes defensively and
records when it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

SCORING_VERSION: Final[str] = "1.0.0"


class Direction(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class Criterion(StrEnum):
    TOTAL_LANDED_COST = "total_landed_cost"  # lower
    EFFECTIVE_UNIT_COST = "effective_unit_cost"  # lower
    SPEC_COMPLIANCE = "spec_compliance"  # higher, fraction
    LEAD_TIME = "lead_time"  # lower, days
    CAPACITY = "capacity"  # higher
    MOQ_FLEXIBILITY = "moq_flexibility"  # higher (lower MOQ scores better; inverted upstream)
    PAYMENT_TERMS = "payment_terms"  # higher, days of credit
    QUALITY_HISTORY = "quality_history"  # higher, fraction
    DEFECT_RATE = "defect_rate"  # lower, fraction
    ON_TIME_DELIVERY = "on_time_delivery"  # higher, fraction
    USER_DEFINED = "user_defined"


DEFAULT_DIRECTIONS: Final[dict[Criterion, Direction]] = {
    Criterion.TOTAL_LANDED_COST: Direction.LOWER_IS_BETTER,
    Criterion.EFFECTIVE_UNIT_COST: Direction.LOWER_IS_BETTER,
    Criterion.SPEC_COMPLIANCE: Direction.HIGHER_IS_BETTER,
    Criterion.LEAD_TIME: Direction.LOWER_IS_BETTER,
    Criterion.CAPACITY: Direction.HIGHER_IS_BETTER,
    Criterion.MOQ_FLEXIBILITY: Direction.HIGHER_IS_BETTER,
    Criterion.PAYMENT_TERMS: Direction.HIGHER_IS_BETTER,
    Criterion.QUALITY_HISTORY: Direction.HIGHER_IS_BETTER,
    Criterion.DEFECT_RATE: Direction.LOWER_IS_BETTER,
    Criterion.ON_TIME_DELIVERY: Direction.HIGHER_IS_BETTER,
}


@dataclass(frozen=True, slots=True)
class CriterionSpec:
    criterion: Criterion
    weight: Decimal  # fraction; 0 allowed (evaluated, contributes nothing)
    direction: Direction
    label: str  # display label; required for USER_DEFINED
    is_sample_weight: bool = False  # demonstration weights are labelled as such

    def __post_init__(self) -> None:
        # The API boundary already enforces weight in [0, 1]; this is the
        # defence-in-depth the scorer's [0,1] total-score proof relies on -
        # a negative weight silently voids it (2026-08 calculation audit F10).
        if self.weight < 0:
            raise ValueError(f"criterion weight must be >= 0, got {self.weight}")


@dataclass(frozen=True, slots=True)
class SupplierCriterionValue:
    """A raw criterion observation for one supplier. None = missing (shown,
    renormalized away for that supplier, never imputed)."""

    criterion: Criterion
    value: Decimal | None
    source: str  # e.g. "landed_cost_result", "supplier_performance", "user_override"


@dataclass(frozen=True, slots=True)
class SupplierScoringInput:
    supplier_id: UUID
    supplier_name: str
    values: tuple[SupplierCriterionValue, ...]
    excluded: bool = False
    exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CriterionScore:
    criterion: Criterion
    raw_value: Decimal | None
    normalized_score: Decimal | None  # [0, 1]; None when missing
    effective_weight: Decimal  # after per-supplier renormalization
    weighted_contribution: Decimal  # normalized * effective_weight, 0 if missing
    reason: str  # human-auditable, e.g. "lowest landed cost 7240.55 of cohort 7240.55-9100.00"


@dataclass(frozen=True, slots=True)
class SupplierScore:
    supplier_id: UUID
    supplier_name: str
    total_score: Decimal  # [0, 1] at RATIO_SCALE
    rank: int  # 1-based; ties share the smallest rank
    criterion_scores: tuple[CriterionScore, ...]
    missing_criteria: tuple[Criterion, ...]
    weights_renormalized: bool
    excluded: bool = False
    exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScoringResult:
    scores: tuple[SupplierScore, ...]  # ranked, excluded suppliers last
    weights_used: tuple[CriterionSpec, ...]
    cohort_size: int  # scored (non-excluded) suppliers
    notes: tuple[str, ...] = field(default_factory=tuple)  # e.g. global renormalizations
    scoring_version: str = SCORING_VERSION


class VendorScorer(Protocol):
    version: str

    def score(
        self,
        suppliers: tuple[SupplierScoringInput, ...],
        weights: tuple[CriterionSpec, ...],
    ) -> ScoringResult: ...


# Demonstration weights (SPEC example), labelled as sample assumptions.
SAMPLE_WEIGHTS: Final[tuple[CriterionSpec, ...]] = (
    CriterionSpec(Criterion.TOTAL_LANDED_COST, Decimal("0.35"),
                  Direction.LOWER_IS_BETTER, "Total landed cost", True),
    CriterionSpec(Criterion.SPEC_COMPLIANCE, Decimal("0.25"),
                  Direction.HIGHER_IS_BETTER, "Specification compliance", True),
    CriterionSpec(Criterion.LEAD_TIME, Decimal("0.15"),
                  Direction.LOWER_IS_BETTER, "Lead time", True),
    CriterionSpec(Criterion.ON_TIME_DELIVERY, Decimal("0.10"),
                  Direction.HIGHER_IS_BETTER, "Supplier reliability", True),
    CriterionSpec(Criterion.PAYMENT_TERMS, Decimal("0.05"),
                  Direction.HIGHER_IS_BETTER, "Payment terms", True),
    CriterionSpec(Criterion.MOQ_FLEXIBILITY, Decimal("0.05"),
                  Direction.HIGHER_IS_BETTER, "MOQ flexibility", True),
    CriterionSpec(Criterion.QUALITY_HISTORY, Decimal("0.05"),
                  Direction.HIGHER_IS_BETTER, "Quality history", True),
)
