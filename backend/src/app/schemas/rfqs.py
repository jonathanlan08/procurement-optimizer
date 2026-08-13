"""RFQ request/response schemas (docs/planning/03-api-contract.md §4.8).

Wire conventions (§1.2): decimal fields are JSON strings, never numbers.
`required_quantity` reuses `app.schemas.boms.QuantityString` - both
`rfq_lines.required_quantity` and `bill_of_material_lines.quantity_per_
assembly` are `NUMERIC(18,6)` (QTY_SCALE, 6dp), so the same parse/serialize
pair applies unchanged. `CurrencyCode` and `RequiredSpecifications` are
likewise reused from `app.schemas.parts` rather than re-implemented: both are
plain validation helpers with no RFQ-specific behavior, and duplicating them
would just be a second place to keep the same rule in sync (§5's "currency
codes validated against a static ISO-4217 allowlist" and "required
specifications... nonempty keys, <=100 entries" apply identically here).

**Create: explicit lines XOR BOM explosion.** `RfqCreate` matches the
brief and 03-api-contract.md §4.8 ("optionally `{source_bom_id,
assembly_quantity}` to explode a BOM into lines"): a caller supplies either
`lines` (a manually-built RFQ, at least one line - mirrors `BomCreate.lines`
requiring `min_length=1`) or `source_bom_id` (the BOM's own lines are copied
in by the service, `RfqService.explode_from_bom`), never both.
`assembly_quantity` is only meaningful paired with `source_bom_id` (the
multiplier applied to every copied line's quantity, per the brief: "copies
BOM lines into rfq_lines with quantities multiplied by an assembly_quantity
parameter defaulting to 1"); providing it without `source_bom_id` is a
validation error, not silently ignored.

**Update: draft-only with an administrator override.** 03-api-contract.md
§4.8's `PATCH /rfqs/{id}` row reads: "`If-Match`; line edits blocked once
`status != draft` unless `A`/`O` with `{override_reason}` (audited)". That
sentence covers both this schema (`RfqUpdate.override_reason`) and the line
mutation schemas below (`RfqLineBulkCreate`/`RfqLineUpdate`/
`RfqLineDeleteRequest` all carry the same field) - `RfqService` enforces the
role/reason gate identically in both places (`_ensure_draft_or_override`).
Concurrency itself is carried by the `If-Match` header, not the body - same
convention as `PartUpdate`/`SupplierUpdate` (no `version` field here).

**Status change: `reason` on the wire, `note` internally.** 03-api-
contract.md §4.8 names the body field `reason` (`{to_status, reason}`); the
`RfqStatusHistory` model column it is stored into is named `note` (per the
principal's status-transition ruling text quoted in app/models/rfqs.py).
Rather than pick one and contradict the other, the wire field here is named
`reason` (contract wins on the wire shape) and `RfqService.change_status`
maps it onto `RfqStatusHistory.note` - the identical "wire name differs from
the column it lands in" shape already used by `BomArchiveRequest.reason` ->
`BillOfMaterials.archive_reason` / `Part.archive_reason` elsewhere in this
codebase.

**Supplier invitation: `supplier_ids`, not the literal `supplier_id[]`.**
§4.8's route table entry reads `invite {supplier_id[]}` - read as prose
shorthand for "an array of supplier ids", the same shorthand convention the
contract uses at `excluded_supplier_ids[]` / `locked_allocations[]` in
§4.16's actual JSON body examples (which spell the field
`excluded_supplier_ids`, plural, no brackets). `RfqSupplierInviteRequest`
follows that literal JSON shape: `{"supplier_ids": [...]}`.

**Supplier sub-resource id.** `{sid}` in `DELETE /rfqs/{id}/suppliers/{sid}`
is the `RfqSupplier` join-row's own `id`, not the invited `Supplier`'s id -
mirroring `/parts/{id}/alternatives/{aid}` and `/suppliers/{id}/contacts/
{cid}`, both of which key their child sub-resource routes by the child row's
own id, not by the id of the thing it references.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.rfqs import Rfq, RfqLine, RfqStatus, RfqStatusHistory, RfqSupplier
from app.schemas.boms import QuantityString
from app.schemas.parts import CurrencyCode, RequiredSpecifications


class RfqLineCreate(BaseModel):
    """One input line for `RfqCreate.lines[]` / `RfqLineBulkCreate.lines[]`.

    `unit_definition_id` is optional: when omitted, `RfqService` defaults it
    to the referenced part's own `unit_definition_id` - same convention as
    `BomLineCreate.unit_definition_id` (app/schemas/boms.py).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    part_id: uuid.UUID
    required_quantity: QuantityString = Field(gt=0)
    unit_definition_id: uuid.UUID | None = None
    required_specifications: RequiredSpecifications = None
    notes: str | None = Field(default=None, max_length=4000)


class RfqCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    internal_reference: str = Field(min_length=1, max_length=100)
    base_currency: CurrencyCode
    due_date: date
    requested_delivery_date: date | None = None
    requested_payment_terms: str | None = Field(default=None, max_length=200)
    requested_incoterm: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[RfqLineCreate] = Field(default_factory=list)
    source_bom_id: uuid.UUID | None = None
    assembly_quantity: QuantityString | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_lines_xor_bom(self) -> RfqCreate:
        if self.source_bom_id is not None:
            if self.lines:
                raise ValueError(
                    "lines must be omitted when source_bom_id is provided; the "
                    "BOM's own lines are copied in instead"
                )
        else:
            if not self.lines:
                raise ValueError(
                    "at least one line is required when source_bom_id is not provided"
                )
            if self.assembly_quantity is not None:
                raise ValueError(
                    "assembly_quantity is only meaningful together with source_bom_id"
                )
        return self


class RfqUpdate(BaseModel):
    """Partial update (PATCH): only fields present in the payload are
    applied. Blocked once the RFQ's status is not `draft`, unless the actor
    is administrator+ and supplies `override_reason` - see module docstring.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    internal_reference: str | None = Field(default=None, min_length=1, max_length=100)
    base_currency: CurrencyCode | None = None
    due_date: date | None = None
    requested_delivery_date: date | None = None
    requested_payment_terms: str | None = Field(default=None, max_length=200)
    requested_incoterm: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=4000)
    override_reason: str | None = Field(default=None, max_length=1000)


class RfqStatusChangeRequest(BaseModel):
    """`POST /rfqs/{id}/status` body. See module docstring for the
    `reason` (wire) -> `note` (model column) mapping."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    to_status: RfqStatus
    reason: str | None = Field(default=None, max_length=1000)


class RfqLineBulkCreate(BaseModel):
    """`POST /rfqs/{id}/lines` body - "bulk POST accepted" (§4.8): one or
    more lines added in a single call, each becoming its own audit event
    (`rfq.line_added`) and its own `line_number` continuing on from the
    RFQ's current highest line number."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    lines: list[RfqLineCreate] = Field(min_length=1)
    override_reason: str | None = Field(default=None, max_length=1000)


class RfqLineUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    part_id: uuid.UUID | None = None
    required_quantity: QuantityString | None = Field(default=None, gt=0)
    unit_definition_id: uuid.UUID | None = None
    required_specifications: RequiredSpecifications = None
    notes: str | None = Field(default=None, max_length=4000)
    override_reason: str | None = Field(default=None, max_length=1000)


class RfqLineDeleteRequest(BaseModel):
    """Optional `DELETE /rfqs/{id}/lines/{lid}` body: empty (or omitted)
    while the RFQ is `draft`; `override_reason` required otherwise - same
    override gate as every other line mutation."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    override_reason: str | None = Field(default=None, max_length=1000)


class RfqSupplierInviteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    supplier_ids: list[uuid.UUID] = Field(min_length=1)


class RfqSupplierExcludeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    exclusion_reason: str = Field(min_length=1, max_length=1000)


class RfqLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    line_number: int
    part_id: str
    required_quantity: QuantityString
    unit_definition_id: str
    required_specifications: dict[str, Any] | None
    notes: str | None

    @classmethod
    def from_model(cls, line: RfqLine) -> RfqLineResponse:
        return cls(
            id=str(line.id),
            line_number=line.line_number,
            part_id=str(line.part_id),
            required_quantity=line.required_quantity,
            unit_definition_id=str(line.unit_definition_id),
            required_specifications=line.required_specifications,
            notes=line.notes,
        )


class RfqSupplierResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rfq_id: str
    supplier_id: str
    invited_at: datetime
    excluded_at: datetime | None
    exclusion_reason: str | None
    notes: str | None

    @classmethod
    def from_model(cls, rs: RfqSupplier) -> RfqSupplierResponse:
        return cls(
            id=str(rs.id),
            rfq_id=str(rs.rfq_id),
            supplier_id=str(rs.supplier_id),
            invited_at=rs.invited_at,
            excluded_at=rs.excluded_at,
            exclusion_reason=rs.exclusion_reason,
            notes=rs.notes,
        )


class RfqStatusHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rfq_id: str
    from_status: str
    to_status: str
    actor_user_id: str
    # resolved display name (org-scoped); None when the actor is unknown -
    # raw UUIDs in the status timeline were a 2026-08 external-review P3
    actor_full_name: str | None = None
    note: str | None
    occurred_at: datetime

    @classmethod
    def from_model(
        cls, entry: RfqStatusHistory, *, actor_full_name: str | None = None
    ) -> RfqStatusHistoryResponse:
        return cls(
            id=str(entry.id),
            rfq_id=str(entry.rfq_id),
            from_status=entry.from_status.value,
            to_status=entry.to_status.value,
            actor_user_id=str(entry.actor_user_id),
            actor_full_name=actor_full_name,
            note=entry.note,
            occurred_at=entry.occurred_at,
        )


class RfqResponse(BaseModel):
    """Full detail: one RFQ with its lines and summary counts. Used by every
    endpoint that returns a single RFQ (create/get/update/status-change) -
    mirrors `BomResponse` always carrying its lines.

    `line_count`/`invited_supplier_count` are the contract's "summary +
    counts" for `GET /rfqs/{id}` (§4.8). `quote_count`/
    `unresolved_review_count` are not included: the quotes subsystem
    (migration 0009 `quotes`/`quote_lines`, built concurrently in a separate
    task) does not exist yet in this codebase, so there is nothing to count
    - adding those counts is left to whichever phase lands quotes.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    internal_reference: str
    status: str
    base_currency: str
    requested_payment_terms: str | None
    requested_incoterm: str | None
    due_date: date
    requested_delivery_date: date | None
    notes: str | None
    source_bom_id: str | None
    created_by_id: str
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    line_count: int
    invited_supplier_count: int
    lines: list[RfqLineResponse]

    @classmethod
    def from_model(
        cls,
        rfq: Rfq,
        lines: list[RfqLine],
        *,
        invited_supplier_count: int,
    ) -> RfqResponse:
        return cls(
            id=str(rfq.id),
            name=rfq.name,
            internal_reference=rfq.internal_reference,
            status=rfq.status.value,
            base_currency=rfq.base_currency,
            requested_payment_terms=rfq.requested_payment_terms,
            requested_incoterm=rfq.requested_incoterm,
            due_date=rfq.due_date,
            requested_delivery_date=rfq.requested_delivery_date,
            notes=rfq.notes,
            source_bom_id=(str(rfq.source_bom_id) if rfq.source_bom_id is not None else None),
            created_by_id=str(rfq.created_by_id),
            is_archived=rfq.is_archived,
            archived_at=rfq.archived_at,
            archive_reason=rfq.archive_reason,
            version=rfq.version,
            created_at=rfq.created_at,
            updated_at=rfq.updated_at,
            line_count=len(lines),
            invited_supplier_count=invited_supplier_count,
            lines=[RfqLineResponse.from_model(line) for line in lines],
        )


class RfqSummaryResponse(BaseModel):
    """Version-free summary for `GET /rfqs` (list): no `lines` array, to
    keep list payloads bounded - mirrors `BomSummaryResponse`.
    `invited_supplier_count` is still omitted here (unlike `RfqResponse`):
    computing it per row would turn a bounded list query into an N+1, and
    the contract only asks for it on the single-resource `GET /rfqs/{id}`
    (§4.8).

    `line_count` IS included, unlike `invited_supplier_count` above, despite
    that same N+1 concern - 2026-08 product-audit remediation, P2: "Lines
    column always displays a dash because the summary API omits the count".
    `RfqRepository.search` sources it from one `GROUP BY` subquery joined
    onto the same paged query that fetches the rows (see that repository's
    module docstring), so adding it here does not reintroduce the N+1 this
    class's own docstring warns against for supplier counts - it was never
    an N+1 to begin with. FROZEN wire name: `line_count`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    internal_reference: str
    status: str
    base_currency: str
    due_date: date
    requested_delivery_date: date | None
    source_bom_id: str | None
    is_archived: bool
    version: int
    created_at: datetime
    updated_at: datetime
    line_count: int

    @classmethod
    def from_model(cls, rfq: Rfq, *, line_count: int) -> RfqSummaryResponse:
        return cls(
            id=str(rfq.id),
            name=rfq.name,
            internal_reference=rfq.internal_reference,
            status=rfq.status.value,
            base_currency=rfq.base_currency,
            due_date=rfq.due_date,
            requested_delivery_date=rfq.requested_delivery_date,
            source_bom_id=(str(rfq.source_bom_id) if rfq.source_bom_id is not None else None),
            is_archived=rfq.is_archived,
            version=rfq.version,
            created_at=rfq.created_at,
            updated_at=rfq.updated_at,
            line_count=line_count,
        )


class RfqPageInfo(BaseModel):
    limit: int
    offset: int
    total: int


class RfqListResponse(BaseModel):
    items: list[RfqSummaryResponse]
    page: RfqPageInfo


class RfqLineListResponse(BaseModel):
    items: list[RfqLineResponse]


class RfqSupplierListResponse(BaseModel):
    items: list[RfqSupplierResponse]


class RfqStatusHistoryListResponse(BaseModel):
    items: list[RfqStatusHistoryResponse]


__all__ = [
    "RfqCreate",
    "RfqLineBulkCreate",
    "RfqLineCreate",
    "RfqLineDeleteRequest",
    "RfqLineListResponse",
    "RfqLineResponse",
    "RfqLineUpdate",
    "RfqListResponse",
    "RfqPageInfo",
    "RfqResponse",
    "RfqStatusChangeRequest",
    "RfqStatusHistoryListResponse",
    "RfqStatusHistoryResponse",
    "RfqSummaryResponse",
    "RfqSupplierExcludeRequest",
    "RfqSupplierInviteRequest",
    "RfqSupplierListResponse",
    "RfqSupplierResponse",
    "RfqUpdate",
]
