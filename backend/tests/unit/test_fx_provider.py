"""Unit tests for SyntheticFxProvider: purely functional, deterministic,
offline currency fixture (SPEC §9, docs/planning/05-calculation-methodology.md
§4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.money import quantize_rate
from app.providers.fx.synthetic import SOURCE_LABEL, SyntheticFxProvider

D1 = date(2026, 3, 1)
D2 = date(2019, 11, 20)  # any other date: the fixture is date-invariant


class TestDeterminism:
    def test_same_instance_same_inputs_same_output(self) -> None:
        provider = SyntheticFxProvider()
        assert provider.get_rate("USD", "EUR", D1) == provider.get_rate("USD", "EUR", D1)

    def test_different_instances_agree(self) -> None:
        a = SyntheticFxProvider()
        b = SyntheticFxProvider()
        assert a.get_rate("USD", "JPY", D1) == b.get_rate("USD", "JPY", D1)

    def test_date_invariant(self) -> None:
        provider = SyntheticFxProvider()
        assert provider.get_rate("USD", "GBP", D1) == provider.get_rate("USD", "GBP", D2)

    def test_describe_source_is_stable(self) -> None:
        assert SyntheticFxProvider().describe_source() == SOURCE_LABEL == "synthetic_fixture"


class TestSameCurrency:
    def test_identity_rate_is_exactly_one(self) -> None:
        provider = SyntheticFxProvider()
        for code in ("USD", "EUR", "GBP", "ZZZ"):  # even an unknown code: identity is trivial
            assert provider.get_rate(code, code, D1) == Decimal("1.000000000000")


class TestUsdBaseRates:
    def test_usd_to_eur_matches_fixture_table(self) -> None:
        provider = SyntheticFxProvider()
        assert provider.get_rate("USD", "EUR", D1) == Decimal("0.920000000000")

    def test_usd_to_jpy_matches_fixture_table(self) -> None:
        provider = SyntheticFxProvider()
        assert provider.get_rate("USD", "JPY", D1) == Decimal("149.500000000000")

    def test_usd_to_cny_gbp_mxn_inr_are_known(self) -> None:
        provider = SyntheticFxProvider()
        for currency in ("GBP", "CNY", "MXN", "INR"):
            rate = provider.get_rate("USD", currency, D1)
            assert rate is not None
            assert rate > 0


class TestInverse:
    def test_inverse_is_exact_at_12dp(self) -> None:
        provider = SyntheticFxProvider()
        usd_to_eur = provider.get_rate("USD", "EUR", D1)
        eur_to_usd = provider.get_rate("EUR", "USD", D1)
        assert usd_to_eur is not None
        assert eur_to_usd is not None
        # 05-calculation-methodology.md §4: "if only the inverse pair exists,
        # use 1/rate computed at precision 34" then quantized to the column
        # scale (RATE_SCALE, 12dp) — recomputing that exact operation here
        # must reproduce the provider's own answer bit-for-bit.
        assert eur_to_usd == quantize_rate(Decimal(1) / usd_to_eur)

    def test_inverse_round_trip_is_close_to_one(self) -> None:
        provider = SyntheticFxProvider()
        usd_to_gbp = provider.get_rate("USD", "GBP", D1)
        gbp_to_usd = provider.get_rate("GBP", "USD", D1)
        assert usd_to_gbp is not None
        assert gbp_to_usd is not None
        # 12dp rounding on both legs means the round trip is extremely close
        # to, but not necessarily bit-exact, 1 — bounded by two roundings.
        assert abs(usd_to_gbp * gbp_to_usd - 1) < Decimal("0.000000001")


class TestTriangulation:
    def test_cross_rate_triangulates_through_usd(self) -> None:
        provider = SyntheticFxProvider()
        usd_to_eur = provider.get_rate("USD", "EUR", D1)
        usd_to_jpy = provider.get_rate("USD", "JPY", D1)
        eur_to_jpy = provider.get_rate("EUR", "JPY", D1)
        assert usd_to_eur is not None
        assert usd_to_jpy is not None
        assert eur_to_jpy is not None
        assert eur_to_jpy == quantize_rate(usd_to_jpy / usd_to_eur)

    def test_cross_rate_is_internally_consistent_both_directions(self) -> None:
        provider = SyntheticFxProvider()
        eur_to_gbp = provider.get_rate("EUR", "GBP", D1)
        gbp_to_eur = provider.get_rate("GBP", "EUR", D1)
        assert eur_to_gbp is not None
        assert gbp_to_eur is not None
        assert gbp_to_eur == quantize_rate(Decimal(1) / eur_to_gbp)


class TestUnknownCurrency:
    def test_unknown_quote_currency_returns_none(self) -> None:
        provider = SyntheticFxProvider()
        assert provider.get_rate("USD", "ZZZ", D1) is None

    def test_unknown_base_currency_returns_none(self) -> None:
        provider = SyntheticFxProvider()
        assert provider.get_rate("ZZZ", "USD", D1) is None

    def test_unknown_pair_neither_side_known_returns_none(self) -> None:
        provider = SyntheticFxProvider()
        assert provider.get_rate("ZZZ", "QQQ", D1) is None
