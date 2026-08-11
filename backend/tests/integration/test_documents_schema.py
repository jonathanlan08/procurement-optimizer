"""Document/extraction pipeline schema tests: migration coverage, composite-FK
org isolation on every new table's every composite FK (quote_documents ->
rfqs/suppliers, document_pages -> quote_documents, extraction_runs ->
quote_documents, extraction_fields -> extraction_runs, quote_corrections ->
quotes/extraction_fields, part_match_candidates -> quote_lines/rfq_lines/
parts), the extraction_runs -> extraction_fields CASCADE (02-erd.md §11's
whitelist), CHECK-constraint coverage (confidence bounds, size/page/rank
non-negativity, confirmation pairing), uniqueness (content_sha256 per org,
page_number per document, run_number per document, field_path per run,
line/part/strategy per org), and document_state_enum/extraction_state_enum/
confidence_band_enum/match_strategy_enum rejecting invalid values
(docs/planning/02-erd.md §6; docs/planning/04-document-pipeline.md).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError


def _mk_org(db, org_id, slug) -> None:
    db.execute(
        text(
            "INSERT INTO organizations (id, slug, name, base_currency, is_demo,"
            " version, created_at, updated_at)"
            " VALUES (:id, :slug, 'Test Org', 'USD', false, 1, :now, :now)"
        ),
        {"id": org_id, "slug": slug, "now": datetime.now(UTC)},
    )


def _mk_user(db, user_id, email) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO users (id, email, password_hash, full_name, is_active,"
            " created_at, updated_at)"
            " VALUES (:id, :email, 'x', 'Test User', true, :now, :now)"
        ),
        {"id": user_id, "email": email, "now": now},
    )


def _mk_unit_definition(db, unit_id, code="each") -> None:
    db.execute(
        text(
            "INSERT INTO unit_definitions (id, organization_id, code, display_name,"
            " dimension, to_canonical_factor, is_user_defined)"
            " VALUES (:id, NULL, :code, :code, 'count'::dimension_enum, 1, false)"
        ),
        {"id": unit_id, "code": code},
    )


def _mk_part(db, part_id, org_id, unit_id, internal_part_number, name="Test Part") -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO parts (id, organization_id, internal_part_number, name,"
            " unit_definition_id, is_active, version, created_at, updated_at)"
            " VALUES (:id, :org, :ipn, :name, :unit, true, 1, :now, :now)"
        ),
        {
            "id": part_id,
            "org": org_id,
            "ipn": internal_part_number,
            "name": name,
            "unit": unit_id,
            "now": now,
        },
    )


def _mk_supplier(db, supplier_id, org_id, code) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO suppliers (id, organization_id, code, name, country_code,"
            " created_at, updated_at)"
            " VALUES (:id, :org, :code, :code, 'US', :now, :now)"
        ),
        {"id": supplier_id, "org": org_id, "code": code, "now": now},
    )


def _mk_rfq(db, rfq_id, org_id, created_by_id, internal_reference) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO rfqs (id, organization_id, name, internal_reference,"
            " status, base_currency, due_date, created_by_id,"
            " version, created_at, updated_at)"
            " VALUES (:id, :org, :ref, :ref, 'draft'::rfq_status_enum,"
            " 'USD', :due, :creator, 1, :now, :now)"
        ),
        {
            "id": rfq_id,
            "org": org_id,
            "ref": internal_reference,
            "due": date(2026, 9, 1),
            "creator": created_by_id,
            "now": now,
        },
    )


def _mk_rfq_line(db, line_id, org_id, rfq_id, line_number, part_id, unit_id, quantity="1") -> None:
    db.execute(
        text(
            "INSERT INTO rfq_lines (id, organization_id, rfq_id, line_number,"
            " part_id, required_quantity, unit_definition_id)"
            " VALUES (:id, :org, :rfq, :line_no, :part, :qty, :unit)"
        ),
        {
            "id": line_id,
            "org": org_id,
            "rfq": rfq_id,
            "line_no": line_number,
            "part": part_id,
            "qty": quantity,
            "unit": unit_id,
        },
    )


def _mk_quote(db, quote_id, org_id, rfq_id, supplier_id, quote_date=date(2026, 8, 1)) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO quotes (id, organization_id, rfq_id, supplier_id,"
            " quote_date, currency, status, source,"
            " version, created_at, updated_at)"
            " VALUES (:id, :org, :rfq, :supplier, :quote_date, 'USD',"
            " 'draft'::quote_status_enum, 'manual'::quote_source_enum, 1, :now, :now)"
        ),
        {
            "id": quote_id,
            "org": org_id,
            "rfq": rfq_id,
            "supplier": supplier_id,
            "quote_date": quote_date,
            "now": now,
        },
    )


def _mk_quote_line(db, line_id, org_id, quote_id, line_number, unit_id, quantity="10") -> None:
    db.execute(
        text(
            "INSERT INTO quote_lines (id, organization_id, quote_id, line_number,"
            " quantity, unit_definition_id, match_status)"
            " VALUES (:id, :org, :quote, :line_no, :qty, :unit, 'unmatched'::match_status_enum)"
        ),
        {
            "id": line_id,
            "org": org_id,
            "quote": quote_id,
            "line_no": line_number,
            "qty": quantity,
            "unit": unit_id,
        },
    )


def _sha256(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def _mk_quote_document(
    db,
    doc_id,
    org_id,
    rfq_id,
    uploaded_by_id,
    supplier_id=None,
    sha256=None,
    state="uploaded",
    size_bytes=1024,
    original_filename="quote.pdf",
    storage_key=None,
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO quote_documents (id, organization_id, rfq_id, supplier_id,"
            " original_filename, storage_key, detected_mime, declared_mime,"
            " size_bytes, content_sha256, state, page_count, uploaded_by_id, uploaded_at)"
            " VALUES (:id, :org, :rfq, :supplier, :filename, :storage_key,"
            " 'application/pdf', 'application/pdf', :size, :sha,"
            " CAST(:state AS document_state_enum), NULL, :uploaded_by, :now)"
        ),
        {
            "id": doc_id,
            "org": org_id,
            "rfq": rfq_id,
            "supplier": supplier_id,
            "filename": original_filename,
            "storage_key": storage_key or f"orgs/{org_id}/documents/{doc_id}/original.pdf",
            "size": size_bytes,
            "sha": sha256,
            "state": state,
            "uploaded_by": uploaded_by_id,
            "now": now,
        },
    )


def _mk_document_page(
    db,
    page_id,
    org_id,
    document_id,
    page_number,
    ocr_confidence=None,
    extraction_source="text_layer",
) -> None:
    db.execute(
        text(
            "INSERT INTO document_pages (id, organization_id, document_id, page_number,"
            " text_layer, ocr_confidence, extraction_source)"
            " VALUES (:id, :org, :doc, :page_no, 'sample text', :ocr_conf, :source)"
        ),
        {
            "id": page_id,
            "org": org_id,
            "doc": document_id,
            "page_no": page_number,
            "ocr_conf": ocr_confidence,
            "source": extraction_source,
        },
    )


def _mk_extraction_run(
    db,
    run_id,
    org_id,
    document_id,
    started_by_id,
    run_number=1,
    state="queued",
    overall_confidence=None,
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO extraction_runs (id, organization_id, document_id, run_number,"
            " state, provider_name, schema_version, prompt_version, overall_confidence,"
            " started_by_id, started_at)"
            " VALUES (:id, :org, :doc, :run_no, CAST(:state AS extraction_state_enum),"
            " 'mock', 'v1', 'v1', :conf, :started_by, :now)"
        ),
        {
            "id": run_id,
            "org": org_id,
            "doc": document_id,
            "run_no": run_number,
            "state": state,
            "conf": overall_confidence,
            "started_by": started_by_id,
            "now": now,
        },
    )


def _mk_extraction_field(
    db,
    field_id,
    org_id,
    extraction_run_id,
    field_path="quote.lines[0].unit_price",
    confidence="0.97",
    band="high",
    is_confirmed=False,
    confirmed_by_id=None,
    confirmed_at=None,
) -> None:
    db.execute(
        text(
            "INSERT INTO extraction_fields (id, organization_id, extraction_run_id,"
            " field_path, target_entity, field_name, raw_value, normalized_value,"
            " value_type, confidence, band, is_confirmed, confirmed_by_id, confirmed_at)"
            " VALUES (:id, :org, :run, :path, 'quote_line', 'unit_price', '1,234.00',"
            " '1234.00', 'money', :conf, CAST(:band AS confidence_band_enum),"
            " :is_confirmed, :confirmed_by, :confirmed_at)"
        ),
        {
            "id": field_id,
            "org": org_id,
            "run": extraction_run_id,
            "path": field_path,
            "conf": confidence,
            "band": band,
            "is_confirmed": is_confirmed,
            "confirmed_by": confirmed_by_id,
            "confirmed_at": confirmed_at,
        },
    )


def _mk_quote_correction(
    db,
    correction_id,
    org_id,
    quote_id,
    corrected_by_id,
    extraction_field_id=None,
    target_row_id=None,
) -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO quote_corrections (id, organization_id, quote_id,"
            " extraction_field_id, target_table, target_row_id, field_name,"
            " before_value, after_value, corrected_by_id, corrected_at)"
            " VALUES (:id, :org, :quote, :field, 'quote_lines', :row, 'unit_price',"
            " '1234.00', '1250.00', :corrected_by, :now)"
        ),
        {
            "id": correction_id,
            "org": org_id,
            "quote": quote_id,
            "field": extraction_field_id,
            "row": target_row_id or quote_id,
            "corrected_by": corrected_by_id,
            "now": now,
        },
    )


def _mk_part_match_candidate(
    db,
    candidate_id,
    org_id,
    quote_line_id,
    rfq_line_id,
    part_id,
    strategy="internal_pn",
    confidence="1.0",
    rank=1,
    human_confirmed=False,
    confirmed_by_id=None,
    confirmed_at=None,
) -> None:
    db.execute(
        text(
            "INSERT INTO part_match_candidates (id, organization_id, quote_line_id,"
            " rfq_line_id, part_id, strategy, confidence, rank, human_confirmed,"
            " confirmed_by_id, confirmed_at)"
            " VALUES (:id, :org, :line, :rfq_line, :part,"
            " CAST(:strategy AS match_strategy_enum), :conf, :rank, :human_confirmed,"
            " :confirmed_by, :confirmed_at)"
        ),
        {
            "id": candidate_id,
            "org": org_id,
            "line": quote_line_id,
            "rfq_line": rfq_line_id,
            "part": part_id,
            "strategy": strategy,
            "conf": confidence,
            "rank": rank,
            "human_confirmed": human_confirmed,
            "confirmed_by": confirmed_by_id,
            "confirmed_at": confirmed_at,
        },
    )


class _Fixture:
    """Bundles one org's worth of parent rows the document pipeline needs."""

    def __init__(self, db, make_uuid, label="org") -> None:
        self.db = db
        self.make_uuid = make_uuid
        org_id = make_uuid.new_id()
        self.org_id = org_id
        _mk_org(db, org_id, f"{label}-{org_id}")
        self.user_id = make_uuid.new_id()
        _mk_user(db, self.user_id, f"user-{self.user_id}@example.test")
        self.unit_id = make_uuid.new_id()
        _mk_unit_definition(db, self.unit_id, code=f"each-{org_id}")
        self.part_id = make_uuid.new_id()
        _mk_part(db, self.part_id, org_id, self.unit_id, f"IPN-{org_id}")
        self.supplier_id = make_uuid.new_id()
        _mk_supplier(db, self.supplier_id, org_id, f"SUP-{org_id}")
        self.rfq_id = make_uuid.new_id()
        _mk_rfq(db, self.rfq_id, org_id, self.user_id, f"RFQ-{org_id}")
        self.rfq_line_id = make_uuid.new_id()
        _mk_rfq_line(db, self.rfq_line_id, org_id, self.rfq_id, 1, self.part_id, self.unit_id)
        self.quote_id = make_uuid.new_id()
        _mk_quote(db, self.quote_id, org_id, self.rfq_id, self.supplier_id)
        self.quote_line_id = make_uuid.new_id()
        _mk_quote_line(db, self.quote_line_id, org_id, self.quote_id, 1, self.unit_id)

    def mk_document(self, **kwargs):
        doc_id = self.make_uuid.new_id()
        _mk_quote_document(self.db, doc_id, self.org_id, self.rfq_id, self.user_id, **kwargs)
        return doc_id

    def mk_page(self, document_id, page_number=1, **kwargs):
        page_id = self.make_uuid.new_id()
        _mk_document_page(self.db, page_id, self.org_id, document_id, page_number, **kwargs)
        return page_id

    def mk_run(self, document_id, **kwargs):
        run_id = self.make_uuid.new_id()
        _mk_extraction_run(self.db, run_id, self.org_id, document_id, self.user_id, **kwargs)
        return run_id

    def mk_field(self, extraction_run_id, **kwargs):
        field_id = self.make_uuid.new_id()
        _mk_extraction_field(self.db, field_id, self.org_id, extraction_run_id, **kwargs)
        return field_id

    def mk_correction(self, quote_id=None, **kwargs):
        correction_id = self.make_uuid.new_id()
        _mk_quote_correction(
            self.db, correction_id, self.org_id, quote_id or self.quote_id, self.user_id, **kwargs
        )
        return correction_id

    def mk_candidate(self, quote_line_id=None, rfq_line_id=None, part_id=None, **kwargs):
        candidate_id = self.make_uuid.new_id()
        _mk_part_match_candidate(
            self.db,
            candidate_id,
            self.org_id,
            quote_line_id or self.quote_line_id,
            rfq_line_id or self.rfq_line_id,
            part_id or self.part_id,
            **kwargs,
        )
        return candidate_id


@pytest.fixture()
def fx(db, make_uuid):
    return _Fixture(db, make_uuid, label="org-a")


class TestDocumentsSchema:
    def test_migration_creates_document_tables(self, db) -> None:
        rows = (
            db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name IN"
                    " ('quote_documents', 'document_pages', 'extraction_runs',"
                    " 'extraction_fields', 'quote_corrections', 'part_match_candidates')"
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {
            "quote_documents",
            "document_pages",
            "extraction_runs",
            "extraction_fields",
            "quote_corrections",
            "part_match_candidates",
        }

    # -- quote_documents ---------------------------------------------------

    def test_composite_fk_rejects_cross_org_document_to_rfq(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            _mk_quote_document(
                db, make_uuid.new_id(), org_a.org_id, org_b.rfq_id, org_a.user_id
            )

    def test_composite_fk_rejects_cross_org_document_to_supplier(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            _mk_quote_document(
                db,
                make_uuid.new_id(),
                org_a.org_id,
                org_a.rfq_id,
                org_a.user_id,
                supplier_id=org_b.supplier_id,
            )

    def test_composite_fk_accepts_same_org_document(self, fx) -> None:
        doc_id = fx.mk_document()
        count = fx.db.execute(
            text("SELECT count(*) FROM quote_documents WHERE id = :id"), {"id": doc_id}
        ).scalar_one()
        assert count == 1

    def test_document_supplier_nullable_until_identified(self, fx) -> None:
        doc_id = fx.mk_document(supplier_id=None)
        supplier_id = fx.db.execute(
            text("SELECT supplier_id FROM quote_documents WHERE id = :id"), {"id": doc_id}
        ).scalar_one()
        assert supplier_id is None

    def test_content_sha256_unique_per_org(self, fx) -> None:
        sha = _sha256("duplicate-upload")
        fx.mk_document(sha256=sha)
        with pytest.raises(IntegrityError):
            fx.mk_document(sha256=sha)

    def test_content_sha256_duplicate_allowed_across_orgs(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        sha = _sha256("cross-org-same-bytes")

        org_a.mk_document(sha256=sha)
        org_b.mk_document(sha256=sha)  # must not raise

        count = db.execute(text("SELECT count(*) FROM quote_documents")).scalar_one()
        assert count == 2

    def test_content_sha256_nullable_before_computed(self, fx) -> None:
        """module docstring point 2: a document can exist (e.g. quarantined
        at Stage 1) before its hash is ever computed."""
        doc_id = fx.mk_document(sha256=None, state="uploaded")
        sha = fx.db.execute(
            text("SELECT content_sha256 FROM quote_documents WHERE id = :id"), {"id": doc_id}
        ).scalar_one()
        assert sha is None

    def test_size_bytes_check_rejects_negative(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_document(size_bytes=-1)

    def test_document_state_enum_rejects_invalid_value(self, fx) -> None:
        with pytest.raises(DataError):
            fx.mk_document(state="not_a_real_state")

    # -- document_pages -----------------------------------------------------

    def test_composite_fk_rejects_cross_org_page_to_document(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        doc_b = org_b.mk_document()

        with pytest.raises(IntegrityError):
            _mk_document_page(db, make_uuid.new_id(), org_a.org_id, doc_b, 1)

    def test_composite_fk_accepts_same_org_page(self, fx) -> None:
        doc_id = fx.mk_document()
        page_id = fx.mk_page(doc_id)
        count = fx.db.execute(
            text("SELECT count(*) FROM document_pages WHERE id = :id"), {"id": page_id}
        ).scalar_one()
        assert count == 1

    def test_page_number_unique_per_document(self, fx) -> None:
        doc_id = fx.mk_document()
        fx.mk_page(doc_id, page_number=1)
        with pytest.raises(IntegrityError):
            fx.mk_page(doc_id, page_number=1)

    def test_page_number_check_rejects_zero(self, fx) -> None:
        doc_id = fx.mk_document()
        with pytest.raises(IntegrityError):
            fx.mk_page(doc_id, page_number=0)

    def test_ocr_confidence_check_rejects_out_of_bounds(self, fx) -> None:
        doc_id = fx.mk_document()
        with pytest.raises(IntegrityError):
            fx.mk_page(doc_id, ocr_confidence="1.5")

    def test_ocr_confidence_nullable_for_native_text(self, fx) -> None:
        doc_id = fx.mk_document()
        page_id = fx.mk_page(doc_id, ocr_confidence=None, extraction_source="text_layer")
        ocr_conf = fx.db.execute(
            text("SELECT ocr_confidence FROM document_pages WHERE id = :id"), {"id": page_id}
        ).scalar_one()
        assert ocr_conf is None

    # -- extraction_runs ------------------------------------------------

    def test_composite_fk_rejects_cross_org_run_to_document(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        doc_b = org_b.mk_document()

        with pytest.raises(IntegrityError):
            _mk_extraction_run(db, make_uuid.new_id(), org_a.org_id, doc_b, org_a.user_id)

    def test_composite_fk_accepts_same_org_run(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        count = fx.db.execute(
            text("SELECT count(*) FROM extraction_runs WHERE id = :id"), {"id": run_id}
        ).scalar_one()
        assert count == 1

    def test_run_number_unique_per_document(self, fx) -> None:
        doc_id = fx.mk_document()
        fx.mk_run(doc_id, run_number=1)
        with pytest.raises(IntegrityError):
            fx.mk_run(doc_id, run_number=1)

    def test_run_number_allowed_to_increment_on_reextraction(self, fx) -> None:
        doc_id = fx.mk_document()
        fx.mk_run(doc_id, run_number=1, state="superseded")
        second_run = fx.mk_run(doc_id, run_number=2)
        count = fx.db.execute(
            text("SELECT count(*) FROM extraction_runs WHERE id = :id"), {"id": second_run}
        ).scalar_one()
        assert count == 1

    def test_run_number_check_rejects_zero(self, fx) -> None:
        doc_id = fx.mk_document()
        with pytest.raises(IntegrityError):
            fx.mk_run(doc_id, run_number=0)

    def test_overall_confidence_check_rejects_out_of_bounds(self, fx) -> None:
        doc_id = fx.mk_document()
        with pytest.raises(IntegrityError):
            fx.mk_run(doc_id, overall_confidence="-0.1")

    def test_extraction_state_enum_rejects_invalid_value(self, fx) -> None:
        doc_id = fx.mk_document()
        with pytest.raises(DataError):
            fx.mk_run(doc_id, state="not_a_real_state")

    # -- extraction_fields ------------------------------------------------

    def test_composite_fk_rejects_cross_org_field_to_run(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        doc_b = org_b.mk_document()
        run_b = org_b.mk_run(doc_b)

        with pytest.raises(IntegrityError):
            _mk_extraction_field(db, make_uuid.new_id(), org_a.org_id, run_b)

    def test_composite_fk_accepts_same_org_field(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        field_id = fx.mk_field(run_id)
        count = fx.db.execute(
            text("SELECT count(*) FROM extraction_fields WHERE id = :id"), {"id": field_id}
        ).scalar_one()
        assert count == 1

    def test_field_path_unique_per_run(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        fx.mk_field(run_id, field_path="quote.lines[0].unit_price")
        with pytest.raises(IntegrityError):
            fx.mk_field(run_id, field_path="quote.lines[0].unit_price")

    def test_extraction_fields_cascade_on_run_delete(self, fx) -> None:
        """02-erd.md §11's explicit whitelist: extraction_runs ->
        extraction_fields is a CASCADE pair — a field cannot exist without
        the run that produced it."""
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        field_id = fx.mk_field(run_id)

        fx.db.execute(text("DELETE FROM extraction_runs WHERE id = :id"), {"id": run_id})

        count = fx.db.execute(
            text("SELECT count(*) FROM extraction_fields WHERE id = :id"), {"id": field_id}
        ).scalar_one()
        assert count == 0

    def test_confidence_check_rejects_out_of_bounds(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        with pytest.raises(IntegrityError):
            fx.mk_field(run_id, confidence="1.5")

    def test_missing_field_represented_as_null_raw_and_normalized_value(self, fx) -> None:
        """04-document-pipeline.md: 'Missing keys become MISSING, never 0 or
        \"\"' — modeled here as NULL raw_value/normalized_value with a low
        band and zero confidence, not a coerced default."""
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        field_id = fx.make_uuid.new_id()
        fx.db.execute(
            text(
                "INSERT INTO extraction_fields (id, organization_id, extraction_run_id,"
                " field_path, target_entity, field_name, raw_value, normalized_value,"
                " value_type, confidence, band, requires_confirmation)"
                " VALUES (:id, :org, :run, 'quote.currency', 'quote', 'currency',"
                " NULL, NULL, 'currency', 0, CAST('low' AS confidence_band_enum), true)"
            ),
            {"id": field_id, "org": fx.org_id, "run": run_id},
        )
        row = fx.db.execute(
            text(
                "SELECT raw_value, normalized_value FROM extraction_fields WHERE id = :id"
            ),
            {"id": field_id},
        ).one()
        assert row.raw_value is None
        assert row.normalized_value is None

    def test_confirmed_by_and_confirmed_at_must_be_paired(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        with pytest.raises(IntegrityError):
            # confirmed_by_id set without confirmed_at
            fx.mk_field(run_id, confirmed_by_id=fx.user_id, confirmed_at=None)

    def test_is_confirmed_must_match_confirmed_at_presence(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        with pytest.raises(IntegrityError):
            # is_confirmed true but no confirmed_at
            fx.mk_field(run_id, is_confirmed=True, confirmed_at=None)

    def test_confirmed_field_accepted_when_paired(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        now = datetime.now(UTC)
        field_id = fx.mk_field(
            run_id, is_confirmed=True, confirmed_by_id=fx.user_id, confirmed_at=now
        )
        count = fx.db.execute(
            text("SELECT count(*) FROM extraction_fields WHERE id = :id"), {"id": field_id}
        ).scalar_one()
        assert count == 1

    def test_confidence_band_enum_rejects_invalid_value(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        with pytest.raises(DataError):
            fx.mk_field(run_id, band="extremely_confident")

    # -- quote_corrections --------------------------------------------------

    def test_composite_fk_rejects_cross_org_correction_to_quote(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            _mk_quote_correction(
                db, make_uuid.new_id(), org_a.org_id, org_b.quote_id, org_a.user_id
            )

    def test_composite_fk_rejects_cross_org_correction_to_extraction_field(
        self, db, make_uuid
    ) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")
        doc_b = org_b.mk_document()
        run_b = org_b.mk_run(doc_b)
        field_b = org_b.mk_field(run_b)

        with pytest.raises(IntegrityError):
            org_a.mk_correction(extraction_field_id=field_b)

    def test_correction_accepted_with_extraction_field(self, fx) -> None:
        doc_id = fx.mk_document()
        run_id = fx.mk_run(doc_id)
        field_id = fx.mk_field(run_id)
        correction_id = fx.mk_correction(extraction_field_id=field_id)
        count = fx.db.execute(
            text("SELECT count(*) FROM quote_corrections WHERE id = :id"), {"id": correction_id}
        ).scalar_one()
        assert count == 1

    def test_correction_accepted_without_extraction_field(self, fx) -> None:
        """module docstring point 8: a manually-entered quote's line can be
        corrected with no extraction run/field to reference at all."""
        correction_id = fx.mk_correction(extraction_field_id=None)
        extraction_field_id = fx.db.execute(
            text("SELECT extraction_field_id FROM quote_corrections WHERE id = :id"),
            {"id": correction_id},
        ).scalar_one()
        assert extraction_field_id is None

    # -- part_match_candidates ------------------------------------------

    def test_composite_fk_rejects_cross_org_candidate_to_quote_line(
        self, db, make_uuid
    ) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            org_a.mk_candidate(quote_line_id=org_b.quote_line_id)

    def test_composite_fk_rejects_cross_org_candidate_to_rfq_line(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            org_a.mk_candidate(rfq_line_id=org_b.rfq_line_id)

    def test_composite_fk_rejects_cross_org_candidate_to_part(self, db, make_uuid) -> None:
        org_a = _Fixture(db, make_uuid, label="org-a")
        org_b = _Fixture(db, make_uuid, label="org-b")

        with pytest.raises(IntegrityError):
            org_a.mk_candidate(part_id=org_b.part_id)

    def test_composite_fk_accepts_same_org_candidate(self, fx) -> None:
        candidate_id = fx.mk_candidate()
        count = fx.db.execute(
            text("SELECT count(*) FROM part_match_candidates WHERE id = :id"),
            {"id": candidate_id},
        ).scalar_one()
        assert count == 1

    def test_candidate_unique_per_line_part_strategy(self, fx) -> None:
        fx.mk_candidate(strategy="fuzzy", rank=1)
        with pytest.raises(IntegrityError):
            fx.mk_candidate(strategy="fuzzy", rank=2)

    def test_candidate_same_line_part_different_strategy_allowed(self, fx) -> None:
        fx.mk_candidate(strategy="internal_pn", rank=1)
        second = fx.mk_candidate(strategy="fuzzy", rank=2)
        count = fx.db.execute(
            text("SELECT count(*) FROM part_match_candidates WHERE id = :id"), {"id": second}
        ).scalar_one()
        assert count == 1

    def test_confidence_check_rejects_out_of_bounds_on_candidate(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_candidate(confidence="1.01")

    def test_rank_check_rejects_zero(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_candidate(rank=0)

    def test_human_confirmed_must_match_confirmed_at_presence(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_candidate(human_confirmed=True, confirmed_at=None)

    def test_confirmed_by_and_confirmed_at_must_be_paired_on_candidate(self, fx) -> None:
        with pytest.raises(IntegrityError):
            fx.mk_candidate(confirmed_by_id=fx.user_id, confirmed_at=None)

    def test_confirmed_candidate_accepted_when_paired(self, fx) -> None:
        now = datetime.now(UTC)
        candidate_id = fx.mk_candidate(
            human_confirmed=True, confirmed_by_id=fx.user_id, confirmed_at=now
        )
        count = fx.db.execute(
            text("SELECT count(*) FROM part_match_candidates WHERE id = :id"),
            {"id": candidate_id},
        ).scalar_one()
        assert count == 1

    def test_match_strategy_enum_rejects_invalid_value(self, fx) -> None:
        with pytest.raises(DataError):
            fx.mk_candidate(strategy="ai_vibes")

    def test_explanation_nullable_for_exact_match(self, fx) -> None:
        """04-document-pipeline.md §10: only non-exact candidates are
        described as always carrying an explanation."""
        candidate_id = fx.mk_candidate(strategy="internal_pn")
        explanation = fx.db.execute(
            text("SELECT explanation FROM part_match_candidates WHERE id = :id"),
            {"id": candidate_id},
        ).scalar_one()
        assert explanation is None
