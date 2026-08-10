"""Supplier request/response schemas (docs/planning/03-api-contract.md §4.4).

Wire conventions (§1.2): money/decimal fields are JSON strings, never numbers.
`DecimalString` below parses with `app.core.money.parse_at_scale` (scale
rejection, not silent rounding) and serializes with `app.core.money.to_wire`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    WithJsonSchema,
)

from app.core.money import QTY_SCALE, MoneyError, parse_at_scale, to_wire
from app.models.suppliers import Supplier


def _decimal_from_wire(scale: Decimal) -> BeforeValidator:
    def _validate(value: Any) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if not isinstance(value, str):
            raise ValueError('must be a decimal string, e.g. "12.500000"')
        try:
            return parse_at_scale(value, scale)
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc

    return BeforeValidator(_validate)


# NUMERIC(18,6) columns (capacity_units_per_month, default_moq) -> QTY_SCALE (6dp)
DecimalString = Annotated[
    Decimal,
    _decimal_from_wire(QTY_SCALE),
    PlainSerializer(to_wire, return_type=str),
    WithJsonSchema({"type": "string", "example": "12.500000"}),
]

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


class SupplierCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    country_code: CountryCode
    supported_currencies: list[CurrencyCode] = Field(default_factory=list, max_length=20)
    standard_payment_terms: str | None = Field(default=None, max_length=200)
    standard_incoterm: str | None = Field(default=None, max_length=100)
    typical_lead_time_days: int | None = Field(default=None, ge=0)
    capacity_units_per_month: DecimalString | None = Field(default=None, ge=0)
    default_moq: DecimalString | None = Field(default=None, ge=0)
    is_active: bool = True


class SupplierUpdate(BaseModel):
    """Partial update (PATCH): only fields present in the payload are applied.

    Concurrency is carried by the `If-Match` header (§1.7), not the body —
    there is deliberately no `version` field here.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    country_code: CountryCode | None = None
    supported_currencies: list[CurrencyCode] | None = Field(default=None, max_length=20)
    standard_payment_terms: str | None = Field(default=None, max_length=200)
    standard_incoterm: str | None = Field(default=None, max_length=100)
    typical_lead_time_days: int | None = Field(default=None, ge=0)
    capacity_units_per_month: DecimalString | None = Field(default=None, ge=0)
    default_moq: DecimalString | None = Field(default=None, ge=0)
    is_active: bool | None = None


class SupplierArchiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class SupplierResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    country_code: str
    supported_currencies: list[str]
    standard_payment_terms: str | None
    standard_incoterm: str | None
    typical_lead_time_days: int | None
    capacity_units_per_month: DecimalString | None
    default_moq: DecimalString | None
    is_active: bool
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, supplier: Supplier) -> SupplierResponse:
        return cls(
            id=str(supplier.id),
            code=supplier.code,
            name=supplier.name,
            country_code=supplier.country_code,
            supported_currencies=list(supplier.supported_currencies),
            standard_payment_terms=supplier.standard_payment_terms,
            standard_incoterm=supplier.standard_incoterm,
            typical_lead_time_days=supplier.typical_lead_time_days,
            capacity_units_per_month=supplier.capacity_units_per_month,
            default_moq=supplier.default_moq,
            is_active=supplier.is_active,
            is_archived=supplier.is_archived,
            archived_at=supplier.archived_at,
            archive_reason=supplier.archive_reason,
            version=supplier.version,
            created_at=supplier.created_at,
            updated_at=supplier.updated_at,
        )


class PageInfo(BaseModel):
    limit: int
    offset: int
    total: int


class SupplierListResponse(BaseModel):
    items: list[SupplierResponse]
    page: PageInfo


__all__ = [
    "PageInfo",
    "SupplierArchiveRequest",
    "SupplierCreate",
    "SupplierListResponse",
    "SupplierResponse",
    "SupplierUpdate",
]
