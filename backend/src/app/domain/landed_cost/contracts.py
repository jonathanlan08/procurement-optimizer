"""Landed-cost calculation contracts.

Formulas (docs/planning/05-calculation-methodology.md §3, ratified in
00-decisions.md; the worked example in 05 §9 carries the corrected
hand-verified values):

    extended_material_cost = normalized_unit_price * accepted_quantity
    allocated_fixed_cost   = tooling + setup + documentation + other_fixed
    logistics_cost         = shipping + insurance + packaging + handling
    import_cost            = tariffs + duties + customs_fees
                             (amounts if quoted; otherwise rate * duty basis,
                              the basis being an explicit labelled assumption)
    quality_risk_cost      = quality_risk_rate * extended_material_cost
    delay_risk_cost        = delay_risk_per_day * max(0, promised - required lead time)
    financing_cost         = extended_material * annual_rate * (baseline_days - terms_days)/365
                             - THE ONE SIGNED COMPONENT: longer payment terms than
                             the baseline produce a negative amount, labelled
                             "financing benefit" (00-decisions.md §2 ruling 2)
    total_landed_cost      = Σ of the seven components
    effective_unit_cost    = total_landed_cost / accepted_quantity

Invariants the implementation MUST uphold:
- every component is quantized at MONEY_SCALE before summing, so the displayed
  components sum EXACTLY to the displayed total;
- effective_unit_cost is quantized at UNIT_PRICE_SCALE and is derived - the
  total is authoritative;
- a missing required input never becomes zero: the component is reported
  missing and the result completeness degrades (INCOMPLETE), unless the caller
  explicitly opted into `assume_missing_costs_zero`, which records an
  assumption and degrades to ASSUMPTION_DEPENDENT;
- the calculator is pure: no I/O, no clock, no randomness; identical inputs
  produce identical results forever (calculation_version bumps otherwise).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from app.domain.values import Completeness, Provenance, Quantified

CALCULATION_VERSION: Final[str] = "1.0.0"


class CostComponent(StrEnum):
    EXTENDED_MATERIAL = "extended_material"
    ALLOCATED_FIXED = "allocated_fixed"
    LOGISTICS = "logistics"
    IMPORT = "import"
    QUALITY_RISK = "quality_risk"
    DELAY_RISK = "delay_risk"
    FINANCING = "financing"


class DutyBasis(StrEnum):
    """Basis for rate-derived import charges. CIF-like is the default
    assumption (00-decisions.md §2 ruling 3) and must be disclosed."""

    MATERIAL_PLUS_LOGISTICS = "material_plus_logistics"  # CIF-like (default)
    MATERIAL_ONLY = "material_only"  # FOB-like


@dataclass(frozen=True, slots=True)
class FixedCosts:
    tooling: Quantified
    setup: Quantified
    documentation: Quantified
    other_fixed: Quantified


@dataclass(frozen=True, slots=True)
class LogisticsCosts:
    shipping: Quantified
    insurance: Quantified
    packaging: Quantified
    handling: Quantified


@dataclass(frozen=True, slots=True)
class ImportCosts:
    """Quoted amounts win; when an amount is MISSING and the matching rate is
    present, the component derives amount = rate * basis and records the basis
    as an assumption."""

    tariff_amount: Quantified
    duty_amount: Quantified
    customs_fees: Quantified
    tariff_rate: Quantified  # fraction (0.035 = 3.5%)
    duty_rate: Quantified
    duty_basis: DutyBasis = DutyBasis.MATERIAL_PLUS_LOGISTICS


@dataclass(frozen=True, slots=True)
class RiskAssumptions:
    quality_risk_rate: Quantified  # fraction of extended material cost
    delay_risk_per_day: Quantified  # money per day late
    promised_lead_time_days: Quantified
    required_lead_time_days: Quantified


@dataclass(frozen=True, slots=True)
class FinancingAssumptions:
    annual_rate: Quantified  # cost of capital, fraction
    payment_terms_days: Quantified
    baseline_terms_days: Quantified  # org default Net-30 unless configured


@dataclass(frozen=True, slots=True)
class LandedCostInput:
    quote_line_id: UUID  # opaque label; the calculator never loads anything
    accepted_quantity: Decimal  # > 0, already unit-normalized
    normalized_unit_price: Quantified  # already unit- and currency-normalized
    currency: str  # RFQ base currency (ISO-4217)
    fixed: FixedCosts
    logistics: LogisticsCosts
    imports: ImportCosts
    risk: RiskAssumptions
    financing: FinancingAssumptions
    assume_missing_costs_zero: bool = False


@dataclass(frozen=True, slots=True)
class MissingInput:
    component: CostComponent
    input_name: str
    consequence: str  # human sentence, printed in reports


@dataclass(frozen=True, slots=True)
class Assumption:
    key: str
    value: str
    description: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ComponentResult:
    component: CostComponent
    amount: Decimal  # quantized MONEY_SCALE; sign only ever negative for FINANCING
    formula: str  # human-readable with substituted values, printed in reports
    inputs: dict[str, str]  # input name -> decimal string or "missing"
    provenance: Provenance  # weakest provenance among the used inputs
    is_assumed: bool
    is_missing: bool  # True when the component could not be computed


@dataclass(frozen=True, slots=True)
class LandedCostResult:
    quote_line_id: UUID
    components: tuple[ComponentResult, ...]  # always all seven, stable order
    total_landed_cost: Decimal  # == sum of computed component amounts, exactly
    effective_unit_cost: Decimal  # total / accepted_quantity @ UNIT_PRICE_SCALE
    currency: str
    completeness: Completeness
    missing_inputs: tuple[MissingInput, ...]
    assumptions: tuple[Assumption, ...]
    calculation_version: str = CALCULATION_VERSION

    def component(self, kind: CostComponent) -> ComponentResult:
        for c in self.components:
            if c.component is kind:
                return c
        raise KeyError(kind)


@dataclass(frozen=True, slots=True)
class NormalizedPrice:
    """Output of unit+currency normalization, input to the calculator."""

    unit_price: Quantified  # in target currency, per target unit
    source_currency: str
    target_currency: str
    fx_rate: Decimal | None  # None when currencies matched
    fx_source: str | None  # e.g. synthetic_fixture | manual | stored
    unit_conversion_factor: Decimal | None  # None when units matched
    conversion_note: str | None


class LandedCostCalculator(Protocol):
    version: str

    def calculate(self, inp: LandedCostInput) -> LandedCostResult: ...


@dataclass(frozen=True, slots=True)
class PriceBreakTier:
    min_quantity: Decimal
    max_quantity: Decimal | None  # None = open-ended (highest tier only)
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class PriceBreakSelection:
    tier: PriceBreakTier | None  # None: quantity below every tier's minimum
    reason: str  # e.g. "quantity 500 falls in tier 100-999"
    tiers_considered: tuple[PriceBreakTier, ...] = field(default_factory=tuple)
