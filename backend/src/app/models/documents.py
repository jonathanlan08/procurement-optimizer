"""Document/extraction pipeline: quote_documents, document_pages,
extraction_runs, extraction_fields, quote_corrections, part_match_candidates
(docs/planning/02-erd.md §6; docs/planning/04-document-pipeline.md).

**Schema-only phase.** No routes, services, or providers land here - only the
tables, their constraints, and the two closed-set state machines the ERD ties
to named Postgres `ENUM` types (`document_state_enum`, `extraction_state_enum`).
The *document* state machine's transition table is explicitly assigned by
04-document-pipeline.md §9 to `app/domain/documents/transitions.py` ("a single
dict ... unit-tested exhaustively") - a concurrent agent's responsibility
(`domain/`), not this file's. `DocumentState` here exists only so the column's
`ENUM` type has the right members; no transition dict for it is defined in
this module.

The *extraction-run* machine has no such explicit alternate home in the docs,
so - mirroring `app/models/rfqs.py`'s `ALLOWED_RFQ_TRANSITIONS` precedent
("services and tests share one source of truth" for a state machine the ERD
says is "enforced in the service layer with an explicit transition table")
- `ALLOWED_EXTRACTION_RUN_TRANSITIONS` is defined alongside `ExtractionRunState`
below, transcribed from 04-document-pipeline.md §9's `stateDiagram-v2` verbatim.
Unlike the RFQ machine, this one has a genuine self-transition
(`needs_review -> needs_review`, "field corrected / confirmed") - captured
faithfully rather than forced into the RFQ machine's "no self-transitions"
shape.

## Deliberate ERD deviations / judgment calls

1. **`QuoteDocument` field names follow 02-erd.md §6's literal box, not the
   delegating task's paraphrase.** The task's own prose says "content_type,
   byte_size, sha256"; the ERD box spells these `detected_mime`/
   `declared_mime`, `size_bytes`, `content_sha256`. Resolved in favor of the
   ERD's literal spelling, the same precedent `app/models/part_imports.py`'s
   module docstring already sets ("the ERD is the more authoritative source
   for the schema shape" when the task's own words differ from the ERD box).
2. **`QuoteDocument` nullability for `detected_mime`, `content_sha256`, and
   `page_count`.** 04-document-pipeline.md §9's document state machine says
   `quarantined` is reachable from *both* `uploaded` and `validated` - i.e. a
   row can exist, and be terminally quarantined, before Stage 2 (magic-byte
   sniffing / hashing) or Stage 4 (page acquisition) ever runs. All three are
   therefore nullable, populated progressively as the pipeline advances;
   `declared_mime`, `size_bytes`, `original_filename`, and `storage_key` are
   NOT NULL because Stage 1 (transport validation, before a byte is stored)
   already has all four for every row that exists at all. `rfq_id` is NOT
   NULL (the ERD box marks only `supplier_id` "nullable until identified";
   the absence of a similar note on `rfq_id` is read as intentional, the same
   contrastive reading `app/models/rfqs.py`'s docstring gives `due_date` vs.
   `requested_delivery_date`).
3. **`QuoteDocument` gets `ArchivableMixin` but not `TimestampedMixin` or
   `VersionedMixin`.** The ERD box lists `archived_at` (so `ArchivableMixin`,
   matching the `Quote`/`Rfq`/`BillOfMaterials` precedent of applying the full
   mixin whenever at least `archived_at` is present) but names its own
   creation timestamp `uploaded_at`, not `created_at`, and lists no
   `updated_at` or `version` at all - followed literally, the same restraint
   `app/models/part_imports.py` already shows for `PartImportBatch`.
4. **`DocumentPage` carries no provenance-bbox JSONB.** The delegating task
   asks for one "if the ERD has them"; 02-erd.md §6's `DOCUMENT_PAGES` box has
   no such column (provenance boxes exist only on `EXTRACTION_FIELDS.
   source_bbox`) - omitted per the task's own conditional.
5. **`ExtractionRun` has no self-referential "supersedes" FK.** The task's
   own prose says "superseded_by self-FK if ERD has it"; 02-erd.md §6's
   `EXTRACTION_RUNS` box has only a plain `timestamptz superseded_at`, no
   self-FK (unlike `Quote.superseded_by_id` or `BillOfMaterials.
   previous_version_id`) - omitted per the task's own conditional. A run's
   "next" run is discoverable via `(document_id, run_number + 1)`, not a
   stored pointer.
6. **`confidence`/`overall_confidence`/`ocr_confidence` are `NUMERIC(9,6)`,
   not `NUMERIC(4,3)`.** The delegating task's own prose offers `NUMERIC(4,3)`
   "or per ERD"; every confidence-shaped column 02-erd.md §6 actually spells
   out (`extraction_fields.confidence`, `extraction_runs.overall_confidence`,
   `document_pages.ocr_confidence`, `part_match_candidates.confidence`) is
   explicitly `"9,6"`, matching `SUPPLIER_PERFORMANCE_RECORDS`' ratio columns
   elsewhere in the same ERD. `NUMERIC(9,6)` is used throughout; the bands
   themselves (thresholds 0.95/0.60) come from `app.domain.confidence`
   (00-decisions.md §4 #27) - `ConfidenceBand` is imported directly rather
   than redefined, and `CONFIDENCE_BAND_ENUM` wraps that same Python enum so
   the DB type's members can never drift from the domain module's.
7. **`PartMatchCandidate.strategy` values follow 02-erd.md §6's literal
   spelling, not the delegating task's paraphrase.** The task's own prose
   says `exact_ipn/mpn/normalized/alternative/fuzzy`; the ERD box (and
   04-document-pipeline.md §10's own strategy table) spell these
   `internal_pn`, `mpn`, `normalized_text`, `alternative`, `fuzzy` - the same
   "ERD wins over the task's own shorthand" resolution as point 1.
8. **`QuoteCorrection.extraction_field_id` is nullable.** The ERD box marks it
   `FK` with no explicit nullability note, but SPEC's correction log is not
   scoped to extraction-derived quotes only - a manually-entered quote
   (`Quote.source = 'manual'`, `app/models/quotes.py`) has no extraction run
   or fields to reference at all, yet its lines are just as correctable. Made
   nullable (composite FK, `MATCH SIMPLE` exempts NULL rows, same pattern as
   every other optional composite FK in this schema) so the same correction
   log serves both origins; documented here as a judgment call rather than
   silently assumed.

8a. **`QuoteCorrection.quote_id` is now nullable, and `QuoteCorrection`
    gains a new nullable `extraction_run_id` composite org FK to
    `extraction_runs`** (migration 0012, part-matching phase). This resolves
    a genuine conflict `services/extraction_service.py`'s own module
    docstring previously flagged in detail: 04-document-pipeline.md's
    pipeline places Stage 10 (Corrections) *before* Stage 11 (Materialize),
    i.e. a correction can legitimately happen while only an `ExtractionRun`/
    `ExtractionField` exist and no `Quote` row has been built yet - but
    `quote_id` was originally `NOT NULL`, so `correct_field` could not write
    a `QuoteCorrection` row at all in that case (only the audit event
    recorded the edit). With `quote_id` nullable (same `MATCH SIMPLE`
    NULL-exempts-the-row pattern as point 8's `extraction_field_id`) and a
    new `extraction_run_id` giving every correction - pre- or
    post-materialization - a durable, directly queryable link back to the
    run that produced the field it corrects (previously the *only* place
    that link existed at all was buried in the `extraction.materialized`
    audit event's `after_state`, per `ExtractionRepository.
    find_materialized_quote_id`'s own docstring), `correct_field` can now
    always write a `QuoteCorrection` row: `quote_id` is populated once a
    quote has been materialized, `extraction_run_id` is always populated,
    and a pre-materialization correction simply has `quote_id IS NULL`. This
    is the one deliberate, narrowly-scoped edit this phase makes to an
    otherwise-FROZEN model.
9. **Every non-cascade FK introduced by this module is `RESTRICT`.**
   02-erd.md §11's cascade whitelist names exactly four pairs; the only one
   relevant to this module is `extraction_runs` → `extraction_fields`
   ("the child cannot exist alone" - a field cannot exist without the run
   that produced it, and re-extraction never mutates a prior run's fields, so
   CASCADE is purely a teardown/purge safety net, same as the whitelist's
   other three pairs). Every other FK here - `quote_documents` →
   `document_pages`, `quote_documents` → `extraction_runs`, `quotes` →
   `quote_corrections`, `extraction_fields` → `quote_corrections`,
   `quote_lines`/`rfq_lines`/`parts` → `part_match_candidates` - is
   `RESTRICT` by the ERD's explicit "everything else is RESTRICT" default,
   the same letter-of-the-whitelist discipline `app/models/quotes.py` and
   `app/models/boms.py` already document for equally aggregate-internal pairs
   the whitelist happens not to name.
10. **Additional uniqueness/indexes beyond 02-erd.md §8/§9's literal text**,
    each the same class of duplicate-row bug the ERD already guards against
    elsewhere in this schema (precedent: `app/models/part_imports.py`'s
    `row_number` uniqueness, not spelled out in §8 either):
    `document_pages`: `UNIQUE (organization_id, document_id, page_number)` -
    two rows cannot claim the same page. `extraction_runs`: §8's literal
    `uq_extraction_run_number` is `UNIQUE (document_id, run_number)`;
    scoped to `organization_id` too, the same adaptation
    `app/models/quotes.py` already documents for `uq_price_break_min`.
    `extraction_fields`: `UNIQUE (organization_id, extraction_run_id,
    field_path)` - one row per field per run. `part_match_candidates`:
    the delegating task's own suggested `UNIQUE (line, part, strategy)`,
    `UNIQUE (organization_id, quote_line_id, part_id, strategy)`.
    `quote_corrections` additionally gets `ix_quote_corrections_quote
    (organization_id, quote_id, corrected_at DESC)` - 02-erd.md §2's own
    stated reason for this table's existence is "the review UI needs to
    query it cheaply," which is exactly the index that query needs (§9's
    "index discipline" rule: "add an index only with a query that needs it").
11. **Confirmation pairing CHECKs** (`extraction_fields`,
    `part_match_candidates`): `confirmed_by_id`/`confirmed_at` must be
    NULL together or set together, and the boolean confirmation flag
    (`is_confirmed`/`human_confirmed`) must agree with whether `confirmed_at`
    is set - the same shape of "a flagged state with a missing partner field
    is a data error, not a valid state" reasoning `app/models/fx.py`'s
    `ck_exchange_rates_override_reason_required` and `app/models/rfqs.py`'s
    `ck_rfq_suppliers_exclusion_reason_paired` already establish. State
    *transitions* (e.g. whether a `low`-band field may be bulk-confirmed)
    stay in the service layer per 02-erd.md §8 - these CHECKs only forbid an
    internally-contradictory row, never encode a workflow rule.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.confidence import ConfidenceBand
from app.models.base import ArchivableMixin, OrgOwnedBase, org_identity_constraint

# -- enums --------------------------------------------------------------


class DocumentState(StrEnum):
    """Coarse `quote_documents.state` machine (04-document-pipeline.md §9).
    Transition table lives in `app/domain/documents/transitions.py` (module
    docstring point - out of this file's scope), not here."""

    UPLOADED = "uploaded"
    VALIDATED = "validated"
    STORED = "stored"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    IN_REVIEW = "in_review"
    CONFIRMED = "confirmed"
    QUARANTINED = "quarantined"
    ARCHIVED = "archived"


DOCUMENT_STATE_ENUM = SaEnum(
    DocumentState,
    name="document_state_enum",
    values_callable=lambda e: [m.value for m in e],
)


class ExtractionRunState(StrEnum):
    """Fine-grained `extraction_runs.state` machine
    (04-document-pipeline.md §9's `stateDiagram-v2`, transcribed verbatim)."""

    QUEUED = "queued"
    RUNNING = "running"
    FAILED_TRANSIENT = "failed_transient"
    FAILED = "failed"
    EXTRACTED = "extracted"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    MATERIALIZED = "materialized"
    SUPERSEDED = "superseded"


EXTRACTION_STATE_ENUM = SaEnum(
    ExtractionRunState,
    name="extraction_state_enum",
    values_callable=lambda e: [m.value for m in e],
)

# The extraction-run state machine's single source of truth (module docstring
# point above; mirrors app/models/rfqs.py's ALLOWED_RFQ_TRANSITIONS). Every
# state is a key, including the terminal SUPERSEDED, mapped to an empty
# frozenset, so lookups never need a fallback default. Transcribed edge for
# edge from 04-document-pipeline.md §9:
#   queued -> running
#   running -> failed_transient | failed | extracted
#   failed_transient -> queued (retry, max 3) | failed (retries exhausted)
#   failed -> superseded (re-run supersedes; terminal otherwise, [*])
#   extracted -> needs_review | ready
#   needs_review -> needs_review (field corrected/confirmed) | ready
#                   | superseded (re-run supersedes)
#   ready -> materialized
#   materialized -> superseded (a newer run is materialized; terminal
#                   otherwise, [*])
ALLOWED_EXTRACTION_RUN_TRANSITIONS: dict[ExtractionRunState, frozenset[ExtractionRunState]] = {
    ExtractionRunState.QUEUED: frozenset({ExtractionRunState.RUNNING}),
    ExtractionRunState.RUNNING: frozenset(
        {
            ExtractionRunState.FAILED_TRANSIENT,
            ExtractionRunState.FAILED,
            ExtractionRunState.EXTRACTED,
        }
    ),
    ExtractionRunState.FAILED_TRANSIENT: frozenset(
        {ExtractionRunState.QUEUED, ExtractionRunState.FAILED}
    ),
    ExtractionRunState.FAILED: frozenset({ExtractionRunState.SUPERSEDED}),
    ExtractionRunState.EXTRACTED: frozenset(
        {ExtractionRunState.NEEDS_REVIEW, ExtractionRunState.READY}
    ),
    ExtractionRunState.NEEDS_REVIEW: frozenset(
        {
            ExtractionRunState.NEEDS_REVIEW,
            ExtractionRunState.READY,
            ExtractionRunState.SUPERSEDED,
        }
    ),
    ExtractionRunState.READY: frozenset({ExtractionRunState.MATERIALIZED}),
    ExtractionRunState.MATERIALIZED: frozenset({ExtractionRunState.SUPERSEDED}),
    ExtractionRunState.SUPERSEDED: frozenset(),
}


class MatchStrategy(StrEnum):
    """`part_match_candidates.strategy` (04-document-pipeline.md §10's
    strategy table; ordering here follows the ERD box's own listed order,
    which differs from §10's execution order - both are the same five
    values, see module docstring point 7)."""

    INTERNAL_PN = "internal_pn"
    MPN = "mpn"
    NORMALIZED_TEXT = "normalized_text"
    ALTERNATIVE = "alternative"
    FUZZY = "fuzzy"


MATCH_STRATEGY_ENUM = SaEnum(
    MatchStrategy,
    name="match_strategy_enum",
    values_callable=lambda e: [m.value for m in e],
)

# ConfidenceBand (app.domain.confidence, PRINCIPAL-OWNED) is imported, not
# redefined - module docstring point 6. This is the DB-type mirror of that
# same Python enum, so `extraction_fields.band`'s legal values can never
# drift from the thresholds module.
CONFIDENCE_BAND_ENUM = SaEnum(
    ConfidenceBand,
    name="confidence_band_enum",
    values_callable=lambda e: [m.value for m in e],
)


# -- tables ---------------------------------------------------------------


class QuoteDocument(ArchivableMixin, OrgOwnedBase):
    """One uploaded file (PDF/image/CSV/XLSX) received against an RFQ. See
    module docstring points 1-3 for the field-naming/nullability/mixin
    decisions."""

    __tablename__ = "quote_documents"
    __table_args__ = (
        org_identity_constraint("quote_documents"),
        # composite org FK: the RFQ this document was received against.
        ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_quote_documents_organization_id_rfq_id",
        ),
        # composite org FK, nullable: "nullable until identified" (ERD).
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_quote_documents_organization_id_supplier_id",
        ),
        # 02-erd.md §8 uq_quote_documents_org_sha: duplicate-upload detection.
        UniqueConstraint(
            "organization_id", "content_sha256", name="uq_quote_documents_org_sha256"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_quote_documents_size_bytes_nonneg"),
        CheckConstraint(
            "page_count IS NULL OR page_count >= 0",
            name="ck_quote_documents_page_count_nonneg",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    rfq_id: Mapped[uuid.UUID] = mapped_column()
    # part of composite FK #2 above, not a single-column ForeignKey; nullable
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    original_filename: Mapped[str] = mapped_column(Text())
    storage_key: Mapped[str] = mapped_column(Text())
    # nullable: populated at Stage 2 content validation (module docstring
    # point 2); declared_mime is known at Stage 1, before storage.
    detected_mime: Mapped[str | None] = mapped_column(Text(), default=None)
    declared_mime: Mapped[str] = mapped_column(Text())
    size_bytes: Mapped[int] = mapped_column(BigInteger())
    # nullable: see module docstring point 2. LargeBinary, not a fixed-length
    # type, matching PartImportBatch.file_sha256 (app/models/part_imports.py).
    content_sha256: Mapped[bytes | None] = mapped_column(LargeBinary(), default=None)
    state: Mapped[DocumentState] = mapped_column(
        DOCUMENT_STATE_ENUM, default=DocumentState.UPLOADED
    )
    quarantine_reason: Mapped[str | None] = mapped_column(Text(), default=None)
    # nullable: populated at Stage 4 (text/table acquisition), after storage.
    page_count: Mapped[int | None] = mapped_column(SmallInteger(), default=None)
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    uploaded_at: Mapped[datetime] = mapped_column()


class DocumentPage(OrgOwnedBase):
    """One page/sheet's acquired text (native or OCR) - the audit record of
    exactly what text the extraction provider saw (04-document-pipeline.md
    §5). No timestamp/version/archive mixins: 02-erd.md §6 lists none for
    `DOCUMENT_PAGES`, the same judgement already made for `RfqLine`/
    `BillOfMaterialLine`."""

    __tablename__ = "document_pages"
    __table_args__ = (
        org_identity_constraint("document_pages"),
        # composite org FK: the document this page belongs to. RESTRICT, not
        # CASCADE - module docstring point 9: not in 02-erd.md §11's whitelist.
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["quote_documents.organization_id", "quote_documents.id"],
            ondelete="RESTRICT",
            name="fk_document_pages_organization_id_document_id",
        ),
        # module docstring point 10: not in §8's literal text, same class of
        # duplicate-row bug guarded against elsewhere in this schema.
        UniqueConstraint(
            "organization_id", "document_id", "page_number", name="uq_document_pages_org_doc_page"
        ),
        CheckConstraint("page_number > 0", name="ck_document_pages_page_number_pos"),
        CheckConstraint(
            "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)",
            name="ck_document_pages_ocr_confidence_bounds",
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    document_id: Mapped[uuid.UUID] = mapped_column()
    page_number: Mapped[int] = mapped_column(SmallInteger())
    text_layer: Mapped[str | None] = mapped_column(Text(), default=None)
    ocr_text: Mapped[str | None] = mapped_column(Text(), default=None)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), default=None)
    preview_storage_key: Mapped[str | None] = mapped_column(Text(), default=None)
    # "text_layer|ocr|sheet|csv" - plain Text + comment, not a DB enum, same
    # pattern as PartAlternative.approval_status (app/models/parts.py): the
    # ERD box types this column lowercase `text`, not a named enum type.
    extraction_source: Mapped[str] = mapped_column(Text())


class ExtractionRun(OrgOwnedBase):
    """One attempt to structure-extract one document via `ExtractionProvider`
    (04-document-pipeline.md §6-9). `ALLOWED_EXTRACTION_RUN_TRANSITIONS`
    above is the authority on which `state` transitions are legal. No
    timestamp/version/archive mixins: 02-erd.md §6 lists none for
    `EXTRACTION_RUNS` beyond its own `started_at`/`finished_at`/
    `superseded_at`."""

    __tablename__ = "extraction_runs"
    __table_args__ = (
        org_identity_constraint("extraction_runs"),
        # composite org FK: the document this run processed. RESTRICT, not
        # CASCADE - module docstring point 9.
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["quote_documents.organization_id", "quote_documents.id"],
            ondelete="RESTRICT",
            name="fk_extraction_runs_organization_id_document_id",
        ),
        # 02-erd.md §8 uq_extraction_run_number, org-scoped (module docstring
        # point 10, the same adaptation app/models/quotes.py documents for
        # uq_price_break_min).
        UniqueConstraint(
            "organization_id", "document_id", "run_number", name="uq_extraction_runs_org_doc_run"
        ),
        CheckConstraint("run_number > 0", name="ck_extraction_runs_run_number_pos"),
        CheckConstraint(
            "overall_confidence IS NULL OR (overall_confidence >= 0 AND overall_confidence <= 1)",
            name="ck_extraction_runs_overall_confidence_bounds",
        ),
        CheckConstraint(
            "fields_requiring_review IS NULL OR fields_requiring_review >= 0",
            name="ck_extraction_runs_fields_requiring_review_nonneg",
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    document_id: Mapped[uuid.UUID] = mapped_column()
    run_number: Mapped[int] = mapped_column(Integer())
    state: Mapped[ExtractionRunState] = mapped_column(
        EXTRACTION_STATE_ENUM, default=ExtractionRunState.QUEUED
    )
    provider_name: Mapped[str] = mapped_column(Text())  # "mock|anthropic"
    provider_model: Mapped[str | None] = mapped_column(Text(), default=None)
    simulated: Mapped[bool] = mapped_column(Boolean(), default=False)
    schema_version: Mapped[str] = mapped_column(Text())
    prompt_version: Mapped[str] = mapped_column(Text())
    # nullable: populated once the run actually dispatches to the provider,
    # not necessarily at 'queued' creation time.
    provider_request_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    overall_confidence: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), default=None)
    fields_requiring_review: Mapped[int | None] = mapped_column(Integer(), default=None)
    error_code: Mapped[str | None] = mapped_column(Text(), default=None)
    error_message: Mapped[str | None] = mapped_column(Text(), default=None)
    started_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    superseded_at: Mapped[datetime | None] = mapped_column(default=None)


class ExtractionField(OrgOwnedBase):
    """One extracted (or explicitly missing) field of one extraction run -
    verbatim + normalized + confidence, per the trust-boundary rule that only
    normalized, confirmed values ever feed `quotes`/`quote_lines`/
    `quote_terms` (04-document-pipeline.md §6). No timestamp/version/archive
    mixins beyond its own `confirmed_at`: 02-erd.md §6 lists none else."""

    __tablename__ = "extraction_fields"
    __table_args__ = (
        org_identity_constraint("extraction_fields"),
        # composite org FK: the run that produced this field. CASCADE per
        # 02-erd.md §11's explicit whitelist (module docstring point 9) - a
        # field cannot exist without its run.
        ForeignKeyConstraint(
            ["organization_id", "extraction_run_id"],
            ["extraction_runs.organization_id", "extraction_runs.id"],
            ondelete="CASCADE",
            name="fk_extraction_fields_organization_id_extraction_run_id",
        ),
        # module docstring point 10: one row per field per run.
        UniqueConstraint(
            "organization_id",
            "extraction_run_id",
            "field_path",
            name="uq_extraction_fields_org_run_field_path",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_extraction_fields_confidence_bounds"
        ),
        CheckConstraint(
            "target_line_index IS NULL OR target_line_index >= 0",
            name="ck_extraction_fields_target_line_index_nonneg",
        ),
        CheckConstraint(
            "source_page IS NULL OR source_page > 0",
            name="ck_extraction_fields_source_page_pos",
        ),
        # module docstring point 11.
        CheckConstraint(
            "(confirmed_by_id IS NULL) = (confirmed_at IS NULL)",
            name="ck_extraction_fields_confirmed_by_paired",
        ),
        CheckConstraint(
            "is_confirmed = (confirmed_at IS NOT NULL)",
            name="ck_extraction_fields_is_confirmed_matches_confirmed_at",
        ),
    )

    # part of composite FK above, not a single-column ForeignKey
    extraction_run_id: Mapped[uuid.UUID] = mapped_column()
    field_path: Mapped[str] = mapped_column(Text())  # e.g. "quote.lines[0].unit_price"
    target_entity: Mapped[str] = mapped_column(Text())  # "quote|quote_line|quote_terms"
    target_line_index: Mapped[int | None] = mapped_column(Integer(), default=None)
    field_name: Mapped[str] = mapped_column(Text())
    raw_value: Mapped[str | None] = mapped_column(Text(), default=None)
    normalized_value: Mapped[str | None] = mapped_column(Text(), default=None)
    value_type: Mapped[str] = mapped_column(Text())  # "money|decimal|date|text|enum|currency"
    confidence: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    band: Mapped[ConfidenceBand] = mapped_column(CONFIDENCE_BAND_ENUM)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean(), default=False)
    is_confirmed: Mapped[bool] = mapped_column(Boolean(), default=False)
    confirmed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    source_page: Mapped[int | None] = mapped_column(SmallInteger(), default=None)
    source_bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    injection_flagged: Mapped[bool] = mapped_column(Boolean(), default=False)


class QuoteCorrection(OrgOwnedBase):
    """An explicit before/after correction log entry (04-document-pipeline.md
    §8: "every edit writes quote_corrections ... and an audit_event"). See
    module docstring point 8 for why `extraction_field_id` is nullable. No
    timestamp/version/archive mixins beyond its own `corrected_at`: the same
    append-only-by-convention shape as `RfqStatusHistory`
    (app/models/rfqs.py) - rows here are never updated."""

    __tablename__ = "quote_corrections"
    __table_args__ = (
        org_identity_constraint("quote_corrections"),
        # composite org FK, nullable: module docstring point 8a - a
        # correction made before materialization has no quote yet.
        ForeignKeyConstraint(
            ["organization_id", "quote_id"],
            ["quotes.organization_id", "quotes.id"],
            ondelete="RESTRICT",
            name="fk_quote_corrections_organization_id_quote_id",
        ),
        # composite org FK, nullable: module docstring point 8.
        ForeignKeyConstraint(
            ["organization_id", "extraction_field_id"],
            ["extraction_fields.organization_id", "extraction_fields.id"],
            ondelete="RESTRICT",
            name="fk_quote_corrections_organization_id_extraction_field_id",
        ),
        # composite org FK, nullable: module docstring point 8a.
        ForeignKeyConstraint(
            ["organization_id", "extraction_run_id"],
            ["extraction_runs.organization_id", "extraction_runs.id"],
            ondelete="RESTRICT",
            name="fk_quote_corrections_organization_id_extraction_run_id",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey; nullable
    # per module docstring point 8a
    quote_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    # part of composite FK #2 above, not a single-column ForeignKey; nullable
    extraction_field_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    # part of composite FK #3 above, not a single-column ForeignKey; nullable
    # per module docstring point 8a
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    target_table: Mapped[str] = mapped_column(Text())
    target_row_id: Mapped[uuid.UUID] = mapped_column()
    field_name: Mapped[str] = mapped_column(Text())
    before_value: Mapped[str | None] = mapped_column(Text(), default=None)
    after_value: Mapped[str | None] = mapped_column(Text(), default=None)
    reason: Mapped[str | None] = mapped_column(Text(), default=None)
    corrected_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    corrected_at: Mapped[datetime] = mapped_column()


class PartMatchCandidate(OrgOwnedBase):
    """One candidate (of possibly several, all kept - SPEC §8) part match for
    one quote line against one RFQ line (04-document-pipeline.md §10). No
    timestamp/version/archive mixins beyond its own `confirmed_at`: 02-erd.md
    §6 lists none else for `PART_MATCH_CANDIDATES`."""

    __tablename__ = "part_match_candidates"
    __table_args__ = (
        org_identity_constraint("part_match_candidates"),
        # composite org FK: the quote line being matched.
        ForeignKeyConstraint(
            ["organization_id", "quote_line_id"],
            ["quote_lines.organization_id", "quote_lines.id"],
            ondelete="RESTRICT",
            name="fk_part_match_candidates_organization_id_quote_line_id",
        ),
        # composite org FK: the RFQ line this candidate targets.
        ForeignKeyConstraint(
            ["organization_id", "rfq_line_id"],
            ["rfq_lines.organization_id", "rfq_lines.id"],
            ondelete="RESTRICT",
            name="fk_part_match_candidates_organization_id_rfq_line_id",
        ),
        # composite org FK: the candidate part.
        ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_part_match_candidates_organization_id_part_id",
        ),
        # module docstring point 10: the delegating task's own suggested
        # uniqueness, "unique per (line, part, strategy)".
        UniqueConstraint(
            "organization_id",
            "quote_line_id",
            "part_id",
            "strategy",
            name="uq_part_match_candidates_org_line_part_strategy",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_part_match_candidates_confidence_bounds",
        ),
        CheckConstraint("rank >= 1", name="ck_part_match_candidates_rank_pos"),
        # module docstring point 11.
        CheckConstraint(
            "(confirmed_by_id IS NULL) = (confirmed_at IS NULL)",
            name="ck_part_match_candidates_confirmed_by_paired",
        ),
        CheckConstraint(
            "human_confirmed = (confirmed_at IS NOT NULL)",
            name="ck_part_match_candidates_human_confirmed_matches_confirmed_at",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    quote_line_id: Mapped[uuid.UUID] = mapped_column()
    # part of composite FK #2 above, not a single-column ForeignKey
    rfq_line_id: Mapped[uuid.UUID] = mapped_column()
    # part of composite FK #3 above, not a single-column ForeignKey
    part_id: Mapped[uuid.UUID] = mapped_column()
    strategy: Mapped[MatchStrategy] = mapped_column(MATCH_STRATEGY_ENUM)
    confidence: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    # nullable: "every non-exact candidate records ... explanation"
    # (04-document-pipeline.md §10) implies strategy 1 (exact) may not.
    explanation: Mapped[str | None] = mapped_column(Text(), default=None)
    rank: Mapped[int] = mapped_column(Integer())
    is_selected: Mapped[bool] = mapped_column(Boolean(), default=False)
    human_confirmed: Mapped[bool] = mapped_column(Boolean(), default=False)
    confirmed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)


# -- indexes not already implied by a UniqueConstraint/PK ------------------
#
# 02-erd.md §9's index plan, transcribed exactly, is created in the migration
# (Alembic `op.create_index`), not here - the same split every other model
# module in this schema already follows (constraints/FKs live on the mapped
# class, plain performance indexes live in the migration). See
# migrations/versions/0010_documents.py.


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in several FKs on most mappers here (the plain org FK plus one
# or more composite FKs each), so `foreign_keys` disambiguates which
# constraint each relationship follows, and `overlaps` silences SQLAlchemy's
# warning about the shared organization_id column between them - the same
# pattern as quotes.py/rfqs.py/boms.py.
QuoteDocument.organization = relationship(
    "Organization", foreign_keys=[QuoteDocument.organization_id], lazy="select"
)
QuoteDocument.rfq = relationship(
    "Rfq",
    foreign_keys=[QuoteDocument.organization_id, QuoteDocument.rfq_id],
    lazy="select",
    overlaps="organization",
)
QuoteDocument.supplier = relationship(
    "Supplier",
    foreign_keys=[QuoteDocument.organization_id, QuoteDocument.supplier_id],
    lazy="select",
    overlaps="organization,rfq",
)
QuoteDocument.uploaded_by = relationship(
    "User", foreign_keys=[QuoteDocument.uploaded_by_id], lazy="select"
)

DocumentPage.organization = relationship(
    "Organization", foreign_keys=[DocumentPage.organization_id], lazy="select"
)
DocumentPage.document = relationship(
    "QuoteDocument",
    foreign_keys=[DocumentPage.organization_id, DocumentPage.document_id],
    lazy="select",
    overlaps="organization",
)

ExtractionRun.organization = relationship(
    "Organization", foreign_keys=[ExtractionRun.organization_id], lazy="select"
)
ExtractionRun.document = relationship(
    "QuoteDocument",
    foreign_keys=[ExtractionRun.organization_id, ExtractionRun.document_id],
    lazy="select",
    overlaps="organization",
)
ExtractionRun.started_by = relationship(
    "User", foreign_keys=[ExtractionRun.started_by_id], lazy="select"
)

ExtractionField.organization = relationship(
    "Organization", foreign_keys=[ExtractionField.organization_id], lazy="select"
)
ExtractionField.extraction_run = relationship(
    "ExtractionRun",
    foreign_keys=[ExtractionField.organization_id, ExtractionField.extraction_run_id],
    lazy="select",
    overlaps="organization",
)
ExtractionField.confirmed_by = relationship(
    "User", foreign_keys=[ExtractionField.confirmed_by_id], lazy="select"
)

QuoteCorrection.organization = relationship(
    "Organization", foreign_keys=[QuoteCorrection.organization_id], lazy="select"
)
QuoteCorrection.quote = relationship(
    "Quote",
    foreign_keys=[QuoteCorrection.organization_id, QuoteCorrection.quote_id],
    lazy="select",
    overlaps="organization",
)
QuoteCorrection.extraction_field = relationship(
    "ExtractionField",
    foreign_keys=[QuoteCorrection.organization_id, QuoteCorrection.extraction_field_id],
    lazy="select",
    overlaps="organization,quote",
)
QuoteCorrection.extraction_run = relationship(
    "ExtractionRun",
    foreign_keys=[QuoteCorrection.organization_id, QuoteCorrection.extraction_run_id],
    lazy="select",
    overlaps="organization,quote,extraction_field",
)
QuoteCorrection.corrected_by = relationship(
    "User", foreign_keys=[QuoteCorrection.corrected_by_id], lazy="select"
)

PartMatchCandidate.organization = relationship(
    "Organization", foreign_keys=[PartMatchCandidate.organization_id], lazy="select"
)
PartMatchCandidate.quote_line = relationship(
    "QuoteLine",
    foreign_keys=[PartMatchCandidate.organization_id, PartMatchCandidate.quote_line_id],
    lazy="select",
    overlaps="organization",
)
PartMatchCandidate.rfq_line = relationship(
    "RfqLine",
    foreign_keys=[PartMatchCandidate.organization_id, PartMatchCandidate.rfq_line_id],
    lazy="select",
    overlaps="organization,quote_line",
)
PartMatchCandidate.part = relationship(
    "Part",
    foreign_keys=[PartMatchCandidate.organization_id, PartMatchCandidate.part_id],
    lazy="select",
    overlaps="organization,quote_line,rfq_line",
)
PartMatchCandidate.confirmed_by = relationship(
    "User", foreign_keys=[PartMatchCandidate.confirmed_by_id], lazy="select"
)

__all__ = [
    "ALLOWED_EXTRACTION_RUN_TRANSITIONS",
    "CONFIDENCE_BAND_ENUM",
    "DOCUMENT_STATE_ENUM",
    "EXTRACTION_STATE_ENUM",
    "MATCH_STRATEGY_ENUM",
    "DocumentPage",
    "DocumentState",
    "ExtractionField",
    "ExtractionRun",
    "ExtractionRunState",
    "MatchStrategy",
    "PartMatchCandidate",
    "QuoteCorrection",
    "QuoteDocument",
]
