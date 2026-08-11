"""Unit tests for pure FX currency normalization of a single unit price
(docs/planning/05-calculation-methodology.md §4, SPEC §9)."""

from __future__ import annotations

from decimal import Decimal

from app.core.money import quantize_rate
from app.domain.fx.normalize import normalize_price
from app.domain.values import Provenance, Quantified


class TestSameCurrencyPassthrough:
    def test_same_currency_price_is_returned_unchanged(self) -> None:
        price = Quantified.supplier(Decimal("10.50"))
        result = normalize_price(price, "USD", "USD", rate=Decimal("1.5"), rate_source="manual")
        assert result.unit_price == price
        assert result.fx_rate is None
        assert result.fx_source is None
        assert result.conversion_note is None

    def test_same_currency_missing_price_stays_missing(self) -> None:
        price = Quantified.missing()
        result = normalize_price(price, "EUR", "EUR", rate=None, rate_source=None)
        assert result.unit_price.is_missing is True
        assert result.fx_rate is None


class TestMissingSourcePrice:
    def test_missing_price_propagates_regardless_of_an_available_rate(self) -> None:
        price = Quantified.missing()
        result = normalize_price(
            price, "EUR", "USD", rate=Decimal("1.086956521739"), rate_source="synthetic_fixture"
        )
        assert result.unit_price.is_missing is True
        assert result.unit_price.value is None
        # no conversion was actually performed -- nothing to disclose
        assert result.fx_rate is None
        assert result.fx_source is None


class TestMissingRateYieldsMissingPrice:
    def test_different_currency_no_rate_yields_a_missing_price_not_a_guess(self) -> None:
        price = Quantified.supplier(Decimal("10.50"))
        result = normalize_price(price, "EUR", "USD", rate=None, rate_source=None)
        assert result.unit_price.is_missing is True
        assert result.unit_price.value is None
        assert result.fx_rate is None
        assert result.fx_source is None
        assert result.conversion_note is not None
        assert "EUR" in result.conversion_note
        assert "USD" in result.conversion_note

    def test_missing_rate_note_is_also_on_the_result_conversion_note(self) -> None:
        price = Quantified.supplier(Decimal("1"))
        result = normalize_price(price, "GBP", "JPY", rate=None, rate_source=None)
        assert result.conversion_note == result.unit_price.note


class TestConversionAppliedAt8dp:
    def test_rate_multiplies_and_quantizes_to_unit_price_scale(self) -> None:
        price = Quantified.supplier(Decimal("100"))
        result = normalize_price(
            price, "USD", "EUR", rate=Decimal("0.92"), rate_source="synthetic_fixture"
        )
        assert result.unit_price.value == Decimal("92.00000000")
        assert result.fx_rate == Decimal("0.92")
        assert result.fx_source == "synthetic_fixture"
        assert result.unit_price.provenance is Provenance.SUPPLIER
        assert result.unit_conversion_factor is None  # this module never touches units

    def test_rate_not_an_exact_decimal_still_rounds_to_exactly_8dp(self) -> None:
        price = Quantified.supplier(Decimal("3.33"))
        result = normalize_price(
            price, "USD", "GBP", rate=Decimal("0.790000000000"), rate_source="s"
        )
        assert result.unit_price.value is not None
        assert result.unit_price.value.as_tuple().exponent == -8


class TestWorkedExample:
    """05-calculation-methodology.md §9: quote EUR 10.50, FX 1 USD = 0.92 EUR,
    unit price -> USD = 10.50 / 0.92 = 11.41304348.

    This module's own convention (see its docstring) is `rate = target per
    source`, i.e. multiply -- the inverse of the worked example's base/quote
    division. Constructing `rate = 1/0.92` at RATE_SCALE (exactly the
    inversion the service layer is documented to perform before calling
    here) and multiplying must reproduce the identical worked-example
    figure.
    """

    def test_eur_to_usd_matches_the_worked_example_via_the_inverse_rate(self) -> None:
        price = Quantified.supplier(Decimal("10.50"))
        rate = quantize_rate(Decimal(1) / Decimal("0.92"))
        assert rate == Decimal("1.086956521739")  # sanity: the exact inverse at 12dp

        result = normalize_price(price, "EUR", "USD", rate=rate, rate_source="synthetic_fixture")

        assert result.unit_price.value == Decimal("11.41304348")
        assert result.fx_rate == rate
        assert result.fx_source == "synthetic_fixture"
