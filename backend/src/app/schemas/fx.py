"""Exchange rate request/response schemas (docs/planning/03-api-contract.md
§4.13).

Wire conventions (§1.2): money/decimal fields are JSON strings, never
numbers. `RateString` mirrors `app.schemas.suppliers.DecimalString` but at
`app.core.money.RATE_SCALE` (12dp, NUMERIC(24,12)) instead of the 6dp money
scale - exchange rates are their own column type, not a money amount.
`CurrencyCode`/`PageInfo` are imported rather than redefined: same ISO-3
pattern and same `{limit, offset, total}` shape as every other paginated
list in this API.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, WithJsonSchema

from app.core.money import RATE_SCALE, MoneyError, parse_at_scale, to_wire
from app.models.fx import ExchangeRate
from app.schemas.suppliers import CurrencyCode, PageInfo


def _rate_from_wire(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        raise ValueError('must be a decimal string, e.g. "1.084000000000"')
    try:
        return parse_at_scale(value, RATE_SCALE)
    except MoneyError as exc:
        raise ValueError(str(exc)) from exc


# NUMERIC(24,12) exchange_rates.rate -> RATE_SCALE (12dp)
RateString = Annotated[
    Decimal,
    BeforeValidator(_rate_from_wire),
    PlainSerializer(to_wire, return_type=str),
    WithJsonSchema({"type": "string", "example": "1.084000000000"}),
]


class ExchangeRateOverrideCreate(BaseModel):
    """POST /exchange-rates body (§4.13): manual override. `override_reason`
    is required and non-blank - `ck_exchange_rates_override_reason_required`
    (migration 0008) is the same rule enforced again at the DB boundary."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    base_currency: CurrencyCode
    quote_currency: CurrencyCode
    rate: RateString = Field(gt=0)
    effective_date: date
    override_reason: str = Field(min_length=1, max_length=1000)


class ExchangeRateResponse(BaseModel):
    """A single stored (always manual-override, in v0.1) exchange rate row."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    base_currency: str
    quote_currency: str
    rate: RateString
    effective_date: date
    source: str
    is_manual_override: bool
    override_reason: str | None
    created_by_id: str
    retrieved_at: datetime
    created_at: datetime

    @classmethod
    def from_model(cls, row: ExchangeRate) -> ExchangeRateResponse:
        return cls(
            id=str(row.id),
            base_currency=row.base_currency,
            quote_currency=row.quote_currency,
            rate=row.rate,
            effective_date=row.effective_date,
            source=row.source,
            is_manual_override=row.is_manual_override,
            override_reason=row.override_reason,
            created_by_id=str(row.created_by_id),
            retrieved_at=row.retrieved_at,
            created_at=row.created_at,
        )


class ExchangeRateListResponse(BaseModel):
    items: list[ExchangeRateResponse]
    page: PageInfo


class EffectiveExchangeRateResponse(BaseModel):
    """GET /exchange-rates?base=&quote=&as_of= (§4.13: "returns the effective
    row plus its provenance"). `exchange_rate_id` is `null` when the answer
    came from the synthetic fixture rather than a stored override - nothing
    was persisted to point at."""

    base_currency: str
    quote_currency: str
    as_of: date
    rate: RateString
    source: str
    is_manual_override: bool
    override_reason: str | None
    effective_date: date
    exchange_rate_id: str | None


class ExchangeRateRefreshResponse(BaseModel):
    """POST /exchange-rates/refresh (§4.13): "in demo/fixture mode returns
    `{source:"fixture", simulated:true}` and never touches the network."""

    source: str
    simulated: bool


__all__ = [
    "EffectiveExchangeRateResponse",
    "ExchangeRateListResponse",
    "ExchangeRateOverrideCreate",
    "ExchangeRateRefreshResponse",
    "ExchangeRateResponse",
    "RateString",
]
