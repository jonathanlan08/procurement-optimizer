"""Unit tests for pure unit-quantity conversion
(docs/planning/05-calculation-methodology.md §5, SPEC §10)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.units.normalize import (
    ConversionAssumptionMissingError,
    Dimension,
    DimensionMismatchError,
    Unit,
    convert_quantity,
)

# Mirrors app.seed.units_catalog.STANDARD_UNIT_CATALOG's factors exactly, as
# lightweight domain-only Units (no ORM/DB dependency).
EACH = Unit("each", Dimension.COUNT, Decimal("1"))
PACK = Unit("pack", Dimension.COUNT, None)
BOX = Unit("box", Dimension.COUNT, None)
KG = Unit("kg", Dimension.MASS, Decimal("1"))
LB = Unit("lb", Dimension.MASS, Decimal("0.45359237"))
M = Unit("m", Dimension.LENGTH, Decimal("1"))
CM = Unit("cm", Dimension.LENGTH, Decimal("0.01"))
FT = Unit("ft", Dimension.LENGTH, Decimal("0.3048"))
IN = Unit("in", Dimension.LENGTH, Decimal("0.0254"))
MM = Unit("mm", Dimension.LENGTH, Decimal("0.001"))


class TestExactFactors:
    """05 §5: "Mass/length conversions use exact defined factors ... stored
    at 12 dp, marked is_exact=true." """

    def test_lb_to_kg_is_the_legally_defined_exact_factor(self) -> None:
        result = convert_quantity(Decimal("1"), LB, KG)
        assert result.value == Decimal("0.453592")
        assert result.conversion_note == "1 lb = 0.45359237 kg"

    def test_ft_to_m_is_the_legally_defined_exact_factor(self) -> None:
        result = convert_quantity(Decimal("1"), FT, M)
        assert result.value == Decimal("0.304800")
        assert result.conversion_note == "1 ft = 0.3048 m"

    def test_in_to_mm(self) -> None:
        result = convert_quantity(Decimal("1"), IN, MM)
        assert result.value == Decimal("25.400000")
        assert result.conversion_note == "1 in = 25.4 mm"

    def test_scales_linearly_with_quantity(self) -> None:
        result = convert_quantity(Decimal("500"), LB, KG)
        assert result.value == Decimal("226.796185")


class TestDimensionMismatch:
    def test_mass_to_length_raises(self) -> None:
        with pytest.raises(DimensionMismatchError):
            convert_quantity(Decimal("1"), KG, M)

    def test_count_to_mass_raises(self) -> None:
        with pytest.raises(DimensionMismatchError):
            convert_quantity(Decimal("1"), EACH, KG)

    def test_error_names_both_units_and_dimensions(self) -> None:
        with pytest.raises(DimensionMismatchError) as exc_info:
            convert_quantity(Decimal("1"), KG, M)
        message = str(exc_info.value)
        assert "kg" in message
        assert "m" in message
        assert "mass" in message
        assert "length" in message


class TestCountDimensionAssumptions:
    """05 §5: pack/box/tray/reel have no universal each-ratio; guessing is
    "the single easiest way to produce a 5000x error"."""

    def test_pack_without_factor_raises(self) -> None:
        with pytest.raises(ConversionAssumptionMissingError):
            convert_quantity(Decimal("10"), PACK, EACH)

    def test_pack_with_factor_works_and_the_note_discloses_the_assumption(self) -> None:
        result = convert_quantity(Decimal("10"), PACK, EACH, part_factor=Decimal("50"))
        assert result.value == Decimal("500.000000")
        assert result.conversion_note == "1 pack = 50 each (per-part conversion assumption)"

    def test_each_to_pack_direction_also_needs_the_factor(self) -> None:
        result = convert_quantity(Decimal("500"), EACH, PACK, part_factor=Decimal("50"))
        assert result.value == Decimal("10.000000")

    def test_both_sides_missing_a_universal_factor_raises_even_with_part_factor(self) -> None:
        # pack -> box: neither has a universal factor; a single part_factor
        # cannot disambiguate two independent unknowns.
        with pytest.raises(ConversionAssumptionMissingError):
            convert_quantity(Decimal("1"), PACK, BOX, part_factor=Decimal("10"))


class TestRoundTripStability:
    def test_lb_kg_round_trip_recovers_the_original_within_rounding_tolerance(self) -> None:
        original = Decimal("137.25")
        to_kg = convert_quantity(original, LB, KG)
        back_to_lb = convert_quantity(to_kg.value, KG, LB)
        assert abs(back_to_lb.value - original) < Decimal("0.0001")

    def test_cm_in_round_trip_recovers_the_original_within_rounding_tolerance(self) -> None:
        # cm <-> in is not a clean ratio (25.4 recurring in the other
        # direction), so this exercises real QTY_SCALE rounding on both legs.
        original = Decimal("10")
        to_in = convert_quantity(original, CM, IN)
        back_to_cm = convert_quantity(to_in.value, IN, CM)
        assert abs(back_to_cm.value - original) < Decimal("0.0001")

    def test_pack_each_round_trip_with_consistent_part_factor(self) -> None:
        original = Decimal("7")
        to_each = convert_quantity(original, PACK, EACH, part_factor=Decimal("50"))
        back_to_pack = convert_quantity(to_each.value, EACH, PACK, part_factor=Decimal("50"))
        assert back_to_pack.value == original
