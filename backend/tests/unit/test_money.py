"""Unit tests for the principal-owned Decimal money policy."""

from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext

import pytest

from app.core.money import (
    MONEY_SCALE,
    UNIT_PRICE_SCALE,
    InvalidDecimalString,
    ScaleExceeded,
    calc_context,
    parse_at_scale,
    parse_decimal,
    quantize_money,
    quantize_unit_price,
    to_wire,
)


class TestParseDecimal:
    def test_parses_plain_string(self) -> None:
        assert parse_decimal("10.50") == Decimal("10.50")

    def test_parses_negative(self) -> None:
        assert parse_decimal("-37.522335") == Decimal("-37.522335")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(InvalidDecimalString):
            parse_decimal("12,50")

    def test_rejects_nan_and_infinity(self) -> None:
        for raw in ("NaN", "Infinity", "-Infinity", "sNaN"):
            with pytest.raises(InvalidDecimalString):
                parse_decimal(raw)

    def test_strips_whitespace(self) -> None:
        assert parse_decimal("  8.60 ") == Decimal("8.60")


class TestParseAtScale:
    def test_accepts_exact_scale(self) -> None:
        assert parse_at_scale("1.123456", MONEY_SCALE) == Decimal("1.123456")

    def test_rejects_finer_than_scale(self) -> None:
        with pytest.raises(ScaleExceeded):
            parse_at_scale("1.1234567", MONEY_SCALE)

    def test_unit_price_allows_8dp(self) -> None:
        assert parse_at_scale("11.41304348", UNIT_PRICE_SCALE) == Decimal("11.41304348")

    def test_integer_is_fine(self) -> None:
        assert parse_at_scale("500", MONEY_SCALE) == Decimal("500")


class TestQuantization:
    def test_bankers_rounding_half_even(self) -> None:
        # 0.0000005 rounds to even neighbour at 6 dp
        assert quantize_money(Decimal("1.0000005")) == Decimal("1.000000")
        assert quantize_money(Decimal("1.0000015")) == Decimal("1.000002")

    def test_worked_example_unit_price(self) -> None:
        # docs/planning/05 §9 (corrected): 10.50 EUR / 0.92 -> USD at 8 dp
        with localcontext(calc_context()):
            unit_usd = Decimal("10.50") / Decimal("0.92")
        assert quantize_unit_price(unit_usd) == Decimal("11.41304348")

    def test_worked_example_financing_correction(self) -> None:
        # principal's hand-verified correction of the planning draft
        with localcontext(calc_context()):
            extended = Decimal("5706.521740")
            financing = (
                extended * Decimal("0.08") * (Decimal("30") - Decimal("60")) / Decimal("365")
            )
        assert quantize_money(financing) == Decimal("-37.522335")

    def test_traps_division_by_zero(self) -> None:
        with localcontext(calc_context()), pytest.raises(DivisionByZero):
            Decimal(1) / Decimal(0)

    def test_traps_invalid_operation(self) -> None:
        with localcontext(calc_context()), pytest.raises(InvalidOperation):
            Decimal(0) / Decimal(0)


class TestWireFormat:
    def test_no_scientific_notation(self) -> None:
        assert to_wire(Decimal("1E+6")) == "1000000"

    def test_preserves_trailing_zeros_of_quantized_value(self) -> None:
        assert to_wire(quantize_money(Decimal("12"))) == "12.000000"
