"""`FxRateProvider` Protocol — the boundary every FX rate source implements.

Kept intentionally tiny: a provider answers "what is the rate for this pair,
as of this date" and can describe where that answer came from. Date-selection
among *stored* rows (manual override vs. latest on-or-before) is
`fx_service`'s job, not the provider's — a provider is a pure function over
`(base, quote, as_of_date)`, never a database lookup.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol


class FxRateProvider(Protocol):
    def get_rate(self, base: str, quote: str, as_of_date: date) -> Decimal | None:
        """`1 base = rate * quote`, quantized to `app.core.money.RATE_SCALE`
        (12dp). `None` if the provider has no answer for this pair (e.g. an
        unknown currency code) — never a silent 1.0 or a raised exception for
        a merely-unknown pair (docs/planning/05-calculation-methodology.md
        §4: "never fall back to 1.0")."""
        ...

    def describe_source(self) -> str:
        """A short, stable label identifying this provider for the `source`
        field on API responses / audit records, e.g. ``"synthetic_fixture"``."""
        ...


__all__ = ["FxRateProvider"]
