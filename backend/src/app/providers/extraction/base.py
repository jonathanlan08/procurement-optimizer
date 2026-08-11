"""Extraction provider contract + versioned payload schema — PRINCIPAL-OWNED.

Trust boundary rules (SPEC §Document and AI security; 04-document-pipeline.md):
- document content is DATA, never instructions: providers receive it inside a
  nonce-delimited envelope (see envelope.py) and must never execute tools,
  browse, or act on text found in documents;
- provider output is UNTRUSTED until it passes the validation ladder
  (schema -> type -> business -> confidence -> human confirmation);
- every numeric value is a decimal STRING in the payload (no floats);
- a field the document does not state is None with confidence omitted —
  providers must never invent values to complete the schema;
- the payload schema is versioned; any shape change bumps EXTRACTION_SCHEMA_VERSION.
"""

from __future__ import annotations

from typing import Final, Protocol

from pydantic import BaseModel, Field

EXTRACTION_SCHEMA_VERSION: Final[str] = "1.0"


class ExtractedField(BaseModel):
    """One extracted scalar: raw text as seen, normalized string form, and the
    provider's confidence in [0,1]. value None => not stated in the document."""

    value: str | None = None
    raw_text: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    page_number: int | None = Field(default=None, ge=1)


class ExtractedPriceBreak(BaseModel):
    min_quantity: ExtractedField
    max_quantity: ExtractedField
    unit_price: ExtractedField


class ExtractedLine(BaseModel):
    part_number: ExtractedField
    description: ExtractedField
    quantity: ExtractedField
    unit_of_measure: ExtractedField
    unit_price: ExtractedField
    moq: ExtractedField
    lead_time_days: ExtractedField
    country_of_origin: ExtractedField
    production_capacity: ExtractedField
    tooling_cost: ExtractedField
    setup_cost: ExtractedField
    packaging_cost: ExtractedField
    shipping_cost: ExtractedField
    insurance_cost: ExtractedField
    tariff_amount: ExtractedField
    duty_amount: ExtractedField
    customs_fees: ExtractedField
    tax_amount: ExtractedField
    price_breaks: list[ExtractedPriceBreak] = Field(default_factory=list)


class ExtractedTerms(BaseModel):
    payment_terms: ExtractedField
    shipping_terms: ExtractedField
    warranty: ExtractedField
    exceptions: ExtractedField
    exclusions: ExtractedField
    notes: ExtractedField


class ExtractedQuotePayload(BaseModel):
    """The complete structured result for one supplier quote document."""

    schema_version: str = EXTRACTION_SCHEMA_VERSION
    supplier_name: ExtractedField
    quote_number: ExtractedField
    quote_date: ExtractedField  # ISO date string when stated
    expiration_date: ExtractedField
    currency: ExtractedField  # ISO-4217 when stated
    lines: list[ExtractedLine] = Field(default_factory=list)
    terms: ExtractedTerms
    overall_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    provider_notes: str | None = None  # provider diagnostics; NEVER document text
    injection_suspected: bool = False  # set by the canary detector, not providers


class ExtractionProvider(Protocol):
    """Implementations: MockExtractionProvider (deterministic fixtures, default)
    and the optional Anthropic adapter (env-gated). The service layer, not the
    provider, runs the validation ladder and confidence banding."""

    name: str
    is_simulated: bool  # mock results are ALWAYS labelled simulated in the UI

    def extract(
        self,
        *,
        document_sha256: str,
        pages: list[str],  # page texts, already acquired upstream
    ) -> ExtractedQuotePayload: ...
