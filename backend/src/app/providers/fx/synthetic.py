"""SyntheticFxProvider — the only FX rate provider shipped in v0.1.

SPEC §9: "Public demo ships deterministic synthetic exchange rates; automated
tests must not depend on external services."
docs/planning/05-calculation-methodology.md §4: "Fixture provider ships a
fixed synthetic table (USD base; EUR, GBP, JPY, CNY, MXN, INR) ... Clearly
labelled synthetic."

These are demonstration rates only — not sourced from any real market feed.
Purely functional: no network I/O, no randomness, no mutable state, and (by
design) no dependence on `as_of_date` — the fixture table is flat. Exercising
as-of *date-selection* logic (picking the latest stored row on or before a
target date, manual override vs. not) is `fx_service`'s job over persisted
`ExchangeRate` rows, not this provider's; see that module's docstring.

Rate direction (05-calculation-methodology.md §4): ``ExchangeRate(base,
quote, rate)`` means ``1 base = rate * quote``. The table below is USD-to-X,
so for a USD-based pair the stored value is used directly; every other pair
is triangulated through USD; the same-currency case is always exactly 1;
an unknown currency code (on either side, when the pair isn't same-currency)
yields `None` rather than a guess.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.money import quantize_rate

# USD base: 1 USD = <value> <currency>. Synthetic demonstration rates only.
_USD_TO_QUOTE: dict[str, Decimal] = {
    "EUR": Decimal("0.92"),
    "GBP": Decimal("0.79"),
    "JPY": Decimal("149.50"),
    "CNY": Decimal("7.24"),
    "MXN": Decimal("17.05"),
    "INR": Decimal("83.12"),
}

_USD = "USD"
_IDENTITY_RATE: Decimal = quantize_rate(Decimal("1"))  # 1.000000000000

SOURCE_LABEL = "synthetic_fixture"


class SyntheticFxProvider:
    """Deterministic, offline FX rate fixture. Stateless — safe to construct
    freely; every instance returns identical answers for identical inputs."""

    def _usd_leg(self, currency: str) -> Decimal | None:
        if currency == _USD:
            return Decimal("1")
        return _USD_TO_QUOTE.get(currency)

    def get_rate(self, base: str, quote: str, as_of_date: date) -> Decimal | None:
        del as_of_date  # flat fixture table: intentionally date-invariant, see module docstring
        if base == quote:
            return _IDENTITY_RATE

        base_leg = self._usd_leg(base)
        quote_leg = self._usd_leg(quote)
        if base_leg is None or quote_leg is None:
            return None

        # 1 base = (usd_to_quote / usd_to_base) * quote — triangulated through
        # USD; when base == "USD" this reduces to usd_to_quote directly, and
        # when quote == "USD" it reduces to the inverse of usd_to_base.
        rate = quote_leg / base_leg
        return quantize_rate(rate)

    def describe_source(self) -> str:
        return SOURCE_LABEL


__all__ = ["SOURCE_LABEL", "SyntheticFxProvider"]
