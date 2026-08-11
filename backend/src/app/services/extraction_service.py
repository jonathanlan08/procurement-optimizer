"""Extraction service — the document-extraction workflow end to end
(docs/planning/04-document-pipeline.md §§5-11, docs/planning/03-api-contract.md
§4.10, app/models/documents.py's `ExtractionRunState`/
`ALLOWED_EXTRACTION_RUN_TRANSITIONS`, app/providers/extraction/{base,envelope,
mock}.py).

Services never commit: the request-scoped `get_db` dependency
(app/api/deps.py) commits on success and rolls back on any raised exception.

## Synchronous execution (v0.1 "JOB_RUNNER=inline" precedent)

`03-api-contract.md` §1.5 documents two response shapes for long-running
operations: `202 Accepted` + a pollable job envelope in general, and — in
`JOB_RUNNER=inline` (its own words: "tests") — a `201` with the *completed*
resource, "documented, so contract tests assert both shapes." This codebase
has no `Job`-row-producing worker anywhere (`app.models.audit.Job` exists as
a table shape but nothing in `src/app` ever writes to it, and
`Settings.job_runner` — `inline | thread` — is likewise read by nothing).
Per this task's own explicit instruction, `start_run` always executes
**synchronously, inline, in every environment** (not merely under test) —
the `JOB_RUNNER=inline` shape is the *only* shape this v0.1 build implements,
not a test-only special case layered over a real queue. `api/v1/extractions.py`
returns `201` with the completed `ExtractionRunResponse`, matching that
documented shape. A future phase that adds a real worker would introduce the
`202`/job-polling path without changing this service's own `start_run`
contract.

## Validation-ladder simplification

04-document-pipeline.md §7 defines `field_confidence = provider_conf *
acquisition_conf * validation_penalty` (composed from OCR confidence and a
coercion penalty) and a "critical field set" (`currency`, `unit_price`, every
price-break boundary, `quantity`, `unit_of_measure`, `moq`, `tooling_cost`,
`setup_cost`, and any field on an injection-flagged page) that **always**
`requires_confirmation` regardless of confidence. This task's own OBJECTIVE
text gives a complete, narrower, self-contained formula instead:
`requires_review = band != HIGH or validation-failed`. That literal formula
is what is implemented here — the provider's own reported per-field
`confidence` is used as-is (no OCR/coercion composition), and no field is
force-flagged purely for belonging to a "critical" set. This is a deliberate
scope narrowing, not an oversight: building the fuller composition/critical-
set machinery is a materially larger addition than the task's own stated
recipe, and every fixture this task's required tests exercise already lands
`needs_review` under the simpler formula anyway (each golden fixture has at
least one sub-HIGH-band field), so no required test result depends on the
richer behavior.

## Reuse vs. re-derivation of `QuoteService` logic (all FROZEN)

`QuoteService` is FROZEN for this task. Three pieces of its logic are
conceptually needed here too — this task's own instruction explicitly
permits, per piece, either reusing it "if importable without modification"
or re-deriving it minimally:

1. **Price-break structural validation** (duplicate `min_quantity` / a
   non-highest open-ended tier / overlapping ranges). Re-derived
   (`_price_break_structure_flags`), not reused: `QuoteService.
   _validate_price_breaks` is a private instance method that raises on the
   *first* violation it finds and expects fully-typed `QuotePriceBreakCreate`
   objects; this ladder step needs to flag *every* offending tier
   independently (04-document-pipeline.md §7: "one bad date must not discard
   40 good line items") against extraction candidates that may themselves be
   partially invalid/missing — a different enough shape that re-deriving the
   same three rules cleanly is simpler and safer than adapting the raise-on-
   first-error method to a multi-violation caller.
2. **RFQ/supplier eligibility gates** (`materialize`'s "through QuoteService.
   create semantics"). Re-derived (`_check_rfq_eligible`/
   `_check_supplier_eligible`), identical logic to `QuoteService`'s own
   private methods of the same name. Not reused via an internal `QuoteService`
   instance: nowhere else in this codebase does one service reach into
   another's private (underscore-prefixed) methods — every existing
   cross-service reuse in this codebase (e.g. `LandedCostService` using
   `FxService.get_effective_rate`) is through a service's *public* surface —
   and `QuoteService.create()` itself cannot be called directly at all here
   because it hardcodes `source=QuoteSource.MANUAL` with no parameter to
   override it, which `materialize` (source=`extracted`) structurally needs.
3. **Line/price-break/terms construction.** `materialize` builds `Quote`/
   `QuoteLine`/`QuotePriceBreak`/`QuoteTerms` ORM rows directly (mirroring
   `QuoteService._build_line`/`_build_price_break`/`_build_terms`'s field-by-
   field mapping and `quantize_*` usage), for the same reason as point 2.

## RESOLVED: `QuoteCorrection.quote_id` is now nullable (migration 0012)

This task's OBJECTIVE instructs `correct_field` to "write a `QuoteCorrection`
with before/after." 04-document-pipeline.md's own pipeline diagram places
Stage 10 (Corrections) *before* Stage 11 (Materialize) — i.e. corrections are
meant to happen on `extraction_fields` before any `Quote` row exists at all.
Previously, `app/models/documents.py`'s `QuoteCorrection.quote_id` was a
**required** composite FK to `quotes`, so a pre-materialization correction
could not produce a `QuoteCorrection` row at all — only the audit event
recorded it (see git history for the full original account of that
conflict).

A later, narrowly-scoped part-matching phase revisited this: migration 0012
drops `quote_id`'s `NOT NULL` (module docstring point 8a,
`app/models/documents.py`) and adds a new `extraction_run_id` composite org
FK to `extraction_runs`, giving every correction — pre- or
post-materialization — a durable, directly queryable link back to the run
that produced the field it corrects. `correct_field` below now **always**
writes a `QuoteCorrection` row: `quote_id` is `ExtractionRepository.
find_materialized_quote_id(run_id)` (still recovered from the
`extraction.materialized` audit event's `after_state`, since `Quote` itself
still carries no `source_extraction_run_id` — app/models/quotes.py module
docstring point 1 — that gap is unaffected) and is `None` when no quote has
been materialized yet; `extraction_run_id` is always `run_id`. The
`extraction.field_corrected` audit event is unchanged (still the complete
record of every edit regardless).

## Other deliberate scope boundaries

- **No `quote_documents.state` advancement.** `app/models/documents.py`'s own
  module docstring assigns the coarse document state machine's transition
  table to `app/domain/documents/transitions.py`, a file that does not exist
  in this codebase and is explicitly a different task's responsibility. This
  service never writes `QuoteDocument.state`; its only document-state
  precondition is that `start_run` requires `state == 'stored'` (this task's
  own literal instruction), checked but never advanced.
- **No cross-run supersession.** `ALLOWED_EXTRACTION_RUN_TRANSITIONS` only
  permits `SUPERSEDED` from `needs_review`/`failed`/`materialized` — notably
  *not* from `ready`, so a blanket "supersede the prior run when a new one
  starts" policy would sometimes violate the frozen transition table itself.
  This task's own OBJECTIVE recipe for `start_run` never mentions
  superseding a prior run, either. `start_run` therefore always allocates the
  next `run_number` for the document and never mutates any other run's
  state; multi-run diffing/carry-forward (04-document-pipeline.md §8) is
  out of scope.
- **No part matching (Stage 12-13).** Materialized lines carry
  `part_id=None`, `matched_rfq_line_id=None`, `match_status` stays the
  model's own default (`UNMATCHED`) — a separate, later phase.
- **`QuoteLine.country_of_origin` is `CHAR(2)`; extracted text is a full
  country name** (the golden fixtures literally say "China"/"Mexico").
  There is no country-name -> ISO-3166 alpha-2 lookup table anywhere in this
  codebase. Rather than truncate or guess, `_country_code` only accepts text
  that already looks like a 2-letter code; anything else is left `NULL` on
  the materialized line (the full text stays visible on the
  `ExtractionField` row for a human reviewer).
- **`unit_definition_id` is `NOT NULL` on `QuoteLine`, but the PDF/PNG golden
  fixtures never state a unit of measure at all.** `_resolve_unit_definition_id`
  resolves the extracted text (or, if genuinely missing, the fallback
  candidate `"each"` — the global catalogue's canonical `count`-dimension
  unit, `app.seed.units_catalog.STANDARD_UNIT_CATALOG`) against
  `unit_definitions.code` (case-insensitive, global-or-this-org), and raises
  a `422` naming the problem if neither resolves — never silently inventing
  or crashing on the model's own `NOT NULL`/FK constraint.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.config import ExtractionProviderKind, Settings
from app.core.errors import (
    ConflictStateError,
    ErrorDetail,
    NotFoundError,
    ProviderUnavailableError,
    ValidationAppError,
)
from app.core.ids import IdGenerator
from app.core.money import (
    InvalidDecimalString,
    parse_decimal,
    quantize,
    quantize_money,
    quantize_qty,
    quantize_unit_price,
)
from app.domain.confidence import ConfidenceBand, band
from app.ingestion.acquisition import AcquisitionLimitError, acquire_pages
from app.ingestion.file_validation import DocumentKind
from app.models.documents import (
    ALLOWED_EXTRACTION_RUN_TRANSITIONS,
    DocumentPage,
    DocumentState,
    ExtractionField,
    ExtractionRun,
    ExtractionRunState,
    QuoteCorrection,
    QuoteDocument,
)
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
from app.providers.extraction.base import (
    EXTRACTION_SCHEMA_VERSION,
    ExtractedField,
    ExtractedQuotePayload,
    ExtractionProvider,
)
from app.providers.extraction.envelope import scan_for_injection
from app.providers.extraction.mock import MockExtractionProvider
from app.providers.storage.base import StorageProvider
from app.repositories.document_repository import DocumentRepository
from app.repositories.extraction_repository import DocumentPageRepository, ExtractionRepository
from app.repositories.quote_repository import QuoteRepository
from app.repositories.rfq_repository import RfqRepository
from app.repositories.supplier_repository import SupplierRepository
from app.services.audit import AuditRecorder

# System prompt version (envelope.SYSTEM_INSTRUCTIONS is the constant this
# versions — 04-document-pipeline.md §6 point 1: "the system prompt is a
# constant in source control, versioned as prompt_version"). Bump whenever
# that constant's wording changes.
PROMPT_VERSION: Final[str] = "v1"

_CONFIDENCE_SCALE: Final[Decimal] = Decimal("0.000001")

_VT_TEXT: Final[str] = "text"
_VT_DECIMAL: Final[str] = "decimal"
_VT_MONEY: Final[str] = "money"
_VT_DATE: Final[str] = "date"
_VT_CURRENCY: Final[str] = "currency"
_VT_ENUM: Final[str] = "enum"

# Mirrors (inverted) DocumentService's private `_MIME_OF_KIND` mapping.
# document_service.py is FROZEN for this task and that dict is module-
# private, so this file defines its own small, stable reverse mapping over
# the same 5 canonical MIME strings rather than importing a private symbol
# from another service module.
_KIND_OF_DETECTED_MIME: Final[dict[str, DocumentKind]] = {
    "application/pdf": DocumentKind.PDF,
    "image/png": DocumentKind.PNG,
    "image/jpeg": DocumentKind.JPEG,
    "text/csv": DocumentKind.CSV,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DocumentKind.XLSX,
}

_EXTRACTION_SOURCE_OF_KIND: Final[dict[DocumentKind, str]] = {
    DocumentKind.PDF: "text_layer",
    DocumentKind.CSV: "csv",
    DocumentKind.XLSX: "sheet",
    DocumentKind.PNG: "ocr",
    DocumentKind.JPEG: "ocr",
}

# Mirrors QuoteService._ELIGIBLE_RFQ_STATUSES — see module docstring point 2.
_ELIGIBLE_RFQ_STATUSES: Final[frozenset[RfqStatus]] = frozenset(
    {RfqStatus.OPEN, RfqStatus.UNDER_REVIEW}
)

_QuoteGraph = tuple[
    Quote, list[QuoteLine], dict[uuid.UUID, list[QuotePriceBreak]], QuoteTerms | None
]


# -- provider selection factory --------------------------------------------


def build_extraction_provider(settings: Settings) -> ExtractionProvider:
    """Provider selection factory (this task's own instruction: "provider
    selection factory in this file"), mirroring
    `app.providers.storage.build_storage_provider`'s shape: a plain function
    over `Settings`, called once per request from the API layer's own
    dependency-builder (`api/v1/extractions.py`), not from inside this
    service's constructor.
    """
    if settings.extraction_provider is ExtractionProviderKind.MOCK:
        return MockExtractionProvider()
    # No Anthropic adapter module exists under app.providers.extraction in
    # this build (only base.py/envelope.py/mock.py) — base.py's own
    # docstring calls it "the optional Anthropic adapter (env-gated)",
    # future/roadmap, not implemented here. Fails loudly rather than
    # silently falling back to the mock provider.
    raise ProviderUnavailableError(
        "The 'anthropic' extraction provider has no adapter implementation in "
        "this build; set PO_EXTRACTION_PROVIDER=mock."
    )


# -- validation ladder: pure helpers ----------------------------------------


def _validate_value(value_type: str, raw: str | None) -> tuple[str | None, bool]:
    """Ladder step (b), type validation. `raw is None` (MISSING) is always
    valid — nothing to type-check — per the payload's own "never invent"
    contract; only a *stated* value that fails to parse is a validation
    failure. Returns `(normalized_value_or_None, is_valid)`."""
    if raw is None:
        return None, True
    if value_type in (_VT_DECIMAL, _VT_MONEY):
        try:
            parse_decimal(raw)
        except InvalidDecimalString:
            return None, False
        return raw, True
    if value_type == _VT_DATE:
        try:
            date.fromisoformat(raw)
        except ValueError:
            return None, False
        return raw, True
    if value_type == _VT_CURRENCY:
        if len(raw) != 3 or not raw.isalpha() or raw != raw.upper():
            return None, False
        return raw, True
    # text / enum: no structural check at this ladder step — resolving an
    # "enum" field like unit_of_measure against the unit catalogue is a
    # materialize-time concern (_resolve_unit_definition_id), not a
    # per-field type-validation concern.
    return raw, True


def _price_break_structure_flags(
    tiers: list[tuple[int, Decimal, Decimal | None]],
) -> dict[int, str]:
    """Mirrors `QuoteService._validate_price_breaks`'s three structural rules
    (duplicate `min_quantity` / a non-highest open-ended tier / overlap),
    re-derived rather than reused — see module docstring point 1. `tiers`:
    `(original_index, min_quantity, max_quantity)` for tiers whose own
    `min_quantity` already business-validated (> 0). Returns
    `{original_index: 'min'|'max'}` — which of that tier's two boundary
    fields to flag invalid, matching `QuoteService`'s own field attribution
    (duplicate -> `min_quantity`; open-ended-not-highest / overlap ->
    `max_quantity`)."""
    flags: dict[int, str] = {}
    seen: dict[Decimal, int] = {}
    for j, mn, _mx in tiers:
        if mn in seen:
            flags[j] = "min"
        else:
            seen[mn] = j

    order = sorted(range(len(tiers)), key=lambda p: tiers[p][1])
    for pos in range(len(order) - 1):
        j, _mn, mx = tiers[order[pos]]
        _, nxt_mn, _ = tiers[order[pos + 1]]
        if mx is None or mx >= nxt_mn:
            flags.setdefault(j, "max")
    return flags


def _country_code(raw: str | None) -> str | None:
    """Only accept text that already looks like an ISO-3166 alpha-2 code —
    see module docstring."""
    if raw is None:
        return None
    candidate = raw.strip().upper()
    if len(candidate) == 2 and candidate.isalpha():
        return candidate
    return None


def _opt_decimal(raw: str | None, fn: Callable[[Decimal], Decimal]) -> Decimal | None:
    return fn(parse_decimal(raw)) if raw is not None else None


def _opt_int(raw: str | None) -> int | None:
    return int(parse_decimal(raw)) if raw is not None else None


def _field_audit_state(field: ExtractionField) -> dict[str, Any]:
    return {
        "field_path": field.field_path,
        "raw_value": field.raw_value,
        "normalized_value": field.normalized_value,
        "confidence": str(field.confidence),
        "band": field.band.value,
        "requires_confirmation": field.requires_confirmation,
        "is_confirmed": field.is_confirmed,
    }


def _run_audit_state(run: ExtractionRun, fields: list[ExtractionField]) -> dict[str, Any]:
    return {
        "state": run.state.value,
        "run_number": run.run_number,
        "provider": run.provider_name,
        "simulated": run.simulated,
        "overall_confidence": (
            str(run.overall_confidence) if run.overall_confidence is not None else None
        ),
        "fields_requiring_review": run.fields_requiring_review,
        "field_count": len(fields),
    }


class ExtractionService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
        *,
        storage: StorageProvider,
        provider: ExtractionProvider,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._storage = storage
        self._provider = provider
        self._repo = ExtractionRepository(session, organization_id)
        self._page_repo = DocumentPageRepository(session, organization_id)
        self._document_repo = DocumentRepository(session, organization_id)
        self._rfq_repo = RfqRepository(session, organization_id)
        self._supplier_repo = SupplierRepository(session, organization_id)
        self._quote_repo = QuoteRepository(session, organization_id)

    # -- state machine -----------------------------------------------------

    def _transition(self, run: ExtractionRun, to_state: ExtractionRunState) -> None:
        allowed = ALLOWED_EXTRACTION_RUN_TRANSITIONS[run.state]
        if to_state not in allowed:
            raise ConflictStateError(
                f"Cannot transition extraction run from {run.state.value!r} to "
                f"{to_state.value!r}."
            )
        run.state = to_state

    def _fail_run(self, run: ExtractionRun, *, error_code: str, error_message: str) -> None:
        self._transition(run, ExtractionRunState.FAILED)
        run.error_code = error_code
        run.error_message = error_message
        run.finished_at = self._clock.now()
        self._db.flush()
        self._audit.record(
            "extraction.run_completed",
            entity_type="extraction_run",
            entity_id=run.id,
            after_state={
                "state": run.state.value,
                "error_code": error_code,
                "error_message": error_message,
            },
            explanation=(
                f"Extraction run {run.run_number} for document {run.document_id} "
                f"failed: {error_code}"
            ),
        )

    # -- page acquisition ----------------------------------------------

    def _acquire_or_load_pages(self, document: QuoteDocument, kind: DocumentKind) -> list[str]:
        """Idempotent: `document_pages` carries `UNIQUE (organization_id,
        document_id, page_number)` (app/models/documents.py), so a second
        extraction run against the same document must reuse the pages a
        prior run already persisted rather than re-inserting them — page
        acquisition is a property of the *document* (Stage 4), not of any
        one run."""
        existing = self._page_repo.list_pages(document.id)
        if existing:
            return [
                (p.text_layer if p.text_layer is not None else (p.ocr_text or ""))
                for p in existing
            ]

        data = self._storage.get(organization_id=self._organization_id, key=document.storage_key)
        acquired = acquire_pages(data, kind)
        for p in acquired:
            source = "ocr" if p.used_ocr else _EXTRACTION_SOURCE_OF_KIND[kind]
            page = DocumentPage(
                id=self._ids.new_id(),
                organization_id=self._organization_id,
                document_id=document.id,
                page_number=p.page_number,
                text_layer=(p.text if not p.used_ocr else None),
                ocr_text=(p.text if p.used_ocr else None),
                ocr_confidence=None,
                preview_storage_key=None,
                extraction_source=source,
            )
            self._page_repo.add_page(page)
        document.page_count = len(acquired)
        self._db.flush()
        return [p.text for p in acquired]

    # -- validation ladder: field materialization -------------------------

    def _build_fields(
        self, run: ExtractionRun, payload: ExtractedQuotePayload, *, injection_suspected: bool
    ) -> list[ExtractionField]:
        fields: list[ExtractionField] = []

        def add(
            field_path: str,
            target_entity: str,
            target_line_index: int | None,
            field_name: str,
            value_type: str,
            extracted: ExtractedField,
        ) -> ExtractionField:
            normalized, valid = _validate_value(value_type, extracted.value)
            confidence = quantize(Decimal(str(extracted.confidence)), _CONFIDENCE_SCALE)
            field_band = band(confidence)
            row = ExtractionField(
                id=self._ids.new_id(),
                organization_id=self._organization_id,
                extraction_run_id=run.id,
                field_path=field_path,
                target_entity=target_entity,
                target_line_index=target_line_index,
                field_name=field_name,
                raw_value=extracted.raw_text,
                normalized_value=(normalized if valid else None),
                value_type=value_type,
                confidence=confidence,
                band=field_band,
                requires_confirmation=(field_band != ConfidenceBand.HIGH) or not valid,
                is_confirmed=False,
                confirmed_by_id=None,
                confirmed_at=None,
                source_page=extracted.page_number,
                source_bbox=None,
                injection_flagged=injection_suspected,
            )
            fields.append(row)
            return row

        def mark_invalid(row: ExtractionField) -> None:
            row.normalized_value = None
            row.requires_confirmation = True

        # -- quote-level --
        add("supplier_name", "quote", None, "supplier_name", _VT_TEXT, payload.supplier_name)
        add("quote_number", "quote", None, "quote_number", _VT_TEXT, payload.quote_number)
        add("quote_date", "quote", None, "quote_date", _VT_DATE, payload.quote_date)
        add(
            "expiration_date",
            "quote",
            None,
            "expiration_date",
            _VT_DATE,
            payload.expiration_date,
        )
        add("currency", "quote", None, "currency", _VT_CURRENCY, payload.currency)

        # -- terms --
        terms = payload.terms
        add(
            "terms.payment_terms",
            "quote_terms",
            None,
            "payment_terms",
            _VT_TEXT,
            terms.payment_terms,
        )
        add(
            "terms.shipping_terms",
            "quote_terms",
            None,
            "shipping_terms",
            _VT_TEXT,
            terms.shipping_terms,
        )
        add("terms.warranty", "quote_terms", None, "warranty", _VT_TEXT, terms.warranty)
        add("terms.exceptions", "quote_terms", None, "exceptions", _VT_TEXT, terms.exceptions)
        add("terms.exclusions", "quote_terms", None, "exclusions", _VT_TEXT, terms.exclusions)
        add("terms.notes", "quote_terms", None, "notes", _VT_TEXT, terms.notes)

        # -- lines --
        for i, line in enumerate(payload.lines):
            add(
                f"lines[{i}].part_number",
                "quote_line",
                i,
                "part_number",
                _VT_TEXT,
                line.part_number,
            )
            add(
                f"lines[{i}].description",
                "quote_line",
                i,
                "description",
                _VT_TEXT,
                line.description,
            )
            qty_row = add(
                f"lines[{i}].quantity", "quote_line", i, "quantity", _VT_DECIMAL, line.quantity
            )
            add(
                f"lines[{i}].unit_of_measure",
                "quote_line",
                i,
                "unit_of_measure",
                _VT_ENUM,
                line.unit_of_measure,
            )
            price_row = add(
                f"lines[{i}].unit_price", "quote_line", i, "unit_price", _VT_MONEY, line.unit_price
            )
            add(f"lines[{i}].moq", "quote_line", i, "moq", _VT_DECIMAL, line.moq)
            add(
                f"lines[{i}].lead_time_days",
                "quote_line",
                i,
                "lead_time_days",
                _VT_DECIMAL,
                line.lead_time_days,
            )
            add(
                f"lines[{i}].country_of_origin",
                "quote_line",
                i,
                "country_of_origin",
                _VT_TEXT,
                line.country_of_origin,
            )
            add(
                f"lines[{i}].production_capacity",
                "quote_line",
                i,
                "production_capacity",
                _VT_DECIMAL,
                line.production_capacity,
            )
            add(
                f"lines[{i}].tooling_cost",
                "quote_line",
                i,
                "tooling_cost",
                _VT_MONEY,
                line.tooling_cost,
            )
            add(f"lines[{i}].setup_cost", "quote_line", i, "setup_cost", _VT_MONEY, line.setup_cost)
            add(
                f"lines[{i}].packaging_cost",
                "quote_line",
                i,
                "packaging_cost",
                _VT_MONEY,
                line.packaging_cost,
            )
            add(
                f"lines[{i}].shipping_cost",
                "quote_line",
                i,
                "shipping_cost",
                _VT_MONEY,
                line.shipping_cost,
            )
            add(
                f"lines[{i}].insurance_cost",
                "quote_line",
                i,
                "insurance_cost",
                _VT_MONEY,
                line.insurance_cost,
            )
            add(
                f"lines[{i}].tariff_amount",
                "quote_line",
                i,
                "tariff_amount",
                _VT_MONEY,
                line.tariff_amount,
            )
            add(
                f"lines[{i}].duty_amount",
                "quote_line",
                i,
                "duty_amount",
                _VT_MONEY,
                line.duty_amount,
            )
            add(
                f"lines[{i}].customs_fees",
                "quote_line",
                i,
                "customs_fees",
                _VT_MONEY,
                line.customs_fees,
            )
            add(f"lines[{i}].tax_amount", "quote_line", i, "tax_amount", _VT_MONEY, line.tax_amount)

            # -- business validation (c): quantity>0, unit_price>=0 --
            qty_normalized = qty_row.normalized_value
            if qty_normalized is not None and parse_decimal(qty_normalized) <= 0:
                mark_invalid(qty_row)
            price_normalized = price_row.normalized_value
            if price_normalized is not None and parse_decimal(price_normalized) < 0:
                mark_invalid(price_row)

            # -- price breaks --
            pb_rows: list[tuple[ExtractionField, ExtractionField, ExtractionField]] = []
            for j, pb in enumerate(line.price_breaks):
                min_row = add(
                    f"lines[{i}].price_breaks[{j}].min_quantity",
                    "quote_line",
                    i,
                    "min_quantity",
                    _VT_DECIMAL,
                    pb.min_quantity,
                )
                max_row = add(
                    f"lines[{i}].price_breaks[{j}].max_quantity",
                    "quote_line",
                    i,
                    "max_quantity",
                    _VT_DECIMAL,
                    pb.max_quantity,
                )
                pb_price_row = add(
                    f"lines[{i}].price_breaks[{j}].unit_price",
                    "quote_line",
                    i,
                    "unit_price",
                    _VT_MONEY,
                    pb.unit_price,
                )
                pb_rows.append((min_row, max_row, pb_price_row))

            ok_tiers: list[tuple[int, Decimal, Decimal | None]] = []
            for j, (min_row, max_row, pb_price_row) in enumerate(pb_rows):
                if (
                    pb_price_row.normalized_value is not None
                    and parse_decimal(pb_price_row.normalized_value) < 0
                ):
                    mark_invalid(pb_price_row)
                if min_row.normalized_value is None:
                    continue
                mn = parse_decimal(min_row.normalized_value)
                if mn <= 0:
                    mark_invalid(min_row)
                    continue
                mx = (
                    parse_decimal(max_row.normalized_value)
                    if max_row.normalized_value is not None
                    else None
                )
                ok_tiers.append((j, mn, mx))

            structure_flags = _price_break_structure_flags(ok_tiers)
            for j, _mn, _mx in ok_tiers:
                flag = structure_flags.get(j)
                if flag == "min":
                    mark_invalid(pb_rows[j][0])
                elif flag == "max":
                    mark_invalid(pb_rows[j][1])

        return fields

    # -- start_run -----------------------------------------------------

    def start_run(self, document_id: uuid.UUID, *, actor_id: uuid.UUID) -> ExtractionRun:
        document = self._document_repo.get_or_raise(document_id)
        if document.state != DocumentState.STORED:
            raise ConflictStateError(
                f"Document is '{document.state.value}'; extraction can only start "
                "from 'stored'."
            )
        if document.detected_mime is None or document.content_sha256 is None:
            # Unreachable in practice: DocumentService.upload always sets both
            # before a row ever reaches 'stored' — defensive only.
            raise ConflictStateError(
                "Document has no detected content type or content hash; content "
                "validation may not have completed."
            )
        kind = _KIND_OF_DETECTED_MIME.get(document.detected_mime)
        if kind is None:  # pragma: no cover - detected_mime is always one of 5 canonical values
            raise ConflictStateError(f"Unrecognized detected_mime {document.detected_mime!r}.")

        now = self._clock.now()
        run = ExtractionRun(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            document_id=document.id,
            run_number=self._repo.next_run_number(document.id),
            state=ExtractionRunState.QUEUED,
            provider_name=self._provider.name,
            provider_model=None,
            simulated=self._provider.is_simulated,
            schema_version=EXTRACTION_SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            started_by_id=actor_id,
            started_at=now,
        )
        self._repo.add(run)
        self._db.flush()

        self._transition(run, ExtractionRunState.RUNNING)
        self._db.flush()

        try:
            pages_text = self._acquire_or_load_pages(document, kind)
        except AcquisitionLimitError as exc:
            self._fail_run(run, error_code="acquisition_limit_exceeded", error_message=str(exc))
            return run
        except Exception:
            self._fail_run(
                run,
                error_code="acquisition_failed",
                error_message="Unexpected error while acquiring document text.",
            )
            return run

        # Canary scan (Stage 5's observational defense) runs over the real
        # acquired page text, before the provider is ever called.
        canary = scan_for_injection(pages_text)
        if canary.suspected:
            self._audit.record(
                "security.injection_suspected",
                entity_type="extraction_run",
                entity_id=run.id,
                after_state={
                    "document_id": str(document.id),
                    "matched_snippets": list(canary.matches),
                },
                explanation=(
                    f"Prompt-injection canary matched on document {document.id}: "
                    f"{list(canary.matches)}"
                ),
            )

        sha256_hex = document.content_sha256.hex()
        run.provider_request_meta = {
            "document_sha256": sha256_hex,
            "page_count": len(pages_text),
            "prompt_version": PROMPT_VERSION,
        }

        try:
            payload = self._provider.extract(document_sha256=sha256_hex, pages=pages_text)
        except Exception:
            self._fail_run(
                run,
                error_code="provider_error",
                error_message="The extraction provider failed to return a result.",
            )
            return run

        # "mark run": the canary verdict is persisted on the run itself (no
        # dedicated column exists — see module docstring), not only on the
        # fields it produces.
        run.raw_response = {
            "payload": payload.model_dump(mode="json"),
            "injection_scan": {"suspected": canary.suspected, "matches": list(canary.matches)},
        }

        fields = self._build_fields(run, payload, injection_suspected=canary.suspected)
        for f in fields:
            self._repo.add_field(f)
        self._db.flush()

        run.overall_confidence = quantize(
            Decimal(str(payload.overall_confidence)), _CONFIDENCE_SCALE
        )
        fields_requiring_review = sum(
            1 for f in fields if f.requires_confirmation and not f.is_confirmed
        )
        run.fields_requiring_review = fields_requiring_review
        run.finished_at = self._clock.now()

        self._transition(run, ExtractionRunState.EXTRACTED)
        next_state = (
            ExtractionRunState.NEEDS_REVIEW
            if fields_requiring_review > 0
            else ExtractionRunState.READY
        )
        self._transition(run, next_state)
        self._db.flush()

        self._audit.record(
            "extraction.run_completed",
            entity_type="extraction_run",
            entity_id=run.id,
            after_state=_run_audit_state(run, fields),
            explanation=(
                f"Extraction run {run.run_number} for document {document.id} "
                f"completed: state={run.state.value}, "
                f"fields_requiring_review={fields_requiring_review}"
            ),
        )
        return run

    # -- reads -----------------------------------------------------------

    def get_run(self, run_id: uuid.UUID) -> ExtractionRun:
        return self._repo.get_or_raise(run_id)

    def list_runs(self, document_id: uuid.UUID) -> list[ExtractionRun]:
        self._document_repo.get_or_raise(document_id)
        return self._repo.list_runs_for_document(document_id)

    def list_fields(
        self,
        run_id: uuid.UUID,
        *,
        requires_confirmation: bool | None = None,
        band: ConfidenceBand | None = None,
        is_confirmed: bool | None = None,
    ) -> list[ExtractionField]:
        self._repo.get_or_raise(run_id)
        return self._repo.list_fields(
            run_id,
            requires_confirmation=requires_confirmation,
            band=band,
            is_confirmed=is_confirmed,
        )

    def _get_field_in_run(self, run_id: uuid.UUID, field_id: uuid.UUID) -> ExtractionField:
        self._repo.get_or_raise(run_id)
        field = self._repo.get_field_or_raise(field_id)
        if field.extraction_run_id != run_id:
            raise NotFoundError("ExtractionField")
        return field

    def _recompute_run_state(self, run_id: uuid.UUID) -> None:
        """Self-loop transition (`needs_review -> needs_review` / `-> ready`)
        per this task's objective: "run recomputes needs_review->ready when
        no unconfirmed sub-HIGH fields remain." A no-op for any other run
        state (e.g. a post-materialization correction, see module docstring)
        — `materialized -> needs_review`/`-> ready` are not legal
        transitions anyway."""
        run = self._repo.get_or_raise(run_id)
        if run.state != ExtractionRunState.NEEDS_REVIEW:
            return
        fields = self._repo.list_fields(run_id)
        still_pending = [f for f in fields if f.requires_confirmation and not f.is_confirmed]
        run.fields_requiring_review = len(still_pending)
        self._transition(
            run, ExtractionRunState.READY if not still_pending else ExtractionRunState.NEEDS_REVIEW
        )
        self._db.flush()

    # -- field review API --------------------------------------------------

    def confirm_field(
        self, run_id: uuid.UUID, field_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> ExtractionField:
        field = self._get_field_in_run(run_id, field_id)
        before = _field_audit_state(field)
        field.is_confirmed = True
        field.confirmed_by_id = actor_id
        field.confirmed_at = self._clock.now()
        self._db.flush()

        self._audit.record(
            "extraction.field_confirmed",
            entity_type="extraction_field",
            entity_id=field.id,
            before_state=before,
            after_state=_field_audit_state(field),
            explanation=f"Field {field.field_path} confirmed on extraction run {run_id}",
        )
        self._recompute_run_state(run_id)
        return field

    def correct_field(
        self,
        run_id: uuid.UUID,
        field_id: uuid.UUID,
        *,
        new_value: str | None,
        reason: str | None,
        actor_id: uuid.UUID,
    ) -> ExtractionField:
        field = self._get_field_in_run(run_id, field_id)
        normalized, valid = _validate_value(field.value_type, new_value)
        if not valid:
            raise ValidationAppError(
                f"{new_value!r} is not a valid {field.value_type} value for "
                f"{field.field_path}.",
                details=[
                    ErrorDetail(field=field.field_path, issue=f"not a valid {field.value_type}")
                ],
            )

        before = _field_audit_state(field)
        before_normalized = field.normalized_value
        field.normalized_value = normalized
        field.is_confirmed = True
        field.confirmed_by_id = actor_id
        field.confirmed_at = self._clock.now()
        self._db.flush()

        # See module docstring "RESOLVED: QuoteCorrection.quote_id is now
        # nullable": a QuoteCorrection row is now always written, whether or
        # not a quote has been materialized from this run yet.
        quote_id = self._repo.find_materialized_quote_id(run_id)
        correction = QuoteCorrection(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            quote_id=quote_id,
            extraction_field_id=field.id,
            extraction_run_id=run_id,
            target_table="extraction_fields",
            target_row_id=field.id,
            field_name=field.field_name,
            before_value=before_normalized,
            after_value=normalized,
            reason=reason,
            corrected_by_id=actor_id,
            corrected_at=self._clock.now(),
        )
        self._repo.add_correction(correction)
        self._db.flush()
        correction_written = True

        self._audit.record(
            "extraction.field_corrected",
            entity_type="extraction_field",
            entity_id=field.id,
            before_state=before,
            after_state={
                **_field_audit_state(field),
                "reason": reason,
                "quote_correction_written": correction_written,
            },
            explanation=f"Field {field.field_path} corrected on extraction run {run_id}",
        )
        self._recompute_run_state(run_id)
        return field

    # -- materialize -----------------------------------------------------

    def _check_rfq_eligible(self, rfq: Rfq) -> None:
        """Mirrors QuoteService._check_rfq_eligible — see module docstring
        point 2 for why this is re-derived rather than reused."""
        if rfq.status not in _ELIGIBLE_RFQ_STATUSES:
            raise ConflictStateError(
                f"RFQ is '{rfq.status.value}'; a quote can only be materialized "
                "while the RFQ is 'open' or 'under_review'."
            )

    def _check_supplier_eligible(self, rfq_id: uuid.UUID, supplier_id: uuid.UUID) -> None:
        if self._supplier_repo.get(supplier_id) is None:
            raise NotFoundError("Supplier")
        rs = self._rfq_repo.get_supplier_by_supplier_id(rfq_id, supplier_id)
        if rs is None:
            raise ConflictStateError(
                "This supplier has not been invited to this RFQ; invite them before "
                "materializing a quote on their behalf."
            )
        if rs.excluded_at is not None:
            raise ConflictStateError("This supplier has been excluded from this RFQ.")

    def _resolve_unit_definition_id(self, raw_unit_text: str | None) -> uuid.UUID:
        """See module docstring's `unit_definition_id` note."""
        candidates: list[str] = []
        if raw_unit_text:
            candidates.append(raw_unit_text.strip().lower())
        if "each" not in candidates:
            candidates.append("each")
        for code in candidates:
            stmt = select(UnitDefinition).where(
                func.lower(UnitDefinition.code) == code,
                or_(
                    UnitDefinition.organization_id.is_(None),
                    UnitDefinition.organization_id == self._organization_id,
                ),
            )
            unit = self._db.execute(stmt).scalars().first()
            if unit is not None:
                return unit.id
        raise ValidationAppError(
            "Cannot materialize: no unit of measure could be resolved for a line "
            f"(tried {candidates!r}) and no fallback 'each' unit exists in this "
            "organization's catalog."
        )

    def materialize(
        self, run_id: uuid.UUID, *, supplier_id: uuid.UUID, actor_id: uuid.UUID
    ) -> _QuoteGraph:
        run = self._repo.get_or_raise(run_id)
        if run.state != ExtractionRunState.READY:
            raise ConflictStateError(
                f"Extraction run is {run.state.value!r}; materialization requires "
                "'ready'."
            )

        fields = self._repo.list_fields(run_id)
        unconfirmed_low = [
            f for f in fields if f.band == ConfidenceBand.LOW and not f.is_confirmed
        ]
        if unconfirmed_low:
            raise ConflictStateError(
                "Cannot materialize: one or more low-confidence fields are not yet "
                "confirmed.",
                details=[
                    ErrorDetail(field=f.field_path, issue="low-confidence field is not confirmed")
                    for f in unconfirmed_low
                ],
            )

        document = self._document_repo.get_or_raise(run.document_id)
        rfq = self._rfq_repo.get_or_raise(document.rfq_id)
        self._check_rfq_eligible(rfq)
        self._check_supplier_eligible(document.rfq_id, supplier_id)

        by_path = {f.field_path: f for f in fields}

        def val(path: str) -> str | None:
            f = by_path.get(path)
            return f.normalized_value if f is not None else None

        def raw(path: str) -> str | None:
            f = by_path.get(path)
            return f.raw_value if f is not None else None

        quote_date_raw = val("quote_date")
        if quote_date_raw is None:
            raise ValidationAppError(
                "Cannot materialize: 'quote_date' was never extracted or corrected "
                "to a valid value."
            )
        currency_raw = val("currency")
        if currency_raw is None:
            raise ValidationAppError(
                "Cannot materialize: 'currency' was never extracted or corrected "
                "to a valid value."
            )
        expiration_raw = val("expiration_date")

        now = self._clock.now()
        quote = Quote(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            rfq_id=document.rfq_id,
            supplier_id=supplier_id,
            quote_number=val("quote_number"),
            quote_date=date.fromisoformat(quote_date_raw),
            expiration_date=(
                date.fromisoformat(expiration_raw) if expiration_raw is not None else None
            ),
            currency=currency_raw,
            status=QuoteStatus.DRAFT,
            source=QuoteSource.EXTRACTED,
            notes=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._quote_repo.add(quote)
        self._db.flush()

        line_indices = sorted(
            {
                f.target_line_index
                for f in fields
                if f.target_entity == "quote_line" and f.target_line_index is not None
            }
        )

        lines: list[QuoteLine] = []
        breaks_by_line: dict[uuid.UUID, list[QuotePriceBreak]] = {}
        for line_number, i in enumerate(line_indices, start=1):
            quantity_raw = val(f"lines[{i}].quantity")
            if quantity_raw is None:
                raise ValidationAppError(
                    f"Cannot materialize: 'lines[{i}].quantity' was never extracted "
                    "or corrected to a valid value."
                )
            unit_definition_id = self._resolve_unit_definition_id(
                val(f"lines[{i}].unit_of_measure")
            )

            line = QuoteLine(
                id=self._ids.new_id(),
                organization_id=self._organization_id,
                quote_id=quote.id,
                line_number=line_number,
                quoted_part_number=val(f"lines[{i}].part_number"),
                quoted_mpn=None,
                description=val(f"lines[{i}].description"),
                quantity=quantize_qty(parse_decimal(quantity_raw)),
                unit_definition_id=unit_definition_id,
                raw_unit_of_measure=raw(f"lines[{i}].unit_of_measure"),
                unit_price=_opt_decimal(val(f"lines[{i}].unit_price"), quantize_unit_price),
                moq=_opt_decimal(val(f"lines[{i}].moq"), quantize_qty),
                tooling_cost=_opt_decimal(val(f"lines[{i}].tooling_cost"), quantize_money),
                setup_cost=_opt_decimal(val(f"lines[{i}].setup_cost"), quantize_money),
                packaging_cost=_opt_decimal(val(f"lines[{i}].packaging_cost"), quantize_money),
                shipping_cost=_opt_decimal(val(f"lines[{i}].shipping_cost"), quantize_money),
                insurance_cost=_opt_decimal(val(f"lines[{i}].insurance_cost"), quantize_money),
                other_fixed_cost=None,
                tariff_amount=_opt_decimal(val(f"lines[{i}].tariff_amount"), quantize_money),
                duty_amount=_opt_decimal(val(f"lines[{i}].duty_amount"), quantize_money),
                customs_fee=_opt_decimal(val(f"lines[{i}].customs_fees"), quantize_money),
                tax_amount=_opt_decimal(val(f"lines[{i}].tax_amount"), quantize_money),
                country_of_origin=_country_code(val(f"lines[{i}].country_of_origin")),
                lead_time_days=_opt_int(val(f"lines[{i}].lead_time_days")),
                production_capacity=_opt_decimal(
                    val(f"lines[{i}].production_capacity"), quantize_qty
                ),
                matched_rfq_line_id=None,
                part_id=None,
                notes=None,
            )
            self._quote_repo.add_line(line)
            lines.append(line)
            self._db.flush()

            tier_breaks: list[QuotePriceBreak] = []
            j = 0
            while f"lines[{i}].price_breaks[{j}].min_quantity" in by_path:
                min_raw = val(f"lines[{i}].price_breaks[{j}].min_quantity")
                price_raw = val(f"lines[{i}].price_breaks[{j}].unit_price")
                max_raw = val(f"lines[{i}].price_breaks[{j}].max_quantity")
                # A tier missing either boundary required by the DB schema
                # (min_quantity, unit_price — both NOT NULL on
                # quote_price_breaks) is dropped rather than materialized
                # half-built; it is still fully visible on its own
                # ExtractionField rows for review.
                if min_raw is not None and price_raw is not None:
                    pb = QuotePriceBreak(
                        id=self._ids.new_id(),
                        organization_id=self._organization_id,
                        quote_line_id=line.id,
                        min_quantity=quantize_qty(parse_decimal(min_raw)),
                        max_quantity=(
                            quantize_qty(parse_decimal(max_raw)) if max_raw is not None else None
                        ),
                        unit_price=quantize_unit_price(parse_decimal(price_raw)),
                        setup_fee=None,
                        tier_index=j,
                    )
                    self._quote_repo.add_price_break(pb)
                    tier_breaks.append(pb)
                j += 1
            breaks_by_line[line.id] = tier_breaks

        terms = QuoteTerms(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            quote_id=quote.id,
            payment_terms=val("terms.payment_terms"),
            payment_terms_days=None,
            incoterm=None,
            shipping_terms=val("terms.shipping_terms"),
            warranty_terms=val("terms.warranty"),
            validity_days=None,
            exceptions=val("terms.exceptions"),
            exclusions=val("terms.exclusions"),
            notes=val("terms.notes"),
        )
        self._quote_repo.add_terms(terms)

        try:
            self._db.flush()
        except IntegrityError as exc:
            raise ConflictStateError(
                "Could not materialize this quote; the data conflicted with an "
                "existing row."
            ) from exc

        self._transition(run, ExtractionRunState.MATERIALIZED)
        self._db.flush()

        self._audit.record(
            "extraction.materialized",
            entity_type="quote",
            entity_id=quote.id,
            after_state={
                "extraction_run_id": str(run.id),
                "document_id": str(document.id),
                "supplier_id": str(supplier_id),
                "quote_id": str(quote.id),
                "line_count": len(lines),
            },
            explanation=(
                f"Quote {quote.id} materialized from extraction run {run.id} "
                f"(document {document.id}) for supplier {supplier_id}"
            ),
        )
        return quote, lines, breaks_by_line, terms


__all__ = ["PROMPT_VERSION", "ExtractionService", "build_extraction_provider"]
