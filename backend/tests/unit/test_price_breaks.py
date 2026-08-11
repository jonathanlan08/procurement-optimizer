"""Boundary tests for price-break tier selection (all-units semantics).

Interval semantics per docs/planning/05-calculation-methodology.md §6: closed
on both ends, max_quantity=None means unbounded. SPEC §11's example table is
used verbatim below.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.landed_cost.breaks import select_price_break
from app.domain.landed_cost.contracts import PriceBreakTier

SPEC_TIERS = (
    PriceBreakTier(
        min_quantity=Decimal("1"), max_quantity=Decimal("99"), unit_price=Decimal("12.00")
    ),
    PriceBreakTier(
        min_quantity=Decimal("100"), max_quantity=Decimal("499"), unit_price=Decimal("10.50")
    ),
    PriceBreakTier(
        min_quantity=Decimal("500"), max_quantity=Decimal("999"), unit_price=Decimal("9.20")
    ),
    PriceBreakTier(min_quantity=Decimal("1000"), max_quantity=None, unit_price=Decimal("8.60")),
)


class TestSpecExampleTable:
    @pytest.mark.parametrize(
        ("quantity", "expected_price"),
        [
            (Decimal("1"), Decimal("12.00")),
            (Decimal("99"), Decimal("12.00")),
            (Decimal("100"), Decimal("10.50")),
            (Decimal("499"), Decimal("10.50")),
            (Decimal("500"), Decimal("9.20")),
            (Decimal("999"), Decimal("9.20")),
            (Decimal("1000"), Decimal("8.60")),
            (Decimal("5000"), Decimal("8.60")),
        ],
    )
    def test_quantity_selects_expected_tier(
        self, quantity: Decimal, expected_price: Decimal
    ) -> None:
        selection = select_price_break(SPEC_TIERS, quantity)
        assert selection.tier is not None
        assert selection.tier.unit_price == expected_price

    def test_all_units_semantics_applies_tier_price_to_whole_quantity(self) -> None:
        # 499 units at the 100-499 tier: the *entire* 499 units cost 10.50 each,
        # not just the units above 100 (all-units, not incremental).
        selection = select_price_break(SPEC_TIERS, Decimal("499"))
        assert selection.tier is not None
        assert selection.tier.unit_price == Decimal("10.50")
        assert selection.tier.min_quantity == Decimal("100")

    def test_tiers_considered_reports_all_tiers(self) -> None:
        selection = select_price_break(SPEC_TIERS, Decimal("500"))
        assert selection.tiers_considered == SPEC_TIERS


class TestBoundaries:
    """Exactly at min, exactly at max, one below min, one above max."""

    TWO_TIERS = (
        PriceBreakTier(
            min_quantity=Decimal("10"), max_quantity=Decimal("20"), unit_price=Decimal("5.00")
        ),
        PriceBreakTier(
            min_quantity=Decimal("21"), max_quantity=Decimal("30"), unit_price=Decimal("4.00")
        ),
    )

    def test_exactly_at_min(self) -> None:
        selection = select_price_break(self.TWO_TIERS, Decimal("10"))
        assert selection.tier is not None
        assert selection.tier.min_quantity == Decimal("10")

    def test_exactly_at_max(self) -> None:
        selection = select_price_break(self.TWO_TIERS, Decimal("20"))
        assert selection.tier is not None
        assert selection.tier.max_quantity == Decimal("20")

    def test_one_below_min_of_lowest_tier_is_none(self) -> None:
        selection = select_price_break(self.TWO_TIERS, Decimal("9"))
        assert selection.tier is None
        assert "below" in selection.reason

    def test_one_above_max_of_bounded_top_tier_is_none(self) -> None:
        selection = select_price_break(self.TWO_TIERS, Decimal("31"))
        assert selection.tier is None
        assert "above" in selection.reason

    def test_min_plus_one_selects_tier(self) -> None:
        selection = select_price_break(self.TWO_TIERS, Decimal("11"))
        assert selection.tier is not None
        assert selection.tier.min_quantity == Decimal("10")

    def test_max_minus_one_selects_tier(self) -> None:
        selection = select_price_break(self.TWO_TIERS, Decimal("19"))
        assert selection.tier is not None
        assert selection.tier.min_quantity == Decimal("10")


class TestOpenEndedTopTier:
    TIERS = (
        PriceBreakTier(
            min_quantity=Decimal("1"), max_quantity=Decimal("9"), unit_price=Decimal("5.00")
        ),
        PriceBreakTier(min_quantity=Decimal("10"), max_quantity=None, unit_price=Decimal("3.00")),
    )

    def test_exactly_at_open_ended_min(self) -> None:
        selection = select_price_break(self.TIERS, Decimal("10"))
        assert selection.tier is not None
        assert selection.tier.unit_price == Decimal("3.00")

    def test_far_above_open_ended_min_still_selects(self) -> None:
        selection = select_price_break(self.TIERS, Decimal("10_000_000"))
        assert selection.tier is not None
        assert selection.tier.unit_price == Decimal("3.00")


class TestGapHandling:
    """A gap between tiers (a validation defect upstream) never nearest-matches."""

    GAPPED_TIERS = (
        PriceBreakTier(
            min_quantity=Decimal("1"), max_quantity=Decimal("10"), unit_price=Decimal("5.00")
        ),
        PriceBreakTier(
            min_quantity=Decimal("20"), max_quantity=Decimal("30"), unit_price=Decimal("4.00")
        ),
    )

    def test_quantity_in_gap_returns_none_with_reason(self) -> None:
        selection = select_price_break(self.GAPPED_TIERS, Decimal("15"))
        assert selection.tier is None
        assert "gap" in selection.reason
        assert "1-10" in selection.reason
        assert "20-30" in selection.reason

    def test_quantity_at_lower_edge_of_gap_still_below_upper_tier(self) -> None:
        selection = select_price_break(self.GAPPED_TIERS, Decimal("11"))
        assert selection.tier is None
        assert "gap" in selection.reason

    def test_quantity_just_before_second_tier_is_still_gap(self) -> None:
        selection = select_price_break(self.GAPPED_TIERS, Decimal("19"))
        assert selection.tier is None

    def test_gap_above_the_last_bounded_tier_with_no_higher_tier(self) -> None:
        single_bounded = (
            PriceBreakTier(
                min_quantity=Decimal("1"), max_quantity=Decimal("10"), unit_price=Decimal("5.00")
            ),
        )
        selection = select_price_break(single_bounded, Decimal("11"))
        assert selection.tier is None
        assert "no higher tier" in selection.reason


class TestSingleTier:
    def test_single_bounded_tier(self) -> None:
        tiers = (
            PriceBreakTier(
                min_quantity=Decimal("1"), max_quantity=Decimal("100"), unit_price=Decimal("2.00")
            ),
        )
        selection = select_price_break(tiers, Decimal("50"))
        assert selection.tier is not None
        assert selection.tier.unit_price == Decimal("2.00")

    def test_single_unbounded_tier(self) -> None:
        tiers = (
            PriceBreakTier(
                min_quantity=Decimal("1"), max_quantity=None, unit_price=Decimal("2.00")
            ),
        )
        selection = select_price_break(tiers, Decimal("999999"))
        assert selection.tier is not None
        assert selection.tier.unit_price == Decimal("2.00")

    def test_no_tiers_at_all(self) -> None:
        selection = select_price_break((), Decimal("5"))
        assert selection.tier is None
        assert "no price-break tiers" in selection.reason


class TestQuantityBelowAllTiers:
    def test_zero_quantity_below_all_tiers(self) -> None:
        selection = select_price_break(SPEC_TIERS, Decimal("0"))
        assert selection.tier is None
        assert "below" in selection.reason


class TestFractionalQuantityAgainstIntegralTiers:
    def test_fractional_quantity_within_a_tier(self) -> None:
        selection = select_price_break(SPEC_TIERS, Decimal("99.5"))
        assert selection.tier is None  # falls in the gap between 1-99 and 100-499
        assert "gap" in selection.reason

    def test_fractional_quantity_inside_bounds(self) -> None:
        selection = select_price_break(SPEC_TIERS, Decimal("100.5"))
        assert selection.tier is not None
        assert selection.tier.unit_price == Decimal("10.50")
