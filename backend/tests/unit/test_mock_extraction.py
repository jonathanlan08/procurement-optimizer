"""Unit tests for MockExtractionProvider (app.providers.extraction.mock): golden-fixture
round-trip against the committed synthetic documents, the injection acceptance test,
heuristic fallback for unrecognized document hashes, and determinism.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.money import parse_decimal
from app.ingestion.acquisition import acquire_pages
from app.ingestion.file_validation import DocumentKind
from app.providers.extraction.base import (
    ExtractedLine,
    ExtractedQuotePayload,
    ExtractedTerms,
)
from app.providers.extraction.mock import MockExtractionProvider

DOCUMENTS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "documents"
EXTRACTION_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "extraction"

FIXTURE_DOCUMENTS: dict[str, DocumentKind] = {
    "shenzhen_precision_quote.pdf": DocumentKind.PDF,
    "pacific_metal_quote.png": DocumentKind.PNG,
    "nordic_fastener_quote.csv": DocumentKind.CSV,
    "baltic_casting_quote.xlsx": DocumentKind.XLSX,
}

# ExtractedLine fields that carry a decimal-shaped value when stated (i.e. every field
# except identifiers/text fields and the price_breaks list itself).
_DECIMAL_LINE_FIELDS = (
    "quantity",
    "unit_price",
    "moq",
    "lead_time_days",
    "tooling_cost",
    "setup_cost",
    "packaging_cost",
    "shipping_cost",
    "insurance_cost",
    "tariff_amount",
    "duty_amount",
    "customs_fees",
    "tax_amount",
)


def _provider() -> MockExtractionProvider:
    return MockExtractionProvider(registry_dir=EXTRACTION_DIR)


def _sha256_of(filename: str) -> str:
    return hashlib.sha256((DOCUMENTS_DIR / filename).read_bytes()).hexdigest()


def _pages_for(filename: str, kind: DocumentKind) -> list[str]:
    data = (DOCUMENTS_DIR / filename).read_bytes()
    return [p.text for p in acquire_pages(data, kind)]


def _extract_fixture(filename: str, kind: DocumentKind) -> ExtractedQuotePayload:
    sha256 = _sha256_of(filename)
    pages = _pages_for(filename, kind)
    return _provider().extract(document_sha256=sha256, pages=pages)


def _decimal_values_of_line(line: ExtractedLine) -> list[str]:
    values = [
        getattr(line, name).value
        for name in _DECIMAL_LINE_FIELDS
        if getattr(line, name).value is not None
    ]
    for price_break in line.price_breaks:
        for name in ("min_quantity", "max_quantity", "unit_price"):
            value = getattr(price_break, name).value
            if value is not None:
                values.append(value)
    return values


def _all_scalar_field_confidences(payload: ExtractedQuotePayload) -> list[float]:
    confidences = [
        payload.supplier_name.confidence,
        payload.quote_number.confidence,
        payload.quote_date.confidence,
        payload.expiration_date.confidence,
        payload.currency.confidence,
    ]
    for line in payload.lines:
        for field_name in ExtractedLine.model_fields:
            if field_name == "price_breaks":
                continue
            confidences.append(getattr(line, field_name).confidence)
    for term_name in ExtractedTerms.model_fields:
        confidences.append(getattr(payload.terms, term_name).confidence)
    return confidences


class TestGoldenFixtureRoundTrip:
    @pytest.mark.parametrize("filename,kind", FIXTURE_DOCUMENTS.items())
    def test_golden_payload_is_schema_valid_and_well_formed(
        self, filename: str, kind: DocumentKind
    ) -> None:
        payload = _extract_fixture(filename, kind)

        assert isinstance(payload, ExtractedQuotePayload)
        assert payload.lines, f"{filename}: golden fixture has no line items"
        assert 0.0 <= payload.overall_confidence <= 1.0
        assert payload.injection_suspected is False  # canary runs in the service, not fixtures

        for line in payload.lines:
            for field_name in ExtractedLine.model_fields:
                if field_name == "price_breaks":
                    continue
                confidence = getattr(line, field_name).confidence
                assert 0.0 <= confidence <= 1.0
            for value in _decimal_values_of_line(line):
                parse_decimal(value)  # raises InvalidDecimalString if malformed

    @pytest.mark.parametrize("filename,kind", FIXTURE_DOCUMENTS.items())
    def test_golden_payload_has_at_least_one_sub_095_field(
        self, filename: str, kind: DocumentKind
    ) -> None:
        payload = _extract_fixture(filename, kind)
        confidences = _all_scalar_field_confidences(payload)
        assert any(c < 0.95 for c in confidences), f"{filename}: no field below 0.95 confidence"

    def test_pacific_metal_scanned_quote_has_a_sub_060_field(self) -> None:
        payload = _extract_fixture("pacific_metal_quote.png", DocumentKind.PNG)
        confidences = _all_scalar_field_confidences(payload)
        assert any(c < 0.60 for c in confidences), "'uncertain extraction' needs a low field"

    def test_injection_document_reports_the_real_price_not_the_injected_one(self) -> None:
        payload = _extract_fixture("nordic_fastener_quote.csv", DocumentKind.CSV)
        injected_line = next(
            line for line in payload.lines if line.part_number.value == "MF-SCR-101"
        )
        assert injected_line.unit_price.value == "0.024"
        assert payload.injection_suspected is False

    def test_baltic_casting_payment_terms_is_missing_not_invented(self) -> None:
        payload = _extract_fixture("baltic_casting_quote.xlsx", DocumentKind.XLSX)
        assert payload.terms.payment_terms.value is None
        assert payload.terms.payment_terms.confidence == 0.0


class TestHeuristicFallback:
    def test_unknown_sha_returns_schema_valid_low_confidence_payload(self) -> None:
        pages = ["MF-TEST-001|Test Widget|100|4.50", "not a table row at all"]
        payload = _provider().extract(document_sha256="0" * 64, pages=pages)

        assert isinstance(payload, ExtractedQuotePayload)
        assert payload.overall_confidence == 0.4
        assert payload.injection_suspected is False
        assert len(payload.lines) == 1

        line = payload.lines[0]
        assert line.part_number.value == "MF-TEST-001"
        assert line.part_number.confidence == 0.5
        assert line.quantity.value == "100"
        assert line.unit_price.value == "4.50"
        parse_decimal(line.quantity.value)
        parse_decimal(line.unit_price.value)

    def test_unknown_sha_with_no_recognizable_rows_still_schema_valid(self) -> None:
        payload = _provider().extract(document_sha256="f" * 64, pages=["irrelevant free text"])
        assert isinstance(payload, ExtractedQuotePayload)
        assert payload.lines == []
        assert payload.overall_confidence == 0.4

    def test_heuristic_never_invents_unit_price_when_absent(self) -> None:
        payload = _provider().extract(
            document_sha256="1" * 64, pages=["MF-TEST-002|Widget No Price"]
        )
        assert payload.lines == []  # too few fields to even be a candidate row

    def test_heuristic_normalizes_a_dollar_prefixed_price(self) -> None:
        # raw_text keeps the "$" as seen; the normalized value is a clean decimal string
        payload = _provider().extract(
            document_sha256="2" * 64, pages=["MF-TEST-003|Widget|100|$45.99"]
        )
        assert len(payload.lines) == 1
        line = payload.lines[0]
        assert line.quantity.value == "100"
        assert line.unit_price.value == "45.99"
        assert line.unit_price.raw_text == "$45.99"
        parse_decimal(line.unit_price.value)


class TestDeterminism:
    def test_two_calls_are_identical_for_a_golden_fixture(self) -> None:
        provider = _provider()
        sha256 = _sha256_of("shenzhen_precision_quote.pdf")
        pages = _pages_for("shenzhen_precision_quote.pdf", DocumentKind.PDF)
        first = provider.extract(document_sha256=sha256, pages=pages)
        second = provider.extract(document_sha256=sha256, pages=pages)
        assert first.model_dump() == second.model_dump()

    def test_two_calls_are_identical_for_the_heuristic_fallback(self) -> None:
        provider = _provider()
        pages = ["MF-TEST-001|Test Widget|100|4.50"]
        first = provider.extract(document_sha256="a" * 64, pages=pages)
        second = provider.extract(document_sha256="a" * 64, pages=pages)
        assert first.model_dump() == second.model_dump()

    def test_provider_is_labelled_simulated(self) -> None:
        provider = _provider()
        assert provider.name == "mock"
        assert provider.is_simulated is True
