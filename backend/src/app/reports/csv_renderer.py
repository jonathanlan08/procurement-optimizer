"""CSV renderer: `ReportDocument` -> bytes (docs/SPEC.md §Reports and
exports).

CSV has no notion of "sections" - this flattens `ReportDocument` into a
single sheet: a header block (title/generated_at/calculation_version/
methodology/disclaimer/missing_data), a blank row, then each block in
order (its own heading row, then its content), separated by blank rows.
Every cell passes through `app.reports.escape.escape_formula_cell` before
being written (module docstring there: the mandatory formula-injection
guard, shared with `xlsx_renderer.py`).
"""

from __future__ import annotations

import csv
import io

from app.reports.document import KeyValueBlock, ReportDocument, TableBlock, TextBlock
from app.reports.escape import escape_formula_cell


def render_csv(document: ReportDocument) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    def row(*cells: str) -> None:
        writer.writerow([escape_formula_cell(c) for c in cells])

    row("Report", document.title)
    row("Generated At", document.generated_at)
    row("Calculation Version", document.calculation_version)
    row("Methodology", document.methodology)
    row("Disclaimer", document.disclaimer)
    if document.missing_data:
        for item in document.missing_data:
            row("Missing Data", item)
    else:
        row("Missing Data", "None")
    writer.writerow([])

    for block in document.blocks:
        row(block.heading)
        if isinstance(block, TableBlock):
            row(*block.columns)
            for data_row in block.rows:
                row(*data_row)
        elif isinstance(block, KeyValueBlock):
            for key, value in block.pairs:
                row(key, value)
        elif isinstance(block, TextBlock):
            for paragraph in block.paragraphs:
                row(paragraph)
        writer.writerow([])

    return buf.getvalue().encode("utf-8")


__all__ = ["render_csv"]
