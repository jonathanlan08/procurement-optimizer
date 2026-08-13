"""BOM request/response schemas (docs/planning/03-api-contract.md §4.7).

Wire conventions (§1.2): decimal fields are JSON strings, never numbers.
`QuantityString` parses with `app.core.money.parse_at_scale` at QTY_SCALE
(6dp, matching the `NUMERIC(18,6)` `quantity_per_assembly` column in
migration 0005) and serializes with `app.core.money.to_wire`. Quantities
must be `> 0` (§5 "Quantities > 0"); `Field(gt=0)` rejects `0` and negative
values with `422` before the request ever reaches the service layer or the
DB's own `ck_bill_of_material_lines_quantity_per_assembly_pos` check.

Versioning (docs/planning/02-erd.md §5/§10, app.models.boms module
docstring): copy-on-write. Every BOM response carries `root_bom_id`,
`version_number`, `previous_version_id`, and `status` so a client can always
tell which version of a BOM's history it is looking at. There is
deliberately no line-edit schema anywhere in this module: lines are
immutable once a version exists (`BillOfMaterialLine` carries no
timestamp/version columns at all - see the model's own docstring); the only
way to change a line is `POST /boms/{id}/versions`, which replaces the whole
line set at once via `BomVersionCreate`.

`BomCreate`/`BomVersionCreate` both require at least one line
(`Field(min_length=1)`): a BOM with zero components is not a resource that
can be exploded into an RFQ or costed, and every scenario in the
brief exercises multi-line BOMs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
)

from app.core.money import QTY_SCALE, MoneyError, parse_at_scale, to_wire
from app.models.boms import BillOfMaterialLine, BillOfMaterials


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


# NUMERIC(18,6) quantity_per_assembly -> QTY_SCALE (6dp)
QuantityString = Annotated[
    Decimal,
    _decimal_from_wire(QTY_SCALE),
    PlainSerializer(to_wire, return_type=str),
    WithJsonSchema({"type": "string", "example": "12.500000"}),
]


class BomLineCreate(BaseModel):
    """One input line for `BomCreate.lines[]` / `BomVersionCreate.lines[]`.

    `line_number` is assigned by the service in submission order (1-based),
    not a client-supplied field: copy-on-write means a whole line set is
    always submitted and replaced together, so there is no "keep this line's
    number stable while I edit another" scenario the way there is for
    individually-editable RFQ lines.

    `unit_definition_id` is optional: when omitted, `BomService` defaults it
    to the referenced part's own `unit_definition_id` (per the
    brief: "unit_definition_id optional - default the part's unit"). When
    provided explicitly it is still validated exactly like
    `Part.unit_definition_id` (global catalogue or this org's own private
    unit; never another organization's) - see `BomService._validate_unit`.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    part_id: uuid.UUID
    quantity_per_assembly: QuantityString = Field(gt=0)
    unit_definition_id: uuid.UUID | None = None
    is_optional: bool = False
    substitute_part_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)


class BomCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[BomLineCreate] = Field(min_length=1)


class BomVersionCreate(BaseModel):
    """`POST /boms/{id}/versions` body: a full line-set replacement plus
    optional metadata overrides (§4.7: "copy-on-write new version from a
    full line payload"). Metadata fields left unset (`None`) copy forward
    from the version being forked from; `lines` is always a full
    replacement of the new version's line set - never merged with the prior
    version's lines.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    product_name: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[BomLineCreate] = Field(min_length=1)


class BomArchiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class BomLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    line_number: int
    part_id: str
    quantity_per_assembly: QuantityString
    unit_definition_id: str
    is_optional: bool
    substitute_part_id: str | None
    notes: str | None

    @classmethod
    def from_model(cls, line: BillOfMaterialLine) -> BomLineResponse:
        return cls(
            id=str(line.id),
            line_number=line.line_number,
            part_id=str(line.part_id),
            quantity_per_assembly=line.quantity_per_assembly,
            unit_definition_id=str(line.unit_definition_id),
            is_optional=line.is_optional,
            substitute_part_id=(
                str(line.substitute_part_id) if line.substitute_part_id is not None else None
            ),
            notes=line.notes,
        )


class BomResponse(BaseModel):
    """Full detail: one BOM version with its lines. Used by every mutating
    endpoint and `GET /boms/{id}` - the contract (§4.7) documents lines as
    present only on the single-resource GET; `GET /boms` and
    `GET /boms/{id}/versions` use `BomSummaryResponse` instead, which omits
    them to keep list payloads bounded.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    root_bom_id: str
    version_number: int
    previous_version_id: str | None
    name: str
    product_name: str
    status: str
    notes: str | None
    created_by_id: str
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    created_at: datetime
    updated_at: datetime
    lines: list[BomLineResponse]

    @classmethod
    def from_model(cls, bom: BillOfMaterials, lines: list[BillOfMaterialLine]) -> BomResponse:
        return cls(
            id=str(bom.id),
            root_bom_id=str(bom.root_bom_id),
            version_number=bom.version_number,
            previous_version_id=(
                str(bom.previous_version_id) if bom.previous_version_id is not None else None
            ),
            name=bom.name,
            product_name=bom.product_name,
            status=bom.status.value,
            notes=bom.notes,
            created_by_id=str(bom.created_by_id),
            is_archived=bom.is_archived,
            archived_at=bom.archived_at,
            archive_reason=bom.archive_reason,
            created_at=bom.created_at,
            updated_at=bom.updated_at,
            lines=[BomLineResponse.from_model(line) for line in lines],
        )


class BomSummaryResponse(BaseModel):
    """Version metadata without lines: `GET /boms` (list) and
    `GET /boms/{id}/versions` (chain)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    root_bom_id: str
    version_number: int
    previous_version_id: str | None
    name: str
    product_name: str
    status: str
    notes: str | None
    created_by_id: str
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, bom: BillOfMaterials) -> BomSummaryResponse:
        return cls(
            id=str(bom.id),
            root_bom_id=str(bom.root_bom_id),
            version_number=bom.version_number,
            previous_version_id=(
                str(bom.previous_version_id) if bom.previous_version_id is not None else None
            ),
            name=bom.name,
            product_name=bom.product_name,
            status=bom.status.value,
            notes=bom.notes,
            created_by_id=str(bom.created_by_id),
            is_archived=bom.is_archived,
            archived_at=bom.archived_at,
            archive_reason=bom.archive_reason,
            created_at=bom.created_at,
            updated_at=bom.updated_at,
        )


class BomPageInfo(BaseModel):
    limit: int
    offset: int
    total: int


class BomListResponse(BaseModel):
    items: list[BomSummaryResponse]
    page: BomPageInfo


class BomVersionChainResponse(BaseModel):
    """`GET /boms/{id}/versions`: the full chain for the given BOM's root,
    ascending by `version_number` (v1 first)."""

    items: list[BomSummaryResponse]


__all__ = [
    "BomArchiveRequest",
    "BomCreate",
    "BomLineCreate",
    "BomLineResponse",
    "BomListResponse",
    "BomPageInfo",
    "BomResponse",
    "BomSummaryResponse",
    "BomVersionChainResponse",
    "BomVersionCreate",
]
