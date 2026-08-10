"""Supplier performance record request/response schemas
(docs/planning/03-api-contract.md §4.4).

Wire conventions (§1.2): decimal fields are JSON strings, never numbers.
`RatioString` parses with `app.core.money.parse_at_scale` at RATIO_SCALE (6dp,
matching the `NUMERIC(9,6)` columns in migration 0002) and serializes with
`app.core.money.to_wire`.

Rate bounds: `on_time_delivery_rate` and `defect_rate` are contractually
"decimal strings 0-1" (§4.4); `quality_score` has no contract-stated upper
bound and the model/migration only enforce non-negativity
(`ck_supplier_performance_records_quality_score_nonneg`), so it is validated
here as `>= 0` only, deliberately looser than the two rate fields.

Records have no `version`/`archived_at` (SupplierPerformanceRecord mixes in
neither VersionedMixin nor ArchivableMixin — see app.models.suppliers): they
are point-in-time history, not a mutable aggregate with a lifecycle. There is
no update/delete route for them in the contract's route table (§4.4 lists
only GET and POST), so no corresponding schemas exist here.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    model_validator,
)

from app.core.money import RATIO_SCALE, MoneyError, parse_at_scale, to_wire
from app.models.suppliers import SupplierPerformanceRecord


def _decimal_from_wire(scale: Decimal) -> BeforeValidator:
    def _validate(value: Any) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if not isinstance(value, str):
            raise ValueError('must be a decimal string, e.g. "0.980000"')
        try:
            return parse_at_scale(value, scale)
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc

    return BeforeValidator(_validate)


# NUMERIC(9,6) columns (on_time_delivery_rate, defect_rate, quality_score) -> RATIO_SCALE (6dp)
RatioString = Annotated[
    Decimal,
    _decimal_from_wire(RATIO_SCALE),
    PlainSerializer(to_wire, return_type=str),
    WithJsonSchema({"type": "string", "example": "0.980000"}),
]

# source ∈ user_entered|synthetic_seed (app.models.suppliers.SupplierPerformanceRecord)
PerformanceSource = Literal["user_entered", "synthetic_seed"]


class SupplierPerformanceCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    period_start: date
    period_end: date
    on_time_delivery_rate: RatioString = Field(ge=0, le=1)
    defect_rate: RatioString = Field(ge=0, le=1)
    quality_score: RatioString = Field(ge=0)
    orders_count: int = Field(ge=0)
    source: PerformanceSource
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _check_period(self) -> SupplierPerformanceCreate:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")
        return self


class SupplierPerformanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    supplier_id: str
    period_start: date
    period_end: date
    on_time_delivery_rate: RatioString
    defect_rate: RatioString
    quality_score: RatioString
    orders_count: int
    source: str
    notes: str | None
    recorded_at: datetime

    @classmethod
    def from_model(cls, record: SupplierPerformanceRecord) -> SupplierPerformanceResponse:
        return cls(
            id=str(record.id),
            supplier_id=str(record.supplier_id),
            period_start=record.period_start,
            period_end=record.period_end,
            on_time_delivery_rate=record.on_time_delivery_rate,
            defect_rate=record.defect_rate,
            quality_score=record.quality_score,
            orders_count=record.orders_count,
            source=record.source,
            notes=record.notes,
            recorded_at=record.recorded_at,
        )


class SupplierPerformancePageInfo(BaseModel):
    limit: int
    offset: int
    total: int


class SupplierPerformanceListResponse(BaseModel):
    items: list[SupplierPerformanceResponse]
    page: SupplierPerformancePageInfo


__all__ = [
    "PerformanceSource",
    "RatioString",
    "SupplierPerformanceCreate",
    "SupplierPerformanceListResponse",
    "SupplierPerformancePageInfo",
    "SupplierPerformanceResponse",
]
