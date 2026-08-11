"""MockExtractionProvider — the default, always-available ExtractionProvider.

SPEC "External-service strategy": "Public demo works without any paid AI key: mock AI
provider, deterministic fixture extraction, synthetic exchange rates, prebuilt demo
workflow, clear labeling of simulated behavior ... Never present a mock response as live."

Deterministic and NEVER RANDOM: given a `document_sha256`, this provider either
(a) returns the exact golden `ExtractedQuotePayload` committed for that hash under the
fixture registry directory (`backend/tests/fixtures/extraction/<sha256>.json` by default —
the same directory `backend/scripts/generate_fixtures.py` writes to), or (b) for any
`document_sha256` it doesn't recognize, falls back to a small regex heuristic over the
acquired page text so an arbitrary upload still demonstrates the review/confirmation flow
rather than failing outright. The heuristic is deliberately weak — fixed confidence 0.5 on
any field it manages to find, 0.4 overall — it exists to keep the demo alive for documents
outside the fixture set, not to compete with a real extraction model.

`is_simulated = True` unconditionally: every payload this provider returns, fixture or
heuristic, is synthetic and must be labelled as such wherever it reaches the UI
(04-document-pipeline.md §6-9's confidence/review pipeline; SPEC "clear labeling of
simulated behavior").

The heuristic fallback's regex expects the "known table layout" this codebase's own
acquisition module (`app.ingestion.acquisition`) produces: XLSX sheets and pdfplumber-found
PDF tables are pipe-separated rows, and CSV survives acquisition as its original
comma-separated text — so candidate line-item rows are split on either delimiter.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.money import InvalidDecimalString, parse_decimal
from app.providers.extraction.base import (
    ExtractedField,
    ExtractedLine,
    ExtractedQuotePayload,
    ExtractedTerms,
)

# backend/src/app/providers/extraction/mock.py -> backend/tests/fixtures/extraction
_DEFAULT_REGISTRY_DIR = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "extraction"

_HEURISTIC_FIELD_CONFIDENCE = 0.5
_HEURISTIC_OVERALL_CONFIDENCE = 0.4

# A part number shaped like the fixtures' own ("MF-BRKT-010", "SP-GEAR-22"): a leading
# alphanumeric token, then 1-4 hyphen-separated alphanumeric segments.
_PART_NUMBER_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,4}$")
_INT_RE = re.compile(r"^\d+$")
_MONEY_RE = re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.\d{2,4})?$")


def _empty_terms() -> ExtractedTerms:
    return ExtractedTerms(
        payment_terms=ExtractedField(),
        shipping_terms=ExtractedField(),
        warranty=ExtractedField(),
        exceptions=ExtractedField(),
        exclusions=ExtractedField(),
        notes=ExtractedField(),
    )


def _normalize_money(raw: str) -> str | None:
    """Strip a `$` prefix / thousands separators and confirm the result parses as a
    decimal string per the codebase's money policy (`app.core.money.parse_decimal`).
    Returns `None` rather than raising — a candidate that fails to parse is simply not a
    price, not a fatal error for the whole heuristic pass."""
    cleaned = raw.lstrip("$").replace(",", "")
    try:
        parse_decimal(cleaned)
    except InvalidDecimalString:
        return None
    return cleaned


def _heuristic_line(raw_line: str, page_number: int) -> ExtractedLine | None:
    fields = [f.strip() for f in re.split(r"[|,]", raw_line) if f.strip()]
    if len(fields) < 3:
        return None

    part_candidate = fields[0].upper()
    if not _PART_NUMBER_RE.match(part_candidate):
        return None

    qty_field = next((f for f in fields[1:] if _INT_RE.match(f)), None)
    price_field = next((f for f in reversed(fields) if _MONEY_RE.match(f.lstrip("$"))), None)
    normalized_price = _normalize_money(price_field) if price_field is not None else None
    if qty_field is None and normalized_price is None:
        return None  # nothing but a plausible part number: too weak to report as a line

    description = next(
        (f for f in fields[1:] if f != qty_field and f != price_field),
        None,
    )

    return ExtractedLine(
        part_number=ExtractedField(
            value=part_candidate,
            raw_text=fields[0],
            confidence=_HEURISTIC_FIELD_CONFIDENCE,
            page_number=page_number,
        ),
        description=(
            ExtractedField(
                value=description,
                raw_text=description,
                confidence=_HEURISTIC_FIELD_CONFIDENCE,
                page_number=page_number,
            )
            if description is not None
            else ExtractedField()
        ),
        quantity=(
            ExtractedField(
                value=qty_field,
                raw_text=qty_field,
                confidence=_HEURISTIC_FIELD_CONFIDENCE,
                page_number=page_number,
            )
            if qty_field is not None
            else ExtractedField()
        ),
        unit_price=(
            ExtractedField(
                value=normalized_price,
                raw_text=price_field,
                confidence=_HEURISTIC_FIELD_CONFIDENCE,
                page_number=page_number,
            )
            if normalized_price is not None
            else ExtractedField()
        ),
        # Fields this regex heuristic never attempts: each `MISSING`
        # (value=None, confidence=0.0) per the payload's not-stated contract.
        unit_of_measure=ExtractedField(),
        moq=ExtractedField(),
        lead_time_days=ExtractedField(),
        country_of_origin=ExtractedField(),
        production_capacity=ExtractedField(),
        tooling_cost=ExtractedField(),
        setup_cost=ExtractedField(),
        packaging_cost=ExtractedField(),
        shipping_cost=ExtractedField(),
        insurance_cost=ExtractedField(),
        tariff_amount=ExtractedField(),
        duty_amount=ExtractedField(),
        customs_fees=ExtractedField(),
        tax_amount=ExtractedField(),
    )


def _heuristic_payload(pages: list[str]) -> ExtractedQuotePayload:
    lines: list[ExtractedLine] = []
    for page_number, page_text in enumerate(pages, start=1):
        for raw_line in page_text.splitlines():
            line = _heuristic_line(raw_line, page_number)
            if line is not None:
                lines.append(line)

    return ExtractedQuotePayload(
        supplier_name=ExtractedField(),
        quote_number=ExtractedField(),
        quote_date=ExtractedField(),
        expiration_date=ExtractedField(),
        currency=ExtractedField(),
        lines=lines,
        terms=_empty_terms(),
        overall_confidence=_HEURISTIC_OVERALL_CONFIDENCE,
        provider_notes=(
            "mock provider heuristic fallback: no fixture registered for this document hash"
        ),
        injection_suspected=False,
    )


class MockExtractionProvider:
    """Deterministic `ExtractionProvider` (see `app.providers.extraction.base.
    ExtractionProvider`). Never random — see module docstring."""

    name = "mock"
    is_simulated = True

    def __init__(self, registry_dir: Path | None = None) -> None:
        self._registry_dir = registry_dir if registry_dir is not None else _DEFAULT_REGISTRY_DIR

    def extract(self, *, document_sha256: str, pages: list[str]) -> ExtractedQuotePayload:
        fixture_path = self._registry_dir / f"{document_sha256}.json"
        if fixture_path.is_file():
            return ExtractedQuotePayload.model_validate_json(
                fixture_path.read_text(encoding="utf-8")
            )
        return _heuristic_payload(pages)


__all__ = ["MockExtractionProvider"]
