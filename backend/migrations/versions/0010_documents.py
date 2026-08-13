"""quote_documents, document_pages, extraction_runs, extraction_fields,
quote_corrections, part_match_candidates

See app/models/documents.py module docstring for the deliberate ERD
deviations (quote_documents field-naming/nullability, document_pages having
no provenance bbox, extraction_runs having no self-referential "supersedes"
FK, NUMERIC(9,6) confidence columns, part_match_candidates.strategy's literal
ERD spelling, quote_corrections.extraction_field_id nullability, the single
CASCADE pair vs. everything else RESTRICT, and the additional uniqueness/
indexes/confirmation-pairing CHECKs beyond 02-erd.md §8's literal text).

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE document_state_enum AS ENUM"
        " ('uploaded','validated','stored','processing','extracted',"
        "'in_review','confirmed','quarantined','archived')"
    )
    op.execute(
        "CREATE TYPE extraction_state_enum AS ENUM"
        " ('queued','running','failed_transient','failed','extracted',"
        "'needs_review','ready','materialized','superseded')"
    )
    op.execute("CREATE TYPE confidence_band_enum AS ENUM ('high','medium','low')")
    op.execute(
        "CREATE TYPE match_strategy_enum AS ENUM"
        " ('internal_pn','mpn','normalized_text','alternative','fuzzy')"
    )

    # -- quote_documents ----------------------------------------------
    op.create_table(
        "quote_documents",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rfq_id", pg.UUID(as_uuid=True), nullable=False),
        # nullable: "nullable until identified" (02-erd.md §6).
        sa.Column("supplier_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        # nullable: populated at Stage 2 content validation.
        sa.Column("detected_mime", sa.Text(), nullable=True),
        sa.Column("declared_mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        # nullable: see app/models/documents.py module docstring point 2.
        sa.Column("content_sha256", sa.LargeBinary(), nullable=True),
        sa.Column(
            "state",
            pg.ENUM(
                "uploaded",
                "validated",
                "stored",
                "processing",
                "extracted",
                "in_review",
                "confirmed",
                "quarantined",
                "archived",
                name="document_state_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="uploaded",
        ),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        # nullable: populated at Stage 4 (text/table acquisition).
        sa.Column("page_count", sa.SmallInteger(), nullable=True),
        sa.Column(
            "uploaded_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("uploaded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "archived_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_quote_documents_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            ondelete="RESTRICT",
            name="fk_quote_documents_organization_id_rfq_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_quote_documents_organization_id_supplier_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "content_sha256", name="uq_quote_documents_org_sha256"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_quote_documents_size_bytes_nonneg"),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count >= 0",
            name="ck_quote_documents_page_count_nonneg",
        ),
    )
    op.create_index(
        "ix_quote_documents_organization_id", "quote_documents", ["organization_id"]
    )
    op.create_index(
        # 02-erd.md §9 ix_qd_rfq: inbox views.
        "ix_qd_rfq",
        "quote_documents",
        ["organization_id", "rfq_id", "uploaded_at"],
        postgresql_ops={"uploaded_at": "DESC"},
    )
    op.create_index(
        # 02-erd.md §9 ix_qd_state: inbox views.
        "ix_qd_state",
        "quote_documents",
        ["organization_id", "state"],
    )

    # -- document_pages -------------------------------------------------
    op.create_table(
        "document_pages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.SmallInteger(), nullable=False),
        sa.Column("text_layer", sa.Text(), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("ocr_confidence", sa.Numeric(9, 6), nullable=True),
        sa.Column("preview_storage_key", sa.Text(), nullable=True),
        sa.Column("extraction_source", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_document_pages_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["quote_documents.organization_id", "quote_documents.id"],
            ondelete="RESTRICT",
            name="fk_document_pages_organization_id_document_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "document_id",
            "page_number",
            name="uq_document_pages_org_doc_page",
        ),
        sa.CheckConstraint("page_number > 0", name="ck_document_pages_page_number_pos"),
        sa.CheckConstraint(
            "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)",
            name="ck_document_pages_ocr_confidence_bounds",
        ),
    )
    op.create_index(
        "ix_document_pages_organization_id", "document_pages", ["organization_id"]
    )

    # -- extraction_runs --------------------------------------------------
    op.create_table(
        "extraction_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            pg.ENUM(
                "queued",
                "running",
                "failed_transient",
                "failed",
                "extracted",
                "needs_review",
                "ready",
                "materialized",
                "superseded",
                name="extraction_state_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("provider_model", sa.Text(), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("provider_request_meta", pg.JSONB(), nullable=True),
        sa.Column("raw_response", pg.JSONB(), nullable=True),
        sa.Column("overall_confidence", sa.Numeric(9, 6), nullable=True),
        sa.Column("fields_requiring_review", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_extraction_runs_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["quote_documents.organization_id", "quote_documents.id"],
            ondelete="RESTRICT",
            name="fk_extraction_runs_organization_id_document_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "document_id",
            "run_number",
            name="uq_extraction_runs_org_doc_run",
        ),
        sa.CheckConstraint("run_number > 0", name="ck_extraction_runs_run_number_pos"),
        sa.CheckConstraint(
            "overall_confidence IS NULL OR (overall_confidence >= 0 AND overall_confidence <= 1)",
            name="ck_extraction_runs_overall_confidence_bounds",
        ),
        sa.CheckConstraint(
            "fields_requiring_review IS NULL OR fields_requiring_review >= 0",
            name="ck_extraction_runs_fields_requiring_review_nonneg",
        ),
    )
    op.create_index(
        "ix_extraction_runs_organization_id", "extraction_runs", ["organization_id"]
    )

    # -- extraction_fields ------------------------------------------------
    op.create_table(
        "extraction_fields",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("extraction_run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("target_entity", sa.Text(), nullable=False),
        sa.Column("target_line_index", sa.Integer(), nullable=True),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column("normalized_value", sa.Text(), nullable=True),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(9, 6), nullable=False),
        sa.Column(
            "band",
            pg.ENUM(
                "high", "medium", "low", name="confidence_band_enum", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "confirmed_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("source_page", sa.SmallInteger(), nullable=True),
        sa.Column("source_bbox", pg.JSONB(), nullable=True),
        sa.Column(
            "injection_flagged", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_extraction_fields_org_identity"
        ),
        # CASCADE: 02-erd.md §11's explicit whitelist - a field cannot exist
        # without its run.
        sa.ForeignKeyConstraint(
            ["organization_id", "extraction_run_id"],
            ["extraction_runs.organization_id", "extraction_runs.id"],
            ondelete="CASCADE",
            name="fk_extraction_fields_organization_id_extraction_run_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "extraction_run_id",
            "field_path",
            name="uq_extraction_fields_org_run_field_path",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_extraction_fields_confidence_bounds",
        ),
        sa.CheckConstraint(
            "target_line_index IS NULL OR target_line_index >= 0",
            name="ck_extraction_fields_target_line_index_nonneg",
        ),
        sa.CheckConstraint(
            "source_page IS NULL OR source_page > 0",
            name="ck_extraction_fields_source_page_pos",
        ),
        sa.CheckConstraint(
            "(confirmed_by_id IS NULL) = (confirmed_at IS NULL)",
            name="ck_extraction_fields_confirmed_by_paired",
        ),
        sa.CheckConstraint(
            "is_confirmed = (confirmed_at IS NOT NULL)",
            name="ck_extraction_fields_is_confirmed_matches_confirmed_at",
        ),
    )
    op.create_index(
        "ix_extraction_fields_organization_id", "extraction_fields", ["organization_id"]
    )
    op.create_index(
        # 02-erd.md §9 ix_ef_run: review queue join.
        "ix_ef_run",
        "extraction_fields",
        ["extraction_run_id"],
    )
    op.create_index(
        # 02-erd.md §9 ix_ef_review: review queue.
        "ix_ef_review",
        "extraction_fields",
        ["extraction_run_id"],
        postgresql_where=sa.text("requires_confirmation AND NOT is_confirmed"),
    )

    # -- quote_corrections --------------------------------------------------
    op.create_table(
        "quote_corrections",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quote_id", pg.UUID(as_uuid=True), nullable=False),
        # nullable: app/models/documents.py module docstring point 8.
        sa.Column("extraction_field_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("target_table", sa.Text(), nullable=False),
        sa.Column("target_row_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("before_value", sa.Text(), nullable=True),
        sa.Column("after_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "corrected_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("corrected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_quote_corrections_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_id"],
            ["quotes.organization_id", "quotes.id"],
            ondelete="RESTRICT",
            name="fk_quote_corrections_organization_id_quote_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "extraction_field_id"],
            ["extraction_fields.organization_id", "extraction_fields.id"],
            ondelete="RESTRICT",
            name="fk_quote_corrections_organization_id_extraction_field_id",
        ),
    )
    op.create_index(
        "ix_quote_corrections_organization_id", "quote_corrections", ["organization_id"]
    )
    op.create_index(
        # module docstring point 10: the review UI's own cheap-query need
        # (02-erd.md §2's stated reason for this table's existence).
        "ix_quote_corrections_quote",
        "quote_corrections",
        ["organization_id", "quote_id", "corrected_at"],
        postgresql_ops={"corrected_at": "DESC"},
    )

    # -- part_match_candidates ----------------------------------------------
    op.create_table(
        "part_match_candidates",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quote_line_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("rfq_line_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("part_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "strategy",
            pg.ENUM(
                "internal_pn",
                "mpn",
                "normalized_text",
                "alternative",
                "fuzzy",
                name="match_strategy_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(9, 6), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "human_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "confirmed_by_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_part_match_candidates_org_identity"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_line_id"],
            ["quote_lines.organization_id", "quote_lines.id"],
            ondelete="RESTRICT",
            name="fk_part_match_candidates_organization_id_quote_line_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "rfq_line_id"],
            ["rfq_lines.organization_id", "rfq_lines.id"],
            ondelete="RESTRICT",
            name="fk_part_match_candidates_organization_id_rfq_line_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "part_id"],
            ["parts.organization_id", "parts.id"],
            ondelete="RESTRICT",
            name="fk_part_match_candidates_organization_id_part_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "quote_line_id",
            "part_id",
            "strategy",
            name="uq_part_match_candidates_org_line_part_strategy",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_part_match_candidates_confidence_bounds",
        ),
        sa.CheckConstraint("rank >= 1", name="ck_part_match_candidates_rank_pos"),
        sa.CheckConstraint(
            "(confirmed_by_id IS NULL) = (confirmed_at IS NULL)",
            name="ck_part_match_candidates_confirmed_by_paired",
        ),
        sa.CheckConstraint(
            "human_confirmed = (confirmed_at IS NOT NULL)",
            name="ck_part_match_candidates_human_confirmed_matches_confirmed_at",
        ),
    )
    op.create_index(
        "ix_part_match_candidates_organization_id",
        "part_match_candidates",
        ["organization_id"],
    )
    op.create_index(
        # 02-erd.md §9 ix_pmc_line_rank: match review.
        "ix_pmc_line_rank",
        "part_match_candidates",
        ["quote_line_id", "rank"],
    )
    op.create_index(
        # 02-erd.md §9 partial index: match review, unconfirmed only.
        "ix_pmc_unconfirmed",
        "part_match_candidates",
        ["quote_line_id"],
        postgresql_where=sa.text("NOT human_confirmed"),
    )


def downgrade() -> None:
    op.drop_table("part_match_candidates")
    op.drop_table("quote_corrections")
    op.drop_table("extraction_fields")
    op.drop_table("extraction_runs")
    op.drop_table("document_pages")
    op.drop_table("quote_documents")
    op.execute("DROP TYPE match_strategy_enum")
    op.execute("DROP TYPE confidence_band_enum")
    op.execute("DROP TYPE extraction_state_enum")
    op.execute("DROP TYPE document_state_enum")
