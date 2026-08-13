"""Hand-verified suite for the landed-cost calculator.

The worked example (docs/planning/05-calculation-methodology.md §9, corrected
by docs/planning/00-decisions.md §3) is the first hand-verified test: it
pins the exact Decimal strings the maintainer hand-checked, not approximate
equality.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.money import to_wire
from app.domain.landed_cost.calculator import (
    LandedCostCalculatorV1,
    NegativeQuantityError,
    ZeroQuantityError,
)
from app.domain.landed_cost.contracts import (
    CostComponent,
    DutyBasis,
    FinancingAssumptions,
    FixedCosts,
    ImportCosts,
    LandedCostInput,
    LogisticsCosts,
    RiskAssumptions,
)
from app.domain.values import Completeness, Quantified

CALCULATOR = LandedCostCalculatorV1()


def _q(value: str) -> Quantified:
    return Quantified.supplier(Decimal(value))


_MISSING = Quantified.missing()


def _complete_fixed(**overrides: Quantified) -> FixedCosts:
    base: dict[str, Quantified] = {
        "tooling": _q("0"),
        "setup": _q("0"),
        "documentation": _q("0"),
        "other_fixed": _q("0"),
    }
    base.update(overrides)
    return FixedCosts(**base)  # type: ignore[arg-type]


def _complete_logistics(**overrides: Quantified) -> LogisticsCosts:
    base: dict[str, Quantified] = {
        "shipping": _q("0"),
        "insurance": _q("0"),
        "packaging": _q("0"),
        "handling": _q("0"),
    }
    base.update(overrides)
    return LogisticsCosts(**base)  # type: ignore[arg-type]


def _complete_imports(**overrides: object) -> ImportCosts:
    base: dict[str, object] = {
        "tariff_amount": _q("0"),
        "duty_amount": _q("0"),
        "customs_fees": _q("0"),
        "tariff_rate": _q("0"),
        "duty_rate": _q("0"),
        "duty_basis": DutyBasis.MATERIAL_PLUS_LOGISTICS,
    }
    base.update(overrides)
    return ImportCosts(**base)  # type: ignore[arg-type]


def _complete_risk(**overrides: Quantified) -> RiskAssumptions:
    base: dict[str, Quantified] = {
        "quality_risk_rate": _q("0"),
        "delay_risk_per_day": _q("0"),
        "promised_lead_time_days": _q("30"),
        "required_lead_time_days": _q("30"),
    }
    base.update(overrides)
    return RiskAssumptions(**base)  # type: ignore[arg-type]


def _complete_financing(**overrides: Quantified) -> FinancingAssumptions:
    base: dict[str, Quantified] = {
        "annual_rate": _q("0"),
        "payment_terms_days": _q("30"),
        "baseline_terms_days": _q("30"),
    }
    base.update(overrides)
    return FinancingAssumptions(**base)  # type: ignore[arg-type]


def _complete_input(**overrides: object) -> LandedCostInput:
    base: dict[str, object] = {
        "quote_line_id": uuid4(),
        "accepted_quantity": Decimal("10"),
        "normalized_unit_price": _q("5"),
        "currency": "USD",
        "fixed": _complete_fixed(),
        "logistics": _complete_logistics(),
        "imports": _complete_imports(),
        "risk": _complete_risk(),
        "financing": _complete_financing(),
        "assume_missing_costs_zero": False,
    }
    base.update(overrides)
    return LandedCostInput(**base)  # type: ignore[arg-type]


def _worked_example_input() -> LandedCostInput:
    """docs/planning/05-calculation-methodology.md §9, principal-corrected values.

    500 pcs at unit price already converted USD 11.41304348 (EUR 10.50 / 0.92);
    tooling already converted USD 869.565217 (EUR 800 / 0.92); shipping +
    insurance already converted to USD 375.000000 total (EUR 300 + 45 / 0.92);
    tariff 3.5% on (material + logistics), no duty/customs quoted (explicit
    zero, not missing); quality risk 2%; no delay (promised == required);
    Net-60 vs baseline Net-30 at 8% annual. Currency conversion itself is out
    of the calculator's scope - the input below is already normalized.
    """
    return _complete_input(
        accepted_quantity=Decimal("500"),
        normalized_unit_price=_q("11.41304348"),
        fixed=_complete_fixed(tooling=_q("869.565217")),
        logistics=_complete_logistics(shipping=_q("326.086957"), insurance=_q("48.913043")),
        imports=_complete_imports(
            tariff_amount=_MISSING,
            tariff_rate=_q("0.035"),
            duty_amount=_q("0"),
            duty_rate=_q("0"),
            customs_fees=_q("0"),
        ),
        risk=_complete_risk(
            quality_risk_rate=_q("0.02"),
            promised_lead_time_days=_q("30"),
            required_lead_time_days=_q("30"),
        ),
        financing=_complete_financing(
            annual_rate=_q("0.08"),
            payment_terms_days=_q("60"),
            baseline_terms_days=_q("30"),
        ),
    )


class TestWorkedExample:
    """(a) the 05 §9 worked example, exactly."""

    def test_unit_price_echoed_from_already_normalized_input(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        extended = result.component(CostComponent.EXTENDED_MATERIAL)
        assert extended.inputs["normalized_unit_price"] == "11.41304348"

    def test_extended_material(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.component(CostComponent.EXTENDED_MATERIAL).amount) == "5706.521740"

    def test_allocated_fixed(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.component(CostComponent.ALLOCATED_FIXED).amount) == "869.565217"

    def test_logistics(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.component(CostComponent.LOGISTICS).amount) == "375.000000"

    def test_import(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.component(CostComponent.IMPORT).amount) == "212.853261"

    def test_quality_risk(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.component(CostComponent.QUALITY_RISK).amount) == "114.130435"

    def test_delay_risk_is_zero(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.component(CostComponent.DELAY_RISK).amount) == "0.000000"

    def test_financing_is_a_negative_benefit(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.component(CostComponent.FINANCING).amount) == "-37.522335"

    def test_total(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.total_landed_cost) == "7240.548318"

    def test_effective_unit_cost(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert to_wire(result.effective_unit_cost) == "14.48109664"

    def test_import_duty_basis_is_recorded_as_an_assumption(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert any(a.description.startswith("tariff amount not quoted") for a in result.assumptions)

    def test_completeness_is_assumption_dependent_not_incomplete(self) -> None:
        # every field is stated except the tariff amount (derived from rate + basis,
        # which is an assumption, not a missing input) -> no missing_inputs at all.
        result = CALCULATOR.calculate(_worked_example_input())
        assert result.missing_inputs == ()
        assert result.completeness is Completeness.ASSUMPTION_DEPENDENT


class TestComponentsSumToTotal:
    """(b) components sum EXACTLY to total."""

    def test_worked_example(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert sum((c.amount for c in result.components), Decimal("0")) == result.total_landed_cost

    def test_seven_components_always_present_in_stable_order(self) -> None:
        result = CALCULATOR.calculate(_worked_example_input())
        assert [c.component for c in result.components] == [
            CostComponent.EXTENDED_MATERIAL,
            CostComponent.ALLOCATED_FIXED,
            CostComponent.LOGISTICS,
            CostComponent.IMPORT,
            CostComponent.QUALITY_RISK,
            CostComponent.DELAY_RISK,
            CostComponent.FINANCING,
        ]


def _bounded_decimal(min_value: str, max_value: str) -> st.SearchStrategy[Decimal]:
    return st.decimals(
        min_value=Decimal(min_value),
        max_value=Decimal(max_value),
        places=6,
        allow_nan=False,
        allow_infinity=False,
    )


def _quantified_strategy(min_value: str, max_value: str) -> st.SearchStrategy[Quantified]:
    present = _bounded_decimal(min_value, max_value).map(Quantified.supplier)
    return st.one_of(st.just(Quantified.missing()), present)


@st.composite
def _random_landed_cost_input(draw: st.DrawFn) -> LandedCostInput:
    qty = draw(_bounded_decimal("0.000001", "100000"))
    fixed = FixedCosts(
        tooling=draw(_quantified_strategy("0", "1000")),
        setup=draw(_quantified_strategy("0", "1000")),
        documentation=draw(_quantified_strategy("0", "1000")),
        other_fixed=draw(_quantified_strategy("0", "1000")),
    )
    logistics = LogisticsCosts(
        shipping=draw(_quantified_strategy("0", "1000")),
        insurance=draw(_quantified_strategy("0", "1000")),
        packaging=draw(_quantified_strategy("0", "1000")),
        handling=draw(_quantified_strategy("0", "1000")),
    )
    imports = ImportCosts(
        tariff_amount=draw(_quantified_strategy("0", "1000")),
        duty_amount=draw(_quantified_strategy("0", "1000")),
        customs_fees=draw(_quantified_strategy("0", "1000")),
        tariff_rate=draw(_quantified_strategy("0", "1")),
        duty_rate=draw(_quantified_strategy("0", "1")),
        duty_basis=draw(st.sampled_from(list(DutyBasis))),
    )
    risk = RiskAssumptions(
        quality_risk_rate=draw(_quantified_strategy("0", "1")),
        delay_risk_per_day=draw(_quantified_strategy("0", "1000")),
        promised_lead_time_days=draw(_quantified_strategy("0", "365")),
        required_lead_time_days=draw(_quantified_strategy("0", "365")),
    )
    financing = FinancingAssumptions(
        annual_rate=draw(_quantified_strategy("0", "1")),
        payment_terms_days=draw(_quantified_strategy("0", "365")),
        baseline_terms_days=draw(_quantified_strategy("0", "365")),
    )
    return LandedCostInput(
        quote_line_id=uuid4(),
        accepted_quantity=qty,
        normalized_unit_price=draw(_quantified_strategy("0", "1000")),
        currency="USD",
        fixed=fixed,
        logistics=logistics,
        imports=imports,
        risk=risk,
        financing=financing,
        assume_missing_costs_zero=draw(st.booleans()),
    )


class TestComponentsSumToTotalProperty:
    @settings(max_examples=100, deadline=None)
    @given(_random_landed_cost_input())
    def test_components_always_sum_to_total(self, inp: LandedCostInput) -> None:
        result = CALCULATOR.calculate(inp)
        assert sum((c.amount for c in result.components), Decimal("0")) == result.total_landed_cost


class TestMissingShipping:
    """(c) missing shipping -> INCOMPLETE with named missing input, total excludes it."""

    def test_missing_shipping_marks_incomplete(self) -> None:
        inp = _complete_input(
            logistics=_complete_logistics(shipping=_MISSING, insurance=_q("10")),
        )
        result = CALCULATOR.calculate(inp)
        assert result.completeness is Completeness.INCOMPLETE
        missing_names = {m.input_name for m in result.missing_inputs}
        assert "shipping" in missing_names
        shipping_missing = next(m for m in result.missing_inputs if m.input_name == "shipping")
        assert shipping_missing.component is CostComponent.LOGISTICS
        assert shipping_missing.consequence  # non-empty human sentence

    def test_missing_shipping_contributes_no_silent_zero_but_other_fields_still_count(
        self,
    ) -> None:
        inp = _complete_input(
            logistics=_complete_logistics(shipping=_MISSING, insurance=_q("10")),
        )
        result = CALCULATOR.calculate(inp)
        logistics = result.component(CostComponent.LOGISTICS)
        assert logistics.is_missing is False  # insurance was present, so it *is* computed
        assert logistics.amount == Decimal("10.000000")  # shipping excluded, not zeroed silently
        assert logistics.inputs["shipping"] == "missing"

    def test_all_logistics_missing_makes_the_component_itself_missing(self) -> None:
        inp = _complete_input(
            logistics=LogisticsCosts(
                shipping=_MISSING, insurance=_MISSING, packaging=_MISSING, handling=_MISSING
            ),
        )
        result = CALCULATOR.calculate(inp)
        logistics = result.component(CostComponent.LOGISTICS)
        assert logistics.is_missing is True
        assert logistics.amount == Decimal("0")
        assert result.completeness is Completeness.INCOMPLETE


class TestAssumeMissingCostsZero:
    """(d) assume_missing_costs_zero -> ASSUMPTION_DEPENDENT with recorded assumption."""

    def test_assume_zero_downgrades_to_assumption_dependent(self) -> None:
        inp = _complete_input(
            logistics=_complete_logistics(shipping=_MISSING),
            assume_missing_costs_zero=True,
        )
        result = CALCULATOR.calculate(inp)
        assert result.missing_inputs == ()
        assert result.completeness is Completeness.ASSUMPTION_DEPENDENT
        assert any(a.description == "missing costs assumed zero" for a in result.assumptions)

    def test_assume_zero_field_contributes_zero_not_a_fabricated_amount(self) -> None:
        inp = _complete_input(
            logistics=_complete_logistics(shipping=_MISSING, insurance=_q("10")),
            assume_missing_costs_zero=True,
        )
        result = CALCULATOR.calculate(inp)
        logistics = result.component(CostComponent.LOGISTICS)
        assert logistics.amount == Decimal("10.000000")
        assert logistics.is_assumed is True
        assert logistics.is_missing is False

    def test_all_missing_with_assume_zero_is_computed_not_missing(self) -> None:
        inp = _complete_input(
            fixed=FixedCosts(
                tooling=_MISSING, setup=_MISSING, documentation=_MISSING, other_fixed=_MISSING
            ),
            assume_missing_costs_zero=True,
        )
        result = CALCULATOR.calculate(inp)
        fixed = result.component(CostComponent.ALLOCATED_FIXED)
        assert fixed.is_missing is False
        assert fixed.amount == Decimal("0.000000")
        assert fixed.is_assumed is True
        assert result.completeness is Completeness.ASSUMPTION_DEPENDENT


class TestQuantityValidation:
    """(e) zero/negative quantity raises."""

    def test_zero_quantity_raises(self) -> None:
        inp = _complete_input(accepted_quantity=Decimal("0"))
        with pytest.raises(ZeroQuantityError):
            CALCULATOR.calculate(inp)

    def test_negative_quantity_raises(self) -> None:
        inp = _complete_input(accepted_quantity=Decimal("-5"))
        with pytest.raises(NegativeQuantityError):
            CALCULATOR.calculate(inp)


class TestExtremeValues:
    """(f) extreme values (qty 10^12, price 10^-8) behave."""

    def test_huge_quantity_and_tiny_price_does_not_raise(self) -> None:
        inp = _complete_input(
            accepted_quantity=Decimal("1000000000000"),
            normalized_unit_price=_q("0.00000001"),
        )
        result = CALCULATOR.calculate(inp)
        extended = result.component(CostComponent.EXTENDED_MATERIAL)
        assert extended.amount == Decimal("10000.000000")
        assert result.total_landed_cost.is_finite()
        assert result.effective_unit_cost.is_finite()


class TestDeterminism:
    """(g) same input twice -> identical results."""

    def test_same_input_twice_is_identical(self) -> None:
        inp = _worked_example_input()
        first = CALCULATOR.calculate(inp)
        second = CALCULATOR.calculate(inp)
        assert first == second


class TestFinancing:
    """(h) financing positive when terms shorter than baseline."""

    def test_shorter_terms_than_baseline_is_a_positive_cost(self) -> None:
        inp = _complete_input(
            normalized_unit_price=_q("100"),
            accepted_quantity=Decimal("10"),
            financing=_complete_financing(
                annual_rate=_q("0.08"),
                payment_terms_days=_q("15"),
                baseline_terms_days=_q("30"),
            ),
        )
        result = CALCULATOR.calculate(inp)
        financing = result.component(CostComponent.FINANCING)
        assert financing.amount > Decimal("0")
        assert "financing benefit" not in financing.formula

    def test_longer_terms_than_baseline_is_a_negative_benefit(self) -> None:
        inp = _complete_input(
            normalized_unit_price=_q("100"),
            accepted_quantity=Decimal("10"),
            financing=_complete_financing(
                annual_rate=_q("0.08"),
                payment_terms_days=_q("60"),
                baseline_terms_days=_q("30"),
            ),
        )
        result = CALCULATOR.calculate(inp)
        financing = result.component(CostComponent.FINANCING)
        assert financing.amount < Decimal("0")
        assert "financing benefit" in financing.formula

    def test_missing_financing_input_is_missing_not_zero(self) -> None:
        inp = _complete_input(
            financing=_complete_financing(annual_rate=_MISSING),
        )
        result = CALCULATOR.calculate(inp)
        financing = result.component(CostComponent.FINANCING)
        assert financing.is_missing is True
        assert result.completeness is Completeness.INCOMPLETE

    def test_missing_extended_material_cascades_to_financing(self) -> None:
        inp = _complete_input(normalized_unit_price=_MISSING)
        result = CALCULATOR.calculate(inp)
        financing = result.component(CostComponent.FINANCING)
        assert financing.is_missing is True
        assert any(m.input_name == "extended_material_cost" for m in result.missing_inputs)


class TestRateDerivedTariff:
    """(i) rate-derived tariff uses the declared basis + records assumption."""

    def test_material_plus_logistics_basis_uses_extended_and_logistics(self) -> None:
        inp = _complete_input(
            normalized_unit_price=_q("10"),
            accepted_quantity=Decimal("100"),  # extended = 1000.000000
            logistics=_complete_logistics(shipping=_q("100")),  # logistics = 100.000000
            imports=_complete_imports(
                tariff_amount=_MISSING,
                tariff_rate=_q("0.10"),
                duty_basis=DutyBasis.MATERIAL_PLUS_LOGISTICS,
            ),
        )
        result = CALCULATOR.calculate(inp)
        import_component = result.component(CostComponent.IMPORT)
        # 0.10 * (1000.000000 + 100.000000) = 110.000000
        assert import_component.amount == Decimal("110.000000")
        assert any(
            a.value == DutyBasis.MATERIAL_PLUS_LOGISTICS.value and "tariff" in a.key
            for a in result.assumptions
        )

    def test_material_only_basis_excludes_logistics(self) -> None:
        inp = _complete_input(
            normalized_unit_price=_q("10"),
            accepted_quantity=Decimal("100"),  # extended = 1000.000000
            logistics=_complete_logistics(shipping=_q("100")),  # logistics = 100.000000 (ignored)
            imports=_complete_imports(
                tariff_amount=_MISSING,
                tariff_rate=_q("0.10"),
                duty_basis=DutyBasis.MATERIAL_ONLY,
            ),
        )
        result = CALCULATOR.calculate(inp)
        import_component = result.component(CostComponent.IMPORT)
        # 0.10 * 1000.000000 = 100.000000 (logistics excluded)
        assert import_component.amount == Decimal("100.000000")

    def test_both_amount_and_rate_missing_is_a_missing_input(self) -> None:
        inp = _complete_input(
            imports=_complete_imports(tariff_amount=_MISSING, tariff_rate=_MISSING),
        )
        result = CALCULATOR.calculate(inp)
        assert any(m.input_name == "tariff_amount" for m in result.missing_inputs)
        assert result.completeness is Completeness.INCOMPLETE


class TestDelayClamp:
    """(j) delay clamp at early delivery."""

    def test_early_delivery_clamps_negative_days_to_zero(self) -> None:
        inp = _complete_input(
            risk=_complete_risk(
                delay_risk_per_day=_q("100"),
                promised_lead_time_days=_q("20"),
                required_lead_time_days=_q("30"),
            ),
        )
        result = CALCULATOR.calculate(inp)
        delay = result.component(CostComponent.DELAY_RISK)
        assert delay.amount == Decimal("0.000000")
        assert delay.is_missing is False
        assert "max(0" in delay.formula

    def test_late_delivery_is_a_positive_cost(self) -> None:
        inp = _complete_input(
            risk=_complete_risk(
                delay_risk_per_day=_q("100"),
                promised_lead_time_days=_q("65"),
                required_lead_time_days=_q("60"),
            ),
        )
        result = CALCULATOR.calculate(inp)
        delay = result.component(CostComponent.DELAY_RISK)
        assert delay.amount == Decimal("500.000000")  # 100 * max(0, 65-60)

    def test_missing_lead_time_is_missing_not_zero(self) -> None:
        inp = _complete_input(
            risk=_complete_risk(promised_lead_time_days=_MISSING),
        )
        result = CALCULATOR.calculate(inp)
        delay = result.component(CostComponent.DELAY_RISK)
        assert delay.is_missing is True
        assert result.completeness is Completeness.INCOMPLETE


class TestQualityRisk:
    def test_missing_rate_is_missing_not_zero(self) -> None:
        inp = _complete_input(risk=_complete_risk(quality_risk_rate=_MISSING))
        result = CALCULATOR.calculate(inp)
        quality = result.component(CostComponent.QUALITY_RISK)
        assert quality.is_missing is True
        assert result.completeness is Completeness.INCOMPLETE

    def test_missing_extended_material_cascades_to_quality_risk(self) -> None:
        inp = _complete_input(normalized_unit_price=_MISSING)
        result = CALCULATOR.calculate(inp)
        quality = result.component(CostComponent.QUALITY_RISK)
        assert quality.is_missing is True


class TestExtendedMaterialMissing:
    """normalized_unit_price MISSING -> whole result INCOMPLETE, extended is_missing."""

    def test_missing_price_marks_extended_material_missing(self) -> None:
        inp = _complete_input(normalized_unit_price=_MISSING)
        result = CALCULATOR.calculate(inp)
        extended = result.component(CostComponent.EXTENDED_MATERIAL)
        assert extended.is_missing is True
        assert extended.amount == Decimal("0")
        assert result.completeness is Completeness.INCOMPLETE

    def test_assume_missing_costs_zero_does_not_rescue_missing_unit_price(self) -> None:
        # assume_missing_costs_zero only covers fixed/logistics/import amounts,
        # never the core normalized_unit_price.
        inp = _complete_input(normalized_unit_price=_MISSING, assume_missing_costs_zero=True)
        result = CALCULATOR.calculate(inp)
        extended = result.component(CostComponent.EXTENDED_MATERIAL)
        assert extended.is_missing is True
        assert result.completeness is Completeness.INCOMPLETE

    def test_total_excludes_the_missing_component_rather_than_treating_it_as_zero(self) -> None:
        inp = _complete_input(normalized_unit_price=_MISSING)
        result = CALCULATOR.calculate(inp)
        # total is still the exact sum of the (zeroed) components -- no other
        # component silently substitutes a guessed extended-material value.
        assert sum((c.amount for c in result.components), Decimal("0")) == result.total_landed_cost
