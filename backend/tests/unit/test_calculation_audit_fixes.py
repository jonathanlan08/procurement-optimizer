"""Regression tests for the 2026-08 independent calculation audit fixes.

One test class per finding:
- F1  — `max_scaling_error` (methodology §7.4) computed and carried on
  `SolverStats` for every solved result.
- F2  — unit-price conversion divides by the UNQUANTIZED factor ratio, not
  the 6-dp-quantized converted quantity (measured 1.8e-5/unit error on the
  documented lb→kg example before the fix).
- F3  — a supplier with no present value on any positive-weight criterion is
  reported as not-scoreable (excluded-style, rank 0, reason), never scored
  a fabricated worst-in-cohort 0.000000.
- F6  — the brief numeric cross-check catches fabricated INTEGERS and
  separator-formatted money, matching numerically so `500` equals a stored
  `500.000000` while a rounded `14.48` still fails closed.
- F7  — a degenerate cohort's reason says "only candidate", not the untrue
  "all candidates equal".
- F8  — the concentration constraint's 10^6 multiplier is covered by the
  pre-solve overflow guard.
- F10 — negative criterion weights are rejected at construction.
- F11 — `parse_decimal` rejects PEP-515 underscores and other shapes
  Python's `Decimal()` accepts but the wire contract does not.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import ClassVar

import pytest

from app.core.errors import ValidationAppError
from app.core.money import InvalidDecimalString, parse_decimal
from app.domain.optimization.contracts import (
    AllocationConstraints,
    AllocationProblem,
    AllocationStatus,
    DemandLine,
    Offer,
    OfferTier,
)
from app.domain.optimization.solver import (
    AllocationSolver,
    ScalingOverflowError,
    _check_overflow,
    _max_scaling_error,
)
from app.domain.scoring.contracts import (
    Criterion,
    CriterionSpec,
    Direction,
    SupplierCriterionValue,
    SupplierScoringInput,
)
from app.domain.scoring.scorer import ScorerV1
from app.domain.units.normalize import Dimension, Unit, convert_quantity
from app.services.brief_service import numeric_cross_check

KG = Unit("kg", Dimension.MASS, Decimal("1"))
LB = Unit("lb", Dimension.MASS, Decimal("0.45359237"))


def _line(qty: int) -> DemandLine:
    return DemandLine(
        rfq_line_id=uuid.uuid4(), part_label="part", required_quantity=qty
    )


def _offer(
    line: DemandLine,
    unit_cost: str,
    *,
    label: str = "S",
    fixed: str = "0",
    capacity: int | None = None,
) -> Offer:
    return Offer(
        quote_line_id=uuid.uuid4(),
        rfq_line_id=line.rfq_line_id,
        supplier_id=uuid.uuid4(),
        supplier_label=label,
        tiers=(OfferTier(min_quantity=0, max_quantity=None, landed_unit_cost=Decimal(unit_cost)),),
        moq=None,
        capacity=capacity,
        fixed_cost=Decimal(fixed),
        incomplete_landed_cost=False,
    )


class TestF1MaxScalingError:
    def test_bound_formula(self) -> None:
        line = _line(1000)
        offers = (_offer(line, "1.00", fixed="50"), _offer(line, "1.10"))
        # 2 x 5e-5 x (1000 units + 1 nonzero fixed cost)
        assert _max_scaling_error((line,), offers) == Decimal("0.1001")

    def test_solved_result_carries_the_disclosure(self) -> None:
        line = _line(10)
        problem = AllocationProblem(
            lines=(line,),
            offers=(_offer(line, "2.00"),),
            constraints=AllocationConstraints(),
        )
        result = AllocationSolver().solve(problem)
        assert result.status is AllocationStatus.OPTIMAL
        assert result.stats is not None
        assert result.stats.max_scaling_error == Decimal("0.0010")


class TestF2UnitRatioPrecision:
    def test_unit_ratio_is_unquantized(self) -> None:
        result = convert_quantity(Decimal("1"), LB, KG)
        assert result.unit_ratio == Decimal("0.45359237")  # full precision
        assert result.value == Decimal("0.453592")  # quantity boundary, 6 dp

    def test_price_division_by_unit_ratio_matches_the_audit_figure(self) -> None:
        # docs/METHODOLOGY.md §6's own lb->kg example: 10.00/lb priced per kg.
        # Dividing by the 6-dp value gave 22.04624420 (wrong in the 8th
        # digit); the unquantized ratio gives the correct figure.
        from decimal import localcontext

        from app.core.money import CALC_CONTEXT, quantize_unit_price

        ratio = convert_quantity(Decimal("1"), LB, KG)
        with localcontext(CALC_CONTEXT):
            adjusted = quantize_unit_price(Decimal("10.00") / ratio.unit_ratio)
        assert adjusted == Decimal("22.04622622")


class TestF3UnscoreableSupplier:
    _WEIGHTS = (
        CriterionSpec(
            criterion=Criterion.TOTAL_LANDED_COST,
            weight=Decimal("1.00"),
            direction=Direction.LOWER_IS_BETTER,
            label="Landed cost",
        ),
    )

    def _supplier(self, name: str, value: Decimal | None) -> SupplierScoringInput:
        values = (
            (
                SupplierCriterionValue(
                    criterion=Criterion.TOTAL_LANDED_COST, value=value, source="test"
                ),
            )
            if value is not None
            else ()
        )
        return SupplierScoringInput(
            supplier_id=uuid.uuid4(), supplier_name=name, values=values, excluded=False,
            exclusion_reason=None,
        )

    def test_no_weighted_value_reports_not_scoreable_never_zero(self) -> None:
        result = ScorerV1().score(
            (self._supplier("Has", Decimal("100")), self._supplier("Lacks", None)),
            self._WEIGHTS,
        )
        by_name = {s.supplier_name: s for s in result.scores}
        lacks = by_name["Lacks"]
        assert lacks.excluded is True
        assert lacks.rank == 0
        assert lacks.exclusion_reason is not None
        assert "not scoreable" in lacks.exclusion_reason
        assert result.cohort_size == 1  # only the scoreable supplier
        assert any("not scoreable" in n for n in result.notes)
        # the scoreable supplier is unaffected
        assert by_name["Has"].rank == 1

    def test_single_candidate_reason_wording(self) -> None:
        result = ScorerV1().score((self._supplier("Solo", Decimal("100")),), self._WEIGHTS)
        solo = result.scores[0]
        assert solo.rank == 1
        reason = solo.criterion_scores[0].reason
        assert "only candidate" in reason
        assert "all candidates equal" not in reason


class TestF6CrossCheckIntegers:
    _FACTS: ClassVar[dict[str, str]] = {
        "rfq_total_quantity": "500.000000",
        "effective_unit_cost": "14.48109664",
        "lead_time_days": "14",
    }

    def test_fabricated_integer_is_blocked(self) -> None:
        with pytest.raises(ValidationAppError, match="25"):
            numeric_cross_check({"s": "Push for a 25 percent discount."}, self._FACTS)

    def test_fabricated_separator_money_is_blocked(self) -> None:
        with pytest.raises(ValidationAppError):
            numeric_cross_check({"s": "A rebate of $8,500 is on the table."}, self._FACTS)

    def test_integer_matching_a_stored_decimal_is_allowed(self) -> None:
        numeric_cross_check({"s": "This RFQ covers 500 units."}, self._FACTS)

    def test_exact_decimal_still_allowed_and_rounding_fails_closed(self) -> None:
        numeric_cross_check({"s": "Landed cost is 14.48109664 per unit."}, self._FACTS)
        with pytest.raises(ValidationAppError):
            numeric_cross_check({"s": "Landed cost is 14.48 per unit."}, self._FACTS)


class TestF8ConcentrationOverflowGuard:
    def test_concentration_cap_overflow_is_named_pre_solve(self) -> None:
        line = _line(1_000_000)
        offers = (_offer(line, "2000000.00"),)  # scaled spend far above int64/1e6
        with pytest.raises(ScalingOverflowError, match="concentration"):
            _check_overflow((line,), offers, has_concentration_cap=True)
        # without the cap the same problem passes the guard
        _check_overflow((line,), offers, has_concentration_cap=False)


class TestF10NegativeWeight:
    def test_negative_weight_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="weight"):
            CriterionSpec(
                criterion=Criterion.TOTAL_LANDED_COST,
                weight=Decimal("-1"),
                direction=Direction.LOWER_IS_BETTER,
                label="Landed cost",
            )


class TestF11StrictDecimalParsing:
    @pytest.mark.parametrize("raw", ["1_0.5", "1,000", "+5", "1e3", "0x10", ".5", "5."])
    def test_loose_shapes_are_rejected(self, raw: str) -> None:
        with pytest.raises(InvalidDecimalString):
            parse_decimal(raw)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("10.5", Decimal("10.5")), ("-3.25", Decimal("-3.25")), ("  5.5  ", Decimal("5.5"))],
    )
    def test_wire_shapes_still_parse(self, raw: str, expected: Decimal) -> None:
        assert parse_decimal(raw) == expected
