"""Pure currency normalization for a single unit price
(docs/planning/05-calculation-methodology.md §4, SPEC §9). Pure domain code:
no database, no clock, no network, no randomness.

Rate LOOKUP (as-of date selection among stored `ExchangeRate` rows, manual
override precedence, `MissingExchangeRateError` vs. triangulation) is
`fx_service`'s job, not this module's - `app.providers.fx.base.FxRateProvider`
and the as-of/inverse/triangulation rules it documents apply *before* this
function is called. This module only does the arithmetic once a rate (or the
absence of one) has already been decided.

**Rate convention - read carefully, it differs from `FxRateProvider`.**
`FxRateProvider.get_rate(base, quote, ...)` returns a rate meaning
`1 base = rate * quote` (base -> quote). Converting an amount stated in the
*quote* currency into the *base* currency therefore *divides* by that rate
(05 §4: `to_base(amount_in_quote, rate) = amount_in_quote / rate`) - that is
exactly the worked example, EUR 10.50 / 0.92 (1 USD = 0.92 EUR) = USD
11.41304348.

This module does not reproduce that base/quote asymmetry. Its `rate`
parameter uses a single, simpler convention stated explicitly so it can never
be misread: **`rate` is "target per source" - `target_amount = source_amount
x rate`**. A caller sitting on a `FxRateProvider`-style base/quote rate must
invert it first when the provider's "base" is the *target* currency (as in
the worked example: the provider's `1 USD = 0.92 EUR` must become
`rate = 1/0.92` before calling `normalize_price(source_currency="EUR",
target_currency="USD", rate=1/0.92, ...)`, so that `10.50 * (1/0.92) =
11.41304348` matches the base/quote division by construction). That
inversion is the service layer's responsibility, precisely because it
requires knowing which side of the stored rate is "source" and which is
"target" for *this* call - information this pure function does not have and
must not guess.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from app.core.money import CALC_CONTEXT, quantize_unit_price
from app.domain.landed_cost.contracts import NormalizedPrice
from app.domain.values import Quantified


def normalize_price(
    unit_price: Quantified,
    source_currency: str,
    target_currency: str,
    rate: Decimal | None,
    rate_source: str | None,
) -> NormalizedPrice:
    """Convert `unit_price` (stated in `source_currency`) into
    `target_currency`.

    - Same currency: passthrough. `unit_price` is returned unchanged
      (missing stays missing, present stays present) and `fx_rate`/
      `fx_source`/`conversion_note` are all `None` - there was nothing to
      convert, so there is nothing to disclose.
    - Different currency, `unit_price` already missing: missingness
      propagates regardless of whether a rate is available - there is no
      price to multiply. `fx_rate`/`fx_source` stay `None` because no
      conversion was actually performed.
    - Different currency, `rate is None`: the price cannot be stated in
      `target_currency` without a rate. Returns a MISSING `unit_price` with
      an explanatory note - never a silent 1.0, never a guess
      (05-calculation-methodology.md §4).
    - Different currency, `rate` present: `unit_price.value * rate`,
      quantized once at `UNIT_PRICE_SCALE`. `fx_rate`/`fx_source` are
      populated so the conversion is auditable.

    Provenance of the converted price is carried through unchanged from the
    input `unit_price` (not upgraded/downgraded to `CALCULATED`): `rate` here
    is a plain, already-resolved `Decimal` with no provenance track of its
    own (unlike, say, `RiskAssumptions.quality_risk_rate`, which arrives as a
    `Quantified`) - the multiplication introduces no additional uncertainty
    the type system can express, so this mirrors `landed_cost.calculator`'s
    `_extended_material`, which likewise keeps `price.provenance` unchanged
    when multiplying a `Quantified` price by a plain, certain `Decimal`
    quantity.
    """
    if source_currency == target_currency:
        return NormalizedPrice(
            unit_price=unit_price,
            source_currency=source_currency,
            target_currency=target_currency,
            fx_rate=None,
            fx_source=None,
            unit_conversion_factor=None,
            conversion_note=None,
        )

    if unit_price.value is None:
        return NormalizedPrice(
            unit_price=Quantified.missing(
                note=(
                    f"{source_currency} unit price is not stated; there is nothing to "
                    f"convert to {target_currency}"
                )
            ),
            source_currency=source_currency,
            target_currency=target_currency,
            fx_rate=None,
            fx_source=None,
            unit_conversion_factor=None,
            conversion_note=None,
        )

    if rate is None:
        note = (
            f"no FX rate available to convert {source_currency} to {target_currency}; "
            f"price is not stated in {target_currency}"
        )
        return NormalizedPrice(
            unit_price=Quantified.missing(note=note),
            source_currency=source_currency,
            target_currency=target_currency,
            fx_rate=None,
            fx_source=None,
            unit_conversion_factor=None,
            conversion_note=note,
        )

    with localcontext(CALC_CONTEXT):
        converted_raw = unit_price.value * rate
    converted = quantize_unit_price(converted_raw)

    note = (
        f"{source_currency} to {target_currency} at rate {rate} "
        f"(target per source: {source_currency} amount x {rate} = {target_currency} amount)"
    )
    # unit_price.value is not None here (handled above), so by the
    # Quantified invariant unit_price.provenance is never MISSING.
    converted_price = Quantified(
        value=converted,
        provenance=unit_price.provenance,
        note=unit_price.note,
    )
    return NormalizedPrice(
        unit_price=converted_price,
        source_currency=source_currency,
        target_currency=target_currency,
        fx_rate=rate,
        fx_source=rate_source,
        unit_conversion_factor=None,
        conversion_note=note,
    )


__all__ = ["normalize_price"]
