"""Quote service — org-scoped business logic for manual quote entry
(docs/planning/03-api-contract.md §4.11, app/models/quotes.py module
docstring, docs/SPEC.md §6-7/§11).

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception. Every mutation
writes exactly one audit event in the same transaction as the data change it
describes (`quote.created`/`quote.updated`/`quote.superseded`/
`quote.archived`, this task's brief).

**Scope: manual entry only.** §4.11's route table also lists
`GET/POST/PATCH/DELETE .../lines[...]`, `PUT .../price-breaks`,
`PUT .../terms`, `GET .../corrections`, and `POST .../confirm` — all of them
belong to the extraction/correction/confirmation pipeline (00-decisions.md
§2 ruling #6: "manual quotes before extraction", a later phase) and are out
of this task's file-creation brief. A manually-entered quote's `source` is
always `QuoteSource.MANUAL`, and its lines/price-breaks/terms are written
once, atomically, at `create()` (or `supersede()`) time — there is no
route anywhere in this module that edits a line, a price break, or the terms
block in place after creation.

**RFQ + supplier eligibility gate (`create` and `supersede`).** Per this
task's brief: the target RFQ must resolve in-org (404 otherwise, never 403,
§1.1) and be `open` or `under_review` (else `409 conflict_state` — a
`draft` RFQ has not been sent to anyone yet, and `awarded`/`closed`/
`archived` are all past the point where a new quote makes sense). The named
supplier must exist in-org (404) and have a live invitation on that RFQ:
`RfqSupplier` row present (else `409 conflict_state` — not invited) and not
excluded (`excluded_at IS NULL`, else `409 conflict_state`). `supersede`
re-runs this same gate against the *old* quote's `rfq_id`/`supplier_id`
rather than merely trusting the original quote's already-passed check: an
RFQ can move past `open`/`under_review`, or a supplier can be excluded, in
the time between the original quote and its revision arriving.

**"only draft/received quotes" (task brief) reads as `DRAFT`/`IN_REVIEW`.**
`QuoteStatus` (app/models/quotes.py) is
`draft|in_review|confirmed|superseded|rejected` — there is no `received`
value anywhere in the ERD, the model, or that model's own module docstring
(which carefully enumerates every deliberate field/enum deviation from the
ERD and lists none for status values). Read as the closest matching pair of
pre-confirmation statuses: `update()` is allowed while a quote is `draft` or
`in_review`, blocked (`409 conflict_state`) once `confirmed`, `superseded`,
or `rejected`.

**Price-break validation matrix — this task IS the deferred validator.**
app/models/quotes.py's own module docstring states the gap explicitly: the
DB's `uq_quote_price_breaks_org_line_min_quantity` "does not catch a tier
whose range partially overlaps a different min_quantity... deferred to an
application-level validator in the quote-line/price-break write path, which
is the next task's responsibility." `_validate_price_breaks` is that
validator, run per line before any row is written:
- duplicate `min_quantity` within one line -> `422` (defense in depth ahead
  of the DB's own unique constraint, with a field-scoped error instead of a
  raw `IntegrityError`)
- tiers are sorted by `min_quantity`; for every adjacent pair, the lower
  tier's `max_quantity` must be set AND strictly less than the next tier's
  `min_quantity` (`break[i].max_quantity < break[i+1].min_quantity`)
- an open-ended (`max_quantity IS NULL`) tier is legal only as the single
  highest tier — any lower tier left open-ended is `422`
- `min_quantity >= 1` and `unit_price >= 0` are plain per-field bounds
  already enforced by `QuotePriceBreakCreate` (Pydantic `Field(...)`), not
  repeated here
- price **direction** across tiers is deliberately never validated (this
  task's brief is explicit: "do not enforce price direction" — real quotes
  sometimes increase per-unit price at a higher-volume tier, e.g. a
  tooling-amortization break)

**Supersede is a `POST`, not gated by `If-Match`.** Quotes are listed among
§1.7's ETag/If-Match resources, but api/v1/rfqs.py already establishes the
precedent this module follows: only `PATCH` itself demands the header as
input; POST-style state-mutating actions (there, `POST .../status`; here,
`POST .../supersede`, `POST .../archive`) return a fresh `ETag` but do not
require `If-Match` on the way in. Consistent with that precedent, a
pre-check on the *current* state (old.status != superseded) substitutes for
optimistic-concurrency gating on this route, exactly like `RfqService.
exclude_supplier`/`reinstate_supplier` guard on `excluded_at` rather than a
version header.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import (
    ConflictStateError,
    ConflictVersionError,
    ErrorDetail,
    NotFoundError,
    ValidationAppError,
)
from app.core.ids import IdGenerator
from app.core.money import quantize_money, quantize_qty, quantize_unit_price
from app.models.quotes import (
    Quote,
    QuoteLine,
    QuotePriceBreak,
    QuoteSource,
    QuoteStatus,
    QuoteTerms,
)
from app.models.rfqs import Rfq, RfqStatus
from app.models.units import UnitDefinition
from app.repositories.part_repository import PartRepository
from app.repositories.quote_repository import QuoteRepository
from app.repositories.rfq_repository import RfqRepository
from app.repositories.supplier_repository import SupplierRepository
from app.schemas.quotes import (
    QuoteCreate,
    QuoteLineCreate,
    QuotePriceBreakCreate,
    QuoteSupersedeRequest,
    QuoteTermsCreate,
    QuoteUpdate,
)
from app.services.audit import AuditRecorder

_ELIGIBLE_RFQ_STATUSES = frozenset({RfqStatus.OPEN, RfqStatus.UNDER_REVIEW})
_UPDATABLE_QUOTE_STATUSES = frozenset({QuoteStatus.DRAFT, QuoteStatus.IN_REVIEW})

# Module-level aliases, not inline `list[...]` annotations: see rfq_service.py/
# bom_service.py for why this precaution is taken codebase-wide even where
# (as here) no method happens to be named `list` — kept consistent rather
# than relying on a name that could change under refactor.
_QuoteGraph = tuple[
    Quote, list[QuoteLine], dict[uuid.UUID, list[QuotePriceBreak]], QuoteTerms | None
]
_QuoteList = list[Quote]


def _opt(value: Decimal | None, fn: Callable[[Decimal], Decimal]) -> Decimal | None:
    return fn(value) if value is not None else None


def _quote_state(
    quote: Quote,
    lines: list[QuoteLine],
    breaks_by_line: dict[uuid.UUID, list[QuotePriceBreak]],
    terms: QuoteTerms | None,
) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "rfq_id": str(quote.rfq_id),
        "supplier_id": str(quote.supplier_id),
        "quote_number": quote.quote_number,
        "quote_date": quote.quote_date.isoformat(),
        "expiration_date": (
            quote.expiration_date.isoformat() if quote.expiration_date else None
        ),
        "currency": quote.currency,
        "status": quote.status.value,
        "source": quote.source.value,
        "notes": quote.notes,
        "superseded_by_id": (
            str(quote.superseded_by_id) if quote.superseded_by_id is not None else None
        ),
        "version": quote.version,
        "is_archived": quote.is_archived,
        "archived_at": (quote.archived_at.isoformat() if quote.archived_at else None),
        "archive_reason": quote.archive_reason,
        "lines": [_line_state(ln, breaks_by_line.get(ln.id, [])) for ln in lines],
        "terms": _terms_state(terms),
    }


def _dec(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _line_state(line: QuoteLine, price_breaks: list[QuotePriceBreak]) -> dict[str, Any]:
    return {
        "line_number": line.line_number,
        "matched_rfq_line_id": (
            str(line.matched_rfq_line_id) if line.matched_rfq_line_id is not None else None
        ),
        "part_id": (str(line.part_id) if line.part_id is not None else None),
        "quoted_part_number": line.quoted_part_number,
        "quoted_mpn": line.quoted_mpn,
        "description": line.description,
        "quantity": str(line.quantity),
        "unit_definition_id": str(line.unit_definition_id),
        "unit_price": _dec(line.unit_price),
        "moq": _dec(line.moq),
        "tooling_cost": _dec(line.tooling_cost),
        "setup_cost": _dec(line.setup_cost),
        "documentation_cost": _dec(line.documentation_cost),
        "packaging_cost": _dec(line.packaging_cost),
        "shipping_cost": _dec(line.shipping_cost),
        "insurance_cost": _dec(line.insurance_cost),
        "handling_cost": _dec(line.handling_cost),
        "other_fixed_cost": _dec(line.other_fixed_cost),
        "tariff_amount": _dec(line.tariff_amount),
        "duty_amount": _dec(line.duty_amount),
        "customs_fee": _dec(line.customs_fee),
        "tax_amount": _dec(line.tax_amount),
        "country_of_origin": line.country_of_origin,
        "lead_time_days": line.lead_time_days,
        "production_capacity": _dec(line.production_capacity),
        "match_status": line.match_status.value,
        "notes": line.notes,
        "price_breaks": [
            {
                "min_quantity": str(pb.min_quantity),
                "max_quantity": _dec(pb.max_quantity),
                "unit_price": str(pb.unit_price),
                "setup_fee": _dec(pb.setup_fee),
                "tier_index": pb.tier_index,
            }
            for pb in price_breaks
        ],
    }


def _terms_state(terms: QuoteTerms | None) -> dict[str, Any] | None:
    if terms is None:
        return None
    return {
        "payment_terms": terms.payment_terms,
        "payment_terms_days": terms.payment_terms_days,
        "incoterm": terms.incoterm,
        "shipping_terms": terms.shipping_terms,
        "warranty_terms": terms.warranty_terms,
        "validity_days": terms.validity_days,
        "exceptions": terms.exceptions,
        "exclusions": terms.exclusions,
        "notes": terms.notes,
    }


class QuoteService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._repo = QuoteRepository(session, organization_id)
        self._rfq_repo = RfqRepository(session, organization_id)
        self._supplier_repo = SupplierRepository(session, organization_id)
        self._part_repo = PartRepository(session, organization_id)

    # -- shared validation -------------------------------------------------

    def _validate_unit_definition(self, unit_definition_id: uuid.UUID, *, field: str) -> None:
        unit = self._db.execute(
            select(UnitDefinition).where(UnitDefinition.id == unit_definition_id)
        ).scalar_one_or_none()
        if unit is None:
            raise ValidationAppError(
                "unit_definition_id does not reference an existing unit.",
                details=[ErrorDetail(field=field, issue="does not reference an existing unit")],
            )
        if unit.organization_id is not None and unit.organization_id != self._organization_id:
            raise ValidationAppError(
                "unit_definition_id references another organization's private unit.",
                details=[
                    ErrorDetail(
                        field=field, issue="references another organization's private unit"
                    )
                ],
            )

    def _check_rfq_eligible(self, rfq: Rfq) -> None:
        if rfq.status not in _ELIGIBLE_RFQ_STATUSES:
            raise ConflictStateError(
                f"RFQ is '{rfq.status.value}'; quotes can only be entered while the "
                "RFQ is 'open' or 'under_review'."
            )

    def _check_supplier_eligible(self, rfq_id: uuid.UUID, supplier_id: uuid.UUID) -> None:
        if self._supplier_repo.get(supplier_id) is None:
            raise NotFoundError("Supplier")
        rs = self._rfq_repo.get_supplier_by_supplier_id(rfq_id, supplier_id)
        if rs is None:
            raise ConflictStateError(
                "This supplier has not been invited to this RFQ; invite them before "
                "entering a quote on their behalf."
            )
        if rs.excluded_at is not None:
            raise ConflictStateError("This supplier has been excluded from this RFQ.")

    def _validate_price_breaks(
        self, line_index: int, breaks: list[QuotePriceBreakCreate]
    ) -> None:
        """See module docstring "Price-break validation matrix"."""
        if not breaks:
            return

        seen: dict[Decimal, int] = {}
        for j, b in enumerate(breaks):
            if b.min_quantity in seen:
                raise ValidationAppError(
                    "Duplicate price-break min_quantity within one line.",
                    details=[
                        ErrorDetail(
                            field=f"lines[{line_index}].price_breaks[{j}].min_quantity",
                            issue=f"duplicate of price_breaks[{seen[b.min_quantity]}]",
                        )
                    ],
                )
            seen[b.min_quantity] = j

        order = sorted(range(len(breaks)), key=lambda j: breaks[j].min_quantity)
        for pos in range(len(order) - 1):
            j = order[pos]
            cur = breaks[j]
            nxt = breaks[order[pos + 1]]
            if cur.max_quantity is None:
                raise ValidationAppError(
                    "An open-ended (null) max_quantity is only legal on the highest "
                    "price-break tier.",
                    details=[
                        ErrorDetail(
                            field=f"lines[{line_index}].price_breaks[{j}].max_quantity",
                            issue="null max_quantity is only legal on the highest tier",
                        )
                    ],
                )
            if cur.max_quantity >= nxt.min_quantity:
                raise ValidationAppError(
                    "Price-break tiers must not overlap.",
                    details=[
                        ErrorDetail(
                            field=f"lines[{line_index}].price_breaks[{j}].max_quantity",
                            issue=(
                                "must be less than the next tier's min_quantity "
                                f"({nxt.min_quantity})"
                            ),
                        )
                    ],
                )

    def _build_line(
        self,
        rfq_id: uuid.UUID,
        quote_id: uuid.UUID,
        index: int,
        body: QuoteLineCreate,
        line_number: int,
    ) -> QuoteLine:
        if body.part_id is not None and self._part_repo.get(body.part_id) is None:
            raise NotFoundError("Part")

        if (
            body.matched_rfq_line_id is not None
            and self._rfq_repo.get_line(rfq_id, body.matched_rfq_line_id) is None
        ):
            raise ValidationAppError(
                "matched_rfq_line_id does not reference a line on this RFQ.",
                details=[
                    ErrorDetail(
                        field=f"lines[{index}].matched_rfq_line_id",
                        issue="does not reference a line on this RFQ",
                    )
                ],
            )

        self._validate_unit_definition(
            body.unit_definition_id, field=f"lines[{index}].unit_definition_id"
        )

        return QuoteLine(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            quote_id=quote_id,
            line_number=line_number,
            matched_rfq_line_id=body.matched_rfq_line_id,
            part_id=body.part_id,
            quoted_part_number=body.quoted_part_number,
            quoted_mpn=body.quoted_mpn,
            description=body.description,
            quantity=quantize_qty(body.quantity),
            unit_definition_id=body.unit_definition_id,
            unit_price=_opt(body.unit_price, quantize_unit_price),
            moq=_opt(body.moq, quantize_qty),
            tooling_cost=_opt(body.tooling_cost, quantize_money),
            setup_cost=_opt(body.setup_cost, quantize_money),
            documentation_cost=_opt(body.documentation_cost, quantize_money),
            packaging_cost=_opt(body.packaging_cost, quantize_money),
            shipping_cost=_opt(body.shipping_cost, quantize_money),
            insurance_cost=_opt(body.insurance_cost, quantize_money),
            handling_cost=_opt(body.handling_cost, quantize_money),
            other_fixed_cost=_opt(body.other_fixed_cost, quantize_money),
            tariff_amount=_opt(body.tariff_amount, quantize_money),
            duty_amount=_opt(body.duty_amount, quantize_money),
            customs_fee=_opt(body.customs_fee, quantize_money),
            tax_amount=_opt(body.tax_amount, quantize_money),
            country_of_origin=body.country_of_origin,
            lead_time_days=body.lead_time_days,
            production_capacity=_opt(body.production_capacity, quantize_qty),
            notes=body.notes,
        )

    def _build_price_break(
        self, quote_line_id: uuid.UUID, body: QuotePriceBreakCreate
    ) -> QuotePriceBreak:
        return QuotePriceBreak(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            quote_line_id=quote_line_id,
            min_quantity=quantize_qty(body.min_quantity),
            max_quantity=_opt(body.max_quantity, quantize_qty),
            unit_price=quantize_unit_price(body.unit_price),
            setup_fee=_opt(body.setup_fee, quantize_money),
            tier_index=body.tier_index,
        )

    def _build_terms(self, quote_id: uuid.UUID, body: QuoteTermsCreate) -> QuoteTerms:
        return QuoteTerms(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            quote_id=quote_id,
            payment_terms=body.payment_terms,
            payment_terms_days=body.payment_terms_days,
            incoterm=body.incoterm,
            shipping_terms=body.shipping_terms,
            warranty_terms=body.warranty_terms,
            validity_days=body.validity_days,
            exceptions=body.exceptions,
            exclusions=body.exclusions,
            notes=body.notes,
        )

    def _write_lines_breaks_terms(
        self,
        rfq_id: uuid.UUID,
        quote_id: uuid.UUID,
        line_bodies: list[QuoteLineCreate],
        terms_body: QuoteTermsCreate | None,
    ) -> tuple[list[QuoteLine], dict[uuid.UUID, list[QuotePriceBreak]], QuoteTerms | None]:
        """Shared by `create()` and `supersede()`. Flushes between tiers so
        each composite org FK (quote_lines -> quotes, quote_price_breaks ->
        quote_lines) targets a row that actually exists, not merely one
        pending in the session — same reasoning as RfqService.create's own
        intermediate flush."""
        for i, lb in enumerate(line_bodies):
            self._validate_price_breaks(i, lb.price_breaks)

        lines: list[QuoteLine] = []
        for i, lb in enumerate(line_bodies):
            line = self._build_line(rfq_id, quote_id, i, lb, i + 1)
            self._repo.add_line(line)
            lines.append(line)
        self._db.flush()

        breaks_by_line: dict[uuid.UUID, list[QuotePriceBreak]] = {}
        for line, lb in zip(lines, line_bodies, strict=True):
            created = [self._build_price_break(line.id, pb) for pb in lb.price_breaks]
            for pb in created:
                self._repo.add_price_break(pb)
            breaks_by_line[line.id] = created

        terms: QuoteTerms | None = None
        if terms_body is not None:
            terms = self._build_terms(quote_id, terms_body)
            self._repo.add_terms(terms)

        try:
            self._db.flush()
        except IntegrityError as exc:
            raise ConflictStateError(
                "Could not save this quote's lines/price breaks/terms; the data "
                "conflicted with an existing row (e.g. a duplicate price-break "
                "min_quantity)."
            ) from exc

        return lines, breaks_by_line, terms

    # -- reads ---------------------------------------------------------

    def get(self, quote_id: uuid.UUID) -> _QuoteGraph:
        quote = self._repo.get_or_raise(quote_id)
        lines = self._repo.list_lines(quote.id)
        breaks_by_line = {ln.id: self._repo.list_price_breaks(ln.id) for ln in lines}
        terms = self._repo.get_terms(quote.id)
        return quote, lines, breaks_by_line, terms

    def list_by_rfq(
        self, rfq_id: uuid.UUID, *, include_superseded: bool = False
    ) -> _QuoteList:
        self._rfq_repo.get_or_raise(rfq_id)
        return self._repo.list_by_rfq(rfq_id, include_superseded=include_superseded)

    def line_count(self, quote_id: uuid.UUID) -> int:
        return self._repo.count_lines(quote_id)

    # -- create --------------------------------------------------------

    def create(self, rfq_id: uuid.UUID, body: QuoteCreate) -> _QuoteGraph:
        rfq = self._rfq_repo.get_or_raise(rfq_id)
        self._check_rfq_eligible(rfq)
        self._check_supplier_eligible(rfq_id, body.supplier_id)

        now = self._clock.now()
        quote = Quote(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            rfq_id=rfq_id,
            supplier_id=body.supplier_id,
            quote_number=body.quote_number,
            quote_date=body.quote_date,
            expiration_date=body.expiration_date,
            currency=body.currency,
            status=QuoteStatus.DRAFT,
            source=QuoteSource.MANUAL,
            notes=body.notes,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(quote)
        self._db.flush()

        lines, breaks_by_line, terms = self._write_lines_breaks_terms(
            rfq_id, quote.id, body.lines, body.terms
        )

        self._audit.record(
            "quote.created",
            entity_type="quote",
            entity_id=quote.id,
            after_state=_quote_state(quote, lines, breaks_by_line, terms),
            explanation=f"Quote from supplier {quote.supplier_id} created for RFQ {rfq_id}",
        )
        return quote, lines, breaks_by_line, terms

    # -- update ----------------------------------------------------------

    def update(
        self, quote_id: uuid.UUID, body: QuoteUpdate, *, if_match_version: int
    ) -> _QuoteGraph:
        quote = self._repo.get_or_raise(quote_id)
        if quote.version != if_match_version:
            raise ConflictVersionError()
        if quote.status not in _UPDATABLE_QUOTE_STATUSES:
            raise ConflictStateError(
                f"Quote is '{quote.status.value}'; only 'draft' or 'in_review' quotes "
                "can be edited."
            )

        lines = self._repo.list_lines(quote.id)
        breaks_by_line = {ln.id: self._repo.list_price_breaks(ln.id) for ln in lines}
        terms = self._repo.get_terms(quote.id)
        before = _quote_state(quote, lines, breaks_by_line, terms)

        changes = body.model_dump(exclude_unset=True)
        for field in ("quote_number", "quote_date", "expiration_date", "currency", "notes"):
            if field in changes:
                setattr(quote, field, changes[field])

        quote.version += 1
        quote.updated_at = self._clock.now()
        self._db.flush()

        self._audit.record(
            "quote.updated",
            entity_type="quote",
            entity_id=quote.id,
            before_state=before,
            after_state=_quote_state(quote, lines, breaks_by_line, terms),
            explanation=f"Quote {quote.id} updated",
        )
        return quote, lines, breaks_by_line, terms

    # -- supersede -------------------------------------------------------

    def supersede(self, quote_id: uuid.UUID, body: QuoteSupersedeRequest) -> _QuoteGraph:
        old = self._repo.get_or_raise(quote_id)
        if old.status == QuoteStatus.SUPERSEDED:
            raise ConflictStateError("This quote has already been superseded.")

        rfq = self._rfq_repo.get_or_raise(old.rfq_id)
        self._check_rfq_eligible(rfq)
        self._check_supplier_eligible(old.rfq_id, old.supplier_id)

        old_lines = self._repo.list_lines(old.id)
        old_breaks = {ln.id: self._repo.list_price_breaks(ln.id) for ln in old_lines}
        old_terms = self._repo.get_terms(old.id)
        before = _quote_state(old, old_lines, old_breaks, old_terms)

        now = self._clock.now()
        new_quote = Quote(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            rfq_id=old.rfq_id,
            supplier_id=old.supplier_id,
            quote_number=body.quote_number,
            quote_date=body.quote_date,
            expiration_date=body.expiration_date,
            currency=body.currency,
            status=QuoteStatus.DRAFT,
            source=QuoteSource.MANUAL,
            notes=body.notes,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._repo.add(new_quote)
        self._db.flush()

        new_lines, new_breaks_by_line, new_terms = self._write_lines_breaks_terms(
            old.rfq_id, new_quote.id, body.lines, body.terms
        )

        old.superseded_by_id = new_quote.id
        old.status = QuoteStatus.SUPERSEDED
        old.version += 1
        old.updated_at = now
        self._db.flush()

        # A single audit event, per this task's brief ("audit quote.
        # superseded") — entity_id is the OLD quote (the event name
        # describes what happened *to it*, the same semantics as the event
        # name itself, mirroring how BomService.activate() keys its own
        # two-entity mutation's one audit event to the entity its event name
        # describes). The replacement's full graph is nested inside
        # `after_state` rather than getting a second `quote.created` event of
        # its own, so the new quote's actual entered data is still fully
        # traceable from this one row instead of being orphaned.
        after = _quote_state(old, old_lines, old_breaks, old_terms)
        after["replacement_quote_id"] = str(new_quote.id)
        after["replacement_quote"] = _quote_state(
            new_quote, new_lines, new_breaks_by_line, new_terms
        )
        self._audit.record(
            "quote.superseded",
            entity_type="quote",
            entity_id=old.id,
            before_state=before,
            after_state=after,
            explanation=(
                f"Quote {old.id} superseded by new quote {new_quote.id} "
                f"(supplier {old.supplier_id}, RFQ {old.rfq_id})"
            ),
        )
        return new_quote, new_lines, new_breaks_by_line, new_terms

    # -- archive ---------------------------------------------------------

    def archive(self, quote_id: uuid.UUID, *, reason: str, actor_id: uuid.UUID) -> _QuoteGraph:
        quote = self._repo.get_or_raise(quote_id)
        if quote.is_archived:
            raise ConflictStateError("Quote is already archived.")

        lines = self._repo.list_lines(quote.id)
        breaks_by_line = {ln.id: self._repo.list_price_breaks(ln.id) for ln in lines}
        terms = self._repo.get_terms(quote.id)
        before = _quote_state(quote, lines, breaks_by_line, terms)

        now = self._clock.now()
        quote.archived_at = now
        quote.archived_by_id = actor_id
        quote.archive_reason = reason
        quote.version += 1
        quote.updated_at = now
        self._db.flush()

        self._audit.record(
            "quote.archived",
            entity_type="quote",
            entity_id=quote.id,
            before_state=before,
            after_state=_quote_state(quote, lines, breaks_by_line, terms),
            explanation=f"Quote {quote.id} archived: {reason}",
        )
        return quote, lines, breaks_by_line, terms


__all__ = ["QuoteService"]
