"""Quotes: quotes, quote_lines, quote_price_breaks, quote_terms
(docs/planning/02-erd.md §6).

This is a schema-only phase: only the tables named above land here.
`quote_documents`, `extraction_runs`, `extraction_fields`,
`part_match_candidates`, and `quote_corrections` (also in ERD §6) belong to
the document/extraction pipeline (00-decisions.md §2 ruling #6: "manual
quotes before extraction" - phase P3 vs P4) and are out of scope.

**Deliberate ERD deviations.** As with `rfqs.py`, the delegating task's field
enumeration for this phase is narrower than, and in a few places differently
shaped than, 02-erd.md §6's field boxes. Each is called out here as an
intentional decision, not an oversight:

1. **`Quote` carries no `source_document_id`, `source_extraction_run_id`,
   `revision`, `has_unconfirmed_low_confidence`, `missing_fields`,
   `confirmed_by_id`, `confirmed_at`, or `created_by_id`**, all present on
   `QUOTES` in the ERD box. The delegating task's field list for `Quote` is
   explicit and narrower ("quote_number, quote_date, expiration_date,
   currency ..., status ..., superseded_by_id ..., source ..., notes,
   timestamps, optimistic version, archivable") and is followed verbatim,
   the same judgement already made in `rfqs.py` for `Rfq`/`RfqLine`. The
   extraction-provenance columns and the low-confidence/missing-fields
   tracking are meaningless before the extraction pipeline exists (P4);
   `created_by_id` is absent from the ERD's own `QUOTES` box (unlike
   `Rfq`/`BillOfMaterials`), so its omission here matches the ERD, not just
   the delegating task.
2. **`Quote.superseded_by_id`, not `supersedes_quote_id`.** The ERD's
   `QUOTES.supersedes_quote_id` is *backward*-pointing: the newer (superseding)
   quote row names the older row it replaces. The delegating task explicitly
   asks for `superseded_by_id` - the opposite, *forward*-pointing direction:
   the older (superseded) row names the newer row that replaced it. Taken
   verbatim per the task's explicit spelling of the column name (the same
   kind of verbatim rename `rfqs.py` documents for
   `RfqStatusHistory.actor_user_id`/`note`/`occurred_at`). Manual supersession
   (00-decisions.md §4 "#16 manual supersede for revisions") is expected to
   set the *old* quote's `superseded_by_id` to the new quote's id and move the
   old quote's `status` to `SUPERSEDED` - a service-layer responsibility
   (02-erd.md §8: state transitions are enforced in the service layer, not by
   a DB CHECK), so no DB constraint couples `status` and `superseded_by_id`
   here, mirroring how `rfqs.py` leaves `Rfq.status` transitions unenforced
   at the DB layer too.
3. **`Quote.source`, not `entry_mode`.** The ERD's `QUOTES.entry_mode` is a
   plain `text` column annotated `"extracted|manual"`. The delegating task
   asks for "source enum manual|extracted per ERD" - a genuine Postgres
   `ENUM` rather than free text. Renamed to `source` (the task's own word for
   it) and typed as `quote_source_enum`, consistent with 02-erd.md §1's
   general preference ("Postgres native ENUM types for closed, stable sets
   ... Prefer ENUM") over the specific ERD box's choice of `text` for this
   one column.
4. **`QuoteLine.part_id` is a genuine addition, not in the ERD's
   `QUOTE_LINES` box at all.** The ERD only gives `quote_lines` a match to an
   RFQ line (`matched_rfq_line_id`) plus a separate, deferred
   `part_match_candidates` table for suggested/candidate part matches. The
   delegating task explicitly asks for a second, independent nullable
   composite FK "to ... optional part" on `quote_lines` itself, so a line can
   be linked straight to a catalogued `Part` (e.g. during manual entry, before
   any RFQ-line matching or candidate scoring exists). Added as instructed;
   flagged here because it is additive to, not merely a rename of, the ERD
   shape.
5. **`QuoteLine.production_capacity`, not `capacity_units`.** Renamed per the
   delegating task's explicit instruction, matching SPEC §7's own wording
   ("production capacity") rather than the ERD box's `capacity_units` label.
6. **`QuoteLine` carries no `missing_fields`.** Same reasoning as point 1:
   the ERD's `missing_fields jsonb` bookkeeping pairs with the
   extraction-review workflow (confidence bands, human confirmation) that
   does not exist yet in this phase. `NULL` on the nullable monetary/optional
   columns already carries the "not stated" meaning SPEC §7 requires (see
   point 7) without a second, redundant column.
7. **Money on `quote_lines` is nullable and `>= 0`, never coerced to zero.**
   Per 02-erd.md §6's own explanatory note: "All monetary columns on
   quote_lines are nullable - NULL means the supplier did not state it, and
   is carried through as MISSING, never coerced to zero (SPEC §7)." Applied
   to `unit_price`, `moq`, every per-line fixed-cost/tariff/duty/customs/tax
   column, and `production_capacity`.
8. **`QuoteTerms` carries no tax/tariff/duty/customs amount columns.** The
   delegating task's own `QuoteTerms` bullet lists "taxes/tariffs/duties/
   customs amounts NUMERIC(18,6) where quoted" as if they belonged on
   `quote_terms`. This directly contradicts 02-erd.md §6, which places
   `tariff_amount`, `duty_amount`, `customs_fee`, and `tax_amount`
   unambiguously on `QUOTE_LINES` (they scale with a line's quantity, the
   same reasoning that puts `tooling_cost`/`setup_cost`/etc. there too), and
   gives `QUOTE_TERMS` no amount columns of any kind - only qualitative
   commercial terms (payment/incoterm/shipping/warranty/validity/exceptions/
   exclusions/notes). Resolved in favor of the ERD, per the delegating task's
   own overriding instruction to "FOLLOW THE ERD's placement of every SPEC §7
   commercial field between quote_lines and quote_terms exactly" - the more
   specific, load-bearing instruction wins over the task's own looser
   parenthetical paraphrase of the same field set. This is *not* a case of
   the ERD's placement being "genuinely missing" (the stop condition in the
   delegating task): the ERD is unambiguous here, just different from the
   task author's shorthand description of it.
9. **`QuoteLine.documentation_cost`/`handling_cost`, added later (migration
   0016, 2026-08 product-audit remediation).** Not in this phase's original
   field list, and not in the ERD's `QUOTE_LINES` box either - added because
   `app.domain.landed_cost.contracts` () has always defined
   `FixedCosts.documentation`/`LogisticsCosts.handling` as real inputs to the
   landed-cost formula, but until this migration nothing anywhere in this
   schema could supply either, so `LandedCostService` always passed both as
   hardcoded-missing (see that service's module docstring and
   `docs/METHODOLOGY.md` §7). Same shape as every sibling fixed/logistics-
   cost column here - `NUMERIC(18,6)`, nullable, `>= 0`, missing stays
   missing (point 7 above applies identically).

**Cascades (02-erd.md §11).** The only `ON DELETE CASCADE` pair the ERD
whitelists among these four tables is `quote_lines` → `quote_price_breaks`
("the child cannot exist alone"); every other FK here - including
`quotes` → `quote_lines` and `quotes` → `quote_terms`, despite being just as
aggregate-internal in spirit - is `RESTRICT` by the ERD's explicit "everything
else is RESTRICT" default. Since business rows are never hard-deleted in
this system anyway (§11), this only matters for test teardown/purge paths,
but the letter of the whitelist is followed exactly rather than generalized.

**Price-break no-overlap (02-erd.md §8).** The ERD's preferred mechanism is
`EXCLUDE USING gist (quote_line_id WITH =, numrange(min_quantity,
COALESCE(max_quantity,'infinity'), '[]') WITH &&)`, which requires the
`btree_gist` extension. This project forbids enabling any Postgres extension
beyond what migration 0001 already ships (docs/planning/00-decisions.md §7,
the no-extensions ruling), so `btree_gist` is not available here. Per the
delegating task's explicit instruction, the adaptation is:
`UNIQUE (organization_id, quote_line_id, min_quantity)` (which is also
exactly 02-erd.md §8's `uq_price_break_min`) catches the most common overlap
case - two tiers claiming the same starting quantity - at the database layer.
It does **not** catch a tier whose range partially overlaps a *different*
`min_quantity` (e.g. `[100, 500)` and `[300, 900)`); that check is deferred to
an application-level validator in the quote-line/price-break write path,
which is the next task's responsibility, plus a documented follow-up (in the
spirit of the accepted gaps already tracked in 02-erd.md §12) for a periodic
integrity sweep until it lands. This gap is stated here explicitly rather
than silently accepted.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SaEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    CURRENCY_CHAR,
    ArchivableMixin,
    OrgOwnedBase,
    TimestampedMixin,
    VersionedMixin,
    org_identity_constraint,
)


class QuoteStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


QUOTE_STATUS_ENUM = SaEnum(
    QuoteStatus,
    name="quote_status_enum",
    values_callable=lambda e: [m.value for m in e],
)


class QuoteSource(StrEnum):
    """Module docstring point 3: renamed from the ERD's `entry_mode` text
    column and promoted to a real Postgres ENUM."""

    MANUAL = "manual"
    EXTRACTED = "extracted"


QUOTE_SOURCE_ENUM = SaEnum(
    QuoteSource,
    name="quote_source_enum",
    values_callable=lambda e: [m.value for m in e],
)


class QuoteLineMatchStatus(StrEnum):
    UNMATCHED = "unmatched"
    AUTO = "auto"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


MATCH_STATUS_ENUM = SaEnum(
    QuoteLineMatchStatus,
    name="match_status_enum",
    values_callable=lambda e: [m.value for m in e],
)


class Quote(TimestampedMixin, VersionedMixin, ArchivableMixin, OrgOwnedBase):
    """One supplier's quote against one RFQ. See module docstring for the
    narrower-than-ERD field set (extraction provenance is a later phase) and
    the `superseded_by_id` direction/`source` rename decisions.
    """

    __tablename__ = "quotes"
    __table_args__ = (
        org_identity_constraint("quotes"),
        # composite org FK: the RFQ this quote responds to.
        ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_quotes_organization_id_rfq_id",
        ),
        # composite org FK: the supplier who issued this quote.
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_quotes_organization_id_supplier_id",
        ),
        # composite org FK, self-referential, nullable: the quote that
        # replaced this one via manual revision supersession (module
        # docstring point 2). NULL is exempt from the constraint (Postgres
        # default MATCH SIMPLE), same as BillOfMaterials.previous_version_id.
        ForeignKeyConstraint(
            ["organization_id", "superseded_by_id"],
            ["quotes.organization_id", "quotes.id"],
            ondelete="RESTRICT",
            name="fk_quotes_organization_id_superseded_by_id",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_quotes_currency_iso"),
        CheckConstraint(
            "superseded_by_id IS NULL OR superseded_by_id <> id",
            name="ck_quotes_superseded_by_not_self",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    rfq_id: Mapped[uuid.UUID] = mapped_column()
    # part of composite FK #2 above, not a single-column ForeignKey
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    quote_number: Mapped[str | None] = mapped_column(Text(), default=None)
    quote_date: Mapped[date] = mapped_column(Date())
    expiration_date: Mapped[date | None] = mapped_column(Date(), default=None)
    currency: Mapped[str] = mapped_column(CURRENCY_CHAR)
    status: Mapped[QuoteStatus] = mapped_column(QUOTE_STATUS_ENUM, default=QuoteStatus.DRAFT)
    # module docstring point 3
    source: Mapped[QuoteSource] = mapped_column(QUOTE_SOURCE_ENUM)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)
    # part of composite FK #3 above, not a single-column ForeignKey; module
    # docstring point 2 - forward-pointing (old quote -> replacement quote)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(default=None)


class QuoteLine(OrgOwnedBase):
    """One quoted line item of one quote: a part/spec, a quantity, a unit
    price, and every per-line commercial term SPEC §7 lists that the ERD
    places on `quote_lines` rather than `quote_terms` (module docstring
    points 4-8)."""

    __tablename__ = "quote_lines"
    __table_args__ = (
        org_identity_constraint("quote_lines"),
        # composite org FK: the quote this line belongs to.
        ForeignKeyConstraint(
            ["organization_id", "quote_id"],
            ["quotes.organization_id", "quotes.id"],
            ondelete="RESTRICT",
            name="fk_quote_lines_organization_id_quote_id",
        ),
        # composite org FK, nullable: the RFQ line this quote line has been
        # matched to (ERD's matched_rfq_line_id). NULL (unmatched) is exempt
        # from the constraint (MATCH SIMPLE), same pattern as
        # Rfq.source_bom_id.
        ForeignKeyConstraint(
            ["organization_id", "matched_rfq_line_id"],
            ["rfq_lines.organization_id", "rfq_lines.id"],
            ondelete="RESTRICT",
            name="fk_quote_lines_organization_id_matched_rfq_line_id",
        ),
        # composite org FK, nullable: module docstring point 4 - an addition
        # beyond the ERD's QUOTE_LINES box, per the delegating task.
        ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_quote_lines_organization_id_part_id",
        ),
        UniqueConstraint(
            "organization_id",
            "quote_id",
            "line_number",
            name="uq_quote_lines_org_quote_line_number",
        ),
        # 02-erd.md §8 ck_quote_lines_qty_pos.
        CheckConstraint("quantity > 0", name="ck_quote_lines_quantity_pos"),
        # 02-erd.md §8 ck_unit_price_nonneg ("0 is legal: free sample line"),
        # nullable per module docstring point 7.
        CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0", name="ck_quote_lines_unit_price_nonneg"
        ),
        # a *stated* MOQ of zero is meaningless (equivalent to "no minimum",
        # which is exactly what NULL already means) - same judgement as
        # production_capacity below.
        CheckConstraint("moq IS NULL OR moq > 0", name="ck_quote_lines_moq_pos"),
        CheckConstraint(
            "production_capacity IS NULL OR production_capacity > 0",
            name="ck_quote_lines_production_capacity_pos",
        ),
        CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0",
            name="ck_quote_lines_lead_time_days_nonneg",
        ),
        # 02-erd.md §8 ck_*_amount_nonneg, one per per-line fixed-cost/
        # tariff/duty/customs/tax column (module docstring point 7).
        CheckConstraint(
            "tooling_cost IS NULL OR tooling_cost >= 0",
            name="ck_quote_lines_tooling_cost_nonneg",
        ),
        CheckConstraint(
            "setup_cost IS NULL OR setup_cost >= 0", name="ck_quote_lines_setup_cost_nonneg"
        ),
        CheckConstraint(
            "packaging_cost IS NULL OR packaging_cost >= 0",
            name="ck_quote_lines_packaging_cost_nonneg",
        ),
        CheckConstraint(
            "shipping_cost IS NULL OR shipping_cost >= 0",
            name="ck_quote_lines_shipping_cost_nonneg",
        ),
        CheckConstraint(
            "insurance_cost IS NULL OR insurance_cost >= 0",
            name="ck_quote_lines_insurance_cost_nonneg",
        ),
        CheckConstraint(
            "other_fixed_cost IS NULL OR other_fixed_cost >= 0",
            name="ck_quote_lines_other_fixed_cost_nonneg",
        ),
        # migration 0016 (2026-08 product-audit remediation): documentation/
        # handling costs, the two `app.domain.landed_cost.contracts` fields
        # (`FixedCosts.documentation`/`LogisticsCosts.handling`) that
        # previously had no source column anywhere in this schema - see
        # module docstring point 9 above.
        CheckConstraint(
            "documentation_cost IS NULL OR documentation_cost >= 0",
            name="ck_quote_lines_documentation_cost_nonneg",
        ),
        CheckConstraint(
            "handling_cost IS NULL OR handling_cost >= 0",
            name="ck_quote_lines_handling_cost_nonneg",
        ),
        CheckConstraint(
            "tariff_amount IS NULL OR tariff_amount >= 0",
            name="ck_quote_lines_tariff_amount_nonneg",
        ),
        CheckConstraint(
            "duty_amount IS NULL OR duty_amount >= 0", name="ck_quote_lines_duty_amount_nonneg"
        ),
        CheckConstraint(
            "customs_fee IS NULL OR customs_fee >= 0", name="ck_quote_lines_customs_fee_nonneg"
        ),
        CheckConstraint(
            "tax_amount IS NULL OR tax_amount >= 0", name="ck_quote_lines_tax_amount_nonneg"
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    quote_id: Mapped[uuid.UUID] = mapped_column()
    line_number: Mapped[int] = mapped_column(Integer())
    quoted_part_number: Mapped[str | None] = mapped_column(Text(), default=None)
    quoted_mpn: Mapped[str | None] = mapped_column(Text(), default=None)
    description: Mapped[str | None] = mapped_column(Text(), default=None)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    # plain FK, not composite - unit_definitions.organization_id is nullable
    # (global catalogue), same reasoning as Part.unit_definition_id (parts.py).
    unit_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    raw_unit_of_measure: Mapped[str | None] = mapped_column(Text(), default=None)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    moq: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    tooling_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    setup_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    packaging_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    insurance_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    other_fixed_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    # migration 0016: module docstring point 9.
    documentation_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    handling_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    tariff_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    duty_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    customs_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    country_of_origin: Mapped[str | None] = mapped_column(CHAR(2), default=None)
    lead_time_days: Mapped[int | None] = mapped_column(Integer(), default=None)
    # module docstring point 5: renamed from ERD's capacity_units
    production_capacity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    # part of composite FK #2 above, not a single-column ForeignKey
    matched_rfq_line_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    match_status: Mapped[QuoteLineMatchStatus] = mapped_column(
        MATCH_STATUS_ENUM, default=QuoteLineMatchStatus.UNMATCHED
    )
    # part of composite FK #3 above, not a single-column ForeignKey; module
    # docstring point 4
    part_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)


class QuotePriceBreak(OrgOwnedBase):
    """One all-units pricing tier of one quote line (00-decisions.md §2
    ruling 1: all-units, not incremental, breaks). See module docstring for
    the no-overlap constraint adaptation (`btree_gist` is forbidden in this
    project)."""

    __tablename__ = "quote_price_breaks"
    __table_args__ = (
        org_identity_constraint("quote_price_breaks"),
        # composite org FK: the quote line this tier belongs to. CASCADE per
        # 02-erd.md §11's explicit whitelist (module docstring "Cascades"):
        # a price break cannot exist without its line.
        ForeignKeyConstraint(
            ["organization_id", "quote_line_id"],
            ["quote_lines.organization_id", "quote_lines.id"],
            ondelete="CASCADE",
            name="fk_quote_price_breaks_organization_id_quote_line_id",
        ),
        # 02-erd.md §8 ck_price_break_range, verbatim.
        CheckConstraint(
            "min_quantity > 0 AND (max_quantity IS NULL OR max_quantity >= min_quantity)",
            name="ck_quote_price_breaks_range",
        ),
        CheckConstraint("unit_price >= 0", name="ck_quote_price_breaks_unit_price_nonneg"),
        CheckConstraint(
            "setup_fee IS NULL OR setup_fee >= 0", name="ck_quote_price_breaks_setup_fee_nonneg"
        ),
        # 02-erd.md §8 uq_price_break_min, scoped to organization_id like
        # every other unique constraint in this schema. Module docstring:
        # this is also the adaptation standing in for the forbidden
        # btree_gist EXCLUDE no-overlap constraint - it catches same-
        # min_quantity duplicates at the DB layer; a service-layer validator
        # (next task) is responsible for true partial-range overlaps.
        UniqueConstraint(
            "organization_id",
            "quote_line_id",
            "min_quantity",
            name="uq_quote_price_breaks_org_line_min_quantity",
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    quote_line_id: Mapped[uuid.UUID] = mapped_column()
    min_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    max_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    setup_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), default=None)
    tier_index: Mapped[int | None] = mapped_column(SmallInteger(), default=None)


class QuoteTerms(OrgOwnedBase):
    """The one-per-quote block of qualitative commercial terms (payment,
    shipping/incoterm, warranty, validity, exceptions/exclusions). See module
    docstring point 8 for why no tax/tariff/duty/customs amount columns live
    here despite the delegating task's own bullet suggesting otherwise - the
    ERD places those on `quote_lines`, and this schema follows the ERD.

    Missing-field semantics: every column here is nullable, and `NULL` means
    *the supplier did not state this term* - it is carried through as
    explicitly missing and must never be silently coerced to a default or to
    zero (SPEC §7).
    """

    __tablename__ = "quote_terms"
    __table_args__ = (
        org_identity_constraint("quote_terms"),
        # composite org FK: the quote these terms belong to. RESTRICT, not
        # CASCADE - see module docstring "Cascades": the ERD's cascade
        # whitelist does not include quotes -> quote_terms.
        ForeignKeyConstraint(
            ["organization_id", "quote_id"],
            ["quotes.organization_id", "quotes.id"],
            ondelete="RESTRICT",
            name="fk_quote_terms_organization_id_quote_id",
        ),
        # one-to-one: 02-erd.md §6 "QUOTES ||--o| QUOTE_TERMS : has".
        UniqueConstraint("organization_id", "quote_id", name="uq_quote_terms_org_quote"),
        CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days >= 0",
            name="ck_quote_terms_payment_terms_days_nonneg",
        ),
        CheckConstraint(
            "validity_days IS NULL OR validity_days >= 0",
            name="ck_quote_terms_validity_days_nonneg",
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    quote_id: Mapped[uuid.UUID] = mapped_column()
    payment_terms: Mapped[str | None] = mapped_column(Text(), default=None)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer(), default=None)
    incoterm: Mapped[str | None] = mapped_column(Text(), default=None)
    shipping_terms: Mapped[str | None] = mapped_column(Text(), default=None)
    warranty_terms: Mapped[str | None] = mapped_column(Text(), default=None)
    validity_days: Mapped[int | None] = mapped_column(Integer(), default=None)
    exceptions: Mapped[str | None] = mapped_column(Text(), default=None)
    exclusions: Mapped[str | None] = mapped_column(Text(), default=None)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in several FKs on most mappers here (the plain org FK plus one
# or more composite FKs each), so `foreign_keys` disambiguates which
# constraint each relationship follows, and `overlaps` silences SQLAlchemy's
# warning about the shared organization_id column between them - the same
# pattern as rfqs.py/boms.py.
Quote.organization = relationship(
    "Organization", foreign_keys=[Quote.organization_id], lazy="select"
)
Quote.rfq = relationship(
    "Rfq",
    foreign_keys=[Quote.organization_id, Quote.rfq_id],
    lazy="select",
    overlaps="organization",
)
Quote.supplier = relationship(
    "Supplier",
    foreign_keys=[Quote.organization_id, Quote.supplier_id],
    lazy="select",
    overlaps="organization,rfq",
)
Quote.superseded_by = relationship(
    "Quote",
    foreign_keys=[Quote.organization_id, Quote.superseded_by_id],
    remote_side=[Quote.organization_id, Quote.id],
    lazy="select",
    overlaps="organization,rfq,supplier",
)

QuoteLine.organization = relationship(
    "Organization", foreign_keys=[QuoteLine.organization_id], lazy="select"
)
QuoteLine.quote = relationship(
    "Quote",
    foreign_keys=[QuoteLine.organization_id, QuoteLine.quote_id],
    lazy="select",
    overlaps="organization",
)
QuoteLine.matched_rfq_line = relationship(
    "RfqLine",
    foreign_keys=[QuoteLine.organization_id, QuoteLine.matched_rfq_line_id],
    lazy="select",
    overlaps="organization,quote",
)
QuoteLine.part = relationship(
    "Part",
    foreign_keys=[QuoteLine.organization_id, QuoteLine.part_id],
    lazy="select",
    overlaps="organization,quote,matched_rfq_line",
)
QuoteLine.unit_definition = relationship(
    "UnitDefinition", foreign_keys=[QuoteLine.unit_definition_id], lazy="select"
)

QuotePriceBreak.organization = relationship(
    "Organization", foreign_keys=[QuotePriceBreak.organization_id], lazy="select"
)
QuotePriceBreak.quote_line = relationship(
    "QuoteLine",
    foreign_keys=[QuotePriceBreak.organization_id, QuotePriceBreak.quote_line_id],
    lazy="select",
    overlaps="organization",
)

QuoteTerms.organization = relationship(
    "Organization", foreign_keys=[QuoteTerms.organization_id], lazy="select"
)
QuoteTerms.quote = relationship(
    "Quote",
    foreign_keys=[QuoteTerms.organization_id, QuoteTerms.quote_id],
    lazy="select",
    overlaps="organization",
)

__all__ = [
    "MATCH_STATUS_ENUM",
    "QUOTE_SOURCE_ENUM",
    "QUOTE_STATUS_ENUM",
    "Quote",
    "QuoteLine",
    "QuoteLineMatchStatus",
    "QuotePriceBreak",
    "QuoteSource",
    "QuoteStatus",
    "QuoteTerms",
]
