"""Part request/response schemas (docs/planning/03-api-contract.md §4.5).

Wire conventions (§1.2): money/decimal fields are JSON strings, never numbers.
`UnitPriceString` parses with `app.core.money.parse_at_scale` at
UNIT_PRICE_SCALE (8dp, matching the `NUMERIC(18,8)` `target_price` column in
migration 0004) and serializes with `app.core.money.to_wire`.

`target_price`/`target_price_currency` pairing mirrors the DB CHECK
`ck_parts_target_price_currency_paired` (migration 0004): "a price without a
currency (or vice versa) is a data error, not a valid 'unset' state" (§1.2).
On `PartCreate` this is a plain both-or-neither check on the parsed values;
on `PartUpdate` (a PATCH, where only fields present in the payload are
applied) it is checked against `model_fields_set` instead, since the client
either leaves both columns untouched or replaces both together - never
`{"target_price": "10.5"}` alone, which would otherwise satisfy the DB CHECK
by accident only if the existing row already had a NULL currency.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

from app.core.money import UNIT_PRICE_SCALE, MoneyError, parse_at_scale, to_wire
from app.models.parts import Part, PartAlternative


def _decimal_from_wire(scale: Decimal) -> BeforeValidator:
    def _validate(value: Any) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if not isinstance(value, str):
            raise ValueError('must be a decimal string, e.g. "10.50000000"')
        try:
            return parse_at_scale(value, scale)
        except MoneyError as exc:
            raise ValueError(str(exc)) from exc

    return BeforeValidator(_validate)


# NUMERIC(18,8) target_price -> UNIT_PRICE_SCALE (8dp)
UnitPriceString = Annotated[
    Decimal,
    _decimal_from_wire(UNIT_PRICE_SCALE),
    PlainSerializer(to_wire, return_type=str),
    WithJsonSchema({"type": "string", "example": "10.50000000"}),
]

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

# approval_status ∈ approved|conditional|rejected (app.models.parts.PartAlternative docstring)
ApprovalStatus = Literal["approved", "conditional", "rejected"]

RequiredSpecificationValue = str | bool | int | float


def _validate_required_specifications(
    value: dict[str, RequiredSpecificationValue] | None,
) -> dict[str, RequiredSpecificationValue] | None:
    """"validate keys nonempty, ≤100 entries" per this task's brief. Values are
    already type-constrained by the annotation; nothing further to check."""
    if value is None:
        return value
    if len(value) > 100:
        raise ValueError("required_specifications may have at most 100 entries")
    for key in value:
        if not key.strip():
            raise ValueError("required_specifications keys must be non-empty")
    return value


RequiredSpecifications = Annotated[
    dict[str, RequiredSpecificationValue] | None,
    AfterValidator(_validate_required_specifications),
]


class PartCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    internal_part_number: str = Field(min_length=1, max_length=100)
    manufacturer_part_number: str | None = Field(default=None, max_length=100)
    manufacturer: str | None = Field(default=None, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category: str | None = Field(default=None, max_length=100)
    unit_definition_id: uuid.UUID
    required_specifications: RequiredSpecifications = None
    target_price: UnitPriceString | None = Field(default=None, ge=0)
    target_price_currency: CurrencyCode | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _check_currency_pairing(self) -> PartCreate:
        if (self.target_price is None) != (self.target_price_currency is None):
            raise ValueError(
                "target_price and target_price_currency must both be set or both be absent"
            )
        return self


class PartUpdate(BaseModel):
    """Partial update (PATCH): only fields present in the payload are applied.

    Concurrency is carried by the `If-Match` header (§1.7), not the body -
    there is deliberately no `version` field here.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    internal_part_number: str | None = Field(default=None, min_length=1, max_length=100)
    manufacturer_part_number: str | None = Field(default=None, max_length=100)
    manufacturer: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category: str | None = Field(default=None, max_length=100)
    unit_definition_id: uuid.UUID | None = None
    required_specifications: RequiredSpecifications = None
    target_price: UnitPriceString | None = Field(default=None, ge=0)
    target_price_currency: CurrencyCode | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _check_currency_pairing(self) -> PartUpdate:
        fields_set = self.model_fields_set
        price_set = "target_price" in fields_set
        currency_set = "target_price_currency" in fields_set
        if price_set != currency_set:
            raise ValueError(
                "target_price and target_price_currency must be provided together"
            )
        if price_set and (self.target_price is None) != (self.target_price_currency is None):
            raise ValueError(
                "target_price and target_price_currency must both be null or both be set"
            )
        return self


class PartArchiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class PartResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    internal_part_number: str
    manufacturer_part_number: str | None
    manufacturer: str | None
    name: str
    description: str | None
    category: str | None
    unit_definition_id: str
    required_specifications: dict[str, Any] | None
    target_price: UnitPriceString | None
    target_price_currency: str | None
    is_active: bool
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, part: Part) -> PartResponse:
        return cls(
            id=str(part.id),
            internal_part_number=part.internal_part_number,
            manufacturer_part_number=part.manufacturer_part_number,
            manufacturer=part.manufacturer,
            name=part.name,
            description=part.description,
            category=part.category,
            unit_definition_id=str(part.unit_definition_id),
            required_specifications=part.required_specifications,
            target_price=part.target_price,
            target_price_currency=part.target_price_currency,
            is_active=part.is_active,
            is_archived=part.is_archived,
            archived_at=part.archived_at,
            archive_reason=part.archive_reason,
            version=part.version,
            created_at=part.created_at,
            updated_at=part.updated_at,
        )


class PageInfo(BaseModel):
    limit: int
    offset: int
    total: int


class PartListResponse(BaseModel):
    items: list[PartResponse]
    page: PageInfo


class PartAlternativeCreate(BaseModel):
    """`{alternative_part_id?|alternative_mpn?, approval_status, rationale}`
    (§4.5): exactly one of `alternative_part_id` (internal - must resolve to a
    part in this organization's catalogue) or `alternative_mpn` (external -
    a manufacturer part number this org has never catalogued) is required."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    alternative_part_id: uuid.UUID | None = None
    alternative_mpn: str | None = Field(default=None, min_length=1, max_length=100)
    approval_status: ApprovalStatus
    rationale: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _check_internal_xor_external(self) -> PartAlternativeCreate:
        has_internal = self.alternative_part_id is not None
        has_external = self.alternative_mpn is not None
        if has_internal == has_external:
            raise ValueError(
                "exactly one of alternative_part_id or alternative_mpn must be provided"
            )
        return self


class PartAlternativeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    part_id: str
    alternative_part_id: str | None
    alternative_mpn: str | None
    approval_status: str
    rationale: str | None
    approved_by_id: str | None
    approved_at: datetime | None

    @classmethod
    def from_model(cls, alt: PartAlternative) -> PartAlternativeResponse:
        return cls(
            id=str(alt.id),
            part_id=str(alt.part_id),
            alternative_part_id=(
                str(alt.alternative_part_id) if alt.alternative_part_id is not None else None
            ),
            alternative_mpn=alt.alternative_mpn,
            approval_status=alt.approval_status,
            rationale=alt.rationale,
            approved_by_id=(str(alt.approved_by_id) if alt.approved_by_id is not None else None),
            approved_at=alt.approved_at,
        )


class PartAlternativeListResponse(BaseModel):
    items: list[PartAlternativeResponse]
    page: PageInfo


__all__ = [
    "ApprovalStatus",
    "PageInfo",
    "PartAlternativeCreate",
    "PartAlternativeListResponse",
    "PartAlternativeResponse",
    "PartArchiveRequest",
    "PartCreate",
    "PartListResponse",
    "PartResponse",
    "PartUpdate",
    "RequiredSpecifications",
    "UnitPriceString",
]
