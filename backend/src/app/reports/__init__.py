"""Report rendering: one `ReportDocument` (renderer-agnostic content model,
`document.py`) rendered to bytes by exactly one of `csv_renderer.py`,
`xlsx_renderer.py`, `pdf_renderer.py`, dispatched by `render()` below on
`app.models.reports.ReportFormat`.
"""

from __future__ import annotations

from app.models.reports import ReportFormat
from app.reports.csv_renderer import render_csv
from app.reports.document import KeyValueBlock, ReportBlock, ReportDocument, TableBlock, TextBlock
from app.reports.pdf_renderer import render_pdf
from app.reports.xlsx_renderer import render_xlsx

_CONTENT_TYPES: dict[ReportFormat, str] = {
    ReportFormat.CSV: "text/csv",
    ReportFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ReportFormat.PDF: "application/pdf",
}


def content_type_for(fmt: ReportFormat) -> str:
    return _CONTENT_TYPES[fmt]


def render(document: ReportDocument, fmt: ReportFormat) -> bytes:
    if fmt is ReportFormat.CSV:
        return render_csv(document)
    if fmt is ReportFormat.XLSX:
        return render_xlsx(document)
    if fmt is ReportFormat.PDF:
        return render_pdf(document)
    raise ValueError(f"Unsupported report format: {fmt!r}")  # pragma: no cover - exhaustive enum


__all__ = [
    "KeyValueBlock",
    "ReportBlock",
    "ReportDocument",
    "TableBlock",
    "TextBlock",
    "content_type_for",
    "render",
]
