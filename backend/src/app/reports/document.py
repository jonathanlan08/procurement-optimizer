"""Renderer-agnostic report content model (docs/SPEC.md §Reports and
exports, docs/planning/03-api-contract.md §4.18).

`app/services/report_service.py` builds exactly one `ReportDocument` per
`GeneratedReport` row from stored data (scenario snapshots, `ScenarioResult`/
`AllocationResultRecord`, `NegotiationBrief`, audit events) - never from
anything recomputed differently than the routes that already expose that
same data (SPEC: "ALL numbers come from stored rows/snapshots"). Each of
`app/reports/csv_renderer.py`, `xlsx_renderer.py`, `pdf_renderer.py` renders
the SAME `ReportDocument` to bytes; the content is decided once, upstream of
any format-specific concern.

Every `ReportDocument` carries the SPEC-mandated header material (§Reports
and exports: "methodology, disclaimer, generation date, calculation
version") plus a `missing_data` disclosure list, so a caller who forgets one
report type cannot silently ship a report missing them - they are fields on
the shared model, not something each renderer must remember to print.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TableBlock:
    """A titled table: `columns` header row + `rows` of same-length tuples.
    Every cell is already a display string (SPEC/§1.2: money and decimals
    never cross into a renderer as a float)."""

    heading: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class KeyValueBlock:
    """A titled list of label/value pairs - scenario metadata, allocation
    totals, and similar "one fact per line" content."""

    heading: str
    pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TextBlock:
    """A titled list of paragraphs - narrative content (negotiation-brief
    sections, infeasibility explanations, notes)."""

    heading: str
    paragraphs: tuple[str, ...]


ReportBlock = TableBlock | KeyValueBlock | TextBlock


@dataclass(frozen=True, slots=True)
class ReportDocument:
    title: str
    generated_at: str  # display string, already formatted by the caller
    calculation_version: str
    methodology: str
    disclaimer: str
    missing_data: tuple[str, ...] = field(default_factory=tuple)
    blocks: tuple[ReportBlock, ...] = field(default_factory=tuple)


__all__ = ["KeyValueBlock", "ReportBlock", "ReportDocument", "TableBlock", "TextBlock"]
