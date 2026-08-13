"""PDF renderer: `ReportDocument` -> bytes, via `reportlab` platypus
(docs/SPEC.md §Reports and exports: "professional PDF").

Built-in Helvetica/Helvetica-Bold only - no custom font files, no network
fetch, nothing reportlab must load beyond its own bundled AFM metrics.
Helvetica is Latin-1 (`cp1252`-adjacent); this codebase's demo dataset and
every string this renderer prints (supplier names, RFQ/scenario names,
generated prose) is ASCII/Latin-1 in practice, matching the delegating
task's own "Helvetica covers the demo dataset" framing. A character outside
Latin-1 raises inside reportlab rather than silently mangling - treated as a
renderer exception per `app/services/report_service.py`'s "persist as a
failed row" policy, not specially handled here.

No formula-injection concern: PDF has no live-formula surface, so unlike
`csv_renderer.py`/`xlsx_renderer.py` this module does NOT call
`app.reports.escape.escape_formula_cell`. Free text IS escaped for
reportlab's own mini-XML `Paragraph` markup (`&`, `<`, `>` -> entities) via
`xml.sax.saxutils.escape` - an unrelated, presentation-layer concern (a
literal `<` in a supplier name must not be parsed as a tag), not a security
control. `_p` escapes plain text with no markup of its own; `_p_labelled`
escapes `label`/`value` INDIVIDUALLY before splicing in this module's own
literal `<b>` markup, so a supplier name containing `<b>` cannot forge bold
text or break the paragraph's XML.
"""

from __future__ import annotations

import io
from xml.sax.saxutils import escape as _xml_escape

# no bundled type stubs, and pyproject.toml's mypy overrides are off-limits
# for this change - same accepted precedent app/importing/part_import_parser.py
# already documents for its own openpyxl import.
from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import-untyped]
from reportlab.lib.units import inch  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.reports.document import KeyValueBlock, ReportDocument, TableBlock, TextBlock

_STYLES = getSampleStyleSheet()


def _p(text: str, style_name: str = "BodyText") -> Paragraph:
    """Plain text -> `Paragraph`, fully escaped; no markup interpretation."""
    return Paragraph(_xml_escape(text), _STYLES[style_name])


def _p_labelled(label: str, value: str, style_name: str = "BodyText") -> Paragraph:
    """`<b>label:</b> value` - `label`/`value` escaped individually before
    this module's own literal bold markup is added around them."""
    return Paragraph(f"<b>{_xml_escape(label)}:</b> {_xml_escape(value)}", _STYLES[style_name])


def render_pdf(document: ReportDocument) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=document.title,
    )
    story: list[object] = [
        _p(document.title, "Title"),
        Spacer(1, 0.15 * inch),
        _p_labelled("Generated", document.generated_at),
        _p_labelled("Calculation version", document.calculation_version),
        Spacer(1, 0.1 * inch),
        _p_labelled("Methodology", document.methodology),
        Spacer(1, 0.06 * inch),
        _p_labelled("Disclaimer", document.disclaimer),
        Spacer(1, 0.1 * inch),
    ]

    story.append(_p("Missing data", "Heading3"))
    if document.missing_data:
        for item in document.missing_data:
            story.append(_p(f"• {item}"))
    else:
        story.append(_p("None."))
    story.append(Spacer(1, 0.2 * inch))

    for block in document.blocks:
        story.append(_p(block.heading, "Heading2"))
        story.append(Spacer(1, 0.06 * inch))
        if isinstance(block, TableBlock):
            table_data = [list(block.columns)] + [list(r) for r in block.rows]
            table = Table(table_data, repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9ca3af")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(table)
        elif isinstance(block, KeyValueBlock):
            for key, value in block.pairs:
                story.append(_p_labelled(key, value))
        elif isinstance(block, TextBlock):
            for paragraph in block.paragraphs:
                story.append(_p(paragraph))
        story.append(Spacer(1, 0.2 * inch))

    doc.build(story)
    return buf.getvalue()


__all__ = ["render_pdf"]
