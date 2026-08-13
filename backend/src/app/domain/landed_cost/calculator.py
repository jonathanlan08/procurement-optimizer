"""Landed-cost calculator - implementation of the principal-owned contract.

Implements `LandedCostCalculator` (`contracts.py`, FROZEN). Pure domain code:
no database, no clock, no network, no randomness. See
`docs/planning/05-calculation-methodology.md` §1-3, §9 and
`docs/planning/00-decisions.md` §2-3 for the ratified formulas and roundings
this module must reproduce exactly.

Design notes (interpretations of the contract's prose, spelled out because the
contract's docstring is normative but not exhaustive pseudocode):

- `assume_missing_costs_zero` governs only the additive money *amounts* that
  make up `allocated_fixed`, `logistics`, and the three `import` charges
  (tariff/duty/customs). It does not apply to rates, lead times, or financing
  assumptions (`quality_risk_rate`, `delay_risk_per_day`, the two lead times,
  `annual_rate`, `payment_terms_days`, `baseline_terms_days`): those missing
  inputs always produce a `missing_inputs` entry and drive the result toward
  `INCOMPLETE`, regardless of the flag. The contract's per-component prose
  scopes the flag explicitly to "fixed/logistics/import amount" and never
  mentions it for the multiplicative components.
- `EXTENDED_MATERIAL`, `QUALITY_RISK`, `DELAY_RISK`, and `FINANCING` are
  products/derived quantities, not sums of independent line items: a missing
  *factor* cannot be treated as "contributes nothing to the sum" without
  silently treating a missing value as zero, which this codebase forbids
  everywhere else. So for these components, ANY missing required input makes
  the whole component `is_missing=True` (amount reported as 0, excluded from
  the total's real contribution, and excluded from provenance aggregation) -
  this mirrors the "every input missing" carve-out the contract spells out
  for the additive components, just applied per-formula rather than
  per-component-with-many-fields.
- Missingness cascades: if `normalized_unit_price` is missing,
  `EXTENDED_MATERIAL` is `is_missing`, and any component that consumes
  `extended_material_cost` as a factor (`QUALITY_RISK`, `FINANCING`, and the
  CIF-like import duty basis) treats that as its own missing input rather
  than silently multiplying by the placeholder 0 stored on the extended
  component's `amount`.
- The CIF-like (`MATERIAL_PLUS_LOGISTICS`) import duty basis is computed from
  the already-quantized `EXTENDED_MATERIAL` and `LOGISTICS` component
  amounts (matching the worked example's arithmetic exactly:
  `0.035 * (5706.521740 + 375.000000)`), and only when neither of those two
  components is itself `is_missing`.
- `AmountOutOfRangeError` (mentioned in 05 §10 point 10) is intentionally not
  implemented: it is not part of the frozen `contracts.py`, and this task's
  extreme-value requirement is only that the calculator not blow up - the 34
  digits of `CALC_PRECISION` comfortably cover quantity 10^12 and price
  10^-8.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final

from app.core.money import CALC_CONTEXT, quantize_money, quantize_unit_price, to_wire
from app.domain.landed_cost.contracts import (
    Assumption,
    ComponentResult,
    CostComponent,
    DutyBasis,
    FinancingAssumptions,
    FixedCosts,
    ImportCosts,
    LandedCostInput,
    LandedCostResult,
    LogisticsCosts,
    MissingInput,
    RiskAssumptions,
)
from app.domain.values import Completeness, Provenance, Quantified


class ZeroQuantityError(ValueError):
    """accepted_quantity == 0; never silently divide or return Infinity/NaN."""

    def __init__(self, quantity: Decimal) -> None:
        super().__init__(f"accepted_quantity must be > 0, got {quantity}")
        self.quantity = quantity


class NegativeQuantityError(ValueError):
    """accepted_quantity < 0; a negative accepted quantity is never valid."""

    def __init__(self, quantity: Decimal) -> None:
        super().__init__(f"accepted_quantity must be > 0, got {quantity}")
        self.quantity = quantity


_PROVENANCE_STRENGTH: Final[dict[Provenance, int]] = {
    Provenance.SUPPLIER: 0,
    Provenance.USER_INPUT: 1,
    Provenance.CALCULATED: 2,
    Provenance.USER_ASSUMPTION: 3,
    Provenance.DEFAULT: 4,
}


def _weakest(provenances: Sequence[Provenance]) -> Provenance:
    """The weakest (least authoritative) provenance among inputs actually used.

    Order strongest -> weakest: SUPPLIER > USER_INPUT > CALCULATED >
    USER_ASSUMPTION > DEFAULT. Never called with MISSING: a missing input is
    never "used" to compute a value (see module docstring).
    """
    if not provenances:
        raise ValueError("_weakest requires at least one provenance")
    return max(provenances, key=lambda p: _PROVENANCE_STRENGTH[p])


@dataclass(frozen=True, slots=True)
class _Resolved:
    """The outcome of resolving one additive money field."""

    value: Decimal
    used: bool  # True: contributes a real or assumed-zero value; False: excluded
    provenance: Provenance | None
    is_assumed: bool
    missing: MissingInput | None
    assumption: Assumption | None


def _resolve_amount_field(
    q: Quantified,
    *,
    component: CostComponent,
    field_label: str,
    assume_missing_zero: bool,
) -> _Resolved:
    """Resolve a single additive money field (a fixed/logistics/import amount)."""
    if q.value is not None:
        return _Resolved(q.value, True, q.provenance, q.is_assumed, None, None)
    if assume_missing_zero:
        assumption = Assumption(
            key=f"{component.value}.{field_label}",
            value="0",
            description="missing costs assumed zero",
            provenance=Provenance.USER_ASSUMPTION,
        )
        return _Resolved(Decimal("0"), True, Provenance.USER_ASSUMPTION, True, None, assumption)
    missing = MissingInput(
        component=component,
        input_name=field_label,
        consequence=(
            f"{field_label} is not stated and assume_missing_costs_zero is False; "
            f"{component.value} excludes it until supplied."
        ),
    )
    return _Resolved(Decimal("0"), False, None, False, missing, None)


def _finalize_additive(
    component: CostComponent,
    resolved: Sequence[tuple[str, _Resolved]],
    original_inputs: dict[str, str],
) -> tuple[ComponentResult, list[MissingInput], list[Assumption]]:
    missing_inputs: list[MissingInput] = []
    assumptions: list[Assumption] = []
    used_provenances: list[Provenance] = []
    any_used = False
    is_assumed = False
    terms: list[str] = []

    with localcontext(CALC_CONTEXT):
        total = Decimal("0")
        for name, r in resolved:
            total += r.value
            terms.append(f"{name}={to_wire(r.value)}")
            if r.used:
                any_used = True
                if r.provenance is not None:
                    used_provenances.append(r.provenance)
                if r.is_assumed:
                    is_assumed = True
            if r.missing is not None:
                missing_inputs.append(r.missing)
            if r.assumption is not None:
                assumptions.append(r.assumption)
        amount = quantize_money(total)

    if not any_used:
        provenance = Provenance.MISSING
        is_missing = True
        formula = f"all {component.value} inputs missing; {component.value} cost cannot be computed"
    else:
        provenance = _weakest(used_provenances) if used_provenances else Provenance.MISSING
        is_missing = False
        formula = " + ".join(terms) + f" = {to_wire(amount)}"

    result = ComponentResult(
        component=component,
        amount=amount,
        formula=formula,
        inputs=original_inputs,
        provenance=provenance,
        is_assumed=is_assumed,
        is_missing=is_missing,
    )
    return result, missing_inputs, assumptions


def _extended_material(
    price: Quantified, quantity: Decimal
) -> tuple[ComponentResult, list[MissingInput]]:
    qty_str = to_wire(quantity)
    if price.value is None:
        missing = MissingInput(
            component=CostComponent.EXTENDED_MATERIAL,
            input_name="normalized_unit_price",
            consequence=(
                "normalized unit price is not stated; extended material cost, and every "
                "component that depends on it, cannot be computed."
            ),
        )
        result = ComponentResult(
            component=CostComponent.EXTENDED_MATERIAL,
            amount=Decimal("0"),
            formula="normalized_unit_price missing; extended material cost cannot be computed",
            inputs={"normalized_unit_price": "missing", "accepted_quantity": qty_str},
            provenance=Provenance.MISSING,
            is_assumed=False,
            is_missing=True,
        )
        return result, [missing]

    with localcontext(CALC_CONTEXT):
        value = price.value * quantity
        amount = quantize_money(value)
    result = ComponentResult(
        component=CostComponent.EXTENDED_MATERIAL,
        amount=amount,
        formula=f"{to_wire(price.value)} * {qty_str} = {to_wire(amount)}",
        inputs={"normalized_unit_price": to_wire(price.value), "accepted_quantity": qty_str},
        provenance=price.provenance,
        is_assumed=price.is_assumed,
        is_missing=False,
    )
    return result, []


def _fixed(
    fixed: FixedCosts, assume_missing_zero: bool
) -> tuple[ComponentResult, list[MissingInput], list[Assumption]]:
    fields: list[tuple[str, Quantified]] = [
        ("tooling", fixed.tooling),
        ("setup", fixed.setup),
        ("documentation", fixed.documentation),
        ("other_fixed", fixed.other_fixed),
    ]
    resolved = [
        (
            name,
            _resolve_amount_field(
                q,
                component=CostComponent.ALLOCATED_FIXED,
                field_label=name,
                assume_missing_zero=assume_missing_zero,
            ),
        )
        for name, q in fields
    ]
    original_inputs = {
        name: ("missing" if q.value is None else to_wire(q.value)) for name, q in fields
    }
    return _finalize_additive(CostComponent.ALLOCATED_FIXED, resolved, original_inputs)


def _logistics(
    logistics: LogisticsCosts, assume_missing_zero: bool
) -> tuple[ComponentResult, list[MissingInput], list[Assumption]]:
    fields: list[tuple[str, Quantified]] = [
        ("shipping", logistics.shipping),
        ("insurance", logistics.insurance),
        ("packaging", logistics.packaging),
        ("handling", logistics.handling),
    ]
    resolved = [
        (
            name,
            _resolve_amount_field(
                q,
                component=CostComponent.LOGISTICS,
                field_label=name,
                assume_missing_zero=assume_missing_zero,
            ),
        )
        for name, q in fields
    ]
    original_inputs = {
        name: ("missing" if q.value is None else to_wire(q.value)) for name, q in fields
    }
    return _finalize_additive(CostComponent.LOGISTICS, resolved, original_inputs)


def _resolve_duty_charge(
    *,
    label: str,
    amount_q: Quantified,
    rate_q: Quantified,
    basis: DutyBasis,
    extended: ComponentResult,
    logistics: ComponentResult,
    assume_missing_zero: bool,
) -> _Resolved:
    """Quoted amount wins; else derive rate * basis; else missing/assumed-zero."""
    if amount_q.value is not None:
        return _Resolved(amount_q.value, True, amount_q.provenance, amount_q.is_assumed, None, None)

    if rate_q.value is not None:
        if basis is DutyBasis.MATERIAL_PLUS_LOGISTICS:
            basis_available = not (extended.is_missing or logistics.is_missing)
            basis_amount = extended.amount + logistics.amount if basis_available else None
        else:  # MATERIAL_ONLY
            basis_available = not extended.is_missing
            basis_amount = extended.amount if basis_available else None

        if basis_amount is not None:
            with localcontext(CALC_CONTEXT):
                derived = rate_q.value * basis_amount
            assumption = Assumption(
                key=f"import.{label}.duty_basis",
                value=basis.value,
                description=f"{label} amount not quoted; derived as rate x {basis.value} basis",
                provenance=Provenance.USER_ASSUMPTION,
            )
            provenance = _weakest([rate_q.provenance, Provenance.USER_ASSUMPTION])
            return _Resolved(derived, True, provenance, True, None, assumption)

        missing = MissingInput(
            component=CostComponent.IMPORT,
            input_name=f"{label}_basis",
            consequence=(
                f"{label} rate is stated but the {basis.value} basis cannot be computed because "
                f"extended material or logistics cost is unknown; {label} amount cannot be derived."
            ),
        )
        return _Resolved(Decimal("0"), False, None, False, missing, None)

    return _resolve_amount_field(
        amount_q,
        component=CostComponent.IMPORT,
        field_label=f"{label}_amount",
        assume_missing_zero=assume_missing_zero,
    )


def _import(
    imports: ImportCosts,
    extended: ComponentResult,
    logistics: ComponentResult,
    assume_missing_zero: bool,
) -> tuple[ComponentResult, list[MissingInput], list[Assumption]]:
    tariff = _resolve_duty_charge(
        label="tariff",
        amount_q=imports.tariff_amount,
        rate_q=imports.tariff_rate,
        basis=imports.duty_basis,
        extended=extended,
        logistics=logistics,
        assume_missing_zero=assume_missing_zero,
    )
    duty = _resolve_duty_charge(
        label="duty",
        amount_q=imports.duty_amount,
        rate_q=imports.duty_rate,
        basis=imports.duty_basis,
        extended=extended,
        logistics=logistics,
        assume_missing_zero=assume_missing_zero,
    )
    customs = _resolve_amount_field(
        imports.customs_fees,
        component=CostComponent.IMPORT,
        field_label="customs_fees",
        assume_missing_zero=assume_missing_zero,
    )
    resolved = [("tariff", tariff), ("duty", duty), ("customs_fees", customs)]
    original_inputs = {
        "tariff_amount": _fmt_or_missing(imports.tariff_amount),
        "tariff_rate": _fmt_or_missing(imports.tariff_rate),
        "duty_amount": _fmt_or_missing(imports.duty_amount),
        "duty_rate": _fmt_or_missing(imports.duty_rate),
        "customs_fees": _fmt_or_missing(imports.customs_fees),
        "duty_basis": imports.duty_basis.value,
    }
    return _finalize_additive(CostComponent.IMPORT, resolved, original_inputs)


def _fmt_or_missing(q: Quantified) -> str:
    return "missing" if q.value is None else to_wire(q.value)


def _quality_risk(
    risk: RiskAssumptions, extended: ComponentResult
) -> tuple[ComponentResult, list[MissingInput]]:
    rate = risk.quality_risk_rate
    missing: list[MissingInput] = []
    if extended.is_missing:
        missing.append(
            MissingInput(
                component=CostComponent.QUALITY_RISK,
                input_name="extended_material_cost",
                consequence="extended material cost is unknown; quality risk cannot be computed.",
            )
        )
    if rate.value is None:
        missing.append(
            MissingInput(
                component=CostComponent.QUALITY_RISK,
                input_name="quality_risk_rate",
                consequence="quality risk rate is not stated; quality risk cannot be computed.",
            )
        )
    inputs = {
        "quality_risk_rate": _fmt_or_missing(rate),
        "extended_material_cost": "missing" if extended.is_missing else to_wire(extended.amount),
    }
    if missing:
        result = ComponentResult(
            component=CostComponent.QUALITY_RISK,
            amount=Decimal("0"),
            formula="quality_risk_rate * extended_material_cost; missing input, cannot be computed",
            inputs=inputs,
            provenance=Provenance.MISSING,
            is_assumed=False,
            is_missing=True,
        )
        return result, missing

    assert rate.value is not None
    with localcontext(CALC_CONTEXT):
        value = rate.value * extended.amount
        amount = quantize_money(value)
    provenance = _weakest([rate.provenance, extended.provenance])
    result = ComponentResult(
        component=CostComponent.QUALITY_RISK,
        amount=amount,
        formula=f"{to_wire(rate.value)} * {to_wire(extended.amount)} = {to_wire(amount)}",
        inputs=inputs,
        provenance=provenance,
        is_assumed=(rate.is_assumed or extended.is_assumed),
        is_missing=False,
    )
    return result, []


def _delay_risk(risk: RiskAssumptions) -> tuple[ComponentResult, list[MissingInput]]:
    per_day = risk.delay_risk_per_day
    promised = risk.promised_lead_time_days
    required = risk.required_lead_time_days
    missing: list[MissingInput] = []
    if per_day.value is None:
        missing.append(
            MissingInput(
                component=CostComponent.DELAY_RISK,
                input_name="delay_risk_per_day",
                consequence="delay risk per-day rate is not stated; delay risk cannot be computed.",
            )
        )
    if promised.value is None:
        missing.append(
            MissingInput(
                component=CostComponent.DELAY_RISK,
                input_name="promised_lead_time_days",
                consequence="promised lead time is not stated; delay risk cost cannot be computed.",
            )
        )
    if required.value is None:
        missing.append(
            MissingInput(
                component=CostComponent.DELAY_RISK,
                input_name="required_lead_time_days",
                consequence="required lead time is not stated; delay risk cost cannot be computed.",
            )
        )
    inputs = {
        "delay_risk_per_day": _fmt_or_missing(per_day),
        "promised_lead_time_days": _fmt_or_missing(promised),
        "required_lead_time_days": _fmt_or_missing(required),
    }
    if missing:
        result = ComponentResult(
            component=CostComponent.DELAY_RISK,
            amount=Decimal("0"),
            formula="delay_risk_per_day * max(0, promised - required); missing input, not computed",
            inputs=inputs,
            provenance=Provenance.MISSING,
            is_assumed=False,
            is_missing=True,
        )
        return result, missing

    assert per_day.value is not None
    assert promised.value is not None
    assert required.value is not None
    with localcontext(CALC_CONTEXT):
        days = max(Decimal("0"), promised.value - required.value)
        value = per_day.value * days
        amount = quantize_money(value)
    provenance = _weakest([per_day.provenance, promised.provenance, required.provenance])
    formula = (
        f"{to_wire(per_day.value)} * max(0, {to_wire(promised.value)} - {to_wire(required.value)}) "
        f"= {to_wire(per_day.value)} * {to_wire(days)} = {to_wire(amount)}"
    )
    result = ComponentResult(
        component=CostComponent.DELAY_RISK,
        amount=amount,
        formula=formula,
        inputs=inputs,
        provenance=provenance,
        is_assumed=(per_day.is_assumed or promised.is_assumed or required.is_assumed),
        is_missing=False,
    )
    return result, []


def _financing(
    financing: FinancingAssumptions, extended: ComponentResult
) -> tuple[ComponentResult, list[MissingInput]]:
    annual_rate = financing.annual_rate
    terms = financing.payment_terms_days
    baseline = financing.baseline_terms_days
    missing: list[MissingInput] = []
    if extended.is_missing:
        missing.append(
            MissingInput(
                component=CostComponent.FINANCING,
                input_name="extended_material_cost",
                consequence="extended material cost is unknown; financing cost cannot be computed.",
            )
        )
    if annual_rate.value is None:
        missing.append(
            MissingInput(
                component=CostComponent.FINANCING,
                input_name="annual_rate",
                consequence="annual cost-of-capital rate is not stated; financing not computed.",
            )
        )
    if terms.value is None:
        missing.append(
            MissingInput(
                component=CostComponent.FINANCING,
                input_name="payment_terms_days",
                consequence="payment terms are not stated; financing cost cannot be computed.",
            )
        )
    if baseline.value is None:
        missing.append(
            MissingInput(
                component=CostComponent.FINANCING,
                input_name="baseline_terms_days",
                consequence="baseline payment terms are not stated; financing cannot be computed.",
            )
        )
    inputs = {
        "extended_material_cost": "missing" if extended.is_missing else to_wire(extended.amount),
        "annual_rate": _fmt_or_missing(annual_rate),
        "payment_terms_days": _fmt_or_missing(terms),
        "baseline_terms_days": _fmt_or_missing(baseline),
    }
    if missing:
        result = ComponentResult(
            component=CostComponent.FINANCING,
            amount=Decimal("0"),
            formula=(
                "extended_material_cost * annual_rate * "
                "(baseline_terms_days - payment_terms_days)/365; "
                "missing input, cannot be computed"
            ),
            inputs=inputs,
            provenance=Provenance.MISSING,
            is_assumed=False,
            is_missing=True,
        )
        return result, missing

    assert annual_rate.value is not None
    assert terms.value is not None
    assert baseline.value is not None
    with localcontext(CALC_CONTEXT):
        value = extended.amount * annual_rate.value * (baseline.value - terms.value) / Decimal(365)
        amount = quantize_money(value)
    provenance = _weakest(
        [extended.provenance, annual_rate.provenance, terms.provenance, baseline.provenance]
    )
    label = " (financing benefit)" if amount < 0 else ""
    formula = (
        f"{to_wire(extended.amount)} * {to_wire(annual_rate.value)} * "
        f"({to_wire(baseline.value)} - {to_wire(terms.value)})/365 = {to_wire(amount)}{label}"
    )
    result = ComponentResult(
        component=CostComponent.FINANCING,
        amount=amount,
        formula=formula,
        inputs=inputs,
        provenance=provenance,
        is_assumed=(
            extended.is_assumed or annual_rate.is_assumed or terms.is_assumed or baseline.is_assumed
        ),
        is_missing=False,
    )
    return result, []


class LandedCostCalculatorV1:
    """Implements `LandedCostCalculator` (contracts.py). Pure, deterministic."""

    version: str = "1.0.0"

    def calculate(self, inp: LandedCostInput) -> LandedCostResult:
        if inp.accepted_quantity == 0:
            raise ZeroQuantityError(inp.accepted_quantity)
        if inp.accepted_quantity < 0:
            raise NegativeQuantityError(inp.accepted_quantity)

        missing_inputs: list[MissingInput] = []
        assumptions: list[Assumption] = []

        extended, extended_missing = _extended_material(
            inp.normalized_unit_price, inp.accepted_quantity
        )
        missing_inputs.extend(extended_missing)

        fixed_result, fixed_missing, fixed_assumptions = _fixed(
            inp.fixed, inp.assume_missing_costs_zero
        )
        missing_inputs.extend(fixed_missing)
        assumptions.extend(fixed_assumptions)

        logistics_result, logistics_missing, logistics_assumptions = _logistics(
            inp.logistics, inp.assume_missing_costs_zero
        )
        missing_inputs.extend(logistics_missing)
        assumptions.extend(logistics_assumptions)

        import_result, import_missing, import_assumptions = _import(
            inp.imports, extended, logistics_result, inp.assume_missing_costs_zero
        )
        missing_inputs.extend(import_missing)
        assumptions.extend(import_assumptions)

        quality_result, quality_missing = _quality_risk(inp.risk, extended)
        missing_inputs.extend(quality_missing)

        delay_result, delay_missing = _delay_risk(inp.risk)
        missing_inputs.extend(delay_missing)

        financing_result, financing_missing = _financing(inp.financing, extended)
        missing_inputs.extend(financing_missing)

        components = (
            extended,
            fixed_result,
            logistics_result,
            import_result,
            quality_result,
            delay_result,
            financing_result,
        )

        with localcontext(CALC_CONTEXT):
            total_raw = Decimal("0")
            for c in components:
                total_raw += c.amount
            total = quantize_money(total_raw)
            effective_raw = total / inp.accepted_quantity
        effective_unit_cost = quantize_unit_price(effective_raw)

        if missing_inputs:
            completeness = Completeness.INCOMPLETE
        elif assumptions:
            completeness = Completeness.ASSUMPTION_DEPENDENT
        else:
            completeness = Completeness.COMPLETE

        return LandedCostResult(
            quote_line_id=inp.quote_line_id,
            components=components,
            total_landed_cost=total,
            effective_unit_cost=effective_unit_cost,
            currency=inp.currency,
            completeness=completeness,
            missing_inputs=tuple(missing_inputs),
            assumptions=tuple(assumptions),
        )
