"""Quote request/response schemas - manual entry only (docs/planning/
03-api-contract.md §4.11, docs/SPEC.md §6-7/§11, app/models/quotes.py).

Wire conventions (§1.2): decimal fields are JSON strings, never numbers.
`quote_lines`/`quote_price_breaks` mix two DB scales: `NUMERIC(18,6)`
(quantities, MOQ, production capacity, every per-line fixed-cost/tariff/
duty/customs/tax amount, and a price break's `min_quantity`/`max_quantity`/
`setup_fee`) and `NUMERIC(18,8)` (both tables' `unit_price`). `app.core.money`
defines `QTY_SCALE` and `MONEY_SCALE` as the *same* value
(`Decimal("0.000001")`, 6dp) - so rather than mint a byte-identical
`MoneyAmountString` type next to `QuantityString`, every 6dp field below
reuses `app.schemas.boms.QuantityString` directly (the same de-duplication
already applied to `CurrencyCode`/`RequiredSpecifications` in
app/schemas/rfqs.py); the 8dp fields reuse `app.schemas.parts.UnitPriceString`.

**Missing stays missing (SPEC §7, §1.2).** Every commercial field on a quote
line is optional and nullable with no default - omitting a field from the
request body, or sending it as `null`, is preserved as `NULL` end to end
(`QuoteService` never coerces an absent amount to `0`; the DB's own
`ck_quote_lines_*_nonneg` constraints already read "IS NULL OR ... >= 0" for
exactly this reason, per app/models/quotes.py).

**`rfq_id` is a path parameter, not a body field.** The delegating task's
prose lists `rfq_id` among `QuoteCreate`'s fields, but the contract's own
route table nests quote creation under the RFQ
(`POST /rfqs/{id}/quotes`, §4.11) - the same shape already established for
every other RFQ sub-resource create in this codebase (`RfqLineCreate`,
`RfqSupplierInviteRequest`, and `POST /rfqs/{id}/quote-documents`'s
`supplier_id`-only body all take their parent id from the path, never
duplicated in the body). `QuoteCreate` follows that precedent: no `rfq_id`
field here at all; `QuoteService.create` receives it as a method argument
supplied by the route from the path.

**`unit_definition_id` is required, not optional-with-part-default.**
Unlike `RfqLineCreate.unit_definition_id`/`BomLineCreate.unit_definition_id`
(both optional, defaulting to the referenced part's own unit), `QuoteLine.
part_id` is itself optional (app/models/quotes.py module docstring point 4:
"a line can be linked straight to a catalogued Part... [or not]") - there is
no reliable fallback to default *from* when a line has no catalogued part at
all (a supplier's raw, as-quoted line item). `unit_definition_id` is
therefore always required on `QuoteLineCreate`.

**`quoted_part_number`/`quoted_mpn` are included despite the delegating
task's prose omitting them.** The task's line-schema bullet lists description/
quantity/unit_price/moq/... but says to "follow models/quotes.py placement
EXACTLY", and `QuoteLine.quoted_part_number`/`quoted_mpn` are real, named
columns on that model (the supplier's own as-quoted identifiers, independent
of `part_id`/`matched_rfq_line_id`) - SPEC §7's own extraction-field list
opens with "part number". Read as the task's shorthand being incomplete
relative to its own "follow the model exactly" instruction, the same
judgement app/models/quotes.py's own docstring (point 8) already made once
for this exact phrase.

**`documentation_cost`/`handling_cost` (migration 0016, 2026-08 product-audit
remediation) - no longer folded into `other_fixed_cost`.** This paragraph
previously read "handling has no separate column - maps to
`other_fixed_cost`", because at the time `QuoteLine` genuinely had no
`handling_cost` column and `app.domain.landed_cost.contracts.LogisticsCosts.
handling` could only ever be supplied as missing (see
`services/landed_cost_service.py`'s module docstring and
`docs/METHODOLOGY.md` §7). Both `QuoteLine.documentation_cost` and
`QuoteLine.handling_cost` are now real columns (same shape as every sibling
fixed/logistics-cost field: nullable `NUMERIC(18,6)`, `>= 0`, missing stays
missing), so both are included below as ordinary optional fields, mirroring
`packaging_cost`/`other_fixed_cost` exactly. `other_fixed_cost` remains this
model's own general-purpose catch-all for anything that isn't one of the
named fixed-cost fields - it does not "mean" handling anymore.

**No `expiration_date >= quote_date` validation.** The contract's §5
"Validation rules" section lists this as a rule, but 00-decisions.md §4
explicitly rules "expired quotes usable with prominent warning", and the
migration/model carry no such CHECK (see
`tests/integration/test_quotes_schema.py`'s own module docstring, which
states this gap is deliberate). Enforcing it here would silently reject
already-expired manual entries the system is supposed to accept. No
validator for this pairing exists in this module.

**Update scope: whole-quote metadata only.** `QuoteUpdate` (`PATCH
/quotes/{id}`) only carries `quote_number`/`quote_date`/`expiration_date`/
`currency`/`notes` - §4.11's other quote-shaped routes (`PATCH .../lines
[...]`, `PUT .../price-breaks`, `PUT .../terms`) are separate, later-phase
sub-resource endpoints not in the file-creation brief; a manual
quote's lines/price-breaks/terms are written once, atomically, at create (or
`supersede`) time only.

**Supersede reuses the old quote's `rfq_id`/`supplier_id`.**
`QuoteSupersedeRequest` has the same shape as `QuoteCreate` minus
`supplier_id` (and `rfq_id`, already excluded) - a manual revision
(app/models/quotes.py module docstring point 2, 00-decisions.md §4 #16) is a
new quote from the *same* supplier against the *same* RFQ as the one it
replaces, not a way to reassign a quote to a different supplier (that is
simply a new, independent `POST /rfqs/{id}/quotes`).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.quotes import Quote, QuoteLine, QuotePriceBreak, QuoteTerms
from app.schemas.boms import QuantityString
from app.schemas.parts import CurrencyCode, UnitPriceString

CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


# -- price breaks ------------------------------------------------------


class QuotePriceBreakCreate(BaseModel):
    """One all-units pricing tier (00-decisions.md §2 ruling 1). Structural
    validation only lives here (per-field bounds); the cross-tier no-overlap
    matrix - duplicate `min_quantity`, overlapping ranges, an open-ended
    `max_quantity` on a non-highest tier - is a service-layer concern
    (`QuoteService._validate_price_breaks`), since it can only be evaluated
    across a whole line's tiers at once. Price *direction* across tiers is
    deliberately not validated anywhere: real quotes sometimes increase
    per-unit price at a higher volume tier (e.g. a tooling-amortization
    break), so only structure is checked, never monotonicity.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    min_quantity: QuantityString = Field(ge=1)
    max_quantity: QuantityString | None = Field(default=None, ge=1)
    unit_price: UnitPriceString = Field(ge=0)
    setup_fee: QuantityString | None = Field(default=None, ge=0)
    tier_index: int | None = Field(default=None, ge=0, le=32767)

    @model_validator(mode="after")
    def _check_range(self) -> QuotePriceBreakCreate:
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise ValueError("max_quantity must be >= min_quantity")
        return self


class QuotePriceBreakResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    min_quantity: QuantityString
    max_quantity: QuantityString | None
    unit_price: UnitPriceString
    setup_fee: QuantityString | None
    tier_index: int | None

    @classmethod
    def from_model(cls, pb: QuotePriceBreak) -> QuotePriceBreakResponse:
        return cls(
            id=str(pb.id),
            min_quantity=pb.min_quantity,
            max_quantity=pb.max_quantity,
            unit_price=pb.unit_price,
            setup_fee=pb.setup_fee,
            tier_index=pb.tier_index,
        )


# -- lines ---------------------------------------------------------------


class QuoteLineCreate(BaseModel):
    """One quoted line item. See module docstring for why
    `unit_definition_id` is required (unlike RFQ/BOM lines) and why
    `quoted_part_number`/`quoted_mpn`/`other_fixed_cost` are included."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    matched_rfq_line_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    quoted_part_number: str | None = Field(default=None, max_length=100)
    quoted_mpn: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=4000)
    quantity: QuantityString = Field(gt=0)
    unit_definition_id: uuid.UUID
    unit_price: UnitPriceString | None = Field(default=None, ge=0)
    moq: QuantityString | None = Field(default=None, gt=0)
    tooling_cost: QuantityString | None = Field(default=None, ge=0)
    setup_cost: QuantityString | None = Field(default=None, ge=0)
    documentation_cost: QuantityString | None = Field(default=None, ge=0)
    packaging_cost: QuantityString | None = Field(default=None, ge=0)
    shipping_cost: QuantityString | None = Field(default=None, ge=0)
    insurance_cost: QuantityString | None = Field(default=None, ge=0)
    handling_cost: QuantityString | None = Field(default=None, ge=0)
    other_fixed_cost: QuantityString | None = Field(default=None, ge=0)
    tariff_amount: QuantityString | None = Field(default=None, ge=0)
    duty_amount: QuantityString | None = Field(default=None, ge=0)
    customs_fee: QuantityString | None = Field(default=None, ge=0)
    tax_amount: QuantityString | None = Field(default=None, ge=0)
    country_of_origin: CountryCode | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    production_capacity: QuantityString | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=4000)
    price_breaks: list[QuotePriceBreakCreate] = Field(default_factory=list)


class QuoteLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    line_number: int
    matched_rfq_line_id: str | None
    part_id: str | None
    quoted_part_number: str | None
    quoted_mpn: str | None
    description: str | None
    quantity: QuantityString
    unit_definition_id: str
    unit_price: UnitPriceString | None
    moq: QuantityString | None
    tooling_cost: QuantityString | None
    setup_cost: QuantityString | None
    documentation_cost: QuantityString | None
    packaging_cost: QuantityString | None
    shipping_cost: QuantityString | None
    insurance_cost: QuantityString | None
    handling_cost: QuantityString | None
    other_fixed_cost: QuantityString | None
    tariff_amount: QuantityString | None
    duty_amount: QuantityString | None
    customs_fee: QuantityString | None
    tax_amount: QuantityString | None
    country_of_origin: str | None
    lead_time_days: int | None
    production_capacity: QuantityString | None
    match_status: str
    notes: str | None
    price_breaks: list[QuotePriceBreakResponse]

    @classmethod
    def from_model(cls, line: QuoteLine, price_breaks: list[QuotePriceBreak]) -> QuoteLineResponse:
        return cls(
            id=str(line.id),
            line_number=line.line_number,
            matched_rfq_line_id=(
                str(line.matched_rfq_line_id) if line.matched_rfq_line_id is not None else None
            ),
            part_id=(str(line.part_id) if line.part_id is not None else None),
            quoted_part_number=line.quoted_part_number,
            quoted_mpn=line.quoted_mpn,
            description=line.description,
            quantity=line.quantity,
            unit_definition_id=str(line.unit_definition_id),
            unit_price=line.unit_price,
            moq=line.moq,
            tooling_cost=line.tooling_cost,
            setup_cost=line.setup_cost,
            documentation_cost=line.documentation_cost,
            packaging_cost=line.packaging_cost,
            shipping_cost=line.shipping_cost,
            insurance_cost=line.insurance_cost,
            handling_cost=line.handling_cost,
            other_fixed_cost=line.other_fixed_cost,
            tariff_amount=line.tariff_amount,
            duty_amount=line.duty_amount,
            customs_fee=line.customs_fee,
            tax_amount=line.tax_amount,
            country_of_origin=line.country_of_origin,
            lead_time_days=line.lead_time_days,
            production_capacity=line.production_capacity,
            match_status=line.match_status.value,
            notes=line.notes,
            price_breaks=[QuotePriceBreakResponse.from_model(pb) for pb in price_breaks],
        )


# -- terms -----------------------------------------------------------------


class QuoteTermsCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    payment_terms: str | None = Field(default=None, max_length=200)
    payment_terms_days: int | None = Field(default=None, ge=0)
    incoterm: str | None = Field(default=None, max_length=100)
    shipping_terms: str | None = Field(default=None, max_length=1000)
    warranty_terms: str | None = Field(default=None, max_length=1000)
    validity_days: int | None = Field(default=None, ge=0)
    exceptions: str | None = Field(default=None, max_length=1000)
    exclusions: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)


class QuoteTermsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_terms: str | None
    payment_terms_days: int | None
    incoterm: str | None
    shipping_terms: str | None
    warranty_terms: str | None
    validity_days: int | None
    exceptions: str | None
    exclusions: str | None
    notes: str | None

    @classmethod
    def from_model(cls, terms: QuoteTerms) -> QuoteTermsResponse:
        return cls(
            payment_terms=terms.payment_terms,
            payment_terms_days=terms.payment_terms_days,
            incoterm=terms.incoterm,
            shipping_terms=terms.shipping_terms,
            warranty_terms=terms.warranty_terms,
            validity_days=terms.validity_days,
            exceptions=terms.exceptions,
            exclusions=terms.exclusions,
            notes=terms.notes,
        )


# -- quotes ----------------------------------------------------------------


class QuoteCreate(BaseModel):
    """`POST /rfqs/{rfq_id}/quotes` body. See module docstring for why
    `rfq_id` is not a field here (path-sourced)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    supplier_id: uuid.UUID
    quote_number: str | None = Field(default=None, max_length=100)
    quote_date: date
    expiration_date: date | None = None
    currency: CurrencyCode
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[QuoteLineCreate] = Field(min_length=1)
    terms: QuoteTermsCreate | None = None


class QuoteSupersedeRequest(BaseModel):
    """`POST /quotes/{id}/supersede` body - see module docstring: same shape
    as `QuoteCreate` minus `supplier_id` (inherited from the quote being
    superseded)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    quote_number: str | None = Field(default=None, max_length=100)
    quote_date: date
    expiration_date: date | None = None
    currency: CurrencyCode
    notes: str | None = Field(default=None, max_length=4000)
    lines: list[QuoteLineCreate] = Field(min_length=1)
    terms: QuoteTermsCreate | None = None


class QuoteUpdate(BaseModel):
    """Partial update (PATCH): only fields present in the payload are
    applied. Concurrency is carried by the `If-Match` header (§1.7), not the
    body. See module docstring for the deliberately narrow field set (no
    lines/price-breaks/terms - separate, later-phase routes)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    quote_number: str | None = Field(default=None, max_length=100)
    quote_date: date | None = None
    expiration_date: date | None = None
    currency: CurrencyCode | None = None
    notes: str | None = Field(default=None, max_length=4000)


class QuoteArchiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class QuoteResponse(BaseModel):
    """Full detail: one quote with its lines (each carrying its own price
    breaks) and terms. Used by every endpoint that returns a single quote
    (create/get/update/supersede/archive)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    rfq_id: str
    supplier_id: str
    quote_number: str | None
    quote_date: date
    expiration_date: date | None
    currency: str
    status: str
    source: str
    notes: str | None
    superseded_by_id: str | None
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    lines: list[QuoteLineResponse]
    terms: QuoteTermsResponse | None

    @classmethod
    def from_model(
        cls,
        quote: Quote,
        lines: list[QuoteLine],
        price_breaks_by_line: dict[uuid.UUID, list[QuotePriceBreak]],
        terms: QuoteTerms | None,
    ) -> QuoteResponse:
        return cls(
            id=str(quote.id),
            rfq_id=str(quote.rfq_id),
            supplier_id=str(quote.supplier_id),
            quote_number=quote.quote_number,
            quote_date=quote.quote_date,
            expiration_date=quote.expiration_date,
            currency=quote.currency,
            status=quote.status.value,
            source=quote.source.value,
            notes=quote.notes,
            superseded_by_id=(
                str(quote.superseded_by_id) if quote.superseded_by_id is not None else None
            ),
            is_archived=quote.is_archived,
            archived_at=quote.archived_at,
            archive_reason=quote.archive_reason,
            version=quote.version,
            created_at=quote.created_at,
            updated_at=quote.updated_at,
            lines=[
                QuoteLineResponse.from_model(ln, price_breaks_by_line.get(ln.id, []))
                for ln in lines
            ],
            terms=(QuoteTermsResponse.from_model(terms) if terms is not None else None),
        )


class QuoteSummaryResponse(BaseModel):
    """Version-free summary for `GET /rfqs/{rfq_id}/quotes` (list): no
    `lines`/`terms`, to keep list payloads bounded - mirrors
    `RfqSummaryResponse`."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    rfq_id: str
    supplier_id: str
    quote_number: str | None
    quote_date: date
    expiration_date: date | None
    currency: str
    status: str
    source: str
    superseded_by_id: str | None
    is_archived: bool
    version: int
    created_at: datetime
    updated_at: datetime
    line_count: int

    @classmethod
    def from_model(cls, quote: Quote, *, line_count: int) -> QuoteSummaryResponse:
        return cls(
            id=str(quote.id),
            rfq_id=str(quote.rfq_id),
            supplier_id=str(quote.supplier_id),
            quote_number=quote.quote_number,
            quote_date=quote.quote_date,
            expiration_date=quote.expiration_date,
            currency=quote.currency,
            status=quote.status.value,
            source=quote.source.value,
            superseded_by_id=(
                str(quote.superseded_by_id) if quote.superseded_by_id is not None else None
            ),
            is_archived=quote.is_archived,
            version=quote.version,
            created_at=quote.created_at,
            updated_at=quote.updated_at,
            line_count=line_count,
        )


class QuoteListResponse(BaseModel):
    items: list[QuoteSummaryResponse]


__all__ = [
    "CountryCode",
    "QuoteArchiveRequest",
    "QuoteCreate",
    "QuoteLineCreate",
    "QuoteLineResponse",
    "QuoteListResponse",
    "QuotePriceBreakCreate",
    "QuotePriceBreakResponse",
    "QuoteResponse",
    "QuoteSummaryResponse",
    "QuoteSupersedeRequest",
    "QuoteTermsCreate",
    "QuoteTermsResponse",
    "QuoteUpdate",
]
